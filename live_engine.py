import asyncio
import logging
import time
from typing import Any, Dict, List, Optional
from cachetools import TTLCache
import httpx

logger = logging.getLogger("livef1_engine")

# In-memory caches:
# Live timing updates frequently (TTL 5 seconds)
# Standings and calendar change infrequently (TTL 1 hour)
live_cache: TTLCache[str, Any] = TTLCache(maxsize=100, ttl=5)
meta_cache: TTLCache[str, Any] = TTLCache(maxsize=100, ttl=3600)

OPENF1_BASE = "https://api.openf1.org/v1"
JOLPICA_BASE = "https://api.jolpi.ca/ergast/f1"


class LiveF1Engine:
    def __init__(self):
        self._client = httpx.AsyncClient(timeout=10.0)
        self._livef1_available = False
        try:
            import livef1  # noqa: F401
            self._livef1_available = True
            logger.info("livef1 toolkit initialized successfully.")
        except Exception as e:
            logger.warning(f"livef1 package load notice (using direct async pipeline): {e}")

    async def get_live_timing(self) -> Dict[str, Any]:
        """Returns the active live session timing, positions, and interval gaps."""
        cache_key = "live_timing"
        if cache_key in live_cache:
            return live_cache[cache_key]

        data = await self._fetch_live_timing_data()
        live_cache[cache_key] = data
        return data

    async def _fetch_live_timing_data(self) -> Dict[str, Any]:
        try:
            # 1. Fetch latest session metadata
            res = await self._client.get(f"{OPENF1_BASE}/sessions?session_key=latest")
            if res.status_code != 200 or not res.json():
                return {"status": "inactive", "message": "No active live session on track.", "drivers": []}

            session = res.json()[0]
            session_key = session.get("session_key")

            # 2. Concurrently fetch positions, intervals, and driver info
            pos_task = self._client.get(f"{OPENF1_BASE}/position?session_key={session_key}")
            drivers_task = self._client.get(f"{OPENF1_BASE}/drivers?session_key={session_key}")
            intervals_task = self._client.get(f"{OPENF1_BASE}/intervals?session_key={session_key}")

            pos_res, drivers_res, intervals_res = await asyncio.gather(
                pos_task, drivers_task, intervals_task, return_exceptions=True
            )

            drivers_map = {}
            if not isinstance(drivers_res, Exception) and drivers_res.status_code == 200:
                for d in drivers_res.json():
                    drivers_map[d.get("driver_number")] = d

            # Latest position per driver
            latest_positions = {}
            if not isinstance(pos_res, Exception) and pos_res.status_code == 200:
                for p in pos_res.json():
                    num = p.get("driver_number")
                    latest_positions[num] = p.get("position")

            leaderboard = []
            for num, pos in sorted(latest_positions.items(), key=lambda x: x[1] if x[1] else 99):
                driver_info = drivers_map.get(num, {})
                leaderboard.append({
                    "position": pos,
                    "driver_number": num,
                    "broadcast_name": driver_info.get("broadcast_name", f"Driver {num}"),
                    "name_acronym": driver_info.get("name_acronym", "DRV"),
                    "team_name": driver_info.get("team_name", "F1 Team"),
                    "team_colour": driver_info.get("team_colour", "FFFFFF"),
                })

            return {
                "status": "live",
                "session_name": session.get("session_name"),
                "circuit_short_name": session.get("circuit_short_name"),
                "country_name": session.get("country_name"),
                "session_key": session_key,
                "timestamp": time.time(),
                "leaderboard": leaderboard,
                "engine": "livef1-backend"
            }
        except Exception as e:
            logger.error(f"Error gathering live timing: {e}")
            return {"status": "error", "message": str(e), "leaderboard": []}

    async def get_live_weather(self) -> Dict[str, Any]:
        """Returns the latest track and air weather conditions."""
        cache_key = "live_weather"
        if cache_key in live_cache:
            return live_cache[cache_key]

        try:
            res = await self._client.get(f"{OPENF1_BASE}/weather?session_key=latest")
            if res.status_code == 200 and res.json():
                latest = res.json()[-1]
                data = {
                    "air_temperature": latest.get("air_temperature"),
                    "track_temperature": latest.get("track_temperature"),
                    "humidity": latest.get("humidity"),
                    "wind_speed": latest.get("wind_speed"),
                    "rainfall": latest.get("rainfall", 0) > 0,
                    "timestamp": latest.get("date")
                }
                live_cache[cache_key] = data
                return data
        except Exception as e:
            logger.error(f"Weather fetch error: {e}")

        return {"air_temperature": 25.0, "track_temperature": 32.0, "humidity": 45.0, "rainfall": False}

    async def get_race_control(self) -> List[Dict[str, Any]]:
        """Returns recent race control messages (flags, safety car)."""
        cache_key = "race_control"
        if cache_key in live_cache:
            return live_cache[cache_key]

        try:
            res = await self._client.get(f"{OPENF1_BASE}/race_control?session_key=latest")
            if res.status_code == 200 and res.json():
                # return last 10 messages
                data = res.json()[-10:]
                live_cache[cache_key] = data
                return data
        except Exception as e:
            logger.error(f"Race control error: {e}")

        return []

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
        await self._client.aclose()


engine = LiveF1Engine()
