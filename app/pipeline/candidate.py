"""Per-source extraction of a candidate's name and URL from its raw payload.

Shared by the gate (which needs both for its structural/evidence checks) and
dedupe (which needs the same URL to land on the same domain gate already
verified) so the two stages never disagree about what a record "is".
"""
from __future__ import annotations

import re
from typing import Any

_CIK_SUFFIX = re.compile(r"\s*\(CIK\s*\d+\)\s*$", re.IGNORECASE)
SHOW_OR_LAUNCH_HN = re.compile(r"^\s*(show hn|launch hn)\s*[:\-–—]", re.IGNORECASE)
_HN_PREFIX = re.compile(r"^\s*(show hn|launch hn)\s*[:\-–—]\s*", re.IGNORECASE)
_HN_SPLIT = re.compile(r"\s+[\-–—:|(]\s*|\s+[\-–—]\s+")


def is_migrated_legacy(payload: dict[str, Any]) -> bool:
    return bool(payload.get("_migrated_from_v1"))


def _hn_company_name(title: str) -> str:
    stripped = _HN_PREFIX.sub("", title).strip()
    first = _HN_SPLIT.split(stripped, maxsplit=1)[0].strip(" .")
    return (first or stripped or title).strip()


def candidate_name(source: str, payload: dict[str, Any]) -> str | None:
    """Best-effort name from raw payload alone — cheap, pre-derivation.
    Real name derivation (og:site_name etc.) happens later, per company, in
    the derive stage; this is only good enough for gate-layer-1 checks."""
    if source == "github":
        owner = payload.get("owner") or {}
        if owner.get("type") == "Organization":
            return owner.get("login") or payload.get("name")
        return payload.get("name")
    if source == "hn":
        title = payload.get("title")
        if title is None:
            return payload.get("name")  # migrated legacy row
        return _hn_company_name(title)
    if source == "sec_form_d":
        names = payload.get("display_names") or []
        return _CIK_SUFFIX.sub("", names[0]).strip() if names else None
    if source == "product_hunt":
        return (payload.get("name") or "").strip() or None
    return payload.get("name")


def candidate_url(source: str, payload: dict[str, Any]) -> str | None:
    """Whatever URL this source's payload carries for a live-site check.
    ``None`` means "no evidence yet" (e.g. a Show HN self-post, or a Form D
    filing, which never carries a company website)."""
    if source == "github":
        homepage = (payload.get("homepage") or "").strip()
        return homepage or None
    if source == "hn":
        url = (payload.get("url") or "").strip()
        return url or None
    if source == "product_hunt":
        return payload.get("website") or payload.get("url")
    return None  # sec_form_d: no website field exists on a Form D filing


_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SUFFIXES = {
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "corp",
    "corporation", "co", "company", "the",
}


def guessed_domain(name: str) -> str | None:
    """Single best-guess ``.com`` domain from a bare company name — the only
    way a Form-D-only record (no website field at all) can ever reach a
    domain. Verified against the fetched page before being trusted (see
    gate.py); this just proposes a candidate."""
    tokens = [
        t for t in _NON_ALNUM.sub(" ", name.lower()).split() if t not in _SUFFIXES
    ]
    slug = "".join(tokens)
    return f"{slug}.com" if slug else None
