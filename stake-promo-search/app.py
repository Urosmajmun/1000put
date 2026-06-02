"""FastAPI application for the Stake promotion knowledge base.

Run with::

    python app.py

then open http://localhost:8000 in a browser.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import database
import scraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("stake_promo_search")

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Stake Promotion Search", version="1.0.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

PREVIEW_LENGTH = 220


class RefreshState:
    """Tracks the background refresh job so only one runs at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running: bool = False
        self.last_started: Optional[str] = None
        self.last_finished: Optional[str] = None
        self.last_summary: Optional[dict[str, Any]] = None
        self.last_error: Optional[str] = None

    def try_start(self) -> bool:
        """Return True and mark running if no job is active, else False."""
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.last_started = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.last_error = None
            return True

    def finish(self, summary: Optional[dict[str, Any]], error: Optional[str]) -> None:
        with self._lock:
            self.running = False
            self.last_finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.last_summary = summary
            self.last_error = error

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "last_started": self.last_started,
                "last_finished": self.last_finished,
                "last_summary": self.last_summary,
                "last_error": self.last_error,
            }


refresh_state = RefreshState()


def _run_refresh() -> None:
    """Background worker that performs a full scrape.

    Playwright's sync API must run outside the asyncio event loop, so this is
    executed in a plain thread rather than as an async task.
    """
    summary: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    try:
        summary = scraper.scrape_all()
    except scraper.ScraperError as exc:
        error = str(exc)
        logger.error("Refresh failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        error = f"Unexpected error: {exc}"
        logger.exception("Refresh crashed")
    finally:
        refresh_state.finish(summary, error)


def _preview(text: str, length: int = PREVIEW_LENGTH) -> str:
    text = " ".join((text or "").split())
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0] + "…"


@app.on_event("startup")
def _on_startup() -> None:
    database.init_db()
    logger.info("Application ready. %d promotion(s) in database.", database.count_promotions())


@app.get("/", response_class=HTMLResponse)
def homepage(request: Request) -> HTMLResponse:
    """Render the homepage with the search UI and current categories."""
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "categories": database.list_categories(),
            "total": database.count_promotions(),
        },
    )


@app.get("/promotion/{promotion_id}", response_class=HTMLResponse)
def promotion_detail(request: Request, promotion_id: int) -> HTMLResponse:
    """Render the full detail page for a single promotion."""
    promotion = database.get_promotion(promotion_id)
    if promotion is None:
        raise HTTPException(status_code=404, detail="Promotion not found")
    return templates.TemplateResponse(
        request,
        "promotion.html",
        {"promotion": promotion},
    )


@app.get("/api/search")
def api_search(
    q: str = Query("", description="Keyword query"),
    category: Optional[str] = Query(None, description="Optional category filter"),
    limit: int = Query(100, ge=1, le=500),
) -> JSONResponse:
    """Keyword search across promotion title, body and terms."""
    category_filter = category if category else None
    results = database.search(q.strip(), category=category_filter, limit=limit)
    payload = [
        {
            "id": row["id"],
            "title": row["title"],
            "category": row["category"],
            "url": row["url"],
            "preview": _preview(row["content"]) or _preview(row["terms"]),
            "scraped_at": row["scraped_at"],
        }
        for row in results
    ]
    return JSONResponse({"query": q, "category": category_filter, "count": len(payload), "results": payload})


@app.post("/api/refresh")
def api_refresh() -> JSONResponse:
    """Start a background refresh of the promotion data.

    Returns immediately. Poll ``GET /api/refresh/status`` to learn when the
    scrape has finished and how many promotions were collected.
    """
    if not refresh_state.try_start():
        return JSONResponse(
            {"status": "already_running", "detail": "A refresh is already in progress."},
            status_code=409,
        )
    thread = threading.Thread(target=_run_refresh, name="refresh-worker", daemon=True)
    thread.start()
    logger.info("Background refresh started.")
    return JSONResponse({"status": "started"})


@app.get("/api/refresh/status")
def api_refresh_status() -> JSONResponse:
    """Report the state of the most recent refresh job."""
    state = refresh_state.snapshot()
    state["total_in_db"] = database.count_promotions()
    return JSONResponse(state)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
