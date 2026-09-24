"""
Pitwall live engine: F1's official live timing feed (livetiming.formula1.com, SignalR Core),
authenticated with an F1 TV Pro token.

Rules this module keeps:
  * Every value served comes from the feed. Nothing is estimated, padded or defaulted:
    a field the feed has not sent is null/empty.
  * On connect the hub's Subscribe call returns a full snapshot of every topic; it is applied
    first, then incremental "feed" updates are merged into it.
  * CarData.z and Position.z (car telemetry and car coordinates) need the F1 TV Pro
    entitlement. They arrive base64-encoded raw-deflate JSON.
"""
import base64
import json
import logging
import os
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import requests
from cachetools import TTLCache
from signalrcore.hub_connection_builder import HubConnectionBuilder

logger = logging.getLogger("livef1_engine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

LIVETIMING_BASE = "https://livetiming.formula1.com"
JOLPICA_BASE = "https://api.jolpi.ca/ergast/f1"
meta_cache: TTLCache[str, Any] = TTLCache(maxsize=100, ttl=3600)

TOPICS = [
    "Heartbeat", "SessionInfo", "SessionStatus", "DriverList", "TimingData", "TimingAppData",
    "TrackStatus", "WeatherData", "RaceControlMessages", "LapCount", "TeamRadio",
    "PitLaneTimeCollection", "CarData.z", "Position.z",
]

# CarData channel ids in the F1 feed.
CH_RPM, CH_SPEED, CH_GEAR, CH_THROTTLE, CH_BRAKE, CH_DRS = "0", "2", "3", "4", "5", "45"


def load_f1tv_token() -> str:
    """F1TV_TOKEN from the environment (the host's secret), else a git-ignored local .env file."""
    token = os.environ.get("F1TV_TOKEN", "").strip()
    if token:
        return token
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("F1TV_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def token_expiry(token: str) -> Optional[datetime]:
    """The JWT's exp claim, read without verifying the signature (it is only logged)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return datetime.fromtimestamp(int(exp), tz=timezone.utc) if exp else None
    except Exception:
        return None


def inflate(data: Any) -> Any:
    """Decodes a .z topic payload: base64 of raw-deflate JSON."""
    if not isinstance(data, str):
        return data
    try:
        return json.loads(zlib.decompress(base64.b64decode(data), -zlib.MAX_WBITS))
    except Exception as e:
        logger.warning(f"Could not inflate compressed payload: {e}")
        return None


def merge(target: Any, source: Any) -> Any:
    """
    Merges a feed update into stored state. The feed sends lists in snapshots and
    {"index": partial} dicts in updates; those patch the list in place instead of replacing it.
    """
    if isinstance(target, dict) and isinstance(source, dict):
        for k, v in source.items():
            if k == "_deleted" and isinstance(v, list):
                for key in v:
                    target.pop(str(key), None)
                continue
            target[k] = merge(target.get(k), v) if k in target else v
        return target
    if isinstance(target, list) and isinstance(source, dict):
        for k, v in source.items():
            try:
                i = int(k)
            except (TypeError, ValueError):
                continue
            while len(target) <= i:
                target.append({})
            target[i] = merge(target[i], v)
        return target
    return source


def _value(obj: Any) -> str:
    """A feed field that may be a bare value or {"Value": ...}."""
    if isinstance(obj, dict):
        obj = obj.get("Value")
    return "" if obj is None else str(obj).strip()


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_lap_str(t: Any) -> Optional[float]:
    s = _value(t)
    if not s:
        return None
    try:
        parts = s.split(":")
        return float(parts[0]) * 60.0 + float(parts[1]) if len(parts) == 2 else float(parts[0])
    except Exception:
        return None


def _as_list(container: Any) -> List[Any]:
    if isinstance(container, list):
        return container
    if isinstance(container, dict):
        return [container[k] for k in sorted(container, key=lambda x: int(x) if str(x).isdigit() else 0)]
    return []


class LiveF1Engine:
    def __init__(self):
        self._client = httpx.AsyncClient(timeout=10.0)
        self._lock = threading.Lock()
        self._token = load_f1tv_token()
        self._token_exp = token_expiry(self._token) if self._token else None
        self._reset_state()
        self._last_event_time: float = 0.0
        self._hub_thread: Optional[threading.Thread] = None
        self._connection: Optional[Any] = None
        self._is_connected = False
        self._stopped = False

    def _reset_state(self):
        self._session_info: Dict[str, Any] = {}
        self._session_status: str = ""
        self._drivers: Dict[str, Dict[str, Any]] = {}
        self._timing: Dict[str, Any] = {}
        self._timing_app: Dict[str, Any] = {}
        self._weather: Dict[str, Any] = {}
        self._track_status: Dict[str, Any] = {}
        self._lap_count: Dict[str, Any] = {}
        self._race_control: List[Dict[str, Any]] = []
        self._radio: List[Dict[str, Any]] = []
        self._pits: Dict[str, Dict[str, Any]] = {}
        self._positions: Dict[str, Dict[str, Any]] = {}
        self._car_data: Dict[str, Dict[str, Any]] = {}

    # ---------------------------------------------------------------- connection

    def start(self):
        self._stopped = False
        if not self._token:
            # Timing, tyres, radio, race control and weather are public; only car positions and
            # telemetry (CarData.z, Position.z) need an F1 TV Pro token.
            logger.warning("F1TV_TOKEN is not set: connecting without it (no car positions or telemetry).")
        if self._token_exp:
            left = (self._token_exp - datetime.now(timezone.utc)).total_seconds() / 3600
            logger.info(f"F1 TV token expires {self._token_exp.isoformat()} ({left:.1f} h left).")
        if self._hub_thread is None or not self._hub_thread.is_alive():
            self._hub_thread = threading.Thread(target=self._run_signalr, daemon=True, name="SignalR-Worker")
            self._hub_thread.start()

    def stop(self):
        self._stopped = True
        self._is_connected = False
        if self._connection:
            try:
                self._connection.stop()
            except Exception as e:
                logger.warning(f"Error stopping connection: {e}")

    def health(self) -> Dict[str, Any]:
        return {
            "connected": self._is_connected,
            "token_set": bool(self._token),
            "token_expires": self._token_exp.isoformat() if self._token_exp else None,
            "token_expired": bool(self._token_exp and self._token_exp < datetime.now(timezone.utc)),
            "last_event_age_s": round(time.time() - self._last_event_time, 1) if self._last_event_time else None,
        }

    def _run_signalr(self):
        negotiate_url = f"{LIVETIMING_BASE}/signalrcore/negotiate"
        ws_url = "wss://livetiming.formula1.com/signalrcore"
        while not self._stopped:
            try:
                headers = {}
                try:
                    r = requests.options(negotiate_url, timeout=6)
                    if "AWSALBCORS" in r.cookies:
                        headers["Cookie"] = f"AWSALBCORS={r.cookies['AWSALBCORS']}"
                except Exception as ex:
                    logger.warning(f"Pre-negotiate cookie fetch failed: {ex}")

                token = self._token
                options = {"verify_ssl": True, "headers": headers}
                if token:
                    options["access_token_factory"] = lambda: token
                conn = HubConnectionBuilder() \
                    .with_url(ws_url, options=options) \
                    .configure_logging(logging.WARNING) \
                    .build()

                def on_open():
                    logger.info("F1 live timing connected; subscribing.")
                    self._is_connected = True
                    conn.send("Subscribe", [TOPICS], on_invocation=self._on_snapshot)

                def on_close():
                    logger.info("F1 live timing connection closed.")
                    self._is_connected = False

                conn.on_open(on_open)
                conn.on_close(on_close)
                conn.on("feed", self._on_feed)
                self._connection = conn
                conn.start()
                # start() returns before on_open fires: wait for the handshake, then hold.
                deadline = time.time() + 20
                while not self._is_connected and not self._stopped and time.time() < deadline:
                    time.sleep(0.5)
                while self._is_connected and not self._stopped:
                    time.sleep(2)
                try:
                    conn.stop()
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"SignalR loop error: {e}")
                self._is_connected = False
            if not self._stopped:
                time.sleep(5)

    def _on_snapshot(self, message: Any):
        """The Subscribe call's result: the current state of every topic."""
        msgs = message if isinstance(message, list) else [message]
        for m in msgs:
            result = getattr(m, "result", None)
            if not isinstance(result, dict):
                continue
            with self._lock:
                # SessionInfo first so a session change resets before the rest lands.
                if "SessionInfo" in result:
                    self._apply("SessionInfo", result["SessionInfo"], snapshot=True)
                for topic, data in result.items():
                    if topic != "SessionInfo":
                        self._apply(topic, data, snapshot=True)
            self._last_event_time = time.time()
            logger.info(f"Snapshot applied: {sorted(result.keys())}")

    def _on_feed(self, data: Any):
        if not isinstance(data, list) or len(data) < 2:
            return
        self._last_event_time = time.time()
        with self._lock:
            try:
                self._apply(data[0], data[1], snapshot=False)
            except Exception as e:
                logger.warning(f"Error applying {data[0]}: {e}")

    # ---------------------------------------------------------------- topic handlers

    def _apply(self, topic: str, payload: Any, snapshot: bool):
        if topic.endswith(".z"):
            payload = inflate(payload)
            topic = topic[:-2]
        if payload is None:
            return

        if topic == "SessionInfo" and isinstance(payload, dict):
            new_key = payload.get("Key")
            if new_key is not None and self._session_info.get("Key") not in (None, new_key):
                logger.info(f"Session changed to {payload.get('Name')}: clearing state.")
                self._reset_state()
            merge(self._session_info, payload)
        elif topic == "SessionStatus" and isinstance(payload, dict):
            self._session_status = str(payload.get("Status") or self._session_status)
        elif topic == "DriverList" and isinstance(payload, dict):
            for num, info in payload.items():
                if str(num).isdigit() and isinstance(info, dict):
                    merge(self._drivers.setdefault(str(int(num)), {}), info)
        elif topic == "TimingData" and isinstance(payload, dict):
            merge(self._timing, payload)
        elif topic == "TimingAppData" and isinstance(payload, dict):
            merge(self._timing_app, payload)
        elif topic == "TrackStatus" and isinstance(payload, dict):
            merge(self._track_status, payload)
        elif topic == "WeatherData" and isinstance(payload, dict):
            merge(self._weather, payload)
        elif topic == "LapCount" and isinstance(payload, dict):
            merge(self._lap_count, payload)
        elif topic == "RaceControlMessages" and isinstance(payload, dict):
            seen = {(m.get("Utc"), m.get("Message")) for m in self._race_control}
            for m in _as_list(payload.get("Messages")):
                if isinstance(m, dict) and (m.get("Utc"), m.get("Message")) not in seen:
                    self._race_control.append(m)
                    seen.add((m.get("Utc"), m.get("Message")))
            self._race_control = self._race_control[-300:]
        elif topic == "TeamRadio" and isinstance(payload, dict):
            seen = {c.get("Path") for c in self._radio}
            for c in _as_list(payload.get("Captures")):
                if isinstance(c, dict) and c.get("Path") and c.get("Path") not in seen:
                    self._radio.append(c)
                    seen.add(c.get("Path"))
            self._radio = self._radio[-200:]
        elif topic == "PitLaneTimeCollection" and isinstance(payload, dict):
            for num, pit in (payload.get("PitTimes") or {}).items():
                if not isinstance(pit, dict) or not str(num).isdigit():
                    continue
                lap = str(pit.get("Lap") or "")
                self._pits[f"{int(num)}:{lap}"] = {**pit, "RacingNumber": str(int(num)), "_seen": time.time()}
        elif topic == "Position" and isinstance(payload, dict):
            for frame in payload.get("Position") or []:
                ts = frame.get("Timestamp")
                for num, e in (frame.get("Entries") or {}).items():
                    if isinstance(e, dict) and str(num).isdigit():
                        self._positions[str(int(num))] = {**e, "Timestamp": ts}
        elif topic == "CarData" and isinstance(payload, dict):
            for entry in payload.get("Entries") or []:
                utc = entry.get("Utc")
                for num, car in (entry.get("Cars") or {}).items():
                    ch = (car or {}).get("Channels") or {}
                    if str(num).isdigit():
                        self._car_data[str(int(num))] = {**{k: ch.get(k) for k in ch}, "Utc": utc}

    # ---------------------------------------------------------------- read models

    def _session_utc(self, field: str) -> Optional[str]:
        """SessionInfo StartDate/EndDate are circuit-local; GmtOffset turns them into UTC."""
        local = self._session_info.get(field)
        offset = str(self._session_info.get("GmtOffset") or "")
        if not local or not offset:
            return None
        try:
            sign = -1 if offset.startswith("-") else 1
            h, m, *_ = [int(x) for x in offset.lstrip("+-").split(":")]
            dt = datetime.fromisoformat(str(local)[:19]) - sign * timedelta(hours=h, minutes=m)
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            return None

    def _session_part(self) -> Optional[int]:
        part = self._timing.get("SessionPart")
        try:
            return int(part) if part is not None else None
        except (TypeError, ValueError):
            return None

    def _driver_meta(self, num: str) -> Dict[str, Any]:
        d = self._drivers.get(num, {})
        colour = str(d.get("TeamColour") or "").replace("#", "").strip()
        return {
            "broadcast_name": d.get("BroadcastName") or d.get("FullName") or None,
            "full_name": d.get("FullName") or None,
            "name_acronym": d.get("Tla") or None,
            "team_name": d.get("TeamName") or None,
            "team_colour": colour or None,
            "headshot_url": d.get("HeadshotUrl") or None,
        }

    def _stints(self, num: str) -> List[Dict[str, Any]]:
        line = (self._timing_app.get("Lines") or {}).get(num) or {}
        out = []
        for i, s in enumerate(_as_list(line.get("Stints"))):
            if not isinstance(s, dict) or not s.get("Compound"):
                continue
            out.append({
                "stint_number": i + 1,
                "compound": str(s.get("Compound")).upper(),
                "new": str(s.get("New")).lower() == "true" if s.get("New") is not None else None,
                "tyre_age_at_start": int(s["StartLaps"]) if str(s.get("StartLaps", "")).isdigit() else None,
                "tyre_age": int(s["TotalLaps"]) if str(s.get("TotalLaps", "")).isdigit() else None,
            })
        return out

    def _race_control_out(self) -> List[Dict[str, Any]]:
        out = []
        for m in self._race_control:
            num = m.get("RacingNumber")
            out.append({
                "date": m.get("Utc"),
                "lap_number": int(m["Lap"]) if str(m.get("Lap", "")).isdigit() else None,
                "category": m.get("Category"),
                "flag": m.get("Flag"),
                "scope": m.get("Scope"),
                "sector": m.get("Sector"),
                "message": m.get("Message"),
                "driver_number": int(num) if str(num or "").isdigit() else None,
            })
        return out

    def _leaderboard(self) -> List[Dict[str, Any]]:
        lines = self._timing.get("Lines") or {}
        part = self._session_part()
        board = []
        for num, line in lines.items():
            if not str(num).isdigit() or not isinstance(line, dict):
                continue
            num = str(int(num))
            meta = self._driver_meta(num)

            best = _value(line.get("BestLapTime"))
            gap = _value(line.get("GapToLeader")) or _value(line.get("TimeDiffToFastest"))
            interval = _value(line.get("IntervalToPositionAhead")) or _value(line.get("TimeDiffToPositionAhead"))
            # Qualifying: best lap and gaps are kept per segment.
            if part:
                seg_best = _as_list(line.get("BestLapTimes"))
                if len(seg_best) >= part and _value(seg_best[part - 1]):
                    best = _value(seg_best[part - 1])
                stats = _as_list(line.get("Stats"))
                if len(stats) >= part and isinstance(stats[part - 1], dict):
                    gap = _value(stats[part - 1].get("TimeDiffToFastest")) or gap
                    interval = _value(stats[part - 1].get("TimeDifftoPositionAhead")) or interval

            sectors = _as_list(line.get("Sectors"))

            def sector(i: int):
                s = sectors[i] if len(sectors) > i and isinstance(sectors[i], dict) else {}
                v = _value(s.get("Value"))
                state = "NONE"
                if v:
                    state = "SESSION_BEST" if s.get("OverallFastest") else "PERSONAL_BEST" if s.get("PersonalFastest") else "SLOWER"
                return v, state

            (s1, st1), (s2, st2), (s3, st3) = sector(0), sector(1), sector(2)
            speeds = line.get("Speeds") or {}
            pos = line.get("Position")
            board.append({
                "position": int(pos) if str(pos or "").isdigit() else None,
                "driver_number": int(num),
                **meta,
                "last_lap_time": _value(line.get("LastLapTime")),
                "best_lap_time": best,
                "gap_to_leader": gap,
                "interval": interval,
                "sector1": s1, "sector2": s2, "sector3": s3,
                "sector1_state": st1, "sector2_state": st2, "sector3_state": st3,
                "speed_trap": _value(speeds.get("ST")) if isinstance(speeds, dict) else "",
                "laps": line.get("NumberOfLaps"),
                "in_pit": bool(line.get("InPit")),
                "pit_out": bool(line.get("PitOut")),
                "number_of_pit_stops": line.get("NumberOfPitStops"),
                "is_retired": bool(line.get("Retired")),
                "is_stopped": bool(line.get("Stopped")),
                "knocked_out": bool(line.get("KnockedOut")),
                # The feed has no crash flag; stopped cars are reported as stopped.
                "is_crashed": False,
                "stints": self._stints(num),
            })
        board.sort(key=lambda d: (d["position"] is None, d["position"] or 0, d["driver_number"]))
        return board

    def _positions_out(self) -> List[Dict[str, Any]]:
        out = []
        for num, p in self._positions.items():
            if p.get("X") is None or p.get("Y") is None:
                continue
            out.append({
                "driver_number": int(num), "date": p.get("Timestamp"), "status": p.get("Status"),
                "x": _num(p.get("X")), "y": _num(p.get("Y")), "z": _num(p.get("Z")),
            })
        return out

    def _car_data_out(self) -> List[Dict[str, Any]]:
        out = []
        for num, c in self._car_data.items():
            brake = _num(c.get(CH_BRAKE))
            out.append({
                "driver_number": int(num), "date": c.get("Utc"),
                "rpm": int(c[CH_RPM]) if c.get(CH_RPM) is not None else None,
                "speed": int(c[CH_SPEED]) if c.get(CH_SPEED) is not None else None,
                "n_gear": int(c[CH_GEAR]) if c.get(CH_GEAR) is not None else None,
                "throttle": int(c[CH_THROTTLE]) if c.get(CH_THROTTLE) is not None else None,
                # The feed sends brake as on/off (0 or 1, sometimes 100); served as 0 or 100.
                "brake": None if brake is None else (100 if brake > 0 else 0),
                "drs": int(c[CH_DRS]) if c.get(CH_DRS) is not None else None,
            })
        return out

    def _status(self) -> str:
        if not self._timing.get("Lines"):
            return "idle"
        recent = time.time() - self._last_event_time < 300
        finished = self._session_status in ("Finished", "Finalised", "Ends")
        return "live" if recent and not finished else "completed"

    async def get_live_timing(self) -> Dict[str, Any]:
        with self._lock:
            meeting = self._session_info.get("Meeting") or {}
            path = self._session_info.get("Path") or ""
            radio = [{
                "driver_number": int(c["RacingNumber"]) if str(c.get("RacingNumber", "")).isdigit() else None,
                "date": c.get("Utc"),
                "recording_url": f"{LIVETIMING_BASE}/static/{path}{c['Path']}" if path else None,
            } for c in self._radio]
            pits = [{
                "driver_number": int(p["RacingNumber"]),
                "lap_number": int(p["Lap"]) if str(p.get("Lap", "")).isdigit() else None,
                "pit_duration": _num(p.get("Duration")),
            } for p in sorted(self._pits.values(), key=lambda p: p["_seen"])]
            return {
                "status": self._status(),
                "session_status": self._session_status or None,
                "session_name": self._session_info.get("Name"),
                "session_type": self._session_info.get("Type"),
                "session_part": self._session_part(),
                "circuit_short_name": (meeting.get("Circuit") or {}).get("ShortName"),
                "country_name": (meeting.get("Country") or {}).get("Name"),
                "meeting_name": meeting.get("Name"),
                "session_key": self._session_info.get("Key"),
                "date_start": self._session_utc("StartDate"),
                "date_end": self._session_utc("EndDate"),
                "timestamp": time.time(),
                "track_flag": self._track_status.get("Message"),
                "track_status": self._track_status.get("Status"),
                "current_lap": self._lap_count.get("CurrentLap"),
                "total_laps": self._lap_count.get("TotalLaps"),
                "race_control_messages": self._race_control_out(),
                "leaderboard": self._leaderboard(),
                "positions": self._positions_out(),
                "car_data": self._car_data_out(),
                "team_radio": [r for r in radio if r["recording_url"] and r["driver_number"] is not None],
                "pit_stops": pits,
                "engine": "f1-livetiming-signalr",
            }

    async def get_live_positions(self) -> Dict[str, Any]:
        with self._lock:
            return {"session_key": self._session_info.get("Key"), "positions": self._positions_out(), "car_data": self._car_data_out()}

    async def get_live_weather(self) -> Dict[str, Any]:
        with self._lock:
            w = self._weather
            rain = w.get("Rainfall")
            return {
                "air_temperature": _num(w.get("AirTemp")),
                "track_temperature": _num(w.get("TrackTemp")),
                "humidity": _num(w.get("Humidity")),
                "wind_speed": _num(w.get("WindSpeed")),
                "wind_direction": _num(w.get("WindDirection")),
                "pressure": _num(w.get("Pressure")),
                "rainfall": None if rain is None else (_num(rain) or 0) > 0,
                "timestamp": w.get("Utc"),
            }

    async def get_race_control(self) -> List[Dict[str, Any]]:
        with self._lock:
            return self._race_control_out()

    # ---------------------------------------------------------------- Jolpica passthrough

    async def _jolpica(self, key: str, path: str) -> Dict[str, Any]:
        if key in meta_cache:
            return meta_cache[key]
        try:
            res = await self._client.get(f"{JOLPICA_BASE}/{path}")
            if res.status_code == 200:
                meta_cache[key] = res.json()
                return meta_cache[key]
        except Exception as e:
            logger.error(f"{key} error: {e}")
        return {}

    async def get_calendar(self) -> Dict[str, Any]:
        return await self._jolpica("calendar", "current.json")

    async def get_driver_standings(self) -> Dict[str, Any]:
        return await self._jolpica("driver_standings", "current/driverStandings.json")

    async def get_constructor_standings(self) -> Dict[str, Any]:
        return await self._jolpica("constructor_standings", "current/constructorStandings.json")

    async def close(self):
        self.stop()
        await self._client.aclose()


engine = LiveF1Engine()
