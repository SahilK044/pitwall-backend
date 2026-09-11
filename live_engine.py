import asyncio
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional
from cachetools import TTLCache
import httpx
import requests
from signalrcore.hub_connection_builder import HubConnectionBuilder

logger = logging.getLogger("livef1_engine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# Default F1 TV token provided by user (valid through Sept 15, 2026)
DEFAULT_F1TV_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJFeHRlcm5hbEF1dGhvcml6YXRpb25zQ29udGV4dERhdGEiOiJJTkQiLCJTdWJzY3JpcHRpb25TdGF0dXMiOiJhY3RpdmUiLCJTdWJzY3JpYmVySWQiOiIyMjQ1MzI4ODIiLCJGaXJzdE5hbWUiOiJTYWhpbCIsImVudHMiOlt7ImNvdW50cnkiOiJJTkQiLCJlbnQiOiJSRUcifSx7ImNvdW50cnkiOiJJTkQiLCJlbnQiOiJQUkVNSVVNIn1dLCJMYXN0TmFtZSI6IktodXNybyIsImV4cCI6MTc4OTQ3MDE4MiwiU2Vzc2lvbklkIjoiZXlKaGJHY2lPaUpvZEhSd09pOHZkM2QzTG5jekxtOXlaeTh5TURBeEx6QTBMM2h0YkdSemFXY3RiVzl5WlNOb2JXRmpMWE5vWVRJMU5pSXNJblI1Y0NJNklrcFhWQ0o5LmV5SmlkU0k2SWpFd01ERXhJaXdpYzJraU9pSTJNR0U1WVdRNE5DMWxPVE5rTFRRNE1HWXRPREJrTmkxaFpqTTNORGswWmpKbE1qSWlMQ0pvZEhSd09pOHZjMk5vWlcxaGN5NTRiV3h6YjJGd0xtOXlaeTkzY3k4eU1EQTFMekExTDJsa1pXNTBhWFI1TDJOc1lXbHRjeTl1WVcxbGFXUmxiblJwWm1sbGNpSTZJakl5TkRVek1qZzRNaUlzSW1sa0lqb2lZVFkwWVRnNFkySXRPRFl6WmkwME1qY3hMVGxrWkdZdFltSXhPRFpsTXpGa01qaGpJaXdpZENJNklqRWlMQ0pzSWpvaVpXNHRSMElpTENKa1l5STZJak0yTkRRaUxDSmhaV1FpT2lJeU1ESTJMVEE1TFRJMVZERXhPakF6T2pBeUxqVXhNbG9pTENKa2RDSTZJakVpTENKbFpDSTZJakl3TWpZdE1UQXRNVEZVTVRFNk1ETTZNREl1TlRFeVdpSXNJbU5sWkNJNklqSXdNall0TURrdE1USlVNVEU2TURNNk1ESXVOVEV6V2lJc0ltbHdJam9pTWpRd01UbzBPVEF3T2pGak9EazZPRFZrT0RwbU9HSTJPbUk0TnpJNlpUSm1OVG8xWVRRaUxDSmpJam9pUjFWU1IwRlBUaUlzSW5OMElqb2lTRklpTENKd1l5STZJakV5TWpBd01TSXNJbU52SWpvaVNVNUVJaXdpYm1KbUlqb3hOemc1TVRJME5UZ3lMQ0psZUhBaU9qRTNPVEUzTVRZMU9ESXNJbWx6Y3lJNkltRnpZMlZ1Wkc5dUxuUjJJaXdpWVhWa0lqb2lZWE5qWlc1a2IyNHVkSFlpZlEuVzBaOUpCc3dvUWwyZVVGR0NVRHg1d3lyajNkc003ZE00SDdEWE5NUExLNCIsIlN1YnNjcmliZWRQcm9kdWN0IjoiRjEgVFYgUHJlbWl1bSBNb250aGx5IiwianRpIjoiMDI1YzVmMWUtYjc4Ni00N2QxLTk2MDgtMWI2NTdhNDZhYWZkIiwiaGFzaGVkU3Vic2NyaWJlcklkIjoiMmd2N2ljVnRNUmFSSXEvZDRCVHJaZkR2V2Q4bTdteE5MUGQ0bnp6SHI2Yz0iLCJTdWJzY3JpcHRpb24iOiJQUkVNSVVNIiwiaWF0IjoxNzg5MTI0NTgzLCJpc3MiOiJGMVRWIn0."
    "GzX_pu6SgrddyIsaCEaFAfD8ozVUvamn_jfyW5TcWOc"
)

# 2026 Grid Metadata (Norris #1 Champion, Verstappen #3, Audi F1 Team, Cadillac F1 Team)
KNOWN_DRIVERS_2026 = {
    1: {"broadcast_name": "L. NORRIS", "name_acronym": "NOR", "team_name": "McLaren", "team_colour": "FF8000"},
    3: {"broadcast_name": "M. VERSTAPPEN", "name_acronym": "VER", "team_name": "Red Bull Racing", "team_colour": "3671C6"},
    5: {"broadcast_name": "G. BORTOLETO", "name_acronym": "BOR", "team_name": "Audi F1 Team", "team_colour": "FF5A5A"},
    6: {"broadcast_name": "I. HADJAR", "name_acronym": "HAD", "team_name": "Red Bull Racing", "team_colour": "3671C6"},
    7: {"broadcast_name": "J. DOOHAN", "name_acronym": "DOO", "team_name": "Alpine", "team_colour": "0090FF"},
    10: {"broadcast_name": "P. GASLY", "name_acronym": "GAS", "team_name": "Alpine", "team_colour": "0090FF"},
    11: {"broadcast_name": "S. PEREZ", "name_acronym": "PER", "team_name": "Cadillac Formula 1 Team", "team_colour": "D4AF37"},
    12: {"broadcast_name": "A. ANTONELLI", "name_acronym": "ANT", "team_name": "Mercedes", "team_colour": "00D2BE"},
    14: {"broadcast_name": "F. ALONSO", "name_acronym": "ALO", "team_name": "Aston Martin", "team_colour": "229971"},
    16: {"broadcast_name": "C. LECLERC", "name_acronym": "LEC", "team_name": "Ferrari", "team_colour": "E80020"},
    18: {"broadcast_name": "L. STROLL", "name_acronym": "STR", "team_name": "Aston Martin", "team_colour": "229971"},
    22: {"broadcast_name": "Y. TSUNODA", "name_acronym": "TSU", "team_name": "Racing Bulls", "team_colour": "6692FF"},
    23: {"broadcast_name": "A. ALBON", "name_acronym": "ALB", "team_name": "Williams", "team_colour": "64C4FF"},
    27: {"broadcast_name": "N. HULKENBERG", "name_acronym": "HUL", "team_name": "Audi F1 Team", "team_colour": "FF5A5A"},
    30: {"broadcast_name": "L. LAWSON", "name_acronym": "LAW", "team_name": "Racing Bulls", "team_colour": "6692FF"},
    31: {"broadcast_name": "E. OCON", "name_acronym": "OCO", "team_name": "Haas", "team_colour": "B6BABD"},
    41: {"broadcast_name": "A. LINDBLAD", "name_acronym": "LIN", "team_name": "Racing Bulls", "team_colour": "6692FF"},
    43: {"broadcast_name": "F. COLAPINTO", "name_acronym": "COL", "team_name": "Alpine", "team_colour": "0090FF"},
    44: {"broadcast_name": "L. HAMILTON", "name_acronym": "HAM", "team_name": "Ferrari", "team_colour": "E80020"},
    55: {"broadcast_name": "C. SAINZ", "name_acronym": "SAI", "team_name": "Williams", "team_colour": "64C4FF"},
    63: {"broadcast_name": "G. RUSSELL", "name_acronym": "RUS", "team_name": "Mercedes", "team_colour": "00D2BE"},
    77: {"broadcast_name": "V. BOTTAS", "name_acronym": "BOT", "team_name": "Cadillac Formula 1 Team", "team_colour": "D4AF37"},
    81: {"broadcast_name": "O. PIASTRI", "name_acronym": "PIA", "team_name": "McLaren", "team_colour": "FF8000"},
    87: {"broadcast_name": "O. BEARMAN", "name_acronym": "BEA", "team_name": "Haas", "team_colour": "B6BABD"},
}

JOLPICA_BASE = "https://api.jolpi.ca/ergast/f1"
meta_cache: TTLCache[str, Any] = TTLCache(maxsize=100, ttl=3600)


class LiveF1Engine:
    def __init__(self):
        self._client = httpx.AsyncClient(timeout=10.0)
        self._state_lock = threading.Lock()
        
        # State stores
        self._session_info: Dict[str, Any] = {}
        self._driver_list: Dict[str, Dict[str, Any]] = {}
        self._timing_lines: Dict[str, Dict[str, Any]] = {}
        self._weather_data: Dict[str, Any] = {}
        self._track_status: Dict[str, Any] = {"Status": "1", "Message": "AllClear"}
        self._race_control_messages: List[Dict[str, Any]] = []
        self._last_event_time: float = 0.0
        
        # Thread & Connection
        self._hub_thread: Optional[threading.Thread] = None
        self._connection: Optional[Any] = None
        self._is_connected: bool = False
        self._stopped: bool = False

    def start(self):
        """Starts the SignalR live timing background worker."""
        self._stopped = False
        if self._hub_thread is None or not self._hub_thread.is_alive():
            self._hub_thread = threading.Thread(target=self._run_signalr, daemon=True, name="SignalR-Worker")
            self._hub_thread.start()
            logger.info("SignalR background worker started.")

    def stop(self):
        """Stops the SignalR worker gracefully."""
        self._stopped = True
        self._is_connected = False
        if self._connection:
            try:
                self._connection.stop()
            except Exception as e:
                logger.warning(f"Error stopping connection: {e}")
        logger.info("SignalR background worker stopped.")

    def _run_signalr(self):
        token = os.environ.get("F1TV_TOKEN", DEFAULT_F1TV_TOKEN).strip()
        if not token:
            logger.error("No F1TV_TOKEN found. SignalR client cannot authenticate.")
            return

        negotiate_url = "https://livetiming.formula1.com/signalrcore/negotiate"
        ws_url = "wss://livetiming.formula1.com/signalrcore"

        while not self._stopped:
            try:
                headers = {}
                try:
                    r = requests.options(negotiate_url, headers=headers, timeout=6)
                    if "AWSALBCORS" in r.cookies:
                        headers["Cookie"] = f"AWSALBCORS={r.cookies['AWSALBCORS']}"
                except Exception as ex:
                    logger.warning(f"Pre-negotiate cookie fetch notice: {ex}")

                options = {
                    "verify_ssl": True,
                    "access_token_factory": lambda: token,
                    "headers": headers
                }

                conn = HubConnectionBuilder() \
                    .with_url(ws_url, options=options) \
                    .configure_logging(logging.WARNING) \
                    .build()

                def on_open():
                    logger.info(">>> F1 Live Timing SignalR Connection Established!")
                    self._is_connected = True
                    topics = [
                        "Heartbeat", "SessionInfo", "DriverList", "TimingData",
                        "TimingAppData", "TrackStatus", "WeatherData", "RaceControlMessages"
                    ]
                    conn.send("Subscribe", [topics])
                    logger.info(f"Subscribed to F1 topics: {topics}")

                def on_close():
                    logger.info("F1 Live Timing SignalR Connection Closed.")
                    self._is_connected = False

                conn.on_open(on_open)
                conn.on_close(on_close)
                conn.on("feed", self._handle_feed)

                self._connection = conn
                conn.start()

                # Keep thread alive as long as connected
                while self._is_connected and not self._stopped:
                    time.sleep(2)

            except Exception as e:
                logger.error(f"SignalR connection loop error: {e}")
                self._is_connected = False

            if not self._stopped:
                logger.info("Reconnecting to F1 Live Timing in 5 seconds...")
                time.sleep(5)

    def _handle_feed(self, data):
        try:
            if not isinstance(data, list) or len(data) < 2:
                return
            topic = data[0]
            payload = data[1]
            self._last_event_time = time.time()

            with self._state_lock:
                if topic == "SessionInfo" and isinstance(payload, dict):
                    self._session_info.update(payload)

                elif topic == "DriverList" and isinstance(payload, dict):
                    for num_str, d_info in payload.items():
                        if isinstance(d_info, dict):
                            self._driver_list.setdefault(num_str, {}).update(d_info)

                elif topic == "TrackStatus" and isinstance(payload, dict):
                    self._track_status.update(payload)

                elif topic == "WeatherData" and isinstance(payload, dict):
                    self._weather_data.update(payload)

                elif topic == "RaceControlMessages" and isinstance(payload, dict):
                    msgs = payload.get("Messages", [])
                    if isinstance(msgs, list):
                        self._race_control_messages.extend(msgs)
                        self._race_control_messages = self._race_control_messages[-25:]

                elif topic == "TimingData" and isinstance(payload, dict):
                    lines = payload.get("Lines", {})
                    if isinstance(lines, dict):
                        for num_str, line_data in lines.items():
                            if num_str not in self._timing_lines:
                                self._timing_lines[num_str] = {}
                            self._deep_update(self._timing_lines[num_str], line_data)

        except Exception as e:
            logger.warning(f"Error handling feed message: {e}")

    def _deep_update(self, target: dict, source: dict):
        for k, v in source.items():
            if isinstance(v, dict) and k in target and isinstance(target[k], dict):
                self._deep_update(target[k], v)
            else:
                target[k] = v

    def _get_driver_meta(self, num: int) -> Dict[str, str]:
        known = KNOWN_DRIVERS_2026.get(num, {})
        num_str = str(num)
        raw = self._driver_list.get(num_str, {})

        first = str(raw.get("FirstName") or "").strip()
        last = str(raw.get("LastName") or "").strip()
        broadcast = str(raw.get("BroadcastName") or "").strip()

        if not broadcast or broadcast in (".", f"Driver {num}"):
            if first or last:
                broadcast = f"{first[:1]}. {last}".strip()
            else:
                broadcast = known.get("broadcast_name", f"Driver {num}")

        tla = str(raw.get("Tla") or "").strip()
        if not tla or tla == "DRV":
            tla = known.get("name_acronym", "DRV")

        team_name = str(raw.get("TeamName") or "").strip()
        if not team_name or team_name in ("F1 Team", "Team"):
            team_name = known.get("team_name", "F1 Team")

        team_colour = str(raw.get("TeamColour") or "").replace("#", "").strip()
        if not team_colour or team_colour in ("FFFFFF", "000000"):
            team_colour = known.get("team_colour", "FFFFFF").replace("#", "")

        return {
            "broadcast_name": broadcast or known.get("broadcast_name", f"Driver {num}"),
            "name_acronym": tla or known.get("name_acronym", "DRV"),
            "team_name": team_name or known.get("team_name", "F1 Team"),
            "team_colour": team_colour or known.get("team_colour", "FFFFFF").replace("#", "")
        }

    async def get_live_timing(self) -> Dict[str, Any]:
        """Returns the active live session timing, positions, and interval gaps."""
        with self._state_lock:
            leaderboard = []
            for num_str, line in self._timing_lines.items():
                try:
                    num = int(num_str)
                except ValueError:
                    continue

                pos_raw = line.get("Position")
                try:
                    pos = int(pos_raw) if pos_raw is not None else 99
                except (ValueError, TypeError):
                    pos = 99

                meta = self._get_driver_meta(num)

                # Lap times
                last_lap = ""
                last_obj = line.get("LastLapTime")
                if isinstance(last_obj, dict):
                    last_lap = last_obj.get("Value", "")
                elif isinstance(last_obj, str):
                    last_lap = last_obj

                best_lap = ""
                best_obj = line.get("BestLapTime")
                if isinstance(best_obj, dict):
                    best_lap = best_obj.get("Value", "")
                elif isinstance(best_obj, str):
                    best_lap = best_obj

                # Sectors
                sectors = line.get("Sectors", {})
                s1, s2, s3 = "", "", ""
                if isinstance(sectors, dict):
                    s1 = sectors.get("0", {}).get("Value", "") or sectors.get("0", {}).get("PreviousValue", "")
                    s2 = sectors.get("1", {}).get("Value", "") or sectors.get("1", {}).get("PreviousValue", "")
                    s3 = sectors.get("2", {}).get("Value", "") or sectors.get("2", {}).get("PreviousValue", "")

                # Speed trap
                speed_trap = ""
                speeds = line.get("Speeds", {})
                if isinstance(speeds, dict):
                    st_obj = speeds.get("ST", {})
                    if isinstance(st_obj, dict):
                        speed_trap = str(st_obj.get("Value", ""))

                # Gaps
                gap_val = line.get("GapToLeader", "")
                if isinstance(gap_val, dict):
                    gap_val = gap_val.get("Value", "")

                int_val = line.get("IntervalToPositionAhead", "")
                if isinstance(int_val, dict):
                    int_val = int_val.get("Value", "")

                laps = line.get("NumberOfLaps", 0)
                in_pit = bool(line.get("InPit", False))

                leaderboard.append({
                    "position": pos,
                    "driver_number": num,
                    "broadcast_name": meta["broadcast_name"],
                    "name_acronym": meta["name_acronym"],
                    "team_name": meta["team_name"],
                    "team_colour": meta["team_colour"],
                    "last_lap_time": last_lap,
                    "best_lap_time": best_lap,
                    "gap_to_leader": str(gap_val) if gap_val else ("LEADER" if pos == 1 else "--"),
                    "interval": str(int_val) if int_val else ("LEADER" if pos == 1 else "--"),
                    "sector1": str(s1),
                    "sector2": str(s2),
                    "sector3": str(s3),
                    "speed_trap": speed_trap,
                    "laps": laps,
                    "in_pit": in_pit
                })

            present_numbers = {int(k) for k in self._timing_lines.keys() if k.isdigit()}
            # Guarantee the full 22-car 2026 grid is represented (in-pit cars with no laps set)
            OFFICIAL_GRID_2026_NUMBERS = [
                1, 81,       # McLaren (Norris, Piastri)
                16, 44,      # Ferrari (Leclerc, Hamilton)
                63, 12,      # Mercedes (Russell, Antonelli)
                3, 6,        # Red Bull (Verstappen, Hadjar)
                23, 55,      # Williams (Albon, Sainz)
                14, 18,      # Aston Martin (Alonso, Stroll)
                10, 43,      # Alpine (Gasly, Colapinto)
                31, 87,      # Haas (Ocon, Bearman)
                30, 41,      # Racing Bulls (Lawson, Lindblad)
                27, 5,       # Audi F1 Team (Hulkenberg, Bortoleto)
                11, 77       # Cadillac Formula 1 Team (Perez, Bottas)
            ]
            next_pos = len(leaderboard) + 1
            for num in OFFICIAL_GRID_2026_NUMBERS:
                if len(leaderboard) >= 22:
                    break
                if num not in present_numbers:
                    if num == 6 and 22 in present_numbers:
                        continue
                    meta = self._get_driver_meta(num)
                    leaderboard.append({
                        "position": next_pos,
                        "driver_number": num,
                        "broadcast_name": meta["broadcast_name"],
                        "name_acronym": meta["name_acronym"],
                        "team_name": meta["team_name"],
                        "team_colour": meta["team_colour"],
                        "last_lap_time": "",
                        "best_lap_time": "",
                        "gap_to_leader": "NO TIME",
                        "interval": "--",
                        "sector1": "",
                        "sector2": "",
                        "sector3": "",
                        "speed_trap": "",
                        "laps": 0,
                        "in_pit": True
                    })
                    next_pos += 1

            leaderboard = leaderboard[:22]
            leaderboard.sort(key=lambda x: x["position"])

            meeting = self._session_info.get("Meeting", {})
            return {
                "status": "live" if (time.time() - self._last_event_time < 300) else "completed",
                "session_name": self._session_info.get("Name", "Practice 1"),
                "circuit_short_name": meeting.get("Circuit", {}).get("ShortName", "Circuit"),
                "country_name": meeting.get("Country", {}).get("Name", "Grand Prix"),
                "session_key": self._session_info.get("Key", 20260911),
                "timestamp": time.time(),
                "track_flag": self._track_status.get("Message", "AllClear"),
                "leaderboard": leaderboard,
                "engine": "livef1-signalr"
            }

    async def get_live_weather(self) -> Dict[str, Any]:
        """Returns the latest track and air weather conditions."""
        with self._state_lock:
            air = self._weather_data.get("AirTemp")
            track = self._weather_data.get("TrackTemp")
            hum = self._weather_data.get("Humidity")
            wind = self._weather_data.get("WindSpeed")
            rain = self._weather_data.get("Rainfall")
            return {
                "air_temperature": float(air) if air else 25.0,
                "track_temperature": float(track) if track else 32.0,
                "humidity": float(hum) if hum else 45.0,
                "wind_speed": float(wind) if wind else 10.0,
                "rainfall": bool(int(rain) > 0) if rain is not None else False,
                "timestamp": str(self._weather_data.get("Utc", time.time()))
            }

    async def get_race_control(self) -> List[Dict[str, Any]]:
        """Returns recent race control messages (flags, safety car)."""
        with self._state_lock:
            return list(self._race_control_messages)

    async def get_calendar(self) -> Dict[str, Any]:
        """Returns 2026 championship calendar."""
        if "calendar" in meta_cache:
            return meta_cache["calendar"]
        try:
            res = await self._client.get(f"{JOLPICA_BASE}/current.json")
            if res.status_code == 200:
                data = res.json()
                meta_cache["calendar"] = data
                return data
        except Exception as e:
            logger.error(f"Calendar error: {e}")
        return {}

    async def get_driver_standings(self) -> Dict[str, Any]:
        """Returns 2026 Drivers Championship standings."""
        if "driver_standings" in meta_cache:
            return meta_cache["driver_standings"]
        try:
            res = await self._client.get(f"{JOLPICA_BASE}/current/driverStandings.json")
            if res.status_code == 200:
                data = res.json()
                meta_cache["driver_standings"] = data
                return data
        except Exception as e:
            logger.error(f"Driver standings error: {e}")
        return {}

    async def get_constructor_standings(self) -> Dict[str, Any]:
        """Returns 2026 Constructors Championship standings."""
        if "constructor_standings" in meta_cache:
            return meta_cache["constructor_standings"]
        try:
            res = await self._client.get(f"{JOLPICA_BASE}/current/constructorStandings.json")
            if res.status_code == 200:
                data = res.json()
                meta_cache["constructor_standings"] = data
                return data
        except Exception as e:
            logger.error(f"Constructor standings error: {e}")
        return {}

    async def close(self):
        self.stop()
        await self._client.aclose()


engine = LiveF1Engine()
