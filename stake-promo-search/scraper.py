"""Scraper for Stake promotions (the Stake.com site and the Stake Community
forum) using Playwright (Chromium).

Why Playwright? stake.com is a client-side JavaScript single-page application
served behind Cloudflare, which rejects plain HTTP clients (requests /
BeautifulSoup) and the GraphQL API with HTTP 403. A real browser renders the
publicly visible promotions exactly as a support agent would see them, so it is
the simplest approach that actually works. The same browser is reused to read
the forum boards.

Promotions are grouped by ``source``:

* ``site``  — promotions from https://stake.com/promotions/category/*
* ``forum`` — opening posts of topics on the Stake Community boards (replies and
  other members' comments are skipped)

This module performs only normal browser automation against publicly accessible
pages. It does not attempt to bypass any security control or access restriction.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urldefrag, urljoin, urlparse

import database

logger = logging.getLogger(__name__)

BASE_URL = "https://stake.com"
FORUM_BASE_URL = "https://stakecommunity.com"

# Source identifiers (the two top-level groups shown in the UI).
SOURCE_SITE = "site"
SOURCE_FORUM = "forum"

# Stake.com: category name -> listing page URL.
CATEGORY_URLS: dict[str, str] = {
    "casino": "https://stake.com/promotions/category/casino",
    "poker": "https://stake.com/promotions/category/poker",
    "esports": "https://stake.com/promotions/category/esports",
    "sports": "https://stake.com/promotions/category/sports",
}

# Stake Community forum boards to scrape.
FORUM_BOARD_URLS: list[str] = [
    "https://stakecommunity.com/board/138-casino/",
    "https://stakecommunity.com/board/406-limited-time/",
    "https://stakecommunity.com/board/217-exclusive-vip-promotions/",
    "https://stakecommunity.com/board/230-sportsbook/",
    "https://stakecommunity.com/board/403-monthly-promotions/",
    "https://stakecommunity.com/board/404-free-to-play/",
    "https://stakecommunity.com/board/405-limited-time/",
    "https://stakecommunity.com/board/232-community/",
    "https://stakecommunity.com/board/402-esports/",
    "https://stakecommunity.com/board/382-past-events/",
]

# Forum boards whose promotions are always finished (archives of past events).
FORUM_FINISHED_BOARDS: set[str] = {
    "https://stakecommunity.com/board/382-past-events/",
}

# A realistic, current desktop User-Agent so pages render normally.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Timing / safety limits.
PAGE_TIMEOUT_MS = 45_000
SCROLL_PASSES = 8
SCROLL_PAUSE_S = 1.2
DETAIL_DELAY_S = 0.8  # polite delay between detail-page visits
MAX_PROMOTIONS_PER_LISTING = 200

# Number of forum board pages to scrape per board (first page only by default).
try:
    MAX_FORUM_PAGES = max(1, int(os.environ.get("STAKE_FORUM_PAGES", "1")))
except ValueError:
    MAX_FORUM_PAGES = 1

# How long to wait for Cloudflare's "verifying you are human" interstitial to
# clear on its own. We do not solve or bypass the challenge — we simply let the
# real browser complete the standard verification, exactly as a person would.
CHALLENGE_TIMEOUT_S = 40

# Whether to run the browser headless. Cloudflare's bot check blocks classic
# headless Chromium, so we default to a real (visible) browser, which verifies
# normally. Set STAKE_HEADLESS=1 to force headless (e.g. on a server with a
# virtual display); challenges may then not clear.
HEADLESS = os.environ.get("STAKE_HEADLESS", "0").strip().lower() in {"1", "true", "yes"}

# Optional slow-motion (ms) between Playwright actions; can help on slow links.
try:
    SLOWMO_MS = int(os.environ.get("STAKE_SLOWMO_MS", "0"))
except ValueError:
    SLOWMO_MS = 0

# Persistent browser profile directory, so the Cloudflare clearance cookie is
# reused across pages and runs (normal browser cookie behaviour).
PROFILE_DIR = Path(__file__).resolve().parent / ".pw-profile"

# Text fragments that identify a Cloudflare / bot-verification interstitial
# rather than real promotion content.
_CHALLENGE_MARKERS = (
    "just a moment",
    "performing security verification",
    "verify you are human",
    "verifying you are human",
    "verifies you are not a bot",
    "security service to protect",
    "protect against malicious bots",
    "needs to review the security of your connection",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "cf-browser-verification",
    "challenge-platform",
    "attention required",
)


class ScraperError(RuntimeError):
    """Raised when scraping cannot proceed (e.g. Playwright not installed)."""


def _looks_like_challenge(*texts: str) -> bool:
    """Return True if any text looks like a bot-verification interstitial."""
    blob = " ".join(t.lower() for t in texts if t)
    return any(marker in blob for marker in _CHALLENGE_MARKERS)


# --------------------------------------------------------------------------- #
# JavaScript evaluated in the browser
# --------------------------------------------------------------------------- #

# Shared helpers injected into both extractors so the site and forum behave
# consistently (whitespace cleanup, chrome/leaderboard removal, duration, terms).
_JS_HELPERS = r"""
    const MONTHS = '(?:January|February|March|April|May|June|July|August|' +
        'September|October|November|December)';
    const DATE_RANGE = new RegExp(
        MONTHS + '\\s+\\d{1,2},\\s*\\d{4}\\s*(?:[\\u2010-\\u2015\\-]|to)\\s*' +
        MONTHS + '\\s+\\d{1,2},\\s*\\d{4}', 'i'
    );

    const clean = (s) => (s || '')
        .replace(/ /g, ' ')
        .replace(/[ \t]+\n/g, '\n')
        .replace(/\n{3,}/g, '\n\n')
        .trim();

    // Strip site chrome and leaderboard noise from a cloned subtree, in place.
    const stripNoise = (el) => {
        el.querySelectorAll(
            'header,nav,footer,script,style,noscript,svg,form,' +
            '[role="navigation"],[role="banner"],[role="contentinfo"]'
        ).forEach((n) => n.remove());

        // Leaderboards: drop long ranking tables and anything tagged as one.
        el.querySelectorAll('table').forEach((t) => {
            if (t.querySelectorAll('tr').length >= 6) t.remove();
        });
        el.querySelectorAll(
            '[class*="leaderboard" i],[class*="ranking" i],' +
            '[id*="leaderboard" i],[id*="ranking" i]'
        ).forEach((n) => n.remove());
        // ...and sections introduced by a leaderboard-style heading.
        el.querySelectorAll('h1,h2,h3,h4,h5,h6,strong,b,summary,p').forEach((h) => {
            const t = (h.innerText || '').trim().toLowerCase();
            if (t && t.length < 40 &&
                /(leaderboard|rankings?|top (players|wagerers)|live ranking)/.test(t)) {
                const sec = h.closest('section,div');
                if (sec && sec !== el) sec.remove(); else h.remove();
            }
        });
    };

    const extractDuration = (text) => {
        const m = DATE_RANGE.exec(text || '');
        if (!m) return '';
        return m[0]
            .replace(/\s*(?:[‐-―]|-|\bto\b)\s*/i, ' - ')
            .replace(/\s+/g, ' ')
            .trim();
    };

    const extractTerms = (root) => {
        let terms = '';
        const headings = Array.from(root.querySelectorAll('h1,h2,h3,h4,h5,h6,summary,strong,b,p'));
        for (const node of headings) {
            const label = (node.innerText || '').trim().toLowerCase();
            if (!label || label.length > 60) continue;
            if (!/terms|conditions|wagering|t&c/.test(label)) continue;
            let candidate = '';
            const section = node.closest('details,section');
            if (section && section !== root) {
                candidate = section.innerText || '';
            } else {
                const parts = [];
                let sib = node.nextElementSibling;
                let steps = 0;
                while (sib && steps < 8) {
                    parts.push(sib.innerText || '');
                    sib = sib.nextElementSibling;
                    steps += 1;
                }
                candidate = parts.join('\n');
            }
            candidate = clean(candidate);
            if (candidate.length > 20 && candidate.length < 8000 && candidate.length > terms.length) {
                terms = candidate;
            }
        }
        return terms;
    };
"""

# Collect promotion detail links from a Stake.com category listing page.
_COLLECT_LINKS_JS = r"""
() => {
    const out = new Set();
    for (const a of document.querySelectorAll('a[href]')) {
        const href = a.href || '';
        // Keep promotion detail links, drop the category index links themselves.
        if (/\/promotions\//.test(href) && !/\/promotions\/category\//.test(href)) {
            out.add(href);
        }
    }
    return Array.from(out);
}
"""

# Collect topic (promotion) links from a stakecommunity.com forum board.
_COLLECT_FORUM_LINKS_JS = r"""
() => {
    const out = new Set();
    for (const a of document.querySelectorAll('a[href]')) {
        const href = a.href || '';
        if (/\/topic\//.test(href)) out.add(href);
    }
    return Array.from(out);
}
"""

# Extract a single Stake.com promotion's structured content.
_EXTRACT_JS = r"""
() => {
""" + _JS_HELPERS + r"""
    let title = '';
    const h1 = document.querySelector('h1');
    if (h1 && h1.innerText.trim()) {
        title = h1.innerText.trim();
    } else {
        title = (document.title || '').replace(/\s*[|\-–]\s*Stake.*$/i, '').trim();
    }

    const containerSelectors = ['main', 'article', '[class*="promotion"]', '[class*="content"]'];
    let container = null;
    for (const sel of containerSelectors) {
        const el = document.querySelector(sel);
        if (el && el.innerText && el.innerText.trim().length > 80) { container = el; break; }
    }
    if (!container) container = document.body;

    // Capture the duration before stripping, scanning the container then body.
    let duration = extractDuration(container.innerText || '');
    if (!duration) duration = extractDuration(document.body.innerText || '');

    const clone = container.cloneNode(true);
    stripNoise(clone);
    const content = clean(clone.innerText);
    const terms = extractTerms(clone);

    return { title, content, terms, duration };
}
"""

# Extract a single forum promotion (the opening post only, no comments).
_EXTRACT_FORUM_JS = r"""
() => {
""" + _JS_HELPERS + r"""
    let title = '';
    const h1 = document.querySelector('h1');
    if (h1 && h1.innerText.trim()) {
        title = h1.innerText.trim();
    } else {
        title = (document.title || '').replace(/\s*[|\-–]\s*Stake\s*Community.*$/i, '').trim();
    }

    // The opening post is the promotion; later posts are comments we skip.
    let post = document.querySelector('[data-role="commentContent"]')
        || document.querySelector('.cPost_contentWrap')
        || document.querySelector('article [data-role="commentContent"]')
        || document.querySelector('article')
        || document.querySelector('main')
        || document.body;

    let duration = extractDuration(post.innerText || '');
    if (!duration) duration = extractDuration(document.body.innerText || '');

    const clone = post.cloneNode(true);
    // Drop quoted posts, signatures and editor chrome so we keep only the
    // original promotion text, not replies or other members' content.
    clone.querySelectorAll(
        'blockquote,.ipsQuote,[data-role="signature"],.ipsComment_signature,' +
        '.ipsComment_controls,.cAuthorPane,.ipsItemControls'
    ).forEach((n) => n.remove());
    stripNoise(clone);

    const content = clean(clone.innerText);
    const terms = extractTerms(clone);

    return { title, content, terms, duration };
}
"""


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Matches a single "Month D, YYYY" date inside a duration string.
_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2}),\s*(\d{4})",
    re.IGNORECASE,
)
_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        start=1,
    )
}


def _parse_end_date(duration: str) -> str:
    """Return the promotion's end date as ISO ``YYYY-MM-DD``, or '' if unknown.

    The duration is normalised as "Month D, YYYY - Month D, YYYY"; the *last*
    date in the string is taken as the end date (a single date is used as-is).
    """
    if not duration:
        return ""
    matches = _DATE_RE.findall(duration)
    if not matches:
        return ""
    month_name, day, year = matches[-1]
    month = _MONTHS.get(month_name.lower())
    if not month:
        return ""
    try:
        return date(int(year), month, int(day)).isoformat()
    except ValueError:
        return ""


def _normalise_url(href: str) -> Optional[str]:
    """Keep only same-origin Stake.com promotion detail URLs, sans query/hash."""
    if not href:
        return None
    href, _ = urldefrag(href)
    parsed = urlparse(href)
    if parsed.netloc and parsed.netloc != urlparse(BASE_URL).netloc:
        return None
    path = parsed.path.rstrip("/")
    if "/promotions/" not in path or "/promotions/category/" in path:
        return None
    return urljoin(BASE_URL, path)


def _normalise_forum_url(href: str) -> Optional[str]:
    """Keep only Stake Community topic URLs, dropping pagination/query/hash."""
    if not href:
        return None
    href, _ = urldefrag(href)
    parsed = urlparse(href)
    if parsed.netloc and "stakecommunity.com" not in parsed.netloc:
        return None
    path = parsed.path
    if "/topic/" not in path:
        return None
    # Collapse any "/page/N" suffix so all pages of a topic map to one entry.
    path = re.sub(r"/page/\d+/?$", "", path).rstrip("/")
    return urljoin(FORUM_BASE_URL, path)


def _forum_category(board_url: str) -> str:
    """Derive a readable category from a board URL, e.g. '138-casino' -> 'casino'."""
    slug = urlparse(board_url).path.rstrip("/").split("/")[-1]
    match = re.match(r"^\d+-(.*)$", slug)
    return (match.group(1) if match else slug) or "forum"


def _board_page_url(board_url: str, page_number: int) -> str:
    """Return the URL for a given page of a forum board (page 1 is the board)."""
    if page_number <= 1:
        return board_url
    return board_url.rstrip("/") + f"/page/{page_number}/"


# --------------------------------------------------------------------------- #
# Page interaction
# --------------------------------------------------------------------------- #

def _expand_collapsibles(page: Any) -> None:
    """Open <details> elements and click any "terms"-like toggles so their
    text becomes visible before extraction. Failures are non-fatal."""
    try:
        page.evaluate(
            """
            () => {
                // Open native disclosure widgets.
                document.querySelectorAll('details').forEach(d => { d.open = true; });

                // Click in-page expanders ONLY. Never click links (<a>) — a
                // footer "Terms of Service" link would navigate away to the
                // site-wide ToS page. Also skip the site chrome (nav/header/footer).
                const inChrome = (el) => !!el.closest(
                    'nav,header,footer,[role="navigation"],[role="banner"],[role="contentinfo"]'
                );
                const toggles = Array.from(
                    document.querySelectorAll('summary,button,[role="button"],[aria-expanded]')
                );
                for (const t of toggles) {
                    if (t.tagName === 'A' || t.closest('a')) continue;
                    if (inChrome(t)) continue;
                    const label = (t.innerText || '').trim().toLowerCase();
                    if (label && label.length < 40 &&
                        /terms|conditions|wagering|show more|read more|details/.test(label)) {
                        try { t.click(); } catch (e) { /* ignore */ }
                    }
                }
            }
            """
        )
        page.wait_for_timeout(400)
    except Exception as exc:  # noqa: BLE001 - best-effort, never fatal
        logger.debug("Could not expand collapsibles: %s", exc)


def _auto_scroll(page: Any) -> None:
    """Scroll to the bottom repeatedly to trigger lazy-loaded cards."""
    previous_height = 0
    for _ in range(SCROLL_PASSES):
        try:
            height = page.evaluate("() => document.body.scrollHeight")
            page.evaluate("(h) => window.scrollTo(0, h)", height)
            page.wait_for_timeout(int(SCROLL_PAUSE_S * 1000))
            if height == previous_height:
                break
            previous_height = height
        except Exception as exc:  # noqa: BLE001
            logger.debug("Scroll pass failed: %s", exc)
            break


def _wait_for_verification(page: Any, timeout_s: int = CHALLENGE_TIMEOUT_S) -> bool:
    """If a bot-verification interstitial is showing, wait for it to clear.

    This does not solve or circumvent the challenge — it simply gives the real
    browser time to complete the standard verification (which issues a normal
    clearance cookie) before we read the page. Returns True once real content is
    visible, or False if the interstitial is still up when the timeout expires.
    """
    deadline = time.monotonic() + timeout_s
    warned = False
    while True:
        try:
            title = page.title()
        except Exception:  # noqa: BLE001
            title = ""
        try:
            body = page.evaluate(
                "() => document.body ? document.body.innerText.slice(0, 3000) : ''"
            )
        except Exception:  # noqa: BLE001
            body = ""

        if not _looks_like_challenge(title, body):
            return True
        if time.monotonic() >= deadline:
            return False
        if not warned:
            logger.info("Security verification detected; waiting for it to clear…")
            warned = True
        page.wait_for_timeout(2000)


def _collect_listing_urls(
    page: Any,
    listing_url: str,
    collect_js: str,
    normaliser: Callable[[str], Optional[str]],
) -> list[str]:
    """Load a listing/board page and return unique, normalised detail URLs."""
    logger.info("Loading listing page: %s", listing_url)
    page.goto(listing_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    if not _wait_for_verification(page):
        logger.warning("Verification did not clear for %s; results may be partial", listing_url)
    try:
        page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 - networkidle can time out on busy pages
        logger.debug("networkidle not reached for %s; continuing", listing_url)
    _auto_scroll(page)

    raw_links: list[str] = page.evaluate(collect_js)
    urls: list[str] = []
    seen: set[str] = set()
    for href in raw_links:
        normalised = normaliser(href)
        if normalised and normalised not in seen:
            seen.add(normalised)
            urls.append(normalised)
    logger.info("Found %d link(s) on %s", len(urls), listing_url)
    return urls[:MAX_PROMOTIONS_PER_LISTING]


def _scrape_detail(page: Any, url: str, extract_js: str) -> Optional[dict[str, str]]:
    """Load a single detail page and extract its structured content."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        if not _wait_for_verification(page):
            logger.warning("Security verification blocked %s; skipping", url)
            return None
        try:
            page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)
        except Exception:  # noqa: BLE001
            pass
        _expand_collapsibles(page)

        # Safety net: if expanding somehow navigated away (e.g. an unexpected
        # in-page link to a Terms of Service page), go back so we extract the
        # promotion and not whatever page we landed on.
        if page.url.rstrip("/") != url.rstrip("/"):
            logger.info("Page navigated to %s while expanding; returning to %s", page.url, url)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                _wait_for_verification(page)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not return to %s: %s", url, exc)
                return None

        data: dict[str, str] = page.evaluate(extract_js)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to scrape %s: %s", url, exc)
        return None

    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    terms = (data.get("terms") or "").strip()
    duration = (data.get("duration") or "").strip()

    # Never store an interstitial page as if it were promotion content.
    if _looks_like_challenge(title, content):
        logger.warning("Got a verification page instead of content for %s; skipping", url)
        return None

    if not title and not content:
        logger.warning("No usable content extracted from %s", url)
        return None

    if not title:
        # Derive a readable title from the URL slug as a last resort.
        slug = urlparse(url).path.rstrip("/").split("/")[-1]
        title = slug.replace("-", " ").title() or url

    return {"title": title, "content": content, "terms": terms, "duration": duration}


def _process_listing(
    page: Any,
    *,
    source: str,
    category: str,
    listing_url: str,
    collect_js: str,
    normaliser: Callable[[str], Optional[str]],
    extract_js: str,
    processed: set[str],
    counts: dict[str, int],
    finished: bool = False,
) -> None:
    """Collect and store every promotion linked from one listing/board page.

    ``finished`` marks the whole listing as finished (used for the forum's past
    events archive). Site promotions are not flagged here — their finished state
    is derived from the parsed end date at query time.
    """
    try:
        urls = _collect_listing_urls(page, listing_url, collect_js, normaliser)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not load %s listing %s: %s", source, listing_url, exc)
        return

    for url in urls:
        if url in processed:
            continue
        processed.add(url)

        detail = _scrape_detail(page, url, extract_js)
        if detail is None:
            counts["failed"] += 1
            continue

        is_new = database.upsert_promotion(
            url=url,
            title=detail["title"],
            category=category,
            source=source,
            duration=detail["duration"],
            ends_at=_parse_end_date(detail["duration"]),
            finished=finished,
            content=detail["content"],
            terms=detail["terms"],
            scraped_at=_now_iso(),
        )
        counts["inserted" if is_new else "updated"] += 1
        logger.info(
            "Stored [%s/%s] %s (%s)",
            source,
            category,
            detail["title"][:60],
            "new" if is_new else "updated",
        )
        time.sleep(DETAIL_DELAY_S)


def scrape_all() -> dict[str, Any]:
    """Scrape every promotion from the Stake site and the forum, and store them.

    Returns a summary dict with counts. Raises :class:`ScraperError` when the
    Playwright browser is unavailable so the caller can surface install help.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ScraperError(
            "Playwright is not installed. Run:\n"
            "    pip install -r requirements.txt\n"
            "    playwright install chromium"
        ) from exc

    database.init_db()

    counts = {"inserted": 0, "updated": 0, "failed": 0}
    processed_urls: set[str] = set()
    started = time.monotonic()

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as p:
            try:
                # A persistent context behaves like a normal browser profile: it
                # keeps cookies (including Cloudflare's clearance cookie) across
                # pages and runs. Running non-headless by default lets the site's
                # standard bot verification complete the way it would for a human.
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE_DIR),
                    headless=HEADLESS,
                    slow_mo=SLOWMO_MS,
                    user_agent=USER_AGENT,
                    viewport={"width": 1366, "height": 900},
                    locale="en-US",
                    timezone_id="UTC",
                    args=["--disable-blink-features=AutomationControlled"],
                )
            except Exception as exc:  # noqa: BLE001
                raise ScraperError(
                    "Could not launch Chromium. Install the browser binary with:\n"
                    "    playwright install chromium\n"
                    "If you are on a server without a screen, also set "
                    "STAKE_HEADLESS=1 (note: the bot check may then not clear).\n"
                    f"Original error: {exc}"
                ) from exc

            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)

            # Group 1: the Stake.com site.
            for category, listing_url in CATEGORY_URLS.items():
                _process_listing(
                    page,
                    source=SOURCE_SITE,
                    category=category,
                    listing_url=listing_url,
                    collect_js=_COLLECT_LINKS_JS,
                    normaliser=_normalise_url,
                    extract_js=_EXTRACT_JS,
                    processed=processed_urls,
                    counts=counts,
                )

            # Group 2: the Stake Community forum.
            for board_url in FORUM_BOARD_URLS:
                category = _forum_category(board_url)
                board_finished = board_url in FORUM_FINISHED_BOARDS
                for page_number in range(1, MAX_FORUM_PAGES + 1):
                    _process_listing(
                        page,
                        source=SOURCE_FORUM,
                        category=category,
                        listing_url=_board_page_url(board_url, page_number),
                        collect_js=_COLLECT_FORUM_LINKS_JS,
                        normaliser=_normalise_forum_url,
                        extract_js=_EXTRACT_FORUM_JS,
                        processed=processed_urls,
                        counts=counts,
                        finished=board_finished,
                    )

            context.close()
    except ScraperError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ScraperError(f"Scraping failed: {exc}") from exc

    summary = {
        "inserted": counts["inserted"],
        "updated": counts["updated"],
        "failed": counts["failed"],
        "total_in_db": database.count_promotions(),
        "elapsed_seconds": round(time.monotonic() - started, 1),
    }
    logger.info("Scrape complete: %s", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    print(scrape_all())
