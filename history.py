"""Keep searches, keyword expansions and per-variant hit counts in a local SQLite file.

Everything here is best-effort: a locked or unwritable database degrades to "no history"
instead of failing a search.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from settings import ROOT


DB_PATH = ROOT / ".telegram-data" / "search.db"
EXPANSION_TTL = 30 * 60
MAX_STORED_SEARCHES = 200
MIN_USES_BEFORE_DEMOTION = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    created_at REAL NOT NULL,
    state TEXT NOT NULL,
    variants TEXT NOT NULL,
    hits TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expansions (
    key TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    variants TEXT NOT NULL,
    used_ai INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS variant_stats (
    variant TEXT PRIMARY KEY,
    hits INTEGER NOT NULL DEFAULT 0,
    uses INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
"""

_connection: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection | None:
    global _connection
    if _connection is None:
        try:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _connection = sqlite3.connect(DB_PATH)
            _connection.executescript(_SCHEMA)
            _connection.commit()
        except (sqlite3.Error, OSError):
            _connection = None
    return _connection


def _cache_key(query: str, model: str) -> str:
    return hashlib.sha256(f"{model}\0{query}".encode()).hexdigest()


def read_expansion(query: str, model: str) -> tuple[list[str], bool] | None:
    db = _db()
    if db is None:
        return None
    try:
        row = db.execute(
            "select created_at, variants, used_ai from expansions where key = ?",
            (_cache_key(query, model),),
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row or time.time() - row[0] > EXPANSION_TTL:
        return None
    try:
        variants = [value for value in json.loads(row[1]) if isinstance(value, str) and value]
    except (ValueError, TypeError):
        return None
    return (variants, bool(row[2])) if variants else None


def write_expansion(query: str, model: str, variants: list[str], used_ai: bool) -> None:
    db = _db()
    if db is None or not variants:
        return
    try:
        db.execute(
            "insert or replace into expansions (key, created_at, variants, used_ai) values (?, ?, ?, ?)",
            (_cache_key(query, model), time.time(), json.dumps(variants, ensure_ascii=False), int(used_ai)),
        )
        db.execute("delete from expansions where created_at < ?", (time.time() - 7 * 24 * 3600,))
        db.commit()
    except sqlite3.Error:
        pass


def record_search(search_id: str, query: str, state: str, variants: list[str], hits: list[dict]) -> None:
    db = _db()
    if db is None:
        return
    try:
        db.execute(
            "insert or replace into searches (id, query, created_at, state, variants, hits) values (?, ?, ?, ?, ?, ?)",
            (search_id, query, time.time(), state, json.dumps(variants, ensure_ascii=False),
             json.dumps(hits, ensure_ascii=False)),
        )
        db.execute(
            "delete from searches where id not in "
            "(select id from searches order by created_at desc limit ?)",
            (MAX_STORED_SEARCHES,),
        )
        db.commit()
    except sqlite3.Error:
        pass


def recent_searches(limit: int = 12) -> list[dict]:
    db = _db()
    if db is None:
        return []
    try:
        rows = db.execute(
            "select id, query, created_at, state, hits from searches order by created_at desc limit ?",
            (limit * 4,),
        ).fetchall()
    except sqlite3.Error:
        return []
    entries = []
    seen: set[str] = set()
    for search_id, query, created_at, state, hits in rows:
        if query in seen:
            # Repeating the same query only adds noise; keep the latest run of each.
            continue
        seen.add(query)
        try:
            count = len(json.loads(hits))
        except (ValueError, TypeError):
            count = 0
        entries.append({
            "id": search_id,
            "query": query,
            "created_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(created_at)),
            "state": state,
            "count": count,
        })
        if len(entries) >= limit:
            break
    return entries


def load_search(search_id: str) -> dict | None:
    db = _db()
    if db is None:
        return None
    try:
        row = db.execute(
            "select query, state, variants, hits from searches where id = ?", (search_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        variants = [value for value in json.loads(row[2]) if isinstance(value, str)]
        hits = [value for value in json.loads(row[3]) if isinstance(value, dict)]
    except (ValueError, TypeError):
        return None
    return {"query": row[0], "state": row[1], "variants": variants, "hits": hits}


def record_hits(variants: list[str], hits: list[dict]) -> None:
    """Track which keyword variants actually produce candidates."""
    db = _db()
    if db is None:
        return
    now = time.time()
    produced: dict[str, int] = {}
    for hit in hits:
        query = hit.get("query")
        if isinstance(query, str) and query:
            produced[query] = produced.get(query, 0) + 1
    try:
        for variant in variants:
            db.execute(
                "insert into variant_stats (variant, hits, uses, updated_at) values (?, ?, 1, ?) "
                "on conflict(variant) do update set uses = uses + 1, hits = hits + ?, updated_at = ?",
                (variant, produced.get(variant, 0), now, produced.get(variant, 0), now),
            )
        db.commit()
    except sqlite3.Error:
        pass


def demote_barren(variants: list[str]) -> list[str]:
    """Move variants that never produced anything to the end, keeping the rest in order."""
    db = _db()
    if db is None:
        return variants
    try:
        rows = db.execute(
            "select variant, hits, uses from variant_stats where uses >= ?", (MIN_USES_BEFORE_DEMOTION,),
        ).fetchall()
    except sqlite3.Error:
        return variants
    barren = {variant for variant, hits, _ in rows if not hits}
    if not barren:
        return variants
    keep = [variant for variant in variants if variant not in barren]
    drop = [variant for variant in variants if variant in barren]
    return keep + drop
