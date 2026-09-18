"""Pipeline orchestration — the two buttons the UI exposes.

"Run pipeline" is fetch -> gate -> dedupe -> derive, the full spec §2 flow
for anything newly fetched. "Recheck holds" re-applies the gate to hold
records whose ``recheck_at`` has arrived, then runs dedupe/derive over
whatever that promotes (spec §3 layer 4). Both are manual buttons rather
than a background scheduler — this is a single-user tool started by hand,
not a long-running service (see ARCHITECTURE.md decisions).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

from .. import db
from ..sources import github, hacker_news, product_hunt, sec_edgar
from . import dedupe, derive, gate

log = logging.getLogger(__name__)

_UA = "Startup Tracker (personal research tool)"
FETCHERS = [
    ("sec_form_d", sec_edgar.fetch),
    ("github", github.fetch),
    ("hn", hacker_news.fetch),
    ("product_hunt", product_hunt.fetch),
]


@dataclass
class PipelineSummary:
    fetched_per_source: dict[str, int] = field(default_factory=dict)
    fetch_warnings: list[str] = field(default_factory=list)
    gate: gate.GateSummary | None = None
    dedupe: dedupe.DedupeSummary | None = None
    derive: derive.DeriveSummary | None = None


async def _run_fetch(client: httpx.AsyncClient, name: str, fn) -> tuple[str, int, str | None]:
    try:
        result = await fn(client)
    except Exception as exc:  # a source must never take down the pipeline
        log.exception("source %s crashed", name)
        return name, 0, f"{name} crashed: {exc}"

    if not result.ok:
        return name, 0, result.warning

    with db.get_conn() as conn:
        for item in result.items:
            db.insert_raw_record(conn, result.source, item.source_id, item.payload)
    return name, len(result.items), None


async def run_fetch_all() -> tuple[dict[str, int], list[str]]:
    async with httpx.AsyncClient(
        headers={"User-Agent": _UA}, follow_redirects=True
    ) as client:
        results = await asyncio.gather(
            *(_run_fetch(client, name, fn) for name, fn in FETCHERS)
        )
    counts: dict[str, int] = {}
    warnings: list[str] = []
    for name, count, warning in results:
        counts[name] = count
        if warning:
            warnings.append(warning)
    return counts, warnings


async def run_pipeline() -> PipelineSummary:
    counts, warnings = await run_fetch_all()
    summary = PipelineSummary(fetched_per_source=counts, fetch_warnings=warnings)
    summary.gate = await gate.run_gate()
    summary.dedupe = dedupe.run_dedupe()
    summary.derive = await derive.run_derive()
    return summary


async def run_recheck() -> PipelineSummary:
    summary = PipelineSummary()
    summary.gate = await gate.recheck_holds()
    summary.dedupe = dedupe.run_dedupe()
    summary.derive = await derive.run_derive()
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    db.init_db()
    result = asyncio.run(run_pipeline())
    print(f"fetched: {result.fetched_per_source}")
    if result.fetch_warnings:
        print(f"warnings: {result.fetch_warnings}")
    print(f"gate: {result.gate}")
    print(f"dedupe: {result.dedupe}")
    print(f"derive: {result.derive}")

