"""SQLite persistence layer with FTS5 full-text search for Stake promotions.

The schema uses an external-content FTS5 virtual table kept in sync with the
``promotions`` table through triggers, so searches stay fast and the canonical
data lives in a single place. Duplicates are prevented by a UNIQUE constraint
on the promotion URL.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Database file lives next to this module so the app is fully self-contained.
DB_PATH: Path = Path(__file__).resolve().parent / "promotions.db"

# The canonical table. Uniqueness is on (url, content_hash) rather than url
# alone, so a promotion that is renewed at the same URL with different text is
# kept as a separate record while an unchanged re-scrape is deduplicated.
_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS promotions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT    NOT NULL,
    content_hash TEXT    NOT NULL DEFAULT '',
    title        TEXT    NOT NULL,
    category     TEXT    NOT NULL,
    source       TEXT    NOT NULL DEFAULT 'site',
    duration     TEXT    NOT NULL DEFAULT '',
    ends_at      TEXT    NOT NULL DEFAULT '',
    finished     INTEGER NOT NULL DEFAULT 0,
    archived_at  TEXT    NOT NULL DEFAULT '',
    content      TEXT    NOT NULL DEFAULT '',
    terms        TEXT    NOT NULL DEFAULT '',
    scraped_at   TEXT    NOT NULL,
    UNIQUE(url, content_hash)
);
"""

_FTS_TRIGGERS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS promotions_fts USING fts5(
    title,
    content,
    terms,
    content='promotions',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Keep the FTS index in sync with the canonical table.
CREATE TRIGGER IF NOT EXISTS promotions_ai AFTER INSERT ON promotions BEGIN
    INSERT INTO promotions_fts(rowid, title, content, terms)
    VALUES (new.id, new.title, new.content, new.terms);
END;

CREATE TRIGGER IF NOT EXISTS promotions_ad AFTER DELETE ON promotions BEGIN
    INSERT INTO promotions_fts(promotions_fts, rowid, title, content, terms)
    VALUES ('delete', old.id, old.title, old.content, old.terms);
END;

CREATE TRIGGER IF NOT EXISTS promotions_au AFTER UPDATE ON promotions BEGIN
    INSERT INTO promotions_fts(promotions_fts, rowid, title, content, terms)
    VALUES ('delete', old.id, old.title, old.content, old.terms);
    INSERT INTO promotions_fts(rowid, title, content, terms)
    VALUES (new.id, new.title, new.content, new.terms);
END;
"""


def _connect() -> sqlite3.Connection:
    """Open a new connection with sensible defaults.

    A fresh connection per call keeps the layer thread-safe, which matters
    because the scraper runs in a background thread while requests are served.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    """Create / migrate the table, then (re)create the FTS index and triggers."""
    conn = _connect()
    try:
        conn.executescript(_TABLE_DDL)
        _migrate(conn)
        conn.executescript(_FTS_TRIGGERS_DDL)
        _rebuild_fts(conn)
        conn.commit()
        logger.info("Database initialised at %s", DB_PATH)
    finally:
        conn.close()


def _content_hash(title: str, content: str, terms: str) -> str:
    """A stable hash of a promotion's text, ignoring incidental whitespace.

    Two scrapes of the same promotion produce the same hash; a renewed promotion
    with even slightly different wording produces a different one.
    """
    norm = lambda s: " ".join((s or "").split())  # noqa: E731
    blob = "\x01".join([norm(title), norm(content), norm(terms)])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _rebuild_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the full-text index from the canonical table on startup.

    The sync triggers only index rows written *after* the FTS table exists, so a
    database that predates full-text search would return no matches for its
    existing rows. Rebuilding once at startup guarantees the index always matches
    the data; it is idempotent and fast at this scale. (Row counts cannot be used
    to detect drift, since COUNT(*) on an external-content FTS table reflects the
    base table rather than the index itself.)
    """
    if conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0]:
        conn.execute("INSERT INTO promotions_fts(promotions_fts) VALUES ('rebuild')")


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade older databases in place.

    The schema changed from a single row per URL (``url`` UNIQUE) to a row per
    distinct content version (``UNIQUE(url, content_hash)``). SQLite cannot drop
    a UNIQUE constraint with ALTER, so when the ``content_hash`` column is absent
    we rebuild the table: copy every existing row into the new shape (computing
    its content hash) and recreate the FTS index and triggers afterwards.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(promotions)")}
    if "content_hash" in existing:
        return  # already on the current schema

    logger.info("Migrating database to versioned schema (rebuilding table)…")
    old_rows = [dict(r) for r in conn.execute("SELECT * FROM promotions").fetchall()]

    conn.executescript(
        """
        DROP TRIGGER IF EXISTS promotions_ai;
        DROP TRIGGER IF EXISTS promotions_ad;
        DROP TRIGGER IF EXISTS promotions_au;
        DROP TABLE IF EXISTS promotions_fts;
        ALTER TABLE promotions RENAME TO promotions_old;
        """
    )
    conn.executescript(_TABLE_DDL)

    for row in old_rows:
        title = row.get("title") or ""
        content = row.get("content") or ""
        terms = row.get("terms") or ""
        conn.execute(
            """
            INSERT OR IGNORE INTO promotions
                (url, content_hash, title, category, source, duration, ends_at,
                 finished, archived_at, content, terms, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
            """,
            (
                row.get("url") or "",
                _content_hash(title, content, terms),
                title,
                row.get("category") or "",
                row.get("source") or "site",
                row.get("duration") or "",
                row.get("ends_at") or "",
                int(row.get("finished") or 0),
                content,
                terms,
                row.get("scraped_at") or "",
            ),
        )

    conn.execute("DROP TABLE promotions_old")
    logger.info("Migration complete: %d row(s) carried over", len(old_rows))


def store_promotion(
    *,
    url: str,
    title: str,
    category: str,
    source: str,
    duration: str,
    content: str,
    terms: str,
    scraped_at: str,
    ends_at: str = "",
    finished: bool = False,
    keep_history: bool = False,
    today: Optional[str] = None,
) -> str:
    """Store a scraped promotion, versioning it when its text has changed.

    Behaviour depends on whether an identical record (same ``url`` *and* text)
    already exists:

    * **Unchanged** — the same URL and text are already stored: only the
      bookkeeping fields are refreshed, so nothing is duplicated.
    * **Changed text at a known URL** — a renewed promotion. With
      ``keep_history`` (Stake site) the previous version(s) for that URL are
      archived (marked finished, dated today) and the new text is inserted as the
      current record. Without it (forum) the old rows are replaced.
    * **New URL** — inserted as-is.

    Returns one of ``"unchanged"``, ``"versioned"``, ``"updated"`` or
    ``"inserted"``.
    """
    content_hash = _content_hash(title, content, terms)
    today = today or date.today().isoformat()
    finished_int = 1 if finished else 0
    conn = _connect()
    try:
        # 1) Exact same text already stored for this URL -> just refresh fields.
        existing = conn.execute(
            "SELECT id FROM promotions WHERE url = ? AND content_hash = ?",
            (url, content_hash),
        ).fetchone()
        if existing is not None:
            conn.execute(
                """
                UPDATE promotions
                SET category = ?, source = ?, duration = ?, ends_at = ?,
                    finished = ?, scraped_at = ?
                WHERE id = ?
                """,
                (category, source, duration, ends_at, finished_int, scraped_at, existing["id"]),
            )
            conn.commit()
            return "unchanged"

        url_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM promotions WHERE url = ?", (url,)
        ).fetchone()["n"]

        if keep_history:
            if url_rows:
                # Archive the prior version(s) of this promotion.
                conn.execute(
                    """
                    UPDATE promotions
                    SET finished = 1, archived_at = ?
                    WHERE url = ? AND content_hash <> ? AND archived_at = ''
                    """,
                    (today, url, content_hash),
                )
                status = "versioned"
            else:
                status = "inserted"
        else:
            # No history kept (forum): replace any existing rows for this URL.
            if url_rows:
                conn.execute("DELETE FROM promotions WHERE url = ?", (url,))
                status = "updated"
            else:
                status = "inserted"

        conn.execute(
            """
            INSERT INTO promotions
                (url, content_hash, title, category, source, duration, ends_at,
                 finished, archived_at, content, terms, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
            """,
            (
                url, content_hash, title, category, source, duration, ends_at,
                finished_int, content, terms, scraped_at,
            ),
        )
        conn.commit()
        return status
    finally:
        conn.close()


def _build_match_query(query: str) -> Optional[str]:
    """Turn a free-text query into a safe FTS5 MATCH expression.

    Each alphanumeric token becomes a quoted prefix term (``"foo"*``) and the
    tokens are AND-ed together implicitly. Quoting neutralises FTS5 operators so
    arbitrary user input can never produce a syntax error.
    """
    tokens = re.findall(r"[0-9A-Za-z]+", query)
    if not tokens:
        return None
    return " ".join(f'"{token}"*' for token in tokens)


_COLUMNS = (
    "id, url, title, category, source, duration, ends_at, finished, "
    "archived_at, content, terms, scraped_at"
)


def _finished_expr(prefix: str) -> str:
    """SQL expression that is true when a promotion has finished.

    A promotion is finished if it is explicitly flagged (e.g. forum past events)
    or its end date is strictly before today. ``prefix`` is the table alias
    (``"p."`` for the FTS join, ``""`` for the plain table).
    """
    return f"({prefix}finished = 1 OR ({prefix}ends_at <> '' AND {prefix}ends_at < ?))"


def search(
    query: str,
    category: Optional[str] = None,
    source: Optional[str] = None,
    status: Optional[str] = None,
    today: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Search promotions by title/content/terms, optionally filtered.

    Results can be narrowed by ``category``, ``source`` (the top-level group,
    ``site`` or ``forum``) and ``status`` (``active``/``finished``; anything else
    returns both). ``today`` is the ISO date used to evaluate finished state and
    defaults to the current date. With a query the results are ranked by FTS5
    relevance; with an empty query every matching promotion is returned
    alphabetically, so the homepage can show the full catalogue out of the box.
    """
    match = _build_match_query(query) if query else None
    today = today or date.today().isoformat()
    conn = _connect()
    try:
        if match:
            prefix = "p."
            where = ["promotions_fts MATCH ?"]
            params: list[Any] = [match]
            if category:
                where.append(f"{prefix}category = ?")
                params.append(category)
            if source:
                where.append(f"{prefix}source = ?")
                params.append(source)
            if status == "finished":
                where.append(_finished_expr(prefix))
                params.append(today)
            elif status == "active":
                where.append(f"NOT {_finished_expr(prefix)}")
                params.append(today)
            params.append(limit)
            columns = ", ".join(f"{prefix}{col}" for col in _COLUMNS.split(", "))
            sql = f"""
                SELECT {columns}
                FROM promotions_fts f
                JOIN promotions p ON p.id = f.rowid
                WHERE {" AND ".join(where)}
                ORDER BY rank
                LIMIT ?
            """
        else:
            conditions: list[str] = []
            params = []
            if category:
                conditions.append("category = ?")
                params.append(category)
            if source:
                conditions.append("source = ?")
                params.append(source)
            if status == "finished":
                conditions.append(_finished_expr(""))
                params.append(today)
            elif status == "active":
                conditions.append(f"NOT {_finished_expr('')}")
                params.append(today)
            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            params.append(limit)
            sql = f"""
                SELECT {_COLUMNS}
                FROM promotions
                {where}
                ORDER BY title COLLATE NOCASE
                LIMIT ?
            """
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError as exc:
        # A malformed MATCH should never reach here thanks to _build_match_query,
        # but guard anyway so the API degrades gracefully instead of 500-ing.
        logger.warning("Search failed for query %r: %s", query, exc)
        return []
    finally:
        conn.close()


def get_promotion(promotion_id: int) -> Optional[dict[str, Any]]:
    """Return a single promotion by id, or ``None`` if it does not exist."""
    conn = _connect()
    try:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM promotions WHERE id = ?",
            (promotion_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_categories(source: Optional[str] = None) -> list[str]:
    """Return the distinct categories stored, optionally within a source."""
    conn = _connect()
    try:
        if source:
            rows = conn.execute(
                "SELECT DISTINCT category FROM promotions WHERE source = ? ORDER BY category",
                (source,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT category FROM promotions ORDER BY category"
            ).fetchall()
        return [row["category"] for row in rows]
    finally:
        conn.close()


def count_promotions() -> int:
    """Return the total number of stored promotions."""
    conn = _connect()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM promotions").fetchone()
        return int(row["n"])
    finally:
        conn.close()


def purge_expired_site(retention_days: int = 30, today: Optional[str] = None) -> int:
    """Delete finished Stake.com (site) promotions older than the retention window.

    A site promotion is kept for ``retention_days`` days after it finished, then
    removed. "Finished" here means either its end date has passed (``ends_at``)
    or it was superseded by a renewed version (``archived_at``); the cutoff is
    measured from whichever applies. Forum promotions — including the past-events
    archive — are never purged here. Returns the number of rows deleted.
    """
    today_date = date.fromisoformat(today) if today else date.today()
    cutoff = (today_date - timedelta(days=retention_days)).isoformat()
    conn = _connect()
    try:
        cur = conn.execute(
            """
            DELETE FROM promotions
            WHERE source = 'site'
              AND (
                    (ends_at <> '' AND ends_at < ?)
                 OR (archived_at <> '' AND archived_at < ?)
              )
            """,
            (cutoff, cutoff),
        )
        conn.commit()
        deleted = cur.rowcount or 0
        if deleted:
            logger.info("Purged %d site promotion(s) finished before %s", deleted, cutoff)
        return deleted
    finally:
        conn.close()


def prune_to(allowed: dict[str, set[str]]) -> int:
    """Delete stored promotions that are no longer part of the configured set.

    ``allowed`` maps each source (``site``/``forum``) to the set of category
    names that should be kept. Any row whose source is listed but whose category
    is not in that source's allowed set is removed. Sources absent from the map
    are left untouched. Returns the number of rows deleted.
    """
    if not allowed:
        return 0
    conn = _connect()
    try:
        clauses: list[str] = []
        params: list[Any] = []
        for source, categories in allowed.items():
            cats = list(categories)
            if cats:
                placeholders = ",".join("?" for _ in cats)
                clauses.append(f"(source = ? AND category NOT IN ({placeholders}))")
                params.extend([source, *cats])
            else:
                # No categories allowed for this source -> drop all of its rows.
                clauses.append("(source = ?)")
                params.append(source)
        sql = "DELETE FROM promotions WHERE " + " OR ".join(clauses)
        cur = conn.execute(sql, params)
        conn.commit()
        deleted = cur.rowcount or 0
        if deleted:
            logger.info("Pruned %d obsolete promotion(s)", deleted)
        return deleted
    finally:
        conn.close()
