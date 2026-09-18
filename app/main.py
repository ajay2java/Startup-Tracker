"""FastAPI application: Jinja2 + HTMX front end over the companies table.

Single list view, one query, state in the URL (spec §5) — every filter
control (map, stage picker, sector picker) targets the same
``/companies/list`` fragment and pushes the resulting URL, so the page is
always shareable and back-button-safe. The web layer only ever reads
``companies`` (spec §2 step 5); the pipeline that fills it in is triggered
by the two buttons in ``pipeline/run.py``, not by anything in here.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, geo
from .config import STATIC_DIR, TEMPLATES_DIR
from .pipeline import run as pipeline_run
from .pipeline.domain_utils import registrable_domain
from .pipeline.sectors import SECTOR_DESCRIPTIONS

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

app = FastAPI(title="Startup Tracker")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

ALL_SECTORS = [*SECTOR_DESCRIPTIONS.keys(), "Other"]
ALL_STAGES = ["Pre-Seed", "Seed", "Series A", "Series B", "Series C", "Unknown"]
PAGE_SIZE = 30
NEW_DOT_DAYS = 7

with open(STATIC_DIR / "us-map.svg", encoding="utf-8") as _f:
    MAP_SVG = _f.read()

_STATE_ATTR = re.compile(r'data-state="([A-Z]{2})"')


def render_map(state_url, state_page_url) -> str:
    """Inject per-request hx-get links into the static map SVG so every
    state path/circle becomes a filter control targeting the same fragment
    every other picker uses. hx-push-url is set explicitly to the full-page
    URL, not "true" — "true" would push the fragment endpoint itself, which
    has no page shell to reload into."""

    def repl(m: re.Match) -> str:
        code = m.group(1)
        return (
            f'data-state="{code}" hx-get="{state_url(code)}" hx-target="#results" '
            f'hx-swap="innerHTML" hx-push-url="{state_page_url(code)}" '
            f'tabindex="0" role="button"'
        )

    return _STATE_ATTR.sub(repl, MAP_SVG)


def density_css(state_counts: dict[str, int]) -> str:
    """Shade each state's map fill by its share of the current filter's
    busiest state — spec §5: "answering where is this happening outside the
    usual places" when a sector/stage filter is active with no state chosen."""
    if not state_counts:
        return ""
    max_count = max(state_counts.values())
    if max_count == 0:
        return ""
    rules = []
    for state, count in state_counts.items():
        opacity = 0.15 + 0.65 * (count / max_count)
        rules.append(f'.us-map [data-state="{state}"] {{ fill: rgba(37,99,235,{opacity:.2f}); }}')
    return "\n".join(rules)


templates.env.globals["render_map"] = render_map
templates.env.globals["density_css"] = density_css
templates.env.globals["ALL_STAGES"] = ALL_STAGES
templates.env.globals["ALL_SECTORS"] = ALL_SECTORS
templates.env.globals["STATE_CENTROIDS"] = geo.STATE_CENTROIDS
templates.env.globals["DOT_OFFSET"] = geo.DOT_OFFSET
templates.env.globals["SMALL_NORTHEAST_STATES"] = geo.SMALL_NORTHEAST_STATES


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# ---------------------------------------------------------------------------
# Filter state — the entire URL-shareable query, and the query-building
# helpers every clickable filter control in the template uses.
# ---------------------------------------------------------------------------

def _filters(state: str, sector: str, stage: str) -> dict[str, str | None]:
    state = (state or "").strip().upper()
    sector = (sector or "").strip()
    stage = (stage or "").strip()
    return {
        "state": state if state in geo.STATE_CENTROIDS else None,
        "sector": sector if sector in ALL_SECTORS else None,
        "stage": stage if stage in ALL_STAGES else None,
    }


def _url(path: str, filters: dict[str, str | None], **overrides: str | None) -> str:
    merged = {**filters, **overrides}
    query = urlencode({k: v for k, v in merged.items() if v})
    return f"{path}?{query}" if query else path


def _list_url(filters: dict[str, str | None], **overrides: str | None) -> str:
    """Fragment endpoint — what hx-get actually fetches."""
    return _url("/companies/list", filters, **overrides)


def _page_url(filters: dict[str, str | None], **overrides: str | None) -> str:
    """Full-page, shareable URL — what hx-push-url puts in the address bar.
    Must never be the fragment endpoint, or reloading/sharing that link
    would render a bare fragment with no page shell."""
    return _url("/companies", filters, **overrides)


def _toggle(filters: dict, dimension: str, value: str) -> dict[str, str | None]:
    """Clicking an already-active option clears it; otherwise selects it."""
    current = filters.get(dimension)
    return {dimension: None if current == value else value}


def _toggle_url(filters: dict, dimension: str, value: str) -> str:
    return _list_url(filters, **_toggle(filters, dimension, value))


def _toggle_page_url(filters: dict, dimension: str, value: str) -> str:
    return _page_url(filters, **_toggle(filters, dimension, value))


def _new_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=NEW_DOT_DAYS)).isoformat()


def _build_context(request: Request, filters: dict, offset: int) -> dict:
    companies = db.list_companies(filters, limit=PAGE_SIZE, offset=offset)
    total = db.count_companies(filters)
    new_cutoff = _new_cutoff()
    next_offset = offset + PAGE_SIZE
    has_more = next_offset < total
    next_page_url = (
        f"/companies/rows?{urlencode({**{k: v for k, v in filters.items() if v}, 'offset': next_offset})}"
        if has_more else None
    )

    stage_counts = db.stage_counts(filters)
    sector_counts = db.sector_counts(filters)
    state_counts = db.state_counts(filters)
    stage_new = db.new_dot_counts(filters, "stage", new_cutoff)
    sector_new = db.new_dot_counts(filters, "sector", new_cutoff)
    state_new = db.new_dot_counts(filters, "state", new_cutoff)

    chips = []
    if filters.get("state"):
        chips.append({
            "label": filters["state"],
            "remove_url": _list_url(filters, state=None),
            "remove_page_url": _page_url(filters, state=None),
        })
    if filters.get("sector"):
        chips.append({
            "label": filters["sector"],
            "remove_url": _list_url(filters, sector=None),
            "remove_page_url": _page_url(filters, sector=None),
        })
    if filters.get("stage"):
        chips.append({
            "label": filters["stage"],
            "remove_url": _list_url(filters, stage=None),
            "remove_page_url": _page_url(filters, stage=None),
        })

    return {
        "request": request,
        "filters": filters,
        "companies": companies,
        "total": total,
        "shown_so_far": offset + len(companies),
        "has_more": has_more,
        "next_page_url": next_page_url,
        "new_cutoff": new_cutoff,
        "chips": chips,
        "stage_counts": stage_counts,
        "sector_counts": sector_counts,
        "state_counts": state_counts,
        "stage_new": stage_new,
        "sector_new": sector_new,
        "state_new": state_new,
        "self_url": _list_url(filters),
        "stage_url": lambda s: _toggle_url(filters, "stage", s),
        "sector_url": lambda s: _toggle_url(filters, "sector", s),
        "state_url": lambda s: _toggle_url(filters, "state", s),
        "stage_page_url": lambda s: _toggle_page_url(filters, "stage", s),
        "sector_page_url": lambda s: _toggle_page_url(filters, "sector", s),
        "state_page_url": lambda s: _toggle_page_url(filters, "state", s),
        "clear_url": "/companies/list",
        "clear_page_url": "/companies",
    }


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    return companies_page(request, state="", sector="", stage="")


@app.get("/companies", response_class=HTMLResponse)
def companies_page(
    request: Request,
    state: str = Query(default=""),
    sector: str = Query(default=""),
    stage: str = Query(default=""),
):
    filters = _filters(state, sector, stage)
    ctx = _build_context(request, filters, offset=0)
    return templates.TemplateResponse(request, "index.html", ctx)


@app.get("/companies/list", response_class=HTMLResponse)
def companies_list(
    request: Request,
    state: str = Query(default=""),
    sector: str = Query(default=""),
    stage: str = Query(default=""),
):
    filters = _filters(state, sector, stage)
    ctx = _build_context(request, filters, offset=0)
    return templates.TemplateResponse(request, "_results.html", ctx)


@app.get("/companies/rows", response_class=HTMLResponse)
def companies_rows(
    request: Request,
    state: str = Query(default=""),
    sector: str = Query(default=""),
    stage: str = Query(default=""),
    offset: int = Query(default=0),
):
    """Infinite-scroll continuation — rows only, no chrome (spec §5:
    ``hx-trigger="revealed"`` on the last row)."""
    filters = _filters(state, sector, stage)
    ctx = _build_context(request, filters, offset=offset)
    return templates.TemplateResponse(request, "_rows.html", ctx)


# ---------------------------------------------------------------------------
# Manual add — bypasses the gate entirely (spec §3), still goes through
# derive on the next pipeline run since enriched_at is left NULL.
# ---------------------------------------------------------------------------

@app.post("/companies", response_class=HTMLResponse)
def add_company(request: Request, url: str = Form(...), notes: str = Form(default="")):
    url = url.strip()
    domain = registrable_domain(url if "://" in url else f"https://{url}")

    result = {"ok": False, "message": ""}
    if not domain:
        result["message"] = "Couldn't extract a domain from that URL."
    else:
        with db.get_conn() as conn:
            existing = db.get_company_by_domain(conn, domain)
            if existing:
                company_id = existing["id"]
                result["message"] = f"{domain} is already tracked — reopened that record."
            else:
                company_id = db.insert_company(conn, domain)
                result["ok"] = True
                result["message"] = f"Added {domain}. It'll get a name/sector on the next pipeline run."
            db.insert_raw_record(
                conn, "manual", f"manual:{domain}",
                {"url": url, "notes": notes.strip() or None},
                gate_status="promoted",
            )
            raw_id = conn.execute(
                "SELECT id FROM raw_records WHERE source='manual' AND source_id=?",
                (f"manual:{domain}",),
            ).fetchone()["id"]
            db.link_raw_to_company(conn, raw_id, company_id)
            db.touch_company(conn, company_id)

    response = templates.TemplateResponse(request, "_add_result.html", {"request": request, "result": result})
    response.headers["HX-Trigger"] = "companies-updated"
    return response


# ---------------------------------------------------------------------------
# Pipeline buttons (spec: no background scheduler for a single-user tool —
# see ARCHITECTURE.md decisions)
# ---------------------------------------------------------------------------

@app.post("/pipeline/run", response_class=HTMLResponse)
async def run_pipeline_route(request: Request):
    summary = await pipeline_run.run_pipeline()
    response = templates.TemplateResponse(
        request, "_pipeline_result.html", {"request": request, "summary": summary, "action": "Pipeline run"}
    )
    response.headers["HX-Trigger"] = "companies-updated"
    return response


@app.post("/pipeline/recheck", response_class=HTMLResponse)
async def recheck_holds_route(request: Request):
    summary = await pipeline_run.run_recheck()
    response = templates.TemplateResponse(
        request, "_pipeline_result.html", {"request": request, "summary": summary, "action": "Hold recheck"}
    )
    response.headers["HX-Trigger"] = "companies-updated"
    return response
