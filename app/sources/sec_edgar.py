"""SEC EDGAR — Form D filings.

Form D is filed by companies raising capital under a Regulation D exemption,
so it is an early, structured, public signal of a fundraise. No API key is
required; SEC only asks for a descriptive ``User-Agent`` and light request
rates.

We use the EDGAR full-text search backend (``efts.sec.gov``), whose JSON
hits already carry the issuer's business state and location. Each hit is
written verbatim into ``raw_records`` (source ``sec_form_d``) — pooled
investment funds are still filtered here rather than in the gate, since
recognizing them requires knowledge of Form D item codes that's specific to
this source, not a generic structural/evidence rule.

Deriving ``stage`` also needs the filing's total offering amount, which
isn't in the full-text search hit — only in the filing's own
``primary_doc.xml``. That's a second, distinct fetched artifact, so it is
cached verbatim into its own raw_records row (source ``sec_formd_doc``,
keyed by accession number) here at fetch time, once per filing, so re-running
derive after a stage-bucket tuning change never re-fetches it.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

import httpx

from .. import db
from ..config import HTTP_TIMEOUT, SEC_USER_AGENT
from .base import FetchResult, RawItem

log = logging.getLogger(__name__)

SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
LOOKBACK_DAYS = 14
MAX_RESULTS = 120
PAGE_SIZE = 10  # EFTS returns 10 hits per page and ignores larger values.


def _is_pooled_fund(items: list[str]) -> bool:
    return any(str(it).upper().startswith("3C") for it in items or [])


async def _fetch_primary_doc(client: httpx.AsyncClient, cik: str, adsh: str) -> str | None:
    accession_nodash = adsh.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/primary_doc.xml"
    headers = {"User-Agent": SEC_USER_AGENT}
    try:
        resp = await client.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        if resp.status_code == 200 and resp.text.strip():
            return resp.text
    except httpx.HTTPError:
        pass
    return None


async def _cache_offering_docs(client: httpx.AsyncClient, hits: list[dict]) -> None:
    with db.get_conn() as conn:
        for src in hits:
            adsh = src.get("adsh") or ""
            ciks = src.get("ciks") or []
            cik = ciks[0].lstrip("0") if ciks else ""
            if not adsh or not cik:
                continue
            existing = conn.execute(
                "SELECT 1 FROM raw_records WHERE source = 'sec_formd_doc' "
                "AND source_id = ?",
                (adsh,),
            ).fetchone()
            if existing:
                continue
            xml_text = await _fetch_primary_doc(client, cik, adsh)
            if xml_text is not None:
                db.insert_raw_record(conn, "sec_formd_doc", adsh, {"xml": xml_text})
            await asyncio.sleep(0.15)  # be polite to EDGAR


async def fetch(client: httpx.AsyncClient) -> FetchResult:
    end = date.today()
    start = end - timedelta(days=LOOKBACK_DAYS)
    headers = {"User-Agent": SEC_USER_AGENT, "Accept": "application/json"}

    hits: list[dict] = []
    seen_ids: set[str] = set()
    try:
        for offset in range(0, MAX_RESULTS, PAGE_SIZE):
            params = {
                "q": "",
                "forms": "D",
                "startdt": start.isoformat(),
                "enddt": end.isoformat(),
                "from": offset,
            }
            resp = await client.get(
                SEARCH_URL, params=params, headers=headers, timeout=HTTP_TIMEOUT
            )
            resp.raise_for_status()
            page_hits = (resp.json().get("hits") or {}).get("hits") or []
            if not page_hits:
                break
            for hit in page_hits:
                src = hit.get("_source") or {}
                if not src.get("display_names") or _is_pooled_fund(src.get("items", [])):
                    continue
                adsh = src.get("adsh") or ""
                key = adsh or str(src.get("display_names"))
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                hits.append(src)
            if len(page_hits) < PAGE_SIZE:
                break
            await asyncio.sleep(0.15)  # be polite to EDGAR
    except httpx.HTTPStatusError as exc:
        return FetchResult.failed(
            "sec_form_d", f"SEC EDGAR returned HTTP {exc.response.status_code}"
        )
    except (httpx.HTTPError, ValueError) as exc:
        return FetchResult.failed("sec_form_d", f"SEC EDGAR fetch failed: {exc}")

    await _cache_offering_docs(client, hits)

    raw_items = [
        RawItem(source_id=src["adsh"], payload=src) for src in hits if src.get("adsh")
    ]
    log.info("SEC EDGAR: %d Form D raw items", len(raw_items))
    return FetchResult(source="sec_form_d", items=raw_items)
