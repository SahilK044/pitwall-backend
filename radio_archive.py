"""
Transcripts for past sessions' team radio.

The live feed transcribes each clip as it arrives; clips from sessions that ended before the
server saw them (or before a restart) have none. The app sends a finished session's clip URLs
here: known transcripts come straight back (from memory, then the database), the rest are queued
on the same Whisper worker as live radio and come back on a later call. Each clip is transcribed
once, ever. Only F1's own live timing audio is accepted, so this can't be pointed at anything else.
"""
import logging
import re
import threading
import time
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import predictions
import radio_transcriber

logger = logging.getLogger("radio")
router = APIRouter(prefix="/api/v1/radio", tags=["radio"])

ALLOWED_PREFIX = "https://livetiming.formula1.com/static/"
MAX_URLS = 80
# "PIA_81_20261002_122628.mp3": the car number is the second part.
_NUMBER = re.compile(r"/[A-Z]{3}_(\d{1,2})_\d{8}_\d{6}\.mp3$")

_lock = threading.Lock()
_hits: Dict[str, List[float]] = {}


def _allow(ip: str, per_min: int = 30) -> bool:
    now = time.time()
    with _lock:
        recent = [t for t in _hits.get(ip, []) if now - t < 60]
        if len(recent) >= per_min:
            _hits[ip] = recent
            return False
        recent.append(now)
        _hits[ip] = recent
        if len(_hits) > 5000:
            _hits.clear()
        return True


class Driver(BaseModel):
    name: str = ""
    team: str = ""


class TranscriptsIn(BaseModel):
    urls: List[str]
    # Car number -> name and team, so Whisper is primed with the session's names.
    drivers: Dict[str, Driver] = {}


def _db_get(urls: List[str]) -> Dict[str, Optional[str]]:
    s = predictions.store
    if s is None or not urls:
        return {}
    marks = ",".join("?" for _ in urls)
    try:
        rows = s._all(f"SELECT url, text FROM radio_transcripts WHERE url IN ({marks})", tuple(urls))
    except Exception as e:  # the table not there yet, or the database briefly away
        logger.warning(f"Transcript lookup failed: {e}")
        return {}
    return {r["url"]: r["text"] for r in rows}


def _db_put(url: str, text: Optional[str]) -> None:
    s = predictions.store
    if s is None:
        return
    try:
        s._run(
            """INSERT INTO radio_transcripts (url, text, created_at) VALUES (?, ?, ?)
               ON CONFLICT (url) DO UPDATE SET text = excluded.text""",
            (url, text, time.time()),
        )
    except Exception as e:
        logger.warning(f"Transcript save failed: {e}")


def lookup(urls: List[str], drivers: Dict[str, Dict], transcriber) -> Dict:
    """Pure enough to test: what's known, what's still being transcribed."""
    known = _db_get(urls)
    out: Dict[str, Optional[str]] = {}
    pending: List[str] = []
    for url in urls:
        if url in known:
            out[url] = known[url]
            continue
        if transcriber.done(url):
            text = transcriber.get(url)
            _db_put(url, text)
            out[url] = text
            continue
        m = _NUMBER.search(url)
        transcriber.submit(url, radio_transcriber.build_prompt(drivers, m.group(1) if m else None))
        pending.append(url)
    return {"transcripts": out, "pending": pending}


@router.post("/transcripts")
async def transcripts(body: TranscriptsIn, request: Request):
    from live_engine import engine
    t = engine._transcriber
    if not t.enabled:
        raise HTTPException(503, "Transcription is off on this server")
    ip = request.client.host if request.client else "?"
    if not _allow(ip):
        raise HTTPException(429, "Too many requests, try again in a minute")
    urls = [u for u in dict.fromkeys(body.urls) if u.startswith(ALLOWED_PREFIX) and u.endswith(".mp3")][:MAX_URLS]
    drivers = {k: {"FullName": d.name, "TeamName": d.team} for k, d in body.drivers.items()}
    return lookup(urls, drivers, t)
