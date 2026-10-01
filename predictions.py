"""
Pre-race podium predictions.

Fans pick the top three of the next Grand Prix (P1, P2, P3) before lights out. After the race the
server fetches the official result from Jolpica on its own and scores every pick, so results reach
the app without an app update. For three days after a race it re-checks the result and rescores if
the stewards changed the podium.

Scoring, per pick: 5 points for each driver in the exact place, 2 for one who made the podium in
another place, and 5 more for the whole podium exactly right (20 at most).

Players choose a username (unique, case-insensitive) that the leaderboard shows. Identity is an
app-generated install id sent in the X-Pitwall-User header (kept out of URLs and access logs):
good enough for a fan game, not for prizes.

Storage is Postgres when DATABASE_URL is set (production: Render's disk is wiped on every deploy
and restart), otherwise SQLite (WAL) at PREDICTIONS_DB for local runs and tests.
The whole feature is off unless PREDICTIONS_ENABLED=1.
"""
import asyncio
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

JOLPICA = os.environ.get("JOLPICA_BASE", "https://api.jolpi.ca/ergast/f1")
DB_PATH = os.environ.get("PREDICTIONS_DB", os.path.join(os.path.dirname(__file__), "data", "predictions.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
ENABLED = os.environ.get("PREDICTIONS_ENABLED", "0") == "1"
SEASON = os.environ.get("PREDICTIONS_SEASON", "current")
EXACT, ON_PODIUM, PERFECT = 5, 2, 5
RESULT_POLL_S = int(os.environ.get("PREDICTIONS_POLL_S", "300"))
RESULT_GRACE_S = 90 * 60            # start looking for a result 90 minutes after lights out
RESCORE_WINDOW_S = 3 * 86400        # and keep checking it for stewards' changes this long
# Per IP, a flood guard only: mobile carriers put thousands of phones behind one address (CGNAT),
# so this must sit far above what fans on one network send in the rush before lights out. The real
# per-fan limit is per install id.
IP_WRITES_PER_MIN = int(os.environ.get("PREDICTIONS_IP_RATE", "3000"))

router = APIRouter(prefix="/api/v1/predict", tags=["predictions"])
_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.]{2,15}$")
RESERVED = {"admin", "administrator", "pitwall", "moderator", "mod", "support", "staff", "official", "fia", "f1",
            "formula1", "formulaone", "fan", "system", "root", "null", "undefined", "anonymous"}


# ---------------------------------------------------------------- storage
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS podium_picks (
        season TEXT NOT NULL, round INTEGER NOT NULL, user_id TEXT NOT NULL,
        p1 TEXT NOT NULL, p2 TEXT NOT NULL, p3 TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
        points INTEGER, hits INTEGER, PRIMARY KEY (season, round, user_id))""",
    "CREATE INDEX IF NOT EXISTS podium_picks_user ON podium_picks (season, user_id)",
    """CREATE TABLE IF NOT EXISTS podium_results (
        season TEXT NOT NULL, round INTEGER NOT NULL, podium TEXT NOT NULL,
        race_name TEXT, scored_at DOUBLE PRECISION NOT NULL, PRIMARY KEY (season, round))""",
    """CREATE TABLE IF NOT EXISTS players (
        user_id TEXT PRIMARY KEY, username TEXT NOT NULL, username_key TEXT NOT NULL UNIQUE,
        created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL)""",
]


class UsernameTaken(Exception):
    pass


class Store:
    """Picks, results and usernames. Postgres when DATABASE_URL is set, otherwise SQLite at PREDICTIONS_DB.
    The SQL is written once, with ? placeholders, and runs on both. One connection per thread."""

    def __init__(self, path: str, url: str = ""):
        self.pg = url.startswith(("postgres://", "postgresql://"))
        self.url, self.path = url, path
        if not self.pg:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._local = threading.local()
        for stmt in SCHEMA:
            self._run(stmt)

    def _conn(self):
        c = getattr(self._local, "c", None)
        if c is not None and self.pg and c.closed:
            c = None                                  # the server dropped it (idle timeout); reconnect
        if c is None:
            if self.pg:
                import psycopg
                from psycopg.rows import dict_row
                # prepare_threshold=None: poolers such as Neon's (PgBouncer, transaction mode) can't
                # keep server-side prepared statements, which psycopg otherwise starts using.
                c = psycopg.connect(self.url, autocommit=True, row_factory=dict_row, connect_timeout=10, prepare_threshold=None)
            else:
                c = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA synchronous=NORMAL")
                c.row_factory = sqlite3.Row
            self._local.c = c
        return c

    def _sql(self, q: str) -> str:
        return q.replace("?", "%s") if self.pg else q

    def _run(self, q: str, args=(), conn=None):
        c = conn or self._conn()
        try:
            return c.execute(self._sql(q), args)
        except Exception:
            if self.pg and conn is None and c.closed:  # one retry on a dropped connection
                self._local.c = None
                return self._conn().execute(self._sql(q), args)
            raise

    def _one(self, q: str, args=()) -> Optional[Dict[str, Any]]:
        r = self._run(q, args).fetchone()
        return dict(r) if r is not None else None

    def _all(self, q: str, args=()) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._run(q, args).fetchall()]

    # picks
    def upsert_pick(self, season: str, rnd: int, user: str, podium: List[str]) -> None:
        now = time.time()
        self._run(
            """INSERT INTO podium_picks (season, round, user_id, p1, p2, p3, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (season, round, user_id) DO UPDATE SET p1 = excluded.p1, p2 = excluded.p2, p3 = excluded.p3,
                 updated_at = excluded.updated_at""",
            (season, rnd, user, *podium, now, now))

    def pick(self, season: str, rnd: int, user: str) -> Optional[Dict[str, Any]]:
        r = self._one("SELECT p1, p2, p3, points, hits FROM podium_picks WHERE season=? AND round=? AND user_id=?", (season, rnd, user))
        return None if r is None else {"podium": [r["p1"], r["p2"], r["p3"]], "points": r["points"], "hits": r["hits"]}

    def distribution(self, season: str, rnd: int) -> Dict[str, Any]:
        """How many fans put each driver in each place, and on the podium at all."""
        rows = self._all(
            """SELECT d, pos, COUNT(*) AS n FROM (
                 SELECT p1 AS d, 1 AS pos FROM podium_picks WHERE season=? AND round=?
                 UNION ALL SELECT p2, 2 FROM podium_picks WHERE season=? AND round=?
                 UNION ALL SELECT p3, 3 FROM podium_picks WHERE season=? AND round=?
               ) t GROUP BY d, pos""", (season, rnd) * 3)
        total = int((self._one("SELECT COUNT(*) AS n FROM podium_picks WHERE season=? AND round=?", (season, rnd)) or {}).get("n") or 0)
        by: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            e = by.setdefault(r["d"], {"driver_id": r["d"], "p1": 0, "p2": 0, "p3": 0, "podium": 0})
            e[f"p{int(r['pos'])}"] += int(r["n"])
            e["podium"] += int(r["n"])
        drivers = sorted(by.values(), key=lambda e: (-e["podium"], -e["p1"], e["driver_id"]))
        return {"total": total, "drivers": drivers}

    # results and scoring
    def result(self, season: str, rnd: int) -> Optional[Dict[str, Any]]:
        return self._one("SELECT podium, race_name, scored_at FROM podium_results WHERE season=? AND round=?", (season, rnd))

    def save_result_and_score(self, season: str, rnd: int, podium: List[str], race_name: str) -> int:
        w1, w2, w3 = podium
        points = f"""(CASE WHEN p1=? THEN {EXACT} WHEN p1 IN (?,?) THEN {ON_PODIUM} ELSE 0 END)
                   + (CASE WHEN p2=? THEN {EXACT} WHEN p2 IN (?,?) THEN {ON_PODIUM} ELSE 0 END)
                   + (CASE WHEN p3=? THEN {EXACT} WHEN p3 IN (?,?) THEN {ON_PODIUM} ELSE 0 END)
                   + (CASE WHEN p1=? AND p2=? AND p3=? THEN {PERFECT} ELSE 0 END)"""
        hits = "(CASE WHEN p1=? THEN 1 ELSE 0 END) + (CASE WHEN p2=? THEN 1 ELSE 0 END) + (CASE WHEN p3=? THEN 1 ELSE 0 END)"
        steps = [
            ("DELETE FROM podium_results WHERE season=? AND round=?", (season, rnd)),
            ("INSERT INTO podium_results (season, round, podium, race_name, scored_at) VALUES (?, ?, ?, ?, ?)",
             (season, rnd, ",".join(podium), race_name, time.time())),
            (f"UPDATE podium_picks SET points = {points}, hits = {hits} WHERE season=? AND round=?",
             (w1, w2, w3, w2, w1, w3, w3, w1, w2, w1, w2, w3, w1, w2, w3, season, rnd)),
        ]
        c = self._conn()
        if self.pg:
            with c.transaction():
                for q, a in steps:
                    self._run(q, a, conn=c)
        else:
            c.execute("BEGIN IMMEDIATE")
            try:
                for q, a in steps:
                    self._run(q, a, conn=c)
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return int(self._one("SELECT COUNT(*) AS n FROM podium_picks WHERE season=? AND round=?", (season, rnd))["n"])

    def rounds_to_score(self, season: str) -> List[int]:
        """Rounds with picks that have no result yet, plus every scored round (the caller skips those
        past the rescore window)."""
        return [int(r["round"]) for r in self._all("SELECT DISTINCT round FROM podium_picks WHERE season=?", (season,))]

    # leaderboard
    def leaderboard(self, season: str, limit: int) -> List[Dict[str, Any]]:
        rows = self._all(
            """SELECT p.user_id AS user_id, MAX(pl.username) AS username,
                      CAST(SUM(COALESCE(p.points, 0)) AS INTEGER) AS pts, CAST(SUM(COALESCE(p.hits, 0)) AS INTEGER) AS hits
               FROM podium_picks p LEFT JOIN players pl ON pl.user_id = p.user_id
               WHERE p.season=? GROUP BY p.user_id HAVING COUNT(p.points) > 0
               ORDER BY pts DESC, hits DESC, MIN(p.created_at) ASC LIMIT ?""",
            (season, limit))
        return [{"rank": i + 1, "name": r["username"] or "Fan", "points": int(r["pts"]), "correct": int(r["hits"]), "user": r["user_id"]}
                for i, r in enumerate(rows)]

    def user_total(self, season: str, user: str) -> Dict[str, Any]:
        r = self._one(
            """SELECT CAST(SUM(COALESCE(points, 0)) AS INTEGER) AS pts, CAST(SUM(COALESCE(hits, 0)) AS INTEGER) AS hits,
                      COUNT(points) AS scored FROM podium_picks WHERE season=? AND user_id=?""",
            (season, user)) or {}
        return {"points": int(r.get("pts") or 0), "correct": int(r.get("hits") or 0), "scored": int(r.get("scored") or 0)}

    def rank_of(self, season: str, user: str) -> Optional[int]:
        """1-based leaderboard position, or None before this user has a scored pick."""
        me = self.user_total(season, user)
        if me["scored"] == 0:
            return None
        r = self._one(
            """SELECT COUNT(*) AS n FROM (
                 SELECT SUM(COALESCE(points, 0)) AS pts FROM podium_picks WHERE season=? GROUP BY user_id HAVING COUNT(points) > 0
               ) t WHERE t.pts > ?""", (season, me["points"]))
        return int(r["n"]) + 1

    # usernames
    def username(self, user: str) -> Optional[str]:
        r = self._one("SELECT username FROM players WHERE user_id=?", (user,))
        return r["username"] if r else None

    def set_username(self, user: str, name: str) -> None:
        """Claims [name] for [user]; UsernameTaken when someone else has it (any letter case)."""
        key, now = name.lower(), time.time()
        owner = self._one("SELECT user_id FROM players WHERE username_key=?", (key,))
        if owner and owner["user_id"] != user:
            raise UsernameTaken()
        try:
            self._run(
                """INSERT INTO players (user_id, username, username_key, created_at, updated_at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (user_id) DO UPDATE SET username = excluded.username, username_key = excluded.username_key,
                     updated_at = excluded.updated_at""",
                (user, name, key, now, now))
        except Exception as e:                        # lost a race for the same name
            if "unique" in str(e).lower():
                raise UsernameTaken() from e
            raise


store: Optional[Store] = None


# ---------------------------------------------------------------- race data (cached)
class RaceData:
    """Calendar, grid and results from Jolpica, cached. A refresh that fails keeps the last good
    copy, and only one refresh runs at a time, so a slow Jolpica never holds up picks."""

    def __init__(self):
        self._cache: Dict[str, Any] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self.client: Optional[httpx.AsyncClient] = None

    async def _get(self, path: str) -> Dict[str, Any]:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=15, headers={"User-Agent": "pitwall-predictions"})
        r = await self.client.get(f"{JOLPICA}/{path}")
        r.raise_for_status()
        return r.json()

    async def _cached(self, key: str, path: str, ttl: float) -> Dict[str, Any]:
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked() and hit:
            return hit[1]                             # someone is refreshing; serve the last copy
        async with lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
            try:
                value = await self._get(path)
            except Exception:
                if hit:
                    return hit[1]
                raise
            self._cache[key] = (time.time(), value)
            return value

    async def races(self) -> List[Dict[str, Any]]:
        return (await self._cached("calendar", f"{SEASON}.json", 3600))["MRData"]["RaceTable"]["Races"]

    async def season(self) -> str:
        """The calendar's season year: picks are stored under it, so seasons never mix."""
        cal = await self._cached("calendar", f"{SEASON}.json", 3600)
        return str(cal["MRData"]["RaceTable"].get("season") or SEASON)

    async def grid(self) -> List[Dict[str, Any]]:
        """Who is on the grid now, with their team: the latest race's entry list. Season standings
        are no good for this: they keep anyone who raced once (a reserve who stood in) and list a
        driver's team by the last constructor they drove for, which can be a one-off swap."""
        d = await self._cached("grid", f"{SEASON}/last/results.json", 3600)
        races = d["MRData"]["RaceTable"]["Races"]
        rows = races[0]["Results"] if races else []
        return [{
            "driver_id": r["Driver"]["driverId"], "code": r["Driver"].get("code"),
            "given": r["Driver"].get("givenName"), "family": r["Driver"].get("familyName"),
            "number": r["Driver"].get("permanentNumber") or r.get("number"),
            "team": r["Constructor"]["name"],
        } for r in rows]

    @staticmethod
    def start_of(race: Dict[str, Any]) -> float:
        t = race.get("time", "12:00:00Z").replace("Z", "+00:00")
        return datetime.fromisoformat(f"{race['date']}T{t}").astimezone(timezone.utc).timestamp()

    async def next_race(self, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """The race open for picks: the first whose lights-out is still ahead (or the one running now)."""
        now = now or time.time()
        for r in await self.races():
            if self.start_of(r) + 3 * 3600 > now:
                return r
        return None

    async def race(self, rnd: int) -> Optional[Dict[str, Any]]:
        return next((r for r in await self.races() if int(r["round"]) == rnd), None)

    async def result(self, rnd: int) -> Optional[Dict[str, Any]]:
        """The official top three, or None until it is published. Never cached: this is what the
        scoring loop re-checks for stewards' changes."""
        d = await self._get(f"{SEASON}/{rnd}/results.json")
        races = d["MRData"]["RaceTable"]["Races"]
        if not races or len(races[0].get("Results") or []) < 3:
            return None
        res = sorted(races[0]["Results"], key=lambda x: int(x["position"]))
        return {"podium": [x["Driver"]["driverId"] for x in res[:3]], "race_name": races[0].get("raceName", "")}


data = RaceData()


# ---------------------------------------------------------------- rate limiting
_buckets: Dict[str, List[float]] = {}


def _allow(key: str, per_min: int) -> bool:
    now = time.time()
    b = [t for t in _buckets.get(key, []) if now - t < 60]
    if len(b) >= per_min:
        _buckets[key] = b
        return False
    b.append(now)
    _buckets[key] = b
    if len(_buckets) > 50_000:                 # keep memory bounded
        for k in list(_buckets)[:10_000]:
            _buckets.pop(k, None)
    return True


def _guard(request: Request, user: str, kind: str, per_min: int) -> None:
    ip = request.client.host if request.client else "?"
    if not (_allow(f"ip:{ip}", IP_WRITES_PER_MIN) and _allow(f"{kind}:{user}", per_min)):
        raise HTTPException(429, "Too many changes, try again in a minute")


# ---------------------------------------------------------------- cached aggregates
_dist_cache: Dict[Any, Any] = {}
_board_cache: Dict[Any, Any] = {}


async def _distribution(season: str, rnd: int) -> Dict[str, Any]:
    hit = _dist_cache.get((season, rnd))
    if hit and time.time() - hit[0] < 15:
        return hit[1]
    v = await asyncio.to_thread(store.distribution, season, rnd)
    _dist_cache[(season, rnd)] = (time.time(), v)
    return v


# ---------------------------------------------------------------- API
def _require():
    if not ENABLED or store is None:
        raise HTTPException(404, "Not found")


def _user(header: Optional[str], required: bool = False) -> Optional[str]:
    if header and _ID.match(header):
        return header
    if required:
        raise HTTPException(400, "Missing or bad X-Pitwall-User")
    return None


class PickIn(BaseModel):
    round: int = Field(..., ge=1, le=30)
    podium: List[str] = Field(..., min_length=3, max_length=3)


class UsernameIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=32)


def check_username(raw: str) -> str:
    """3 to 16 letters, digits, _ or ., starting with a letter or digit; reserved names refused."""
    name = raw.strip()
    if not _USERNAME.match(name):
        raise HTTPException(400, "Use 3 to 16 letters, numbers, _ or . (start with a letter or number)")
    if name.lower().strip("._") in RESERVED:
        raise HTTPException(400, "That name is reserved, try another")
    return name


@router.get("/current")
async def current(response: Response, x_pitwall_user: Optional[str] = Header(None)):
    """The race open for picks, whether it's still open, the crowd's picks and the caller's own."""
    _require()
    user = _user(x_pitwall_user)
    race = await data.next_race()
    response.headers["Cache-Control"] = "private, max-age=10"
    username = await asyncio.to_thread(store.username, user) if user else None
    if race is None:
        return {"open": False, "race": None, "username": username}
    rnd = int(race["round"])
    season = await data.season()
    lock_at = data.start_of(race)
    mine = await asyncio.to_thread(store.pick, season, rnd, user) if user else None
    dist = await _distribution(season, rnd)
    return {
        "open": time.time() < lock_at,
        "race": {"round": rnd, "name": race["raceName"], "circuit": race["Circuit"]["circuitName"], "locks_at": int(lock_at)},
        "picks_total": dist["total"],
        "grid": await data.grid(),
        "distribution": dist["drivers"],
        "mine": mine,
        "username": username,
    }


@router.post("")
async def make_pick(p: PickIn, request: Request, x_pitwall_user: Optional[str] = Header(None)):
    _require()
    user = _user(x_pitwall_user, required=True)
    _guard(request, user, "pick", 10)
    if len(set(p.podium)) != 3:
        raise HTTPException(400, "Pick three different drivers")
    race = await data.race(p.round)
    if race is None:
        raise HTTPException(404, "No such round")
    if time.time() >= data.start_of(race):
        raise HTTPException(409, "Picks for this race are closed")
    grid = {g["driver_id"] for g in await data.grid()}
    if any(d not in grid for d in p.podium):
        raise HTTPException(400, "That driver isn't on the grid for this race")
    season = await data.season()
    await asyncio.to_thread(store.upsert_pick, season, p.round, user, p.podium)
    # The fan should see their own pick counted: let the crowd's split recount within ~2 s rather
    # than its usual 15 s (at most one recount per 2 s, however many picks arrive).
    hit = _dist_cache.get((season, p.round))
    if hit and time.time() - hit[0] > 2:
        _dist_cache.pop((season, p.round), None)
    return {"ok": True, "round": p.round, "podium": p.podium}


@router.get("/results/{rnd}")
async def results(rnd: int, response: Response, x_pitwall_user: Optional[str] = Header(None)):
    _require()
    if not 1 <= rnd <= 30:
        raise HTTPException(404, "No such round")
    user = _user(x_pitwall_user)
    season = await data.season()
    r = await asyncio.to_thread(store.result, season, rnd)
    mine = await asyncio.to_thread(store.pick, season, rnd, user) if user else None
    response.headers["Cache-Control"] = "private, max-age=30"
    return {
        "round": rnd,
        "scored": r is not None,
        "podium": r["podium"].split(",") if r else [],
        "race_name": r["race_name"] if r else None,
        "mine": mine,
    }


@router.get("/leaderboard")
async def leaderboard(response: Response, limit: int = 50, x_pitwall_user: Optional[str] = Header(None)):
    _require()
    user = _user(x_pitwall_user)
    # private: "me" is the caller's own line, so no shared cache may keep this response
    response.headers["Cache-Control"] = "private, max-age=30"
    season = await data.season()
    limit = max(1, min(limit, 100))
    hit = _board_cache.get((season, limit))
    if not hit or time.time() - hit[0] > 30:
        hit = (time.time(), await asyncio.to_thread(store.leaderboard, season, limit))
        _board_cache[(season, limit)] = hit
    me = None
    if user:
        me = await asyncio.to_thread(store.user_total, season, user)
        me["rank"] = await asyncio.to_thread(store.rank_of, season, user)
    # the user id stays on the server: each row only says whether it is the caller's
    top = [{k: v for k, v in row.items() if k != "user"} | {"you": row["user"] == user} for row in hit[1]]
    return {"top": top, "me": me}


@router.post("/username")
async def set_username(body: UsernameIn, request: Request, x_pitwall_user: Optional[str] = Header(None)):
    """Claims a username for this install. 409 when another player has it (any letter case)."""
    _require()
    user = _user(x_pitwall_user, required=True)
    name = check_username(body.username)            # before the limit: a typo shouldn't use up a try
    _guard(request, user, "name", 5)
    try:
        await asyncio.to_thread(store.set_username, user, name)
    except UsernameTaken:
        raise HTTPException(409, "That username is taken")
    _board_cache.clear()
    return {"ok": True, "username": name}


# ---------------------------------------------------------------- automatic scoring
async def score_finished_races(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Scores every round with picks whose race ended a while ago, and rescores for a few days after
    if the official podium changes (stewards' penalties)."""
    now = now or time.time()
    done = []
    season = await data.season()
    for rnd in await asyncio.to_thread(store.rounds_to_score, season):
        race = await data.race(rnd)
        if race is None:
            continue
        start = data.start_of(race)
        if now < start + RESULT_GRACE_S:
            continue
        stored = await asyncio.to_thread(store.result, season, rnd)
        if stored and now > start + RESCORE_WINDOW_S:
            continue                               # settled
        res = await data.result(rnd)
        if res is None:
            continue                               # official result not published yet; try next time
        if stored and stored["podium"] == ",".join(res["podium"]):
            continue                               # unchanged
        n = await asyncio.to_thread(store.save_result_and_score, season, rnd, res["podium"], res["race_name"])
        _dist_cache.clear(); _board_cache.clear()
        done.append({"round": rnd, "podium": res["podium"], "picks": n, "rescored": stored is not None})
        print(f"[predictions] {'rescored' if stored else 'scored'} round {rnd}: {res['podium']}, {n} picks", flush=True)
    return done


async def _scoring_loop():
    while True:
        try:
            await score_finished_races()
        except Exception as e:                     # keep polling; a Jolpica hiccup must not stop scoring
            print(f"[predictions] scoring pass failed: {e}", flush=True)
        await asyncio.sleep(RESULT_POLL_S)


_task: Optional[asyncio.Task] = None


def start():
    global store, _task
    if not ENABLED:
        return
    store = Store(DB_PATH, DATABASE_URL)
    _task = asyncio.get_event_loop().create_task(_scoring_loop())


async def stop():
    if _task:
        _task.cancel()
    if data.client:
        await data.client.aclose()
