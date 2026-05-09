from __future__ import annotations
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .routes import router, set_error, set_graph

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

_FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


async def _run_analysis() -> None:
    from core.analyzer import analyze_repo
    path = os.getenv("XNAV_PROJECT_PATH", ".")
    logger.info("Starting analysis of %s", path)
    try:
        graph, graph_json = await analyze_repo(path)
        set_graph(graph, graph_json)
        logger.info("Analysis complete: %d units", graph.stats.total_units)
    except Exception as e:
        logger.error("Analysis failed: %s", e)
        set_error(str(e))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Kick off analysis as a background task so the server starts immediately.
    # The frontend polls /api/graph (503 until ready, then 200).
    asyncio.create_task(_run_analysis())
    yield


app = FastAPI(title="Xnav", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

if _FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")
else:
    logger.warning("Frontend directory not found at %s", _FRONTEND_DIR)
