"""Deterministic stage/state extraction from SEC Form D filings.

Per spec: "State is the issuer address. Stage is approximated from total
offering amount plus filing sequence. No Form D -> Unknown and null. Never
guessed from any other source." Offering amount lives in the filing's own
``primary_doc.xml``, cached separately by ``sources/sec_edgar.py`` (source
``sec_formd_doc``) since it's a distinct fetched artifact from the search
hit itself; filing sequence is the count of a CIK's prior Form D filings
already sitting in ``raw_records``.

The dollar thresholds below are starting values (spec: "the tuning surface,"
same as the gate's) — tune them once real accepted companies pile up
against real fundraise sizes.
"""
from __future__ import annotations

import json
import re

from .. import db

_AMOUNT_TAG = re.compile(r"<totalOfferingAmount>\s*([\d,]+)\s*</totalOfferingAmount>", re.IGNORECASE)

# (max prior filings, max total offering amount) -> stage, checked in order.
_STAGE_BUCKETS: list[tuple[int, float, str]] = [
    (0, 2_000_000, "Pre-Seed"),
    (0, 10_000_000, "Seed"),
    (1, 20_000_000, "Series A"),
    (1, 50_000_000, "Series B"),
]
_DEFAULT_STAGE_WITH_AMOUNT = "Series C"
_DEFAULT_STAGE_NO_AMOUNT_FIRST = "Seed"
_DEFAULT_STAGE_NO_AMOUNT_LATER = "Series A"


def _offering_amount(xml_text: str) -> float | None:
    m = _AMOUNT_TAG.search(xml_text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def state_and_stage(conn, company_id: int) -> tuple[str | None, str]:
    """Looks at every Form D raw_record linked to this company and returns
    (state, stage). No Form D evidence -> (None, "Unknown")."""
    filings = [
        r for r in db.raw_records_for_company(conn, company_id) if r["source"] == "sec_form_d"
    ]
    if not filings:
        return None, "Unknown"

    filings.sort(key=lambda r: r["payload"].get("file_date") or "")
    latest = filings[-1]
    biz_states = latest["payload"].get("biz_states") or []
    inc_states = latest["payload"].get("inc_states") or []
    raw_state = (biz_states or inc_states or [None])[0]
    state = raw_state.strip().upper()[:2] if raw_state else None

    ciks = latest["payload"].get("ciks") or []
    cik = ciks[0].lstrip("0") if ciks else None
    prior_count = 0
    if cik:
        cik_filings = db.sec_filings_for_cik(conn, cik)
        prior_count = max(0, len(cik_filings) - 1)

    adsh = latest["payload"].get("adsh")
    amount = None
    if adsh:
        doc_row = conn.execute(
            "SELECT payload FROM raw_records WHERE source = 'sec_formd_doc' AND source_id = ?",
            (adsh,),
        ).fetchone()
        if doc_row:
            xml_text = json.loads(doc_row["payload"]).get("xml", "")
            amount = _offering_amount(xml_text)

    if amount is not None:
        for max_prior, max_amount, bucket in _STAGE_BUCKETS:
            if prior_count <= max_prior and amount <= max_amount:
                return state, bucket
        return state, _DEFAULT_STAGE_WITH_AMOUNT

    return state, _DEFAULT_STAGE_NO_AMOUNT_FIRST if prior_count == 0 else _DEFAULT_STAGE_NO_AMOUNT_LATER
