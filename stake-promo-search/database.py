"""SQLite persistence layer with FTS5 full-text search for Stake promotions.

The schema uses an external-content FTS5 virtual table kept in sync with the
``promotions`` table through triggers, so searches stay fast and the canonical
data lives in a single place. Duplicates are prevented by a UNIQUE constraint
on the promotion URL.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Database file lives next to this module so the app is fully self-contained.
DB_PATH: Path = Path(__file__).resolve().parent / "promotions.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS promotions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT    NOT NULL UNIQUE,
    title       TEXT    NOT NULL,
    category    TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT 'site',
    duration    TEXT    NOT NULL DEFAULT '',
    content     TEXT    NOT NULL DEFAULT '',
    terms       TEXT    NOT NULL DEFAULT '',
    scraped_at  TEXT    NOT NULL
);

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
    """Create tables, the FTS index and triggers if they do not exist yet."""
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        _rebuild_fts(conn)
        conn.commit()
        logger.info("Database initialised at %s", DB_PATH)
    finally:
        conn.close()


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
    """Add columns introduced after the first release to pre-existing databases.

    SQLite has no ``ADD COLUMN IF NOT EXISTS``, so we inspect the schema first.
    Existing rows predate the source/duration split, so they default to the
    Stake site source and an empty duration until they are next scraped.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(promotions)")}
    if "source" not in existing:
        conn.execute("ALTER TABLE promotions ADD COLUMN source TEXT NOT NULL DEFAULT 'site'")
        logger.info("Migrated database: added 'source' column")
    if "duration" not in existing:
        conn.execute("ALTER TABLE promotions ADD COLUMN duration TEXT NOT NULL DEFAULT ''")
        logger.info("Migrated database: added 'duration' column")


def upsert_promotion(
    *,
    url: str,
    title: str,
    category: str,
    source: str,
    duration: str,
    content: str,
    terms: str,
    scraped_at: str,
) -> bool:
    """Insert a promotion or update it in place when the URL already exists.

    Returns ``True`` if a new row was inserted, ``False`` if an existing row
    was updated. The UNIQUE constraint on ``url`` guarantees no duplicates.
    """
    conn = _connect()
    try:
        cur = conn.execute("SELECT 1 FROM promotions WHERE url = ?", (url,))
        existed = cur.fetchone() is not None
        conn.execute(
            """
            INSERT INTO promotions
                (url, title, category, source, duration, content, terms, scraped_at)
            VALUES
                (:url, :title, :category, :source, :duration, :content, :terms, :scraped_at)
            ON CONFLICT(url) DO UPDATE SET
                title      = excluded.title,
                category   = excluded.category,
                source     = excluded.source,
                duration   = excluded.duration,
                content    = excluded.content,
                terms      = excluded.terms,
                scraped_at = excluded.scraped_at
            """,
            {
                "url": url,
                "title": title,
                "category": category,
                "source": source,
                "duration": duration,
                "content": content,
                "terms": terms,
                "scraped_at": scraped_at,
            },
        )
        conn.commit()
        return not existed
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


_COLUMNS = "id, url, title, category, source, duration, content, terms, scraped_at"


def search(
    query: str,
    category: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Search promotions by title/content/terms, optionally filtered.

    Results can be narrowed by ``category`` and/or ``source`` (the top-level
    group, ``site`` or ``forum``). With a query the results are ranked by FTS5
    relevance; with an empty query every matching promotion is returned
    alphabetically, so the homepage can show the full catalogue out of the box.
    """
    match = _build_match_query(query) if query else None
    conn = _connect()
    try:
        if match:
            params: list[Any] = [match]
            clauses = ""
            if category:
                clauses += " AND p.category = ?"
                params.append(category)
            if source:
                clauses += " AND p.source = ?"
                params.append(source)
            params.append(limit)
            prefixed = ", ".join(f"p.{col}" for col in _COLUMNS.split(", "))
            sql = f"""
                SELECT {prefixed}
                FROM promotions_fts f
                JOIN promotions p ON p.id = f.rowid
                WHERE promotions_fts MATCH ?
                {clauses}
                ORDER BY rank
                LIMIT ?
            """
        else:
            params = []
            conditions = []
            if category:
                conditions.append("category = ?")
                params.append(category)
            if source:
                conditions.append("source = ?")
                params.append(source)
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
