from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from live_engine import engine

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
