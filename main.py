from contextlib import asynccontextmanager
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from live_engine import engine

@asynccontextmanager
async def lifespan(app: FastAPI):
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
        "documentation": "/docs"
    }

@app.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    return {"status": "ok", "service": "pitwall-livef1"}

@app.get("/api/v1/live/timing")
async def get_live_timing(response: Response):
    response.headers["Cache-Control"] = "public, max-age=5"
    return await engine.get_live_timing()

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
