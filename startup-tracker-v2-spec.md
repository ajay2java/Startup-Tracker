# Startup Tracker — v2 build spec

## 1. Data model

### `raw_records` — everything ingested, never deleted

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `source` | text | `hn`, `github`, `sec_formd`, `producthunt` |
| `source_id` | text | source's own ID; unique with `source` |
| `payload` | JSON | verbatim API response, unmodified |
| `fetched_at` | timestamp | UTC |
| `gate_status` | text | `pending`, `promoted`, `rejected`, `hold` |
| `gate_reason` | text | e.g. `not_org`, `name_too_long`, `no_website`, `org_too_old` |
| `recheck_at` | date | set for `hold` only; 30 days out |
| `company_id` | int FK | set when promoted |

Unique index on `(source, source_id)` so re-fetching the same record is a no-op.

### `companies` — things that passed the gate

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `domain` | text UNIQUE | registrable domain, lowercased, no `www` — this is the real key |
| `name` | text | from `og:site_name`, falling back to title/domain |
| `description` | text | from meta description chain; nullable |
| `sector` | text | one of 12 enum values |
| `uses_ai` | bool | keyword flag, orthogonal to sector |
| `stage` | text | `Pre-Seed`/`Seed`/`Series A`/`Series B`/`Series C`/`Unknown` |
| `state` | char(2) | nullable |
| `first_seen` | timestamp | drives the "new" dots; never updated |
| `updated_at` | timestamp | bumped when new evidence merges in |
| `enriched_at` | timestamp | null = needs enrichment |
| `enrich_version` | int | bump to force re-derivation after a rule change |
| `desc_quality` | text | `good`/`weak`/`missing` — drives UI fallback |

Indexes on `state`, `stage`, `sector`, `first_seen`.

---

## 2. Pipeline

Five stages, each reading and writing a persisted table. Any stage can be re-run alone.

```
fetch → raw_records → gate → dedupe on domain → companies → derive fields → serve
```

1. **Fetch.** Each source writes verbatim into `raw_records` with `gate_status = pending`. No cleaning here.
2. **Gate.** Reads pending records, stamps each `promoted` / `rejected` / `hold` with a reason code.
3. **Dedupe.** Promoted records normalize to a registrable domain. Domain exists → merge new evidence onto that company, bump `updated_at`. Domain doesn't exist → insert.
4. **Derive.** Worker selects `enriched_at IS NULL OR updated_at > enriched_at OR enrich_version < CURRENT`, fetches each homepage once, extracts name/description/sector/`uses_ai`, writes back.
5. **Serve.** Web layer only ever reads `companies`.

---

## 3. Gate rules

Cheapest checks first. Every outcome writes a `gate_reason`.

**Layer 1 — structural (free)**
- GitHub: reject unless `type == "Organization"` → kills topic tags and personal accounts
- HN: stories only, matching Show HN / Launch HN patterns
- Name > 60 chars → reject (`name_too_long`) — it's a post title
- Name contains comma followed by a verb phrase → reject

**Layer 2 — not-an-early-startup (structural proxies, not a name denylist)**
- GitHub org created > 6 years ago → reject
- Domain registered > 7 years ago → reject
- GitHub org over follower/repo threshold → reject
- Small denylist only for what rules can't catch: foundations, standards bodies, OSS collectives (apache, cncf, rust-lang), universities

**Layer 3 — evidence (the hard requirement)**
- Must resolve to a live site on its own registrable domain. Not github.io, not a personal blog, not a social link.
- Fails this but passed 1 and 2 → **hold**, not reject.

**Layer 4 — hold queue**
- `recheck_at = today + 30 days`. Re-run the gate on holds each night. A Show HN post with no domain today may have one in six weeks — and catching it then is still early.

Thresholds above are starting values. Read the reject log daily for the first two weeks; that's how you find you're discarding good companies.

**Manual adds bypass the gate entirely.** Carried over from v1. Entering a domain writes `source = 'manual'`, `gate_status = 'promoted'` and inserts directly into `companies` — the gate filters records you didn't choose, and a manual add is already your judgment. It still runs through derivation (step 4) so it gets a description and sector like anything else. Existing domain → open that record instead of creating a duplicate. This doubles as the override for promoting something out of the hold queue early.

---

## 4. Field derivation (no LLM)

All local. One homepage fetch per company, cached in `raw_records` so a rule change re-derives without re-fetching.

### Name
1. `og:site_name`
2. `<title>`, split on `|` / `–` / `-`, take the shortest segment
3. Domain minus TLD, title-cased

### Description
Fallback chain, first non-empty wins:
1. `<meta name="description">`
2. `og:description`
3. First `<h1>` + immediately following `<p>` or subhead

Then score it into `desc_quality`:
- `missing` — nothing in the chain
- `weak` — under 40 chars, or matches a fluff pattern (`reimagin`, `the future of`, `next generation`, `revolutioniz`, `empower`, `seamless`) without naming a product or verb
- `good` — everything else

Weak and missing descriptions aren't hidden. The UI shows the domain and a "no description" state, and they sort last within a filter set. Seeing the gaps honestly beats filling them with something invented.

### Sector
Local sentence embeddings, not keywords. Install `sentence-transformers`, use `all-MiniLM-L6-v2` (~90MB, CPU-fine).

1. Write a two-sentence description of each of the 12 sectors, embed once, cache the vectors
2. Embed the company's homepage text
3. Cosine similarity against all 12; take the highest
4. If top score < 0.35, or top two are within 0.03 of each other, assign `Other`

Enum: `BioTech`, `Agtech`, `ClimateTech`, `DeepTech`, `Cybersecurity`, `Industrial Tech`, `DefenseTech`, `EdTech`, `Enterprise Software`, `Consumer Tech`, `FinTech`, `Other`

The thresholds are the tuning surface. Start strict — over-assigning `Other` is recoverable, a confidently wrong sector is not.

### `uses_ai`
Keyword match on homepage text (`machine learning`, `LLM`, `neural`, `AI-powered`, `foundation model`). Crude, but it's a flag not a category, so a false positive costs nothing.

### Stage and state
Deterministic from Form D. State is the issuer address. Stage is approximated from total offering amount plus filing sequence. No Form D → `Unknown` and null. Never guessed from any other source.

### Second-chance company check
The gate can't see page content. After fetching, drop back to `hold` if the site has no pricing/about/contact link **and** is a single page **and** has no `og:site_name`. Catches portfolio sites and OSS project pages that cleared the gate on metadata alone.

---

## 5. UI

### Filter composition

One list view, one query, state in the URL:

```
/companies?state=OH&sector=climatetech&stage=seed
```

- Map, stage picker, and sector picker each set one parameter and re-render the same list fragment
- Filters **add**, never replace — clicking a sector while a state is active keeps the state
- Active filters render as removable chips
- Picker counts reflect the current filter set (ClimateTech shows `3` within Ohio, not `340` globally) — this is what prevents dead-end clicks
- No filters = national list, not an empty prompt to choose something
- HTMX: `hx-get` to the same endpoint, `hx-push-url="true"` for back-button and shareable URLs

### Map behavior

- Persistent chrome at the top, not a first step — so you can enter from any direction
- Selecting a state collapses the map to a compact strip with a removable chip; clicking the strip expands it back. Same component, two heights, one CSS class flip.
- With a sector or stage filter active and no state selected, the map shades by that filter's density — answering "where is this happening outside the usual places"

### "New" dots

- A state gets a dot if it has any company with `first_seen` within the last 7 days
- Time-boxed and self-clearing: no accounts, no per-user seen-state, and dots can't accumulate permanently
- Show the count inside the dot when > 1 — same pixels, tells you whether to click
- Same 7-day rule drives dots on the stage and sector boxes
- Position at state centroid, offset up-and-right to clear the state abbreviation
- **Small northeastern states** (RI, DE, CT, NJ, MA, MD, DC): centroids are too small to hold a dot without bleeding onto neighbors. Use the standard offset stack to the right of the map — labeled boxes in a vertical column with leader lines.
- Wrap the pulse animation in `@media (prefers-reduced-motion: reduce)` with a static dot fallback

### List rows

- Left gutter column reserved for the new-dot so rows stay aligned whether or not they're new, and the column can be scanned vertically
- Name, domain, one-line description, then sector / stage / state badges
- `4 of 340` count near the filter chips — this is the feedback that makes composing filters feel productive
- Infinite scroll via `hx-trigger="revealed"` on the last row

---

## 6. Build order

1. `raw_records` table + rewrite fetchers to write verbatim into it
2. Gate layers 1–3 with reason codes. Run it over existing v1 data and read the reject log.
3. Domain-based dedupe, replacing name-fuzzy as the primary path
4. Deterministic state and stage extraction from Form D
5. Derivation worker (name, description, sector) + backfill over existing rows
6. Filter-composition query layer and URL state
7. Map, pickers, collapsed strip
8. New-dot logic
9. Hold-queue recheck job

Steps 1–3 are what actually fix your v1 output quality. Step 5 makes it readable. Everything after is interface.

No API keys, no secrets to manage, no network dependency beyond the sources themselves, no per-record cost. The whole thing runs offline on your machine.

---
