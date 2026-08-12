"""Tiny FastAPI server that serves the cached reference JSON to the app.

  uvicorn pitchportal_ingest.server:app --host 0.0.0.0 --port 8000
  python -m pitchportal_ingest.server

Endpoints:
  GET  /cache/metrics.json    (app/src/data/leagueCache.ts)
  GET  /cache/arsenals.json   (app/src/data/leagueCache.ts)
  GET  /cache/feed.json       (app/src/data/feedCache.ts) - auto-refreshed
  POST /feed/refresh          force a re-pull now
  GET  /health
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import config, feed, pitching

log = logging.getLogger("pitchportal.feed")

# How often the community feed re-pulls from YouTube RSS. The app polls this
# server far more often than this, but it only ever reads the cache — so the
# refresh rate here is what actually decides how fresh the content is.
FEED_REFRESH_SECONDS = 15 * 60
# Savant updates daily in season; hourly keeps us current without hammering them.
PITCHING_REFRESH_SECONDS = 60 * 60
SAVANT_YEAR = config.SEASON


async def _feed_loop() -> None:
    """Re-pull the community feed forever, so nobody has to press refresh."""
    while True:
        try:
            # refresh() is blocking network IO; keep the event loop free.
            data = await asyncio.to_thread(feed.refresh)
            log.info(
                "feed refreshed: %d posts from %d/%d channels",
                len(data["posts"]),
                data["channelsOk"],
                data["channelsTotal"],
            )
        except Exception as exc:  # never let a bad pull kill the loop
            log.warning("feed refresh failed: %s", exc)
        await asyncio.sleep(FEED_REFRESH_SECONDS)


async def _pitching_loop() -> None:
    """Re-pull the Statcast pitch leaderboards so the Metrics tab stays live."""
    while True:
        try:
            data = await asyncio.to_thread(pitching.refresh, SAVANT_YEAR)
            log.info(
                "pitching refreshed: %d rows / %d pitchers",
                data["counts"]["rows"],
                data["counts"]["pitchers"],
            )
        except Exception as exc:
            log.warning("pitching refresh failed: %s", exc)
        await asyncio.sleep(PITCHING_REFRESH_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = [asyncio.create_task(_feed_loop()), asyncio.create_task(_pitching_loop())]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t


app = FastAPI(title="PitchPortal ingest cache", version="0.2.0", lifespan=lifespan)

# The app fetches from a dev machine / emulator, so allow any origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _load(name: str) -> dict:
    path = config.CACHE_DIR / name
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"{name} not generated yet — run `python -m pitchportal_ingest.pipeline`",
        )
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/")
def index() -> dict:
    return {
        "service": "pitchportal-ingest",
        "cache_dir": str(config.CACHE_DIR),
        "endpoints": [
            "/cache/metrics.json",
            "/cache/arsenals.json",
            "/cache/feed.json",
            "/cache/pitching.json",
            "/feed/refresh",
            "/health",
        ],
    }


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "metrics": (config.CACHE_DIR / "metrics.json").exists(),
        "arsenals": (config.CACHE_DIR / "arsenals.json").exists(),
        "feed": (config.CACHE_DIR / "feed.json").exists(),
        "pitching": (config.CACHE_DIR / "pitching.json").exists(),
    }


@app.get("/cache/metrics.json")
def metrics() -> JSONResponse:
    return JSONResponse(_load("metrics.json"))


@app.get("/cache/arsenals.json")
def arsenals() -> JSONResponse:
    return JSONResponse(_load("arsenals.json"))


@app.get("/cache/feed.json")
def community_feed() -> JSONResponse:
    """Auto-pulled community feed. Refreshed in the background every
    FEED_REFRESH_SECONDS, so this is always warm — the app never waits on
    YouTube. Clients may poll this as often as they like."""
    return JSONResponse(_load("feed.json"))


@app.get("/cache/pitching.json")
def pitching_leaderboards() -> JSONResponse:
    """Statcast pitch-level leaderboards: IVB, VB, HB, velo, whiff, grades.
    Refreshed hourly in the background."""
    return JSONResponse(_load("pitching.json"))


@app.post("/pitching/refresh")
async def force_pitching_refresh() -> dict:
    data = await asyncio.to_thread(pitching.refresh, SAVANT_YEAR)
    return {"ok": True, **data["counts"], "generatedAt": data["generatedAt"]}


@app.post("/feed/refresh")
async def force_feed_refresh() -> dict:
    """Manual kick, for when you don't want to wait for the next tick."""
    data = await asyncio.to_thread(feed.refresh)
    return {
        "ok": True,
        "posts": len(data["posts"]),
        "channelsOk": data["channelsOk"],
        "generatedAt": data["generatedAt"],
    }


def run() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    run()
