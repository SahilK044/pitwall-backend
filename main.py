from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from live_engine import engine
from auth_manager import auth_manager

PRIVACY_HTML_PATH = Path(__file__).parent / "privacy.html"

@asynccontextmanager
async def lifespan(app: FastAPI):
    engine.start()
    yield
    await engine.close()

app = FastAPI(
    title="Pitwall LiveF1 Engine",
    description="High-performance Formula 1 real-time telemetry, timing, and race intelligence microservice.",
    version="1.0.0",
    lifespan=lifespan
)

# Enable CORS for web and mobile clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
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

@app.get("/api/v1/live/timing")
async def get_live_timing(response: Response):
    response.headers["Cache-Control"] = "public, max-age=5"
    return await engine.get_live_timing()

@app.get("/api/v1/live/positions")
async def get_live_positions(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await engine.get_live_positions()

@app.get("/api/v1/live/telemetry")
async def get_live_telemetry(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await engine.get_live_telemetry()

@app.get("/api/v1/auth/status")
async def get_auth_status():
    token = auth_manager.get_token()
    return auth_manager.inspect_token(token)

@app.post("/api/v1/auth/refresh")
async def refresh_auth_token():
    return auth_manager.refresh()

class TokenPayload(BaseModel):
    token: str

@app.post("/api/v1/auth/token")
async def update_auth_token(payload: TokenPayload):
    token = payload.token.strip()
    info = auth_manager.set_token(token)
    if not info.get("valid"):
        return {"success": False, "error": info.get("error", "Invalid token")}
    engine.update_token(token)
    return {"success": True, "token_info": info}

@app.get("/auth/sync", response_class=HTMLResponse)
@app.get("/api/v1/auth/sync", response_class=HTMLResponse)
async def sync_token_via_browser(token: str):
    info = auth_manager.set_token(token.strip())
    if not info.get("valid"):
        return HTMLResponse(
            f"<html><body style='font-family:sans-serif;background:#1a1a1a;color:#ff5252;text-align:center;padding:40px;'>"
            f"<h2>❌ Sync Failed</h2><p>{info.get('error', 'Invalid token')}</p>"
            f"</body></html>",
            status_code=400
        )
    engine.update_token(token.strip())
    rem_h = info.get("remaining_seconds", 0) // 3600
    return HTMLResponse(
        f"<html><head><title>Pitwall Sync</title></head>"
        f"<body style='font-family:system-ui,-apple-system,sans-serif;background:#0d1117;color:#f0f6fc;text-align:center;padding:50px;'>"
        f"<div style='max-width:400px;margin:0 auto;background:#161b22;padding:30px;border-radius:12px;border:1px solid #30363d;box-shadow:0 8px 24px rgba(0,0,0,0.5);'>"
        f"<h2 style='color:#3fb950;margin-top:0;'>✅ Synced to Pitwall!</h2>"
        f"<p style='font-size:16px;margin:8px 0;'><b>Subscriber:</b> {info.get('first_name', '')} {info.get('last_name', '')}</p>"
        f"<p style='font-size:14px;color:#8b949e;margin:4px 0;'><b>Tier:</b> {info.get('subscribed_product', 'PRO')}</p>"
        f"<p style='font-size:14px;color:#8b949e;margin:4px 0;'><b>Valid for:</b> {rem_h} hours</p>"
        f"<p style='font-size:12px;color:#8b949e;margin:4px 0;'>Expires: {info.get('exp_utc')}</p>"
        f"<p style='color:#58a6ff;font-size:12px;margin-top:20px;'>Closing window automatically...</p>"
        f"</div>"
        f"<script>setTimeout(() => window.close(), 3000);</script>"
        f"</body></html>"
    )

@app.get("/api/v1/live/weather")
async def get_live_weather(response: Response):
    response.headers["Cache-Control"] = "public, max-age=15"
    return await engine.get_live_weather()

@app.get("/api/v1/live/race_control")
async def get_race_control(response: Response):
    response.headers["Cache-Control"] = "public, max-age=5"
    return await engine.get_race_control()

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
