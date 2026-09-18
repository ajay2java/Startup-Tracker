"""Derive — turn a bare domain into a named, described, sectored company.

Per spec §4, everything here is local and runs off exactly one homepage
fetch per company (cached in ``raw_records`` under ``homepage_cache`` so a
rule change re-derives without re-fetching). Selection of which companies
need this pass is ``enriched_at IS NULL OR updated_at > enriched_at OR
enrich_version < CURRENT`` (spec §2 step 4) — bump ``CURRENT_ENRICH_VERSION``
whenever a derivation rule changes to force everyone through again.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .. import db
from ..config import HTTP_TIMEOUT
from . import sectors, stage
from .domain_utils import registrable_domain

log = logging.getLogger(__name__)

CURRENT_ENRICH_VERSION = 1
CONCURRENCY = 8
_UA = "Startup Tracker (personal research tool)"

_FLUFF = re.compile(
    r"reimagin|the future of|next generation|revolutioniz|empower|seamless",
    re.IGNORECASE,
)
_WEAK_MIN_LEN = 40
_AI_KEYWORDS = re.compile(
    r"machine learning|\bLLM\b|\bneural\b|AI-powered|foundation model", re.IGNORECASE
)
_SPLIT_TITLE = re.compile(r"\s*[|–-]\s*")
_KEY_PAGE_WORDS = ("pricing", "about", "contact")


@dataclass
class DeriveSummary:
    derived: int = 0
    reverted_to_hold: int = 0
    fetch_failed: int = 0


# ---------------------------------------------------------------------------
# Homepage fetch + cache
# ---------------------------------------------------------------------------

async def _get_homepage_html(client: httpx.AsyncClient, domain: str) -> str | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM raw_records WHERE source = 'homepage_cache' "
            "AND source_id = ?",
            (domain,),
        ).fetchone()
    if row:
        return json.loads(row["payload"]).get("html")

    try:
        resp = await client.get(f"https://{domain}", timeout=HTTP_TIMEOUT)
        if resp.status_code >= 400:
            return None
        # See gate.py's identical fix: decode as UTF-8 explicitly rather
        # than trust resp.text's charset guess.
        html = resp.content.decode("utf-8", errors="replace")[:500_000]
    except httpx.HTTPError:
        return None

    with db.get_conn() as conn:
        db.insert_raw_record(
            conn, "homepage_cache", domain, {"html": html, "url": str(resp.url)}
        )
    return html


# ---------------------------------------------------------------------------
# Field extraction from parsed HTML
# ---------------------------------------------------------------------------

def _meta(soup: BeautifulSoup, **attrs: str) -> str | None:
    tag = soup.find("meta", attrs=attrs)
    content = tag.get("content") if tag else None
    return content.strip() if content and content.strip() else None


def _derive_name(soup: BeautifulSoup, domain: str) -> str:
    site_name = _meta(soup, property="og:site_name")
    if site_name:
        return site_name

    title_tag = soup.find("title")
    if title_tag and title_tag.get_text(strip=True):
        segments = [s.strip() for s in _SPLIT_TITLE.split(title_tag.get_text()) if s.strip()]
        if segments:
            return min(segments, key=len)

    return domain.rsplit(".", 1)[0].replace("-", " ").title()


def _derive_description(soup: BeautifulSoup) -> str | None:
    meta_desc = _meta(soup, name="description")
    if meta_desc:
        return meta_desc

    og_desc = _meta(soup, property="og:description")
    if og_desc:
        return og_desc

    h1 = soup.find("h1")
    if h1:
        sibling = h1.find_next(["p", "h2", "h3"])
        if sibling and sibling.get_text(strip=True):
            return sibling.get_text(strip=True)

    return None


def _desc_quality(description: str | None) -> str:
    if not description:
        return "missing"
    if len(description) < _WEAK_MIN_LEN:
        return "weak"
    if _FLUFF.search(description) and len(description) < 80:
        return "weak"
    return "good"


def _uses_ai(text: str) -> bool:
    return bool(_AI_KEYWORDS.search(text))


def _is_internal_link(href: str, domain: str, base_url: str) -> bool:
    resolved = urljoin(base_url, href)
    return registrable_domain(resolved) == domain


def _passes_second_chance(soup: BeautifulSoup, domain: str, base_url: str, has_site_name: bool) -> bool:
    """False means: looks like a portfolio/OSS page, not a real company site
    (spec §4 "second-chance company check")."""
    if has_site_name:
        return True

    links = soup.find_all("a", href=True)
    for a in links:
        text = (a.get_text() or "").lower()
        href = (a["href"] or "").lower()
        if any(word in text or word in href for word in _KEY_PAGE_WORDS):
            return True

    internal = {
        urljoin(base_url, a["href"])
        for a in links
        if _is_internal_link(a["href"], domain, base_url)
    }
    is_single_page = len(internal) <= 1
    return not is_single_page


# ---------------------------------------------------------------------------
# Per-company derivation
# ---------------------------------------------------------------------------

async def _derive_one(client: httpx.AsyncClient, company: dict) -> dict:
    domain = company["domain"]
    html = await _get_homepage_html(client, domain)
    if html is None:
        return {"outcome": "fetch_failed"}

    soup = BeautifulSoup(html, "lxml")
    site_name = _meta(soup, property="og:site_name")
    homepage_text = soup.get_text(" ", strip=True)

    if not _passes_second_chance(soup, domain, f"https://{domain}", bool(site_name)):
        return {"outcome": "revert_to_hold"}

    description = _derive_description(soup)
    fields = {
        "name": _derive_name(soup, domain),
        "description": description,
        "desc_quality": _desc_quality(description),
        "sector": sectors.classify(homepage_text),
        "uses_ai": 1 if _uses_ai(homepage_text) else 0,
        "enriched_at": db.now_iso(),
        "enrich_version": CURRENT_ENRICH_VERSION,
    }
    with db.get_conn() as conn:
        derived_state, derived_stage = stage.state_and_stage(conn, company["id"])
    if derived_state:
        fields["state"] = derived_state
    if derived_stage:
        fields["stage"] = derived_stage
    return {"outcome": "derived", "fields": fields}


def _revert_to_hold(conn, company_id: int) -> bool:
    """Only revert (and delete the company) when it was never corroborated
    by more than one raw record — see judgment call in the build plan.
    A company backed by independent evidence keeps its promotion."""
    linked = db.raw_records_for_company(conn, company_id)
    if len(linked) != 1:
        return False
    rec = linked[0]
    db.set_gate_result(
        conn, rec["id"], "hold", "second_chance_failed",
        recheck_at=date.today() + timedelta(days=30),
    )
    db.unlink_raw_from_company(conn, rec["id"])
    db.delete_company(conn, company_id)
    return True


async def run_derive() -> DeriveSummary:
    with db.get_conn() as conn:
        companies = db.companies_needing_derivation(conn, CURRENT_ENRICH_VERSION)
    log.info("derive: %d companies need (re-)derivation", len(companies))

    sem = asyncio.Semaphore(CONCURRENCY)

    async def bound(c: dict) -> dict:
        async with sem:
            return await _derive_one(client, c)

    async with httpx.AsyncClient(
        headers={"User-Agent": _UA}, follow_redirects=True
    ) as client:
        results = await asyncio.gather(*(bound(c) for c in companies))

    summary = DeriveSummary()
    with db.get_conn() as conn:
        for company, result in zip(companies, results):
            outcome = result["outcome"]
            if outcome == "fetch_failed":
                summary.fetch_failed += 1
                continue
            if outcome == "revert_to_hold":
                if _revert_to_hold(conn, company["id"]):
                    summary.reverted_to_hold += 1
                    continue
                # corroborated by other evidence — fall through and derive
                # normally using whatever homepage content we did get.
            fields = result.get("fields")
            if fields is None:
                continue
            db.update_company(conn, company["id"], **fields)
            summary.derived += 1

    log.info(
        "derive: %d derived, %d reverted to hold, %d fetch failed",
        summary.derived, summary.reverted_to_hold, summary.fetch_failed,
    )
    return summary
