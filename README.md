---
title: Pitwall LiveF1 Engine
emoji: 🏎️
colorFrom: red
colorTo: black
sdk: docker
app_port: 7860
---

# 🏎️ Pitwall LiveF1 Engine
High-performance live Formula 1 telemetry, timing, and race intelligence microservice built with **FastAPI** and **LiveF1**.

Powers real-time telemetry, live leaderboards, and session tracking for the **Pitwall Android App**.

## 🚀 Public Endpoints
- `GET /` — API overview and health
- `GET /health` — Health check
- `GET /privacy` — Public official Pitwall Privacy Policy page (HTML)
- `GET /api/v1/live/timing` — Live leaderboard, gaps, intervals, and tire stints
- `GET /api/v1/live/weather` — Live track and air temperature, rain probability
- `GET /api/v1/live/race_control` — Safety Car, Virtual Safety Car, and flag notices
- `GET /api/v1/calendar` — 2026 Grand Prix schedule and session timetable
- `GET /api/v1/standings/drivers` — 2026 Drivers Championship standings
- `GET /api/v1/standings/constructors` — 2026 Constructors Championship standings
