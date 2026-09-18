"""The gate — decides which raw records are real, early-stage US startups.

Four layers, cheapest first, every outcome stamped with a ``gate_reason`` so
the reject log actually explains itself (spec: "read the reject log daily
for the first two weeks"). Layers 1-2 can reject outright; layer 3 (the live
evidence check) can only promote or hold — a record that structurally looks
right but hasn't proven a live site yet gets a second chance later via
``recheck_holds`` rather than being thrown away.

Network lookups (GitHub org metadata, RDAP domain age, the live-site fetch
itself) are cached in ``lookup_cache`` so a record stuck in ``hold`` doesn't
re-trigger the same external call every night it's rechecked and still
fails.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import httpx

from .. import db
from ..config import HTTP_TIMEOUT
from .candidate import (
    SHOW_OR_LAUNCH_HN,
    candidate_name,
    candidate_url,
    guessed_domain,
)
from .domain_utils import is_platform_domain, registrable_domain

log = logging.getLogger(__name__)

_UA = "Startup Tracker (personal research tool)"
CONCURRENCY = 8
HOLD_RECHECK_DAYS = 30

# --- Layer 1: structural thresholds -----------------------------------------
NAME_MAX_LEN = 60
_COMMA_VERB = re.compile(
    r",\s*(a|an|the)?\s*\w*(ing|s|ed)\b|,\s*(is|are|was|were|helps?|builds?|makes?|provides?)\b",
    re.IGNORECASE,
)
# Foundations/standards bodies/OSS collectives/universities: what structural
# rules can't catch. Deliberately small — see spec §3 layer 2.
NAME_DENYLIST_SUBSTRINGS = (
    "foundation", "university", "apache software", " cncf", "rust-lang",
    "linux foundation", "mozilla", "wikimedia", "python software foundation",
    "eclipse foundation", "openssf",
)

# --- Layer 2: not-an-early-startup thresholds -------------------------------
GITHUB_ORG_MAX_AGE_YEARS = 6
GITHUB_FOLLOWER_MAX = 3000
GITHUB_REPO_MAX = 150
DOMAIN_MAX_AGE_YEARS = 7


@dataclass
class GateSummary:
    promoted: int = 0
    rejected: int = 0
    hold: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def record(self, status: str, reason: str | None) -> None:
        if status == "promoted":
            self.promoted += 1
        elif status == "rejected":
            self.rejected += 1
        elif status == "hold":
            self.hold += 1
        if reason:
            self.reasons[reason] = self.reasons.get(reason, 0) + 1


def _years_since(iso_date: str) -> float:
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    now = datetime.now(dt.tzinfo)
    return (now - dt).days / 365.25


def _is_denylisted(name: str) -> bool:
    lowered = name.lower()
    return any(term in lowered for term in NAME_DENYLIST_SUBSTRINGS)


# ---------------------------------------------------------------------------
# Layer 1 — structural, no network
# ---------------------------------------------------------------------------

def _layer1(source: str, payload: dict) -> tuple[str | None, str | None]:
    """Returns (name, reject_or_hold_reason). ``reject_or_hold_reason`` of
    ``None`` means layer 1 passed; otherwise the caller decides reject vs.
    hold based on whether it's a genuine structural disqualification
    (reject) or missing data we can't evaluate (hold)."""
    if source == "github":
        owner = payload.get("owner")
        if owner is None:
            return candidate_name(source, payload), "__hold__legacy_incomplete_data"
        if owner.get("type") != "Organization":
            return candidate_name(source, payload), "not_org"
    elif source == "hn":
        title = payload.get("title")
        if title is None:
            return candidate_name(source, payload), "__hold__legacy_incomplete_data"
        if not SHOW_OR_LAUNCH_HN.search(title):
            return candidate_name(source, payload), "not_show_or_launch_hn"
    elif source == "sec_form_d":
        if not (payload.get("display_names") or []):
            return None, "no_issuer_name"

    name = candidate_name(source, payload)
    if not name or not name.strip():
        return name, "no_name"
    if len(name) > NAME_MAX_LEN:
        return name, "name_too_long"
    if _COMMA_VERB.search(name):
        return name, "name_looks_like_title"
    if _is_denylisted(name):
        return name, "denylisted_org"
    return name, None


# ---------------------------------------------------------------------------
# Layer 2 — not-an-early-startup proxies (network, cached)
# ---------------------------------------------------------------------------

async def _github_org_info(client: httpx.AsyncClient, login: str) -> dict | None:
    with db.get_conn() as conn:
        cached = db.cache_get(conn, "gh_org", login)
    if cached is not None:
        return cached or None
    try:
        resp = await client.get(
            f"https://api.github.com/users/{login}",
            headers={"Accept": "application/vnd.github+json"},
            timeout=HTTP_TIMEOUT,
        )
        info = resp.json() if resp.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        info = None  # transient failure — don't cache, fail open below
    if info is not None:
        with db.get_conn() as conn:
            db.cache_set(conn, "gh_org", login, info)
    return info or None


async def _domain_registration_date(client: httpx.AsyncClient, domain: str) -> str | None:
    with db.get_conn() as conn:
        cached = db.cache_get(conn, "rdap", domain)
    if cached is not None:
        return cached or None
    result = None
    try:
        resp = await client.get(f"https://rdap.org/domain/{domain}", timeout=HTTP_TIMEOUT)
        if resp.status_code == 200:
            for event in resp.json().get("events", []):
                if event.get("eventAction") == "registration":
                    result = event.get("eventDate")
                    break
    except (httpx.HTTPError, ValueError):
        return None  # transient failure — fail open, don't cache
    with db.get_conn() as conn:
        db.cache_set(conn, "rdap", domain, result)
    return result


async def _layer2(client: httpx.AsyncClient, source: str, payload: dict, name: str) -> str | None:
    if source == "github":
        owner = payload.get("owner") or {}
        login = owner.get("login")
        if login:
            info = await _github_org_info(client, login)
            if info:
                created = info.get("created_at")
                if created and _years_since(created) > GITHUB_ORG_MAX_AGE_YEARS:
                    return "org_too_old"
                if (
                    (info.get("followers") or 0) > GITHUB_FOLLOWER_MAX
                    or (info.get("public_repos") or 0) > GITHUB_REPO_MAX
                ):
                    return "org_too_established"

    url = candidate_url(source, payload)
    domain = registrable_domain(url) if url else None
    if domain and not is_platform_domain(domain):
        reg_date = await _domain_registration_date(client, domain)
        if reg_date and _years_since(reg_date) > DOMAIN_MAX_AGE_YEARS:
            return "domain_too_old"
    return None


# ---------------------------------------------------------------------------
# Layer 3 — evidence: must resolve to a live site on its own domain
# ---------------------------------------------------------------------------

async def _fetch_and_cache_homepage(client: httpx.AsyncClient, url: str) -> tuple[str, str] | None:
    """GET the URL, cache the body under its final resolved domain (so
    derive reuses it), return (domain, html) or None if unreachable."""
    try:
        resp = await client.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code >= 400:
            return None
        final_domain = registrable_domain(str(resp.url))
        if not final_domain:
            return None
        # Decode as UTF-8 explicitly rather than trust resp.text's charset
        # guess, which mis-detects often enough on real-world pages to
        # produce visible mojibake in derived descriptions.
        html = resp.content.decode("utf-8", errors="replace")[:500_000]
    except httpx.HTTPError:
        return None
    with db.get_conn() as conn:
        db.insert_raw_record(
            conn, "homepage_cache", final_domain, {"html": html, "url": str(resp.url)}
        )
    return final_domain, html


async def _layer3(
    client: httpx.AsyncClient, raw_id: int, source: str, payload: dict, name: str
) -> tuple[str, str | None, str | None]:
    """Returns (status, reason, domain). status is 'promoted' or 'hold'."""
    url = candidate_url(source, payload)
    domain = registrable_domain(url) if url else None

    if domain and is_platform_domain(domain):
        return "hold", "not_own_domain", None

    if domain:
        result = await _fetch_and_cache_homepage(client, url)
        if result is None:
            return "hold", "site_unreachable", None
        final_domain, _html = result
        if is_platform_domain(final_domain):
            return "hold", "not_own_domain", None
        with db.get_conn() as conn:
            db.cache_set(conn, "confirmed_domain", str(raw_id), final_domain)
        return "promoted", None, final_domain

    # No URL at all in the payload (typically sec_form_d) — the only path to
    # a domain is a best-effort guess from the company name, confirmed by
    # checking the guessed page actually mentions this name.
    guess = guessed_domain(name)
    if not guess:
        return "hold", "no_domain_evidence", None
    result = await _fetch_and_cache_homepage(client, f"https://{guess}")
    if result is None:
        return "hold", "no_domain_evidence", None
    final_domain, html = result
    name_token = re.sub(r"[^a-z0-9]", "", name.lower())[:6]
    if name_token and name_token not in re.sub(r"[^a-z0-9]", "", html.lower()):
        return "hold", "guessed_domain_unconfirmed", None
    with db.get_conn() as conn:
        db.cache_set(conn, "confirmed_domain", str(raw_id), final_domain)
    return "promoted", None, final_domain


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def _evaluate(client: httpx.AsyncClient, rec: dict) -> tuple[str, str | None, date | None]:
    source, payload = rec["source"], rec["payload"]

    name, l1_reason = _layer1(source, payload)
    if l1_reason == "__hold__legacy_incomplete_data":
        return "hold", "legacy_incomplete_data", date.today() + timedelta(days=HOLD_RECHECK_DAYS)
    if l1_reason:
        return "rejected", l1_reason, None
    assert name is not None

    l2_reason = await _layer2(client, source, payload, name)
    if l2_reason:
        return "rejected", l2_reason, None

    status, l3_reason, _domain = await _layer3(client, rec["id"], source, payload, name)
    if status == "hold":
        return "hold", l3_reason, date.today() + timedelta(days=HOLD_RECHECK_DAYS)
    return "promoted", None, None


async def _run_over(records: list[dict]) -> GateSummary:
    summary = GateSummary()
    if not records:
        return summary

    sem = asyncio.Semaphore(CONCURRENCY)

    async def bound(rec: dict):
        async with sem:
            return await _evaluate(client, rec)

    async with httpx.AsyncClient(
        headers={"User-Agent": _UA}, follow_redirects=True
    ) as client:
        results = await asyncio.gather(*(bound(r) for r in records))

    with db.get_conn() as conn:
        for rec, (status, reason, recheck_at) in zip(records, results):
            db.set_gate_result(conn, rec["id"], status, reason, recheck_at=recheck_at)
            summary.record(status, reason)
    return summary


async def run_gate() -> GateSummary:
    with db.get_conn() as conn:
        pending = db.pending_raw_records(conn)
    log.info("gate: evaluating %d pending records", len(pending))
    summary = await _run_over(pending)
    log.info(
        "gate: %d promoted, %d rejected, %d hold", summary.promoted, summary.rejected, summary.hold
    )
    return summary


async def recheck_holds() -> GateSummary:
    with db.get_conn() as conn:
        due = db.due_hold_records(conn, date.today())
    log.info("gate: rechecking %d due hold records", len(due))
    summary = await _run_over(due)
    log.info(
        "recheck: %d promoted, %d rejected, %d hold", summary.promoted, summary.rejected, summary.hold
    )
    return summary
