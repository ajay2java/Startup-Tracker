# Startup Tracker App — Project Spec

## Purpose
A personal tool to discover up-and-coming US startups before mainstream tech
press (e.g. TechCrunch) or VCs catch on. Built for one user, run locally.

## Scope & Constraints
- Personal, lightweight tool — runs locally, not a hosted/public product.
- US-based startups only.
- Zero cost — no paid APIs or services.

## Tech Stack
- **Backend:** Python, FastAPI
- **Templates:** Jinja2
- **Frontend interactivity:** HTMX (partial page swaps, no full React app)
- **Database:** SQLite

## Data Sources (v1 — all free)
- **SEC EDGAR (Form D filings)** — public, no API key needed. Primary/most
  reliable source; build this fetcher first.
- **GitHub API** — free personal access token required (read from `.env`,
  never hardcoded/committed).
- **Hacker News API** (Show HN / Launch HN) — public, no key needed.
- **Product Hunt API** — free developer token required (read from `.env`).
- **Optional/manual, later:** scraping state Secretary of State filing sites
  and accelerator/university demo day pages for regional coverage.
- **Explicitly out of scope for v1:** Crunchbase, PitchBook (paid tiers only).

## Database Schema

```sql
startups
├── id            INTEGER PRIMARY KEY
├── name          TEXT NOT NULL
├── url           TEXT
├── description   TEXT
├── state         TEXT              -- e.g. "AL", "VT", "unknown"
├── stage         TEXT              -- "pre-seed", "seed", "series_a", "unknown"
├── source        TEXT              -- "sec_form_d", "github", "product_hunt", "hn", "manual"
├── first_seen    DATE
├── last_checked  DATE
├── signal_score  INTEGER           -- nullable, added in a later phase
├── notes         TEXT
├── starred       BOOLEAN DEFAULT 0

startup_sectors (many-to-many — a startup can have multiple sector tags)
├── startup_id    INTEGER → startups.id
├── sector        TEXT              -- "agtech", "climatetech", "biotech", "edtech",
                                      -- "spacetech", "foodtech", etc.
```

## Core Features (v1)

1. **Manual refresh button** — pulls new startups on demand. No scheduled/
   background polling in v1.
2. **Manual "add a startup"** — lets the user add companies the pipeline
   misses.
   - Minimal required fields: name + URL/description. Other fields can be
     backfilled later.
   - Marked distinctly with `source: manual` vs. pipeline-sourced entries.
   - Goes through the same duplicate-matching logic as pipeline results
     (see below) before being inserted.
3. **Sector/category tagging** — multi-tag capable (a company can be both
   climatetech and hardware). Filterable by category.
4. **Stage tagging** — pre-seed, seed, Series A, etc. "unknown" is a valid
   value when data doesn't specify. Filterable by stage.
5. **US-only filter** — hard filter, not a toggle. SEC Form D has a state
   field for this; sources without location data get `state: unknown` rather
   than a guess.
6. **Composite signal score** — later phase, not v1. Will combine signals
   like Form D filed, job posting spikes, GitHub star velocity, etc.

## Duplicate Matching Logic
- **Exact match first** — match on name + state, or a stable source ID when
  available (e.g. SEC filer ID).
- **Fuzzy match as fallback** — use a library like `rapidfuzz` to catch
  near-duplicate names (e.g. "Acme Inc." vs. "Acme, Inc.") when no exact
  match is found.
- Applies to **both** pipeline-sourced results and manual adds — manual adds
  should never bypass this check.

## Refresh Flow (end-to-end)

1. **User clicks refresh** (frontend) — HTMX sends `POST /refresh`; a loading
   indicator shows on the button/table.
2. **Query each source concurrently** (backend) — use FastAPI's async
   support to fetch from SEC EDGAR, GitHub, Hacker News, and Product Hunt at
   the same time rather than sequentially. Each source has its own fetcher
   function.
   - **Failure handling:** if a source fetch fails (e.g. GitHub API down or
     rate-limited), it must NOT block or crash the refresh. Log the failure,
     continue with the other sources, and surface a **visible but
     non-blocking warning** in the UI (e.g. "GitHub fetch failed — showing
     partial results").
3. **Normalize results** — map each source's raw response into a common
   shape (name, url, description, state, source, raw_date) before matching.
4. **Match against existing DB** — run exact-match first, then fuzzy-match
   fallback, for every normalized candidate. Bucket each as "new" or
   "existing (matched to row X)".
5. **Apply hybrid update logic:**
   - **New candidates** → insert as new rows; set `first_seen` and
     `last_checked` to today; set `source` to the originating pipeline.
   - **Existing matches** → update `last_checked` to today. If `stage`
     differs from the stored value, log the change (note or simple
     change-log) and update it. Other fields (description, url) update
     silently, no logging needed.
6. **Return a summary to the frontend** — e.g. "12 new startups found, 3
   existing companies updated" — plus any source-failure warnings.
7. **Frontend re-renders** — HTMX swaps in the updated table/rows. No full
   page reload.

## Build Order (recommended)
1. SQLite schema + a basic Jinja2/HTMX page that renders the (empty)
   `startups` table.
2. Manual "add a startup" form, wired to the DB — gives an early sanity
   check that frontend ↔ backend ↔ DB are connected correctly.
3. SEC EDGAR fetcher (most reliable, keyless, structured) — get one source
   working fully end-to-end (fetch → normalize → dedup → insert → display)
   before adding the others.
4. GitHub, Hacker News, and Product Hunt fetchers, one at a time.
5. Filters (sector, stage, US-only) and the refresh summary/warning UI.
6. Sector auto-tagging and composite signal score — later phases, not v1.

## Explicitly Out of Scope for v1
- Scheduled/background refresh (manual button only for now).
- Sector auto-tagging via LLM (manual tagging is fine for v1).
- Composite signal scoring.
- Paid data sources (Crunchbase, PitchBook).
- React frontend / hosted multi-user deployment.
