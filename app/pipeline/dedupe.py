"""Dedupe — collapse promoted raw records onto companies by domain.

Domain is the real key (spec §1): a promoted record whose confirmed domain
already has a company merges onto it (bumping ``updated_at`` so derive picks
it up again); otherwise it becomes a new company. The confirmed domain
itself was already computed and verified by the gate's layer-3 live-site
check — dedupe trusts that result (cached under ``confirmed_domain`` in
``lookup_cache``, keyed by raw record id) rather than re-deriving it, since
re-deriving would mean re-fetching the page.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .. import db
from .candidate import candidate_url
from .domain_utils import registrable_domain

log = logging.getLogger(__name__)


@dataclass
class DedupeSummary:
    new_companies: int = 0
    merged: int = 0
    skipped_no_domain: int = 0


def _resolved_domain(conn, rec: dict) -> str | None:
    cached = db.cache_get(conn, "confirmed_domain", str(rec["id"]))
    if cached:
        return cached
    # Fallback for records promoted outside the gate's normal path (e.g. a
    # future direct-promotion route) — recompute from the payload's own URL.
    url = candidate_url(rec["source"], rec["payload"])
    return registrable_domain(url) if url else None


def run_dedupe() -> DedupeSummary:
    summary = DedupeSummary()
    with db.get_conn() as conn:
        for rec in db.promoted_unlinked(conn):
            domain = _resolved_domain(conn, rec)
            if not domain:
                summary.skipped_no_domain += 1
                log.warning(
                    "raw_record %d promoted but has no resolvable domain", rec["id"]
                )
                continue

            existing = db.get_company_by_domain(conn, domain)
            if existing:
                db.link_raw_to_company(conn, rec["id"], existing["id"])
                db.touch_company(conn, existing["id"])
                summary.merged += 1
            else:
                company_id = db.insert_company(conn, domain, first_seen=rec["fetched_at"])
                db.link_raw_to_company(conn, rec["id"], company_id)
                summary.new_companies += 1
    return summary
