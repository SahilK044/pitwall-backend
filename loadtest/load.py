"""Simulated race-day crowd against loadtest/server.py, using the app's own poll intervals
(LiveViewModel): timing every 4 s, positions every 1 s, race control every 5 s, weather every 60 s,
predictions every 30 s while the screen is open, and one pick per fan.

    python loadtest/load.py --pid <server pid> --users 200 400 800 --seconds 30
Prints, per step: requests/s, latency p50/p95/p99, errors, server CPU (share of one core) and
bytes sent, so the result can be scaled to the host's CPU share.
"""
import argparse
import asyncio
import random
import statistics
import time

import httpx
import psutil

BASE = "http://127.0.0.1:7870"
POLLS = [("/api/v1/live/timing", 4.0), ("/api/v1/live/positions", 1.0), ("/api/v1/live/race_control", 5.0),
         ("/api/v1/live/weather", 60.0)]
GRID = []
OFFSET = 0


async def fan(i: int, client: httpx.AsyncClient, stop: float, lat: list, errs: list, sizes: list, voter: bool):
    uid = f"pw_load{i:08d}"
    await asyncio.sleep(random.random() * 4)            # fans don't all arrive in the same instant
    nexts = {p: time.monotonic() + random.random() * iv for p, iv in POLLS}
    if voter:
        nexts["/api/v1/predict/current"] = time.monotonic()
    picked = False
    while time.monotonic() < stop:
        path = min(nexts, key=nexts.get)
        wait = nexts[path] - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        if time.monotonic() >= stop:
            break
        t0 = time.perf_counter()
        try:
            if path == "/api/v1/predict/current":
                r = await client.get(path, params={"user_id": uid})
                if not picked and GRID:
                    r2 = await client.post("/api/v1/predict", json={"user_id": uid, "round": r.json()["race"]["round"], "driver_id": random.choice(GRID)})
                    picked = r2.status_code == 200
                    if r2.status_code != 200:
                        errs.append(f"pick {r2.status_code}")
                nexts[path] = time.monotonic() + 30
            else:
                r = await client.get(path)
                nexts[path] = time.monotonic() + dict(POLLS)[path]
            lat.append(time.perf_counter() - t0)
            sizes.append(int(r.headers.get("content-length") or len(r.content)))
            if r.status_code != 200:
                errs.append(f"{path} {r.status_code}")
        except Exception as e:
            errs.append(type(e).__name__)
            nexts[path] = time.monotonic() + 1


async def step(users: int, seconds: float, proc: psutil.Process):
    lat, errs, sizes = [], [], []
    limits = httpx.Limits(max_connections=users + 10, max_keepalive_connections=users + 10)
    async with httpx.AsyncClient(base_url=BASE, timeout=15, limits=limits, headers={"Accept-Encoding": "gzip"}) as client:
        cpu0 = sum(proc.cpu_times()[:2]); t0 = time.monotonic()
        stop = t0 + seconds
        await asyncio.gather(*(fan(OFFSET + i, client, stop, lat, errs, sizes, voter=(i % 3 == 0)) for i in range(users)))
        wall = time.monotonic() - t0; cpu = sum(proc.cpu_times()[:2]) - cpu0
    lat.sort()
    q = lambda p: lat[min(len(lat) - 1, int(p * len(lat)))] * 1000 if lat else float("nan")
    print(f"{users:5d} fans | {len(lat)/wall:7.1f} req/s | p50 {q(.5):6.1f} ms  p95 {q(.95):6.1f} ms  p99 {q(.99):6.1f} ms | "
          f"errors {len(errs):4d} | server CPU {cpu/wall*100:5.1f}% of a core | {sum(sizes)/wall/1e6:6.2f} MB/s out"
          + (f" | {sorted(set(errs))[:3]}" if errs else ""), flush=True)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--users", type=int, nargs="+", default=[100, 300, 600])
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--offset", type=int, default=0, help="first fan id, so parallel runs don't share ids")
    ap.add_argument("--hold", action="store_true")
    a = ap.parse_args()
    global OFFSET
    OFFSET = a.offset
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        cur = (await c.get("/api/v1/predict/current")).json()
        GRID.extend(d["driver_id"] for d in cur["grid"])
        print(f"race: {cur['race']['name']} (round {cur['race']['round']}), grid of {len(GRID)}")
    proc = psutil.Process(a.pid)
    for u in a.users:
        await step(u, a.seconds, proc)
    if a.hold:
        await asyncio.sleep(0)


asyncio.run(main())
