"""Podium predictions end to end against a temporary SQLite store, with Jolpica replaced by a fake calendar."""
import asyncio
import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import predictions as P

NOW = time.time()
DRIVERS = ["max_verstappen", "norris", "piastri", "russell", "leclerc"]
GRID = [{"driver_id": d, "code": d[:3].upper(), "given": d.title(), "family": d.title(), "number": str(i), "team": "Team"}
        for i, d in enumerate(DRIVERS, start=1)]
RACES = {
    1: {"round": "1", "raceName": "Past GP", "Circuit": {"circuitName": "Old Ring"}, "start": NOW - 4 * 3600},
    2: {"round": "2", "raceName": "Next GP", "Circuit": {"circuitName": "New Ring"}, "start": NOW + 2 * 86400},
}
RESULT = {"podium": ["norris", "piastri", "max_verstappen"], "race_name": "Past GP"}


class FakeData:
    client = None

    async def races(self):
        return list(RACES.values())

    async def grid(self):
        return GRID

    async def season(self):
        return "2026"

    @staticmethod
    def start_of(race):
        return race["start"]

    async def next_race(self, now=None):
        now = now or time.time()
        return next((r for r in RACES.values() if r["start"] + 3 * 3600 > now), None)

    async def race(self, rnd):
        return RACES.get(rnd)

    async def result(self, rnd):
        return dict(RESULT) if rnd == 1 else None


def make_store(tmp_path):
    """SQLite by default; the real Postgres when PG_TEST_URL is set (tables emptied first)."""
    url = os.environ.get("PG_TEST_URL", "")
    if not url:
        return P.Store(str(tmp_path / "p.db"))
    store = P.Store("", url)
    for table in ("podium_picks", "podium_results", "players"):
        store._run(f"DELETE FROM {table}")
    return store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "ENABLED", True)
    monkeypatch.setattr(P, "store", make_store(tmp_path))
    monkeypatch.setattr(P, "data", FakeData())
    P._buckets.clear(); P._dist_cache.clear(); P._board_cache.clear()
    app = FastAPI()
    app.include_router(P.router)
    return TestClient(app)


def uid(n):
    return f"pw_test{n:06d}"


def h(n):
    return {"X-Pitwall-User": uid(n)}


def test_podium_pick_change_and_crowd(client):
    assert client.post("/api/v1/predict", json={"round": 2, "podium": ["norris", "piastri", "leclerc"]}, headers=h(1)).status_code == 200
    assert client.post("/api/v1/predict", json={"round": 2, "podium": ["norris", "russell", "piastri"]}, headers=h(2)).status_code == 200
    # changing a pick replaces it rather than adding a second one
    assert client.post("/api/v1/predict", json={"round": 2, "podium": ["leclerc", "norris", "piastri"]}, headers=h(1)).status_code == 200
    cur = client.get("/api/v1/predict/current", headers=h(1)).json()
    assert cur["open"] is True and cur["race"]["round"] == 2 and cur["picks_total"] == 2
    by = {d["driver_id"]: d for d in cur["distribution"]}
    assert by["norris"] == {"driver_id": "norris", "p1": 1, "p2": 1, "p3": 0, "podium": 2}
    assert by["piastri"]["podium"] == 2 and by["leclerc"]["p1"] == 1 and by["russell"]["p2"] == 1
    assert cur["distribution"][0]["driver_id"] in {"norris", "piastri"}          # most podium picks first
    assert cur["mine"]["podium"] == ["leclerc", "norris", "piastri"]


def test_rejects_bad_picks(client):
    post = lambda body, n=1: client.post("/api/v1/predict", json=body, headers=h(n)).status_code
    assert post({"round": 1, "podium": ["norris", "piastri", "leclerc"]}) == 409          # race already started
    assert post({"round": 2, "podium": ["norris", "norris", "leclerc"]}) == 400          # duplicate driver
    assert post({"round": 2, "podium": ["norris", "tsunoda", "leclerc"]}) == 400         # not on the grid
    assert post({"round": 2, "podium": ["norris", "piastri"]}) == 422                    # not three
    assert client.post("/api/v1/predict", json={"round": 2, "podium": DRIVERS[:3]}).status_code == 400   # no user header
    assert client.post("/api/v1/predict", json={"round": 2, "podium": DRIVERS[:3]}, headers={"X-Pitwall-User": "bad id!"}).status_code == 400


def test_scoring_rescoring_and_leaderboard(client):
    # picks for the finished round go straight into the store (the API would refuse them now)
    P.store.upsert_pick("2026", 1, uid(1), ["norris", "piastri", "max_verstappen"])      # perfect: 5+5+5+5
    P.store.upsert_pick("2026", 1, uid(2), ["piastri", "norris", "max_verstappen"])      # 2+2+5
    P.store.upsert_pick("2026", 1, uid(3), ["norris", "leclerc", "russell"])             # 5
    P.store.upsert_pick("2026", 1, uid(4), ["leclerc", "russell", "piastri"])            # 2
    done = asyncio.run(P.score_finished_races())
    assert done == [{"round": 1, "podium": ["norris", "piastri", "max_verstappen"], "picks": 4, "rescored": False}]
    assert asyncio.run(P.score_finished_races()) == []                                    # unchanged result: no rescore
    pts = lambda n: client.get("/api/v1/predict/results/1", headers=h(n)).json()["mine"]["points"]
    assert [pts(n) for n in (1, 2, 3, 4)] == [20, 9, 5, 2]

    # stewards swap P2 and P3: the round is rescored
    RESULT["podium"] = ["norris", "max_verstappen", "piastri"]
    try:
        assert asyncio.run(P.score_finished_races())[0]["rescored"] is True
        assert [pts(n) for n in (1, 2, 3, 4)] == [9, 6, 5, 5]   # 2: all three on the podium, none in place
    finally:
        RESULT["podium"] = ["norris", "piastri", "max_verstappen"]
        asyncio.run(P.score_finished_races())

    assert client.post("/api/v1/predict/username", json={"username": "BoxBox_44"}, headers=h(1)).json()["username"] == "BoxBox_44"
    board = client.get("/api/v1/predict/leaderboard", headers=h(2)).json()
    assert [(r["name"], r["points"]) for r in board["top"]] == [("BoxBox_44", 20), ("Fan", 9), ("Fan", 5), ("Fan", 2)]
    assert [r["you"] for r in board["top"]] == [False, True, False, False]
    assert all("user" not in r for r in board["top"])
    assert board["me"] == {"points": 9, "correct": 1, "scored": 1, "rank": 2}


def test_usernames_are_unique_and_checked(client):
    name = lambda n, u: client.post("/api/v1/predict/username", json={"username": u}, headers=h(n))
    assert name(1, "Lights_Out").status_code == 200
    assert name(2, "lights_out").status_code == 409                   # taken, whatever the case
    assert name(1, "LIGHTS_OUT").status_code == 200                   # its owner may change the case
    for bad in ["ab", "has space", "_leading", "way_too_long_username_here", "admin", "Pitwall", "fan", "<script>"]:
        assert name(3, bad).status_code == 400, bad
    assert client.get("/api/v1/predict/current", headers=h(1)).json()["username"] == "LIGHTS_OUT"


def test_disabled_is_404(client, monkeypatch):
    monkeypatch.setattr(P, "ENABLED", False)
    assert client.get("/api/v1/predict/current").status_code == 404
