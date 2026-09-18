"""SQLite access layer.

Deliberately thin: raw ``sqlite3`` with ``Row`` factory, no ORM, to keep the
tool lightweight and easy to inspect with any SQLite browser. This module
owns every table's schema and every SQL statement in the app — pipeline code
and the web layer call these functions rather than writing their own SQL, so
there is exactly one place that knows the on-disk shape of the data.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any

from .config import DB_PATH

# ---------------------------------------------------------------------------
# Schema
#
# Two tables carry the pipeline end to end (raw_records -> companies), per
# the v2 data model: raw_records is an append-only ingest log that nothing
# is ever deleted from (gate/dedupe/derive only ever stamp status columns
# onto it), and companies is the served, derived view built from it.
# lookup_cache is a third, generic table for network lookups (RDAP, GitHub
# org metadata, homepage HTML) that are expensive or rate-limited enough to
# be worth persisting so re-running a pipeline stage doesn't refetch them.
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    payload     TEXT NOT NULL,
    fetched_at  TIMESTAMP NOT NULL,
    gate_status TEXT NOT NULL DEFAULT 'pending',
    gate_reason TEXT,
    recheck_at  DATE,
    company_id  INTEGER REFERENCES companies(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_source_id
    ON raw_records(source, source_id);
CREATE INDEX IF NOT EXISTS idx_raw_gate_status ON raw_records(gate_status);
CREATE INDEX IF NOT EXISTS idx_raw_recheck_at ON raw_records(recheck_at);
CREATE INDEX IF NOT EXISTS idx_raw_company_id ON raw_records(company_id);

CREATE TABLE IF NOT EXISTS companies (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    domain         TEXT NOT NULL UNIQUE,
    name           TEXT,
    description    TEXT,
    sector         TEXT,
    uses_ai        BOOLEAN NOT NULL DEFAULT 0,
    stage          TEXT NOT NULL DEFAULT 'Unknown',
    state          CHAR(2),
    first_seen     TIMESTAMP NOT NULL,
    updated_at     TIMESTAMP NOT NULL,
    enriched_at    TIMESTAMP,
    enrich_version INTEGER NOT NULL DEFAULT 0,
    desc_quality   TEXT NOT NULL DEFAULT 'missing'
);

CREATE INDEX IF NOT EXISTS idx_companies_state ON companies(state);
CREATE INDEX IF NOT EXISTS idx_companies_stage ON companies(stage);
CREATE INDEX IF NOT EXISTS idx_companies_sector ON companies(sector);
CREATE INDEX IF NOT EXISTS idx_companies_first_seen ON companies(first_seen);

-- Generic cache for expensive/rate-limited external lookups (RDAP domain
-- age, GitHub org metadata, fetched homepage HTML). Keyed by an arbitrary
-- ``kind`` namespace plus a lookup key, e.g. (rdap, "acme.com").
CREATE TABLE IF NOT EXISTS lookup_cache (
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    PRIMARY KEY (kind, key)
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_iso() -> str:
    """Public timestamp helper for pipeline modules writing derived fields."""
    return _now()


# ---------------------------------------------------------------------------
# raw_records — write side (fetch + migration)
# ---------------------------------------------------------------------------

def insert_raw_record(
    conn: sqlite3.Connection,
    source: str,
    source_id: str,
    payload: dict[str, Any],
    *,
    gate_status: str = "pending",
) -> int:
    """Insert verbatim; a no-op if ``(source, source_id)`` already exists.

    Returns the row id either way (existing or newly inserted), since callers
    generally just need something to point at.
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO raw_records
            (source, source_id, payload, fetched_at, gate_status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (source, source_id, json.dumps(payload), _now(), gate_status),
    )
    if cur.rowcount:
        return int(cur.lastrowid)
    row = conn.execute(
        "SELECT id FROM raw_records WHERE source = ? AND source_id = ?",
        (source, source_id),
    ).fetchone()
    return int(row["id"])


def _parse_raw_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    return d


# ---------------------------------------------------------------------------
# raw_records — read/write side used by gate
# ---------------------------------------------------------------------------

def pending_raw_records(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM raw_records WHERE gate_status = 'pending' ORDER BY id"
    ).fetchall()
    return [_parse_raw_row(r) for r in rows]


def due_hold_records(conn: sqlite3.Connection, today: date) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM raw_records WHERE gate_status = 'hold' AND recheck_at <= ? "
        "ORDER BY id",
        (today.isoformat(),),
    ).fetchall()
    return [_parse_raw_row(r) for r in rows]


def set_gate_result(
    conn: sqlite3.Connection,
    raw_id: int,
    status: str,
    reason: str | None,
    *,
    recheck_at: date | None = None,
) -> None:
    conn.execute(
        "UPDATE raw_records SET gate_status = ?, gate_reason = ?, recheck_at = ? "
        "WHERE id = ?",
        (status, reason, recheck_at.isoformat() if recheck_at else None, raw_id),
    )


def reject_log(limit: int = 200) -> list[dict[str, Any]]:
    """Recent rejections, for the "read the reject log daily" workflow."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, source, source_id, gate_reason, fetched_at, payload "
            "FROM raw_records WHERE gate_status = 'rejected' "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_parse_raw_row(r) for r in rows]


def gate_status_counts() -> dict[str, int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT gate_status, COUNT(*) AS n FROM raw_records GROUP BY gate_status"
        ).fetchall()
    return {r["gate_status"]: r["n"] for r in rows}


def gate_reason_counts() -> dict[str, int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT gate_reason, COUNT(*) AS n FROM raw_records "
            "WHERE gate_reason IS NOT NULL GROUP BY gate_reason ORDER BY n DESC"
        ).fetchall()
    return {r["gate_reason"]: r["n"] for r in rows}


# ---------------------------------------------------------------------------
# raw_records <-> companies linkage, used by dedupe
# ---------------------------------------------------------------------------

def promoted_unlinked(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM raw_records WHERE gate_status = 'promoted' "
        "AND company_id IS NULL ORDER BY id"
    ).fetchall()
    return [_parse_raw_row(r) for r in rows]


def link_raw_to_company(conn: sqlite3.Connection, raw_id: int, company_id: int) -> None:
    conn.execute(
        "UPDATE raw_records SET company_id = ? WHERE id = ?", (company_id, raw_id)
    )


def unlink_raw_from_company(conn: sqlite3.Connection, raw_id: int) -> None:
    conn.execute("UPDATE raw_records SET company_id = NULL WHERE id = ?", (raw_id,))


def raw_records_for_company(conn: sqlite3.Connection, company_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM raw_records WHERE company_id = ? ORDER BY id", (company_id,)
    ).fetchall()
    return [_parse_raw_row(r) for r in rows]


def sec_filings_for_cik(conn: sqlite3.Connection, cik: str) -> list[dict[str, Any]]:
    """All promoted/derivable Form D filings for one issuer, oldest first —
    used to work out filing sequence for the stage heuristic."""
    rows = conn.execute(
        "SELECT * FROM raw_records WHERE source = 'sec_form_d' ORDER BY id"
    ).fetchall()
    out = []
    for r in rows:
        parsed = _parse_raw_row(r)
        if str(parsed["payload"].get("cik")) == str(cik):
            out.append(parsed)
    return out


# ---------------------------------------------------------------------------
# lookup_cache
# ---------------------------------------------------------------------------

def cache_get(conn: sqlite3.Connection, kind: str, key: str) -> Any | None:
    row = conn.execute(
        "SELECT value FROM lookup_cache WHERE kind = ? AND key = ?", (kind, key)
    ).fetchone()
    return json.loads(row["value"]) if row else None


def cache_set(conn: sqlite3.Connection, kind: str, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO lookup_cache (kind, key, value, fetched_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(kind, key) DO UPDATE SET value = excluded.value, "
        "fetched_at = excluded.fetched_at",
        (kind, key, json.dumps(value), _now()),
    )


# ---------------------------------------------------------------------------
# companies — write side, used by dedupe + manual add + derive
# ---------------------------------------------------------------------------

def get_company_by_domain(conn: sqlite3.Connection, domain: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM companies WHERE domain = ?", (domain,)).fetchone()
    return dict(row) if row else None


def get_company(conn: sqlite3.Connection, company_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    return dict(row) if row else None


def insert_company(conn: sqlite3.Connection, domain: str, first_seen: str | None = None) -> int:
    """Bare insert — name defaults to the domain itself until derive runs."""
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO companies
            (domain, name, first_seen, updated_at, enriched_at, enrich_version,
             desc_quality)
        VALUES (?, ?, ?, ?, NULL, 0, 'missing')
        """,
        (domain, domain, first_seen or now, now),
    )
    return int(cur.lastrowid)


def touch_company(conn: sqlite3.Connection, company_id: int) -> None:
    """Bump updated_at — signals derive that new evidence has merged in."""
    conn.execute(
        "UPDATE companies SET updated_at = ? WHERE id = ?", (_now(), company_id)
    )


def update_company(conn: sqlite3.Connection, company_id: int, **fields: Any) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE companies SET {set_clause} WHERE id = ?",
        (*fields.values(), company_id),
    )


def delete_company(conn: sqlite3.Connection, company_id: int) -> None:
    conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))


def companies_needing_derivation(conn: sqlite3.Connection, current_version: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM companies
        WHERE enriched_at IS NULL
           OR updated_at > enriched_at
           OR enrich_version < ?
        ORDER BY id
        """,
        (current_version,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# companies — read side, used by the web layer
#
# Every list/count query below shares the same filter dict shape:
# {"state": str|None, "sector": str|None, "stage": str|None}. The *_counts
# helpers exclude one dimension's own filter so picker counts reflect "what
# else is possible with my other active filters", never "what matches
# myself" — that's what keeps composing filters from ever dead-ending.
# ---------------------------------------------------------------------------

def _where(filters: dict[str, str | None], *, exclude: str = "") -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if filters.get("state") and exclude != "state":
        clauses.append("state = ?")
        params.append(filters["state"])
    if filters.get("sector") and exclude != "sector":
        clauses.append("sector = ?")
        params.append(filters["sector"])
    if filters.get("stage") and exclude != "stage":
        clauses.append("stage = ?")
        params.append(filters["stage"])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def list_companies(
    filters: dict[str, str | None], *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    where, params = _where(filters)
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM companies
            {where}
            ORDER BY
                CASE desc_quality WHEN 'good' THEN 0 WHEN 'weak' THEN 1 ELSE 2 END,
                first_seen DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def count_companies(filters: dict[str, str | None]) -> int:
    where, params = _where(filters)
    with get_conn() as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM companies {where}", params
        ).fetchone()[0]


def _grouped_counts(filters: dict[str, str | None], column: str) -> dict[str, int]:
    where, params = _where(filters, exclude=column)
    where = (where + f" AND {column} IS NOT NULL") if where else f"WHERE {column} IS NOT NULL"
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {column}, COUNT(*) AS n FROM companies {where} GROUP BY {column}",
            params,
        ).fetchall()
    return {r[column]: r["n"] for r in rows}


def state_counts(filters: dict[str, str | None]) -> dict[str, int]:
    return _grouped_counts(filters, "state")


def sector_counts(filters: dict[str, str | None]) -> dict[str, int]:
    return _grouped_counts(filters, "sector")


def stage_counts(filters: dict[str, str | None]) -> dict[str, int]:
    return _grouped_counts(filters, "stage")


def new_dot_counts(filters: dict[str, str | None], column: str, since_iso: str) -> dict[str, int]:
    """Same shape as the grouped counts above, restricted to recent first_seen
    — this is what drives the "new" dots on the map/pickers."""
    where, params = _where(filters, exclude=column)
    since_clause = f"{column} IS NOT NULL AND first_seen >= ?"
    where = (where + f" AND {since_clause}") if where else f"WHERE {since_clause}"
    params = [*params, since_iso]
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {column}, COUNT(*) AS n FROM companies {where} GROUP BY {column}",
            params,
        ).fetchall()
    return {r[column]: r["n"] for r in rows}
