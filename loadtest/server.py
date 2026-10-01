"""The real app (main.py) with the F1 feed replaced by production payloads captured from Render,
so a load test measures the server's own cost per request. Predictions run on a temp SQLite store.

    PREDICTIONS_ENABLED=1 python loadtest/server.py   (listens on 127.0.0.1:7870, one worker like the Dockerfile)
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault("PREDICTIONS_ENABLED", "1")
os.environ.setdefault("PREDICTIONS_DB", os.path.join(tempfile.gettempdir(), "pw_load.db"))

import main  # noqa: E402
from live_engine import engine  # noqa: E402

payload = {name: json.loads((HERE / f"live_{name}.json").read_text(encoding="utf-8")) for name in ("timing", "positions", "weather", "race_control")}


def canned(name):
    async def get():
        return payload[name]
    return get


engine.start = lambda: None                     # no SignalR connection during the test
engine.get_live_timing = canned("timing")
engine.get_live_positions = canned("positions")
engine.get_live_telemetry = canned("positions")
engine.get_live_weather = canned("weather")
engine.get_race_control = canned("race_control")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(main.app, host="127.0.0.1", port=7870, log_level="warning", access_log=False)
