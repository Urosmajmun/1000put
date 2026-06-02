# Stake Promotion Search

A small, self-contained local app that lets customer-support agents search Stake
promotions from their own computer. It scrapes the public promotion pages,
stores them in a local SQLite database, and provides a fast keyword search with
a simple web interface.

- **Backend:** Python + FastAPI
- **Database:** SQLite with FTS5 full-text search
- **Frontend:** HTML + Tailwind (CDN) + vanilla JavaScript
- **Scraper:** Playwright (headless Chromium)

No Docker, Redis, Elasticsearch, PostgreSQL, Celery, React or build step required.

---

## How the data is collected (and why Playwright)

Before choosing an approach, the Stake promotion pages were analysed:

- `https://stake.com/promotions/category/casino`
- `https://stake.com/promotions/category/community`

Findings:

1. **The pages are a client-side JavaScript single-page application (SPA).**
   The initial HTML is an app shell — the actual promotion list and text are
   rendered in the browser after JavaScript runs. A plain HTML fetch therefore
   contains no promotion content.
2. **The site is served behind Cloudflare.** Plain HTTP clients
   (`requests` / `BeautifulSoup`) and the site's internal API receive
   **HTTP 403** and never see the content.

Approaches were considered in the required priority order:

| Option | Approach | Result |
| ------ | -------- | ------ |
| 1 | Direct public API calls | ❌ API is gated behind Cloudflare → 403 |
| 2 | `requests` + `BeautifulSoup` | ❌ 403, and content is JS-rendered anyway |
| 3 | **Playwright headless browser** | ✅ Renders the public pages like a normal browser |

**Conclusion:** Playwright is the simplest approach that actually works, because
the content only exists after JavaScript executes. The scraper performs **normal
browser automation against publicly visible pages only** — it does not attempt
to bypass any security control or access restriction. It loads each category
page, scrolls to load all promotion cards, then opens each promotion to capture
its title, body and terms.

> If Stake changes its markup, the scraper uses resilient, generic selectors
> (headings, main content containers, "terms"-labelled sections) and degrades
> gracefully rather than crashing.

### Cloudflare bot verification

Stake is fronted by Cloudflare, which may show a "Performing security
verification" interstitial. Classic *headless* browsers are blocked by this
check, so the scraper:

- **runs a real, visible browser by default** (headless is off), which passes
  Cloudflare's standard verification the same way your own browser does;
- **keeps a persistent browser profile** (`.pw-profile/`) so the clearance
  cookie is reused across pages and runs;
- **waits for the verification to clear on its own** before reading a page, and
  **never stores the interstitial text** as if it were a promotion.

It does **not** solve CAPTCHAs, forge tokens, or otherwise bypass the security
check — it simply lets a normal browser complete the normal verification.

You'll briefly see a Chromium window open during a refresh; that's expected.

**Environment variables (optional):**

| Variable | Default | Effect |
| -------- | ------- | ------ |
| `STAKE_HEADLESS` | `0` | Set to `1` to run headless (e.g. on a server). The bot check may then not clear. |
| `STAKE_SLOWMO_MS` | `0` | Milliseconds to slow each browser action; can help on slow connections. |

---

## Setup

You need **Python 3.10+**.

```bash
# 1. (optional but recommended) create a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. install Python dependencies
pip install -r requirements.txt

# 3. install the Chromium browser used by Playwright (one-time, ~100 MB)
playwright install chromium
```

> Step 3 downloads the browser Playwright drives. It only needs to be run once
> per machine. If you skip it, the app still starts and you can search, but the
> "Refresh data" button will tell you to run `playwright install chromium`.

---

## Run

```bash
python app.py
```

Then open:

```
http://localhost:8000
```

The first time, the database is empty. Click **Refresh data** to scrape the
Stake promotion pages. The scrape runs in the background (it can take a minute);
the page reports progress and shows results automatically when it finishes.

---

## Using the app

- **Search bar** — type any keyword and press *Search* (or Enter). Matches
  promotion titles, body text and terms & conditions.
- **Category filter** — narrow results to `casino` or `community`.
- **Result list** — each card shows the title, category and a short preview.
- **Click a result** — opens the full promotion: complete content, terms &
  conditions, the source URL, and when it was last updated.
- **Refresh data** — re-scrapes the promotion pages and updates the database.

---

## API endpoints

| Method & path | Description |
| ------------- | ----------- |
| `GET /` | Homepage (search UI) |
| `GET /promotion/{id}` | Full promotion details page |
| `GET /api/search?q=&category=&limit=` | JSON keyword search |
| `POST /api/refresh` | Start a background re-scrape |
| `GET /api/refresh/status` | Progress / result of the last refresh |

Example:

```bash
curl "http://localhost:8000/api/search?q=bonus&category=casino"
curl -X POST "http://localhost:8000/api/refresh"
curl "http://localhost:8000/api/refresh/status"
```

---

## Project structure

```
stake-promo-search/
├── app.py            # FastAPI app: routes, refresh orchestration
├── scraper.py        # Playwright scraper for the promotion pages
├── database.py       # SQLite + FTS5 storage and search
├── requirements.txt
├── README.md
├── templates/
│   ├── index.html    # Homepage / search UI
│   └── promotion.html# Promotion detail page
└── static/
    ├── app.js        # Front-end search & refresh logic
    └── styles.css    # Supplemental styles
```

The SQLite database (`promotions.db`) is created automatically next to the code
on first run.

---

## How storage & search work

- Each promotion is stored with: **title, URL, category, content, terms,
  scraped_at**.
- **Duplicates are prevented** by a `UNIQUE` constraint on the promotion URL;
  re-scraping updates existing rows in place.
- **Search uses SQLite FTS5** (with the `porter` stemmer) over title, content
  and terms, ranked by relevance. User input is converted to safe prefix terms,
  so partial words match and arbitrary input can never break the query.

---

## Troubleshooting

- **"Playwright is not installed" / browser launch error** — run
  `pip install -r requirements.txt` then `playwright install chromium`.
- **Refresh finds 0 promotions / "security verification" content** — Stake may
  be unreachable from your network (region blocking) or Cloudflare's bot check
  did not clear. Confirm the pages load in your own browser first, make sure you
  are **not** running with `STAKE_HEADLESS=1`, and try the refresh again (the
  saved profile in `.pw-profile/` makes later runs pass more easily).
- **Port 8000 already in use** — edit the port at the bottom of `app.py`.
