"""FastAPI application for the Stake promotion knowledge base.

Run with::

    python app.py

then open http://localhost:8000 in a browser.
"""

from __future__ import annotations

import html
import logging
import re
import threading
from datetime import date, datetime, timezone
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

# How long finished Stake.com promotions are kept before being purged.
SITE_RETENTION_DAYS = 30


def _is_finished(row: dict[str, Any], today: str) -> bool:
    """Whether a promotion has finished: explicitly flagged or end date passed."""
    if row.get("finished"):
        return True
    ends_at = row.get("ends_at") or ""
    return bool(ends_at) and ends_at < today


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
        _run_maintenance()
    except scraper.ScraperError as exc:
        error = str(exc)
        logger.error("Refresh failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        error = f"Unexpected error: {exc}"
        logger.exception("Refresh crashed")
    finally:
        refresh_state.finish(summary, error)


# Punctuation glued to the next word, e.g. "bonus.Wagering". The match requires
# a real word (>=2 word chars) or a closing bracket/quote before it and a letter
# after, so numbers and abbreviations like "1.5" or "e.g." are left untouched.
_GLUED_PUNCT = re.compile(r"(?<=[a-z0-9]{2})([.,!?;:])(?=[A-Za-z])")
_GLUED_PUNCT_BRACKET = re.compile(r"""(?<=[)\]"'])([.,!?;:])(?=[A-Za-z])""")
# A sentence boundary: ".", "!" or "?" after a word (or closing bracket/quote),
# before a capital letter or opening parenthesis.
_SENTENCE_BREAK = re.compile(r"(?<=[a-z0-9]{2}[.!?])[ \t]+(?=[A-Z(])")
_SENTENCE_BREAK_BRACKET = re.compile(r"""(?<=[)\]"'][.!?])[ \t]+(?=[A-Z(])""")


def format_promo_text(text: str) -> str:
    """Tidy scraped promotion text for comfortable reading.

    Adds the missing space after punctuation that is stuck to the next word and
    starts each sentence on its own line, while preserving existing paragraph
    breaks. Purely cosmetic and applied at display time, so it never alters the
    stored data or the search index.
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _GLUED_PUNCT.sub(r"\1 ", text)
    text = _GLUED_PUNCT_BRACKET.sub(r"\1 ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = _SENTENCE_BREAK.sub("\n", text)
    text = _SENTENCE_BREAK_BRACKET.sub("\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# Section headings recognised inside promotion content. "How To Enter" is the
# one we make stand out; the others are rendered as plain subheadings.
_HOWTO_HEADINGS = ("how to enter", "how to participate", "how to qualify", "how to claim")
_OTHER_HEADINGS = (
    "how it works", "prize", "prizes", "prize pool", "rewards", "reward",
    "eligibility", "eligible", "requirements", "wagering", "schedule",
    "duration", "important", "terms", "terms and conditions", "terms & conditions",
)


def _heading_kind(line: str) -> Optional[str]:
    """Classify a line as a 'howto' heading, a generic 'section' heading, or None."""
    stripped = line.strip()
    if not stripped or len(stripped) > 70:
        return None
    key = stripped.rstrip(":").strip().lower()
    if any(key == h or key.startswith(h) for h in _HOWTO_HEADINGS):
        return "howto"
    if any(key == h or key.startswith(h) for h in _OTHER_HEADINGS):
        return "section"
    # A short line that ends with a colon is treated as a generic subheading.
    if stripped.endswith(":") and len(stripped) <= 60:
        return "section"
    return None


# Matches an explicit list item: "1.", "2)", "- ", "• ", "* ".
_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-–—•*]\s+(.*)$")
_LEADING_MARKER_RE = re.compile(r"^\s*(?:\d+[.)]|[-–—•*])\s*")


def _split_heading(line: str) -> tuple[str, str]:
    """Split a heading line into (label, remainder) at the first colon.

    Handles content that shares the heading's line, e.g.
    "How To Enter: Opt in" -> ("How To Enter", "Opt in").
    """
    stripped = line.strip()
    label, sep, rest = stripped.partition(":")
    if sep:
        return label.strip(), rest.strip()
    return stripped, ""


def _list_item(line: str) -> tuple[Optional[str], str]:
    """Classify a line as an ordered ('ol') / unordered ('ul') list item, or not."""
    m = _NUMBERED_RE.match(line)
    if m:
        return "ol", m.group(1).strip()
    m = _BULLET_RE.match(line)
    if m:
        return "ul", m.group(1).strip()
    return None, ""


def _strip_marker(text: str) -> str:
    """Drop any leading "1." / "-" marker so it isn't duplicated by styling."""
    return _LEADING_MARKER_RE.sub("", text).strip()


def _render_body(lines: list[str]) -> str:
    """Render body lines, grouping runs of list items into styled lists."""
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        kind, _ = _list_item(lines[i])
        if kind:
            items: list[str] = []
            while i < n:
                k2, text2 = _list_item(lines[i])
                if k2 != kind:
                    break
                items.append(f"<li>{html.escape(text2)}</li>")
                i += 1
            css = "promo-steps" if kind == "ol" else "promo-list"
            out.append(f'<{kind} class="{css}">{"".join(items)}</{kind}>')
        else:
            out.append(f"<p>{html.escape(lines[i].strip())}</p>")
            i += 1
    return "".join(out)


def content_to_html(text: str) -> str:
    """Render formatted promotion content as Stake-styled, safe HTML.

    The 'How To Enter' section becomes a highlighted callout whose lines are laid
    out as numbered steps; other recognised headings become section titles; bullet
    and numbered runs become styled lists; everything else is paragraphs. All text
    is HTML-escaped before any markup is added, so scraped content cannot inject
    markup. Visual styling lives in ``static/styles.css`` (``.promo-*`` classes).
    """
    formatted = format_promo_text(text)
    if not formatted:
        return ""

    lines = formatted.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        kind = _heading_kind(line)
        if kind == "howto":
            label, rest = _split_heading(line)
            body: list[str] = [rest] if rest else []
            j = i + 1
            while j < n and _heading_kind(lines[j]) is None:
                if lines[j].strip():
                    body.append(lines[j].strip())
                j += 1
            steps = "".join(f"<li>{html.escape(_strip_marker(b))}</li>" for b in body if b)
            if steps:
                out.append(
                    '<div class="promo-howto">'
                    f'<p class="promo-howto-title">{html.escape(label)}</p>'
                    f'<ol class="promo-steps">{steps}</ol>'
                    "</div>"
                )
            else:
                out.append(f'<h3 class="promo-section">{html.escape(label)}</h3>')
            i = j
        elif kind == "section":
            label, rest = _split_heading(line)
            out.append(f'<h3 class="promo-section">{html.escape(label)}</h3>')
            run: list[str] = [rest] if rest else []
            i += 1
            while i < n and lines[i].strip() and _heading_kind(lines[i]) is None:
                run.append(lines[i])
                i += 1
            if run:
                out.append(_render_body(run))
        else:
            run = []
            while i < n and lines[i].strip() and _heading_kind(lines[i]) is None:
                run.append(lines[i])
                i += 1
            out.append(_render_body(run))
    return "".join(out)


def _preview(text: str, length: int = PREVIEW_LENGTH) -> str:
    text = " ".join(format_promo_text(text).split())
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0] + "…"


def _configured_categories() -> dict[str, set[str]]:
    """Categories that should exist per group, derived from the scraper config."""
    return {
        scraper.SOURCE_SITE: set(scraper.CATEGORY_URLS.keys()),
        scraper.SOURCE_FORUM: {scraper._forum_category(b) for b in scraper.FORUM_BOARD_URLS},
    }


def _run_maintenance() -> None:
    """Prune obsolete categories and purge long-finished site promotions."""
    database.prune_to(_configured_categories())
    database.purge_expired_site(SITE_RETENTION_DAYS)


@app.on_event("startup")
def _on_startup() -> None:
    database.init_db()
    # Drop promotions for categories no longer configured (e.g. 'community') and
    # remove site promotions that finished more than a month ago.
    _run_maintenance()
    logger.info("Application ready. %d promotion(s) in database.", database.count_promotions())


@app.get("/", response_class=HTMLResponse)
def homepage(request: Request) -> HTMLResponse:
    """Render the homepage with the search UI and per-group categories."""
    configured = _configured_categories()
    # Drive the dropdown from the configured categories (always present) unioned
    # with anything currently stored, so each group lists its categories even
    # before a scrape has populated the database.
    categories_by_group = {
        "site": sorted(configured["site"] | set(database.list_categories("site"))),
        "forum": sorted(configured["forum"] | set(database.list_categories("forum"))),
    }
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "categories_by_group": categories_by_group,
            "total": database.count_promotions(),
        },
    )


@app.get("/promotion/{promotion_id}", response_class=HTMLResponse)
def promotion_detail(request: Request, promotion_id: int) -> HTMLResponse:
    """Render the full detail page for a single promotion."""
    promotion = database.get_promotion(promotion_id)
    if promotion is None:
        raise HTTPException(status_code=404, detail="Promotion not found")
    # Render content as highlighted HTML (How To Enter stands out); format terms
    # for readability. Display-time only — stored data and the index are unchanged.
    content_html = content_to_html(promotion["content"])
    promotion["terms"] = format_promo_text(promotion["terms"])
    promotion["is_finished"] = _is_finished(promotion, date.today().isoformat())
    return templates.TemplateResponse(
        request,
        "promotion.html",
        {"promotion": promotion, "content_html": content_html},
    )


@app.get("/api/search")
def api_search(
    q: str = Query("", description="Keyword query"),
    category: Optional[str] = Query(None, description="Optional category filter"),
    source: Optional[str] = Query(None, description="Group filter: 'site' or 'forum'"),
    status: Optional[str] = Query(None, description="Status filter: 'active' or 'finished'"),
    limit: int = Query(100, ge=1, le=500),
) -> JSONResponse:
    """Keyword search across promotion title, body and terms."""
    category_filter = category or None
    source_filter = source if source in {"site", "forum"} else None
    status_filter = status if status in {"active", "finished"} else None
    today = date.today().isoformat()
    results = database.search(
        q.strip(),
        category=category_filter,
        source=source_filter,
        status=status_filter,
        today=today,
        limit=limit,
    )
    payload = [
        {
            "id": row["id"],
            "title": row["title"],
            "category": row["category"],
            "source": row["source"],
            "duration": row["duration"],
            "finished": _is_finished(row, today),
            "url": row["url"],
            "preview": _preview(row["content"]) or _preview(row["terms"]),
            "scraped_at": row["scraped_at"],
        }
        for row in results
    ]
    return JSONResponse(
        {
            "query": q,
            "category": category_filter,
            "source": source_filter,
            "status": status_filter,
            "count": len(payload),
            "results": payload,
        }
    )


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
