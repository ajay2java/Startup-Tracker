# Startup Tracker

Interface:
<img width="932" height="485" alt="image" src="https://github.com/user-attachments/assets/b7741e75-008e-4fdb-9b62-3e4dca6ee5f4" />

A personal, local tool for spotting up-and-coming **US** startups before the
mainstream tech press or VCs catch on. One user, runs on your machine, zero
paid services.

Built to the spec in [`startup-tracker-v2-spec.md`](startup-tracker-v2-spec.md)
(v1 spec: [`specs.md.md`](specs.md.md)).

## Stack

| Layer      | Choice                          |
|------------|---------------------------------|
| Backend    | Python 3.12, FastAPI            |
| Templates  | Jinja2                          |
| Front end  | HTMX 2 (vendored, no build step)|
| Database   | SQLite (`data/startups.db`)     |
| HTTP       | httpx (async)                   |
| Domain parsing | tldextract                  |
| HTML parsing | BeautifulSoup4 + lxml         |
| Sector classification | fastembed (local ONNX embeddings, `all-MiniLM-L6-v2`) |

## Pipeline

Five persisted stages, each independently re-runnable:

```
fetch → raw_records → gate → dedupe on domain → companies → derive → serve
```

- **Fetch** (`app/sources/`) — each source writes its raw payload verbatim
  into `raw_records`. No cleaning at this stage.
- **Gate** (`app/pipeline/gate.py`) — four layers (structural, not-an-early-
  startup proxies, live-site evidence, hold queue), every outcome stamped
  with a `gate_reason`.
- **Dedupe** (`app/pipeline/dedupe.py`) — promoted records collapse onto
  `companies` by registrable domain, the dedupe key.
- **Derive** (`app/pipeline/derive.py`, `sectors.py`, `stage.py`) — one
  homepage fetch per company, cached, drives name/description/sector/
  `uses_ai`; state/stage come deterministically from linked Form D filings.
- **Serve** (`app/main.py`) — the web layer only ever reads `companies`.

## Data sources (all free)

| Source            | Key needed?                    | Notes |
|-------------------|--------------------------------|-------|
| SEC EDGAR Form D  | No                             | Full-text search + each filing's `primary_doc.xml` for offering amount. Pooled investment funds filtered out. |
| GitHub            | Recommended (`GITHUB_TOKEN`)   | Recently created, fast-growing repos; org age/size checked in the gate. |
| Hacker News       | No                             | "Show HN" and "Launch HN" posts via the Algolia API. |
| Product Hunt      | Yes (`PRODUCT_HUNT_TOKEN`)     | Newest launches via API v2 (GraphQL). Skipped with a visible warning if no token. |

A source failing (down, rate-limited, missing token) never blocks a
pipeline run — the others still run and the UI shows a non-blocking warning.


## Using it

- **Run pipeline** — fetch → gate → dedupe → derive, end to end.
- **Recheck holds** — re-applies the gate to hold records whose 30-day
  `recheck_at` has arrived (spec's hold-queue layer).
- **Add a company manually** — a URL is the only requirement; it bypasses
  the gate (your judgment already vetted it) but still goes through derive.
  An existing domain reopens that record instead of duplicating it.
- **Filters** — state (map or the small-state side boxes), sector, stage;
  all compose (AND), state lives in the URL, and picker counts reflect the
  other active filters so composing never dead-ends.
- **New dots** — any state/sector/stage with a company first seen in the
  last 7 days gets a self-clearing dot; no accounts, no per-user state.

No API keys required for the core sources, no per-record cost, everything
except the one-time model download and each source's own network calls runs
locally.

## Project layout

```
app/
  main.py            FastAPI app + routes, filter-composition query building
  db.py              SQLite schema (raw_records, companies, lookup_cache) + all SQL
  config.py          paths, .env loading
  geo.py             US state centroids for the map
  pipeline/
    gate.py          four-layer gate, reason codes, hold-queue recheck
    dedupe.py        domain-based merge into companies
    derive.py        homepage fetch/cache, name/description/uses_ai, second-chance check
    sectors.py       local sentence-embedding sector classification
    stage.py         deterministic Form D stage/state extraction
    domain_utils.py  registrable-domain extraction, platform-domain denylist
    candidate.py     per-source name/URL extraction shared by gate + dedupe
    run.py           pipeline orchestration (the two UI buttons)
  sources/
    sec_edgar.py, github.py, hacker_news.py, product_hunt.py — fetch only,
    write verbatim payloads
  templates/         Jinja2 + HTMX partials
  static/            app.css, htmx.min.js, us-map.svg (vendored)
scripts/
  migrate_v1.py      one-time v1 -> v2 migration
data/
  startups.db        created on first run (gitignored)
```

