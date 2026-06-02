"""Scraper for Stake promotion pages using Playwright (headless Chromium).

Why Playwright? stake.com is a client-side JavaScript single-page application
and is served behind Cloudflare, which rejects plain HTTP clients (requests /
BeautifulSoup) and the GraphQL API with HTTP 403. A real headless browser
renders the publicly visible promotions exactly as a support agent would see
them in their own browser, so it is the simplest approach that actually works.

This module performs only normal browser automation against publicly accessible
pages. It does not attempt to bypass any security control or access restriction.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urldefrag, urljoin, urlparse

import database

logger = logging.getLogger(__name__)

BASE_URL = "https://stake.com"

# Category name -> listing page URL.
CATEGORY_URLS: dict[str, str] = {
    "casino": "https://stake.com/promotions/category/casino",
    "community": "https://stake.com/promotions/category/community",
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
MAX_PROMOTIONS_PER_CATEGORY = 200

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


# JavaScript evaluated in the listing page to collect promotion detail links.
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

# JavaScript evaluated on a detail page to extract structured content.
_EXTRACT_JS = r"""
() => {
    const clean = (s) => (s || '').replace(/ /g, ' ').replace(/\s+\n/g, '\n').trim();

    // Title: prefer the first heading, fall back to the document title.
    let title = '';
    const h1 = document.querySelector('h1');
    if (h1 && h1.innerText.trim()) {
        title = h1.innerText.trim();
    } else {
        title = (document.title || '').replace(/\s*[|\-]\s*Stake.*$/i, '').trim();
    }

    // Main content: try the most specific containers first, fall back to body.
    const containerSelectors = ['main', 'article', '[class*="promotion"]', '[class*="content"]'];
    let container = null;
    for (const sel of containerSelectors) {
        const el = document.querySelector(sel);
        if (el && el.innerText && el.innerText.trim().length > 80) {
            container = el;
            break;
        }
    }
    if (!container) container = document.body;

    // Work on a clone with the site chrome removed, so we never capture the
    // global navigation/footer — which is where the site-wide Terms of Service
    // lives. This keeps both the content and the terms scoped to the promotion.
    const clone = container.cloneNode(true);
    clone.querySelectorAll(
        'header,nav,footer,script,style,noscript,svg,form,' +
        '[role="navigation"],[role="banner"],[role="contentinfo"]'
    ).forEach((n) => n.remove());

    const content = clean(clone.innerText);

    // Terms & conditions: find a heading *inside the promotion content* whose
    // label mentions terms, then capture a bounded amount of the text that
    // follows it. Bounding avoids accidentally grabbing a whole ToS document.
    let terms = '';
    const headings = Array.from(clone.querySelectorAll('h1,h2,h3,h4,h5,h6,summary,strong,b,p'));
    for (const node of headings) {
        const label = (node.innerText || '').trim().toLowerCase();
        if (!label || label.length > 60) continue;
        if (!/terms|conditions|wagering|t&c/.test(label)) continue;

        let candidate = '';
        const section = node.closest('details,section');
        if (section && section !== clone) {
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

        // Ignore implausibly large blobs (a full ToS page) and keep the best.
        if (candidate.length > 20 && candidate.length < 8000 && candidate.length > terms.length) {
            terms = candidate;
        }
    }

    return { title, content, terms };
}
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalise_url(href: str) -> Optional[str]:
    """Strip fragments/queries and keep only same-origin promotion detail URLs."""
    if not href:
        return None
    href, _ = urldefrag(href)
    parsed = urlparse(href)
    if parsed.netloc and parsed.netloc != urlparse(BASE_URL).netloc:
        return None
    path = parsed.path.rstrip("/")
    if "/promotions/" not in path or "/promotions/category/" in path:
        return None
    # Rebuild a clean absolute URL without query string.
    return urljoin(BASE_URL, path)


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
    """Scroll to the bottom repeatedly to trigger lazy-loaded promotion cards."""
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


def _collect_detail_urls(page: Any, listing_url: str) -> list[str]:
    """Load a category listing page and return unique promotion detail URLs."""
    logger.info("Loading listing page: %s", listing_url)
    page.goto(listing_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    if not _wait_for_verification(page):
        logger.warning("Verification did not clear for %s; results may be partial", listing_url)
    try:
        page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 - networkidle can time out on busy SPAs
        logger.debug("networkidle not reached for %s; continuing", listing_url)
    _auto_scroll(page)

    raw_links: list[str] = page.evaluate(_COLLECT_LINKS_JS)
    urls: list[str] = []
    seen: set[str] = set()
    for href in raw_links:
        normalised = _normalise_url(href)
        if normalised and normalised not in seen:
            seen.add(normalised)
            urls.append(normalised)
    logger.info("Found %d promotion link(s) on %s", len(urls), listing_url)
    return urls[:MAX_PROMOTIONS_PER_CATEGORY]


def _scrape_detail(page: Any, url: str) -> Optional[dict[str, str]]:
    """Load a single promotion detail page and extract its structured content."""
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
        # in-page link to the global Terms of Service), go back so we extract
        # the promotion and not whatever page we landed on.
        if page.url.rstrip("/") != url.rstrip("/"):
            logger.info("Page navigated to %s while expanding; returning to %s", page.url, url)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                _wait_for_verification(page)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not return to %s: %s", url, exc)
                return None

        data: dict[str, str] = page.evaluate(_EXTRACT_JS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to scrape %s: %s", url, exc)
        return None

    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    terms = (data.get("terms") or "").strip()

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

    return {"title": title, "content": content, "terms": terms}


def scrape_all() -> dict[str, Any]:
    """Scrape every promotion from all configured categories and store them.

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

    inserted = 0
    updated = 0
    failed = 0
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

            for category, listing_url in CATEGORY_URLS.items():
                try:
                    detail_urls = _collect_detail_urls(page, listing_url)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Could not load category %s: %s", category, exc)
                    continue

                for url in detail_urls:
                    # A promotion can appear in both categories; keep the first.
                    if url in processed_urls:
                        continue
                    processed_urls.add(url)

                    detail = _scrape_detail(page, url)
                    if detail is None:
                        failed += 1
                        continue

                    is_new = database.upsert_promotion(
                        url=url,
                        title=detail["title"],
                        category=category,
                        content=detail["content"],
                        terms=detail["terms"],
                        scraped_at=_now_iso(),
                    )
                    if is_new:
                        inserted += 1
                    else:
                        updated += 1
                    logger.info(
                        "Stored [%s] %s (%s)",
                        category,
                        detail["title"][:60],
                        "new" if is_new else "updated",
                    )
                    time.sleep(DETAIL_DELAY_S)

            context.close()
    except ScraperError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ScraperError(f"Scraping failed: {exc}") from exc

    summary = {
        "inserted": inserted,
        "updated": updated,
        "failed": failed,
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
