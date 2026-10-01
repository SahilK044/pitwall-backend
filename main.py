import gzip
import hmac
import html
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel
from live_engine import engine
from auth_manager import auth_manager
import predictions

PRIVACY_HTML_PATH = Path(__file__).parent / "privacy.html"

@asynccontextmanager
async def lifespan(app: FastAPI):
    engine.start()
    predictions.start()
    yield
    await predictions.stop()
    await engine.close()

app = FastAPI(
    title="Pitwall LiveF1 Engine",
    description="High-performance Formula 1 real-time telemetry, timing, and race intelligence microservice.",
    version="1.0.0",
    lifespan=lifespan
)

# Enable CORS for web and mobile clients
# Public, read-only data: any origin may read it, but without cookies or credentials
# ("*" with credentials makes Starlette echo every Origin back as allowed).
app.include_router(predictions.router)

# Everything else over 1 KB goes out gzipped (OkHttp asks for it and unpacks it on its own).
# The live feeds below arrive pre-compressed, which this middleware passes through untouched.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "HEAD"],
    allow_headers=["*"],
)

@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {
        "service": "Pitwall LiveF1 Engine",
        "status": "online",
        "version": "1.0.0",
        "documentation": "/docs",
        "privacy_policy": "/privacy"
    }

@app.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    return {"status": "ok", "service": "pitwall-livef1", "feed": engine.health()}

@app.api_route("/privacy", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def privacy_policy():
    if PRIVACY_HTML_PATH.exists():
        content = PRIVACY_HTML_PATH.read_text(encoding="utf-8")
        return HTMLResponse(
            content=content,
            status_code=200,
            headers={"Cache-Control": "public, max-age=3600"}
        )
    return HTMLResponse(content="<h1>Privacy Policy Not Found</h1>", status_code=404)

# Every poller shares one rendering per second, both as JSON and gzipped: during a race each app
# polls these every second or few, so building (and compressing) them per request would scale with
# the audience. The timing snapshot is ~58 KB of JSON and ~7 KB gzipped. no-store, so no HTTP cache
# serves a copy older than that.
_shared: dict = {}

async def _shared_json(request: Request, key: str, produce, ttl: float = 1.0, cache_control: str = "no-store") -> Response:
    now = time.monotonic()
    hit = _shared.get(key)
    if hit is None or now - hit[0] >= ttl:
        body = json.dumps(await produce(), separators=(",", ":")).encode("utf-8")
        hit = (now, body, gzip.compress(body, compresslevel=5))
        _shared[key] = hit
    headers = {"Cache-Control": cache_control, "Vary": "Accept-Encoding"}
    if "gzip" in request.headers.get("accept-encoding", ""):
        return Response(hit[2], media_type="application/json", headers={**headers, "Content-Encoding": "gzip"})
    return Response(hit[1], media_type="application/json", headers=headers)

@app.get("/api/v1/live/timing")
async def get_live_timing(request: Request):
    return await _shared_json(request, "timing", engine.get_live_timing)

@app.get("/api/v1/live/positions")
async def get_live_positions(request: Request):
    return await _shared_json(request, "positions", engine.get_live_positions, ttl=0.5)

@app.get("/api/v1/live/telemetry")
async def get_live_telemetry(request: Request):
    return await _shared_json(request, "telemetry", engine.get_live_telemetry, ttl=0.5)

@app.get("/api/v1/auth/status")
def get_auth_status():
    return auth_manager.public_status()

# Forcing a renewal uses the account's session with F1 TV, so it needs the admin key
# (PITWALL_ADMIN_KEY on the host, sent as X-Admin-Key). Without a configured key it is off.
# A plain def: FastAPI runs it on a worker thread, so the blocking renewal never stalls other requests.
def _require_admin(key: Optional[str]) -> None:
    """404 unless [key] matches PITWALL_ADMIN_KEY (and one is configured): these routes change the
    token every live feed runs on, so nobody else may reach them, or even learn that they exist."""
    admin_key = os.environ.get("PITWALL_ADMIN_KEY", "")
    if not admin_key or not key or not hmac.compare_digest(key.encode(), admin_key.encode()):
        raise HTTPException(status_code=404)

@app.post("/api/v1/auth/refresh")
def refresh_auth_token(x_admin_key: Optional[str] = Header(default=None)):
    _require_admin(x_admin_key)
    info = auth_manager.refresh()
    return {**auth_manager.public_status(), "refreshed": info.get("refreshed")}

class TokenPayload(BaseModel):
    token: str

# Setting the token needs the admin key too: the token's signature can't be checked here, so without
# the key anyone could swap in any token and cut the live feed off for every fan.
@app.post("/api/v1/auth/token")
async def update_auth_token(payload: TokenPayload, x_admin_key: Optional[str] = Header(default=None)):
    _require_admin(x_admin_key)
    token = payload.token.strip()
    info = auth_manager.set_token(token)
    if not info.get("valid"):
        return {"success": False, "error": "Invalid token"}
    engine.update_token(token)
    # Only the expiry: the token's claims name the account holder.
    return {"success": True, "token_info": auth_manager.public_status()}

# The browser sync popup: GET because it is opened as a page, so the admin key rides in the URL
# (?key=...). Without it the route is not there. Nothing from the token is echoed into the page.
@app.get("/auth/sync", response_class=HTMLResponse)
@app.get("/api/v1/auth/sync", response_class=HTMLResponse)
async def sync_token_via_browser(token: str, key: Optional[str] = None):
    _require_admin(key)
    info = auth_manager.set_token(token.strip())
    page = ("<html><head><title>Pitwall Sync</title><meta name='referrer' content='no-referrer'></head>"
            "<body style='font-family:system-ui,-apple-system,sans-serif;background:#0d1117;color:#f0f6fc;text-align:center;padding:50px;'>"
            "<div style='max-width:400px;margin:0 auto;background:#161b22;padding:30px;border-radius:12px;border:1px solid #30363d;'>{}</div>"
            "</body></html>")
    if not info.get("valid"):
        return HTMLResponse(page.format("<h2 style='color:#ff5252;margin-top:0;'>Sync failed</h2><p>That token isn't valid.</p>"),
                            status_code=400, headers={"Cache-Control": "no-store"})
    engine.update_token(token.strip())
    rem_h = int(info.get("remaining_seconds", 0) // 3600)
    body = (f"<h2 style='color:#3fb950;margin-top:0;'>Synced to Pitwall</h2>"
            f"<p style='font-size:14px;color:#8b949e;margin:4px 0;'><b>Valid for:</b> {rem_h} hours</p>"
            f"<p style='font-size:12px;color:#8b949e;margin:4px 0;'>Expires: {html.escape(str(info.get('exp_utc')))}</p>"
            f"<p style='color:#58a6ff;font-size:12px;margin-top:20px;'>Closing window automatically...</p>"
            f"<script>setTimeout(() => window.close(), 3000);</script>")
    return HTMLResponse(page.format(body), headers={"Cache-Control": "no-store"})

@app.get("/api/v1/live/weather")
async def get_live_weather(request: Request):
    return await _shared_json(request, "weather", engine.get_live_weather, ttl=5.0, cache_control="public, max-age=15")

@app.get("/api/v1/live/race_control")
async def get_race_control(request: Request):
    return await _shared_json(request, "race_control", engine.get_race_control, ttl=2.0, cache_control="public, max-age=5")

@app.get("/api/v1/calendar")
async def get_calendar(response: Response):
    response.headers["Cache-Control"] = "public, max-age=3600"
    return await engine.get_calendar()

@app.get("/api/v1/standings/drivers")
async def get_driver_standings(response: Response):
    response.headers["Cache-Control"] = "public, max-age=300"
    return await engine.get_driver_standings()

@app.get("/api/v1/standings/constructors")
async def get_constructor_standings(response: Response):
    response.headers["Cache-Control"] = "public, max-age=300"
    return await engine.get_constructor_standings()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=7860, reload=True)
