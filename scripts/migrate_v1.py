"""One-off migration: v1's ``startups`` rows -> v2's ``raw_records`` queue.

Run once, by hand, after upgrading to the v2 schema::

    .venv\\Scripts\\python -m scripts.migrate_v1

v1 has no gate, so its 335 rows are exactly as noisy as any other
un-gated source — SEC business names that are really investment vehicles,
GitHub personal accounts, HN post titles that never had a company behind
them. Rather than trust v1's own judgment (there wasn't any — v1 shipped
without a gate) and promote these straight into ``companies``, every row is
re-queued as a ``pending`` raw record and left for the real v2 gate to
decide, per the v2 build order's own instruction to "run [the gate] over
existing v1 data and read the reject log."

v1 captured only a flattened subset of each source's real payload (name,
url, description, state, stage) — not the structural fields (GitHub
``owner.type``, HN post ``title`` prefix, SEC ``display_names``) the gate
needs for its layer-1 checks on github/hn rows. Fabricating those fields
would be dishonest data; instead migrated github/hn payloads are marked
``_migrated_from_v1`` and deliberately left without them, and the gate
recognizes the marker and routes anything it can't structurally evaluate to
``hold`` (reason ``legacy_incomplete_data``) rather than guessing. SEC rows
keep enough of their real structure (state, filing date, accession number)
to go through the gate normally.
"""
from __future__ import annotations

import logging
import sqlite3

from app import db
from app.config import DB_PATH

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def _v1_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return conn.execute("SELECT * FROM startups").fetchall()
    except sqlite3.OperationalError:
        return []  # no v1 table — nothing to migrate


def _payload_for(row: sqlite3.Row) -> dict:
    base = {
        "_migrated_from_v1": True,
        "name": row["name"],
        "url": row["url"],
        "description": row["description"],
        "state": row["state"],
        "stage": row["stage"],
    }
    if row["source"] == "sec_form_d":
        state = (row["state"] or "").strip().upper()
        base.update(
            {
                "display_names": [row["name"]],
                "biz_states": [state] if len(state) == 2 else [],
                "inc_states": [],
                "biz_locations": [],
                "file_date": row["first_seen"],
                "adsh": row["source_id"] or "",
                "ciks": [],
                "items": [],
            }
        )
    return base


def main() -> None:
    db.init_db()
    with sqlite3.connect(DB_PATH) as legacy_conn:
        legacy_conn.row_factory = sqlite3.Row
        rows = _v1_rows(legacy_conn)

    if not rows:
        log.info("no v1 startups rows found — nothing to migrate")
        return

    inserted = 0
    with db.get_conn() as conn:
        for row in rows:
            source = row["source"] or "manual"
            source_id = f"v1:{row['id']}"
            db.insert_raw_record(
                conn, source, source_id, _payload_for(row), gate_status="pending"
            )
            inserted += 1

    log.info("migrated %d v1 rows into raw_records as pending", inserted)
    log.info("run the pipeline (fetch/gate/dedupe/derive) to process them")


if __name__ == "__main__":
    main()
