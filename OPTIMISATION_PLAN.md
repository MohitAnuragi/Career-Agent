# Optimisation Plan: fresh jobs, more sources, more cities

Status: **Phases 1–5 implemented** on branch `feat/freshness-cities-sources` (2026-11-20). Phase 6 (Keka / Darwinbox / Zoho
feeds, contacting the map owners) is still open. Decisions taken: undated jobs hidden by default (toggle to show),
one "Delhi NCR" chip, keep India-remote and worldwide-remote roles. Not done: an `hq_city` fallback, because a posting
that only says "India" can be in any city.

Three goals:

1. **Freshness.** Show only openings posted in the **last 14 days** ("this week" and "last week"). Hide anything 3+ weeks old by default.
2. **More sources.** Go beyond Greenhouse/Lever/Ashby and 33 hand-picked boards. Reach hundreds of Indian startups, using lists like the Bangalore and Hyderabad startup maps to *discover* companies.
3. **More cities.** Treat Bengaluru, Hyderabad, Pune, Delhi NCR (Delhi, Gurugram, Noida), Mumbai, Chennai and others as proper filters. Today a city filter is a substring match on the location text.

---

## 0. Where things stand today

| Area | Current behaviour | Problem |
|---|---|---|
| Sources | 3 ATS APIs ([sources.py](app/ingest/sources.py)), 33 boards in [boards.json](app/ingest/boards.json); 18 of them are Greenhouse, mostly global companies | Few Indian startups are covered |
| Posted date | `posted_at` is stored, but no query filters on it | Jobs that are months old rank next to jobs posted yesterday |
| Greenhouse date | `first_published or updated_at` | `updated_at` changes whenever a recruiter edits a posting, so an old job can look brand new |
| Ingest filter | Keeps a job if `is_india or is_remote` ([pipeline.py](app/ingest/pipeline.py) `keep()`) | Global "Remote" roles that are US-only get in |
| City filter | `location ILIKE ANY(...)` plus a small alias table ([search.py](app/search.py) `CITY_ALIASES`) | No Delhi NCR grouping (Gurugram and Delhi are separate groups), no multi-city facet, no index |
| Discovery | Each company is added to boards.json by hand | Doesn't scale beyond a few dozen companies |

---

## 1. Freshness: show only the last 2 weeks

### 1.1 One trustworthy "posted" date per job

Add an `effective_posted_at` column filled at ingest:

| Source | Use | Avoid |
|---|---|---|
| Greenhouse | `first_published` | `updated_at` (an edit makes it look new) |
| Lever | `createdAt` | |
| Ashby | `publishedAt` | |
| New ATSes (section 2) | Their published/released date field | Any "updated" or "modified" field |
| No date from the source | `first_seen_at`, **only if the board had already been ingested before this run** | `first_seen_at` on a board's first ingest. Every job on a new board gets `first_seen_at = now()`, so a 3-month-old job would look fresh |

Also store `posted_at_source`: `'ats' | 'first_seen' | 'unknown'`. Jobs marked `'unknown'` are hidden by default.

To know whether a board is new, add a small `boards` table (section 4.2) with a `first_ingested_at` column.

### 1.2 Filter on both sides: ingest and query

- **At ingest:** drop jobs whose `effective_posted_at` is older than **30 days**. Don't store or embed them. This saves the most work once there are hundreds of boards, because most of an ATS board's listings are old.
  The 30-day buffer means the query window (7, 14 or 30 days) can change without re-ingesting.
- **At query time:** add `posted_within_days: int | None = 14` to `SearchFilters` and one clause in `_where()`:
  ```sql
  effective_posted_at >= now() - make_interval(days => %s)
  ```
  Add the index `CREATE INDEX jobs_fresh_idx ON jobs (is_active, effective_posted_at DESC);`.
- **Nightly expiry:** set `is_active = FALSE` on jobs older than 30 days, even if they are still on the board.

### 1.3 Product surface

- **UI:** a "Posted" filter with *Last 7 days* / *Last 14 days (default)* / *Last 30 days*, plus a badge on each card: `This week` (0–7 days) or `Last week` (8–14 days). [app.js](app/static/app.js) already renders `ago(posted_at)`, so only the badge is new.
- **Sort option:** *Best match* (default) or *Newest first*.
- **Ranking:** a small boost for jobs 3 days old or less in `preference_boost`, so new jobs rise among equally relevant ones. It must stay small, or a weak match that is one day old would outrank a strong match that is ten days old.
- **API and agent:** `/api/search` and `/api/recommendations` accept `posted_within_days`. The `search_jobs` tool gets the same argument, so "jobs from this week only" works in chat. `update_preferences` can store a default window.
- **Stats:** `/api/stats` reports counts for this week and last week, so the header can say "1,240 new this week".

### 1.4 Things that will break, and the fix for each

- **Retrieval eval:** the frozen snapshot ([evals/data/snapshot.jsonl.gz](evals/data/snapshot.jsonl.gz)) is older than 14 days, so a default filter would return nothing. The eval must pass `posted_within_days=None`.
- **Tests:** fixtures with fixed `posted_at` dates would be filtered out. Either use dates relative to `now()` or pass `None` in ranking tests. Add tests for the Greenhouse `updated_at` case and the first-ingest `first_seen_at` case.
- **Saved jobs:** a saved job past 14 days must still appear in "Saved". Apply the window to search and the feed, not to `list_saved_jobs`.

---

## 2. More sources: official ATS APIs beyond Greenhouse

Keep the project's rule: **only official, public, unauthenticated job-board APIs, no HTML scraping of job sites.** Each new source gets a `*_url` + `normalize_*` pair in [sources.py](app/ingest/sources.py) and an entry in `SOURCES`, the same as the current three.

### 2.1 Candidate ATS providers

| Provider | Public endpoint (check against one real board before coding) | Posted-date field | India relevance | Priority |
|---|---|---|---|---|
| **Workable** | `https://apply.workable.com/api/v1/widget/accounts/{slug}` | `published_on` | Medium: many Indian SaaS and D2C startups | P1 |
| **SmartRecruiters** | `https://api.smartrecruiters.com/v1/companies/{id}/postings` (supports `?country=in`) | `releasedDate` | High: large Indian and MNC employers | P1 |
| **Recruitee** | `https://{slug}.recruitee.com/api/offers/` | `published_at` | Low to medium | P2 |
| **Keka Hire** | No documented public API; careers pages at `{company}.keka.com/careers` | ? | **Very high**: one of the most common ATSes at Indian startups | P2, investigate |
| **Darwinbox / Zoho Recruit / Freshteam** | No stable public posting API that I know of | ? | High | P3, only if an official feed exists |
| Wellfound, LinkedIn, Naukri, Instahyre, Cutshort | Not allowed: their terms forbid scraping and there's no public API | — | — | **Don't** |

**Note on Keka, Darwinbox and Zoho:** many Bengaluru and Hyderabad startups use these, not Greenhouse. Before writing any code for them, confirm there is an official public JSON feed and that the terms allow use. If there isn't one, list those companies with a "careers page" link and no ingested jobs, rather than scraping them.

### 2.2 Effort per new source

About 40–60 lines each: fetch, normalise (title, location, department, URL, description HTML→text, posted date, remote flag), add an `is_india` check, add a fixture-based test in [tests/test_normalize.py](tests/test_normalize.py). The pipeline, hashing, expiry and embedding code doesn't change.

---

## 3. Discovery: getting from 33 companies to 300+

### 3.1 What the two linked sites are

| Site | What it shows | Rules for automated access |
|---|---|---|
| [bangalorestartupmap.com](https://www.bangalorestartupmap.com/) | ~1,095 listings (startups and VCs), filterable by area, stage and sector; headline "13,794 jobs" | robots.txt allows `/` but **disallows `/api/`**. Data loads client-side |
| [hyderabadstartupsmap.lol](https://hyderabadstartupsmap.lol/) | "Companies hiring / openings" list (client-rendered, empty when fetched) | robots.txt **disallows `/api/`**, blocks AI crawlers (including ClaudeBot) by name, and sets `ai-train=no, use=reference` |

**Decision: use these maps as a reference list of company names, not as a job source.**
- Don't call their `/api/` endpoints and don't scrape their job listings. robots.txt forbids it, and their job data almost certainly comes from the same company ATS pages we can read directly.
- Use them (and similar lists) to find out *which companies exist*, then ingest each company's jobs from its **own official ATS board**. That also gives the real posted date, which section 1 needs.
- Better option: email the site owners and ask for a company export or a partnership. Both sites have a "Submit" flow, so they curate their data and may share it.

Other legitimate company lists: the YC company directory filtered to India, the Inc42 / Entrackr funding round-ups (already shown on the Bangalore map), NASSCOM lists, and your own target-company list.

### 3.2 ATS auto-detection script (`scripts/discover_boards.py`)

Input: a CSV of `company, website`, filled by hand from the maps or other lists. For each company:

1. **Guess slugs** (`razorpay`, `razorpaysoftware`, ...) and probe the public APIs of Greenhouse, Lever, Ashby, Workable, SmartRecruiters and Recruitee. A hit is an HTTP 200 with at least one job.
2. **Check the careers page:** fetch `website/careers` (one request, respecting the company's robots.txt) and regex for known ATS links: `boards.greenhouse.io/<slug>`, `job-boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`, `apply.workable.com/<slug>`, `careers.smartrecruiters.com/<id>`, `<slug>.recruitee.com`, `*.keka.com`, `*.darwinbox.in`, `zohorecruit`.
3. **Check it's the right company:** slugs collide (a `zeta` board may not be Zeta India). Compare the company name and website domain in the board payload where available.
4. **Output** proposed `boards.json` lines with total jobs, India jobs and jobs from the last 14 days. A person reviews and merges them. Nothing is added automatically.

Target: **300–600 boards**, weighted toward Bengaluru, Hyderabad, Pune, NCR and Mumbai startups.

### 3.3 boards.json additions

```json
{"source": "lever", "board": "razorpay", "company": "Razorpay",
 "hq_city": "Bengaluru", "discovered_from": "bangalorestartupmap", "added": "2026-11-20"}
```
`hq_city` is a fallback when a posting only says "India".

---

## 4. Cities: real filters

### 4.1 Canonical city list

One table in `app/ingest/cities.py`, used by ingest, search, the UI and the agent:

| Canonical | Aliases / areas matched |
|---|---|
| Bengaluru | bangalore, bengaluru, blr |
| Hyderabad | hyderabad, secunderabad, hitec city, gachibowli |
| Pune | pune, poona, hinjewadi |
| **Delhi NCR** | delhi, new delhi, ncr, **gurugram, gurgaon, noida, greater noida, ghaziabad, faridabad** |
| Mumbai | mumbai, bombay, navi mumbai, thane, powai |
| Chennai | chennai, madras |
| Kolkata | kolkata, calcutta |
| Ahmedabad | ahmedabad, gandhinagar |
| Others | jaipur, kochi, chandigarh, indore, coimbatore, trivandrum, mysuru |

- At ingest, compute `cities TEXT[]` for each job. A posting can list several cities. Add `CREATE INDEX jobs_cities_idx ON jobs USING gin (cities);`.
- Search uses `cities && %s` in place of the `location ILIKE` clause. It is faster, exact, and handles multi-location postings.
- `is_india` becomes "has at least one Indian city, or the country is India", so the two lists can't drift apart. The current `_INDIA` regex in [text.py](app/ingest/text.py) and `CITY_ALIASES` in [search.py](app/search.py) collapse into this one table.
- Users can still pick Gurugram on its own: store the specific city too (e.g. `{"Delhi NCR", "Gurugram"}`).

### 4.2 Product surface

- **UI:** multi-select city chips (Bengaluru · Hyderabad · Pune · Delhi NCR · Mumbai · Chennai · Remote) with counts, e.g. "Pune 312". A chip with 0 jobs is greyed out.
- **Preferences:** `Preferences.locations` is normalised to canonical names when saved, so "gurgaon" from chat becomes Delhi NCR / Gurugram.
- **Agent:** `search_jobs` takes `cities: list[str]`, and "Pune or Mumbai only" maps to that directly.
- **Stats:** jobs per city per week.

### 4.3 Remote roles

The current `keep()` lets in every "Remote" job worldwide, and many are US-only. Add `remote_scope`:
- `india`: remote and mentions India or IST,
- `global`: "anywhere" or "worldwide",
- `restricted`: remote but limited to US/EU/etc. Drop these at ingest unless `--all-locations` is set.

---

## 5. Keeping it fast and cheap at 10× the boards

| Concern | Plan |
|---|---|
| Fetch time | ~500 boards × 1 request, 8 workers → a few minutes. Add a per-host limit (max 4 in flight to the same ATS host) and back off on 429 |
| Embedding time | Embed only jobs that pass the 30-day and India/remote filters. Nightly runs embed only new or changed postings (already true via `content_hash`) |
| DB size (Supabase/Neon free tier) | Expire jobs older than 30 days. **Delete** inactive jobs older than 60 days, **except jobs with a feedback row**: `feedback` has `ON DELETE CASCADE`, so deleting a job would silently remove a user's saved job |
| Broken boards | A `boards` table holds `first_ingested_at`, `last_ok_at`, `last_error` and `consecutive_failures`. Disable a board after 7 failed nights and list it in `/api/stats` |
| Duplicates | The same role can appear on two ATSes, or as several postings for different cities. Add a `dedupe_key = hash(lower(company), normalised title, sorted cities)` and show one card per key |
| Freshness cadence | Nightly is enough for a 14-day window. A second run at 18:00 IST is cheap if same-day freshness matters |

---

## 6. Rollout order

| Phase | Scope | Size | Done when |
|---|---|---|---|
| **1. Freshness** | `effective_posted_at` and `posted_at_source`, Greenhouse date fix, 30-day ingest cutoff, `posted_within_days=14` default, UI filter and badges, agent argument, tests and eval fix | Small | Default feed has no jobs older than 14 days; tests and eval pass |
| **2. Cities** | `cities.py`, `cities[]` column and GIN index, search clause, UI chips with counts, preference normalisation, `remote_scope` | Small–medium | "Delhi NCR" returns Gurugram and Noida jobs; per-city counts shown |
| **3. New ATS adapters** | Workable, SmartRecruiters (then Recruitee); fixtures and normaliser tests | Medium | Each adapter ingests at least one real Indian company board |
| **4. Discovery** | `discover_boards.py`, CSV of companies from the maps and other lists, reviewed boards.json expansion, `boards` table and health tracking | Medium | 300+ active boards; failing boards visible in stats |
| **5. Scale and polish** | Dedupe, per-host rate limits, pruning, "new this week" stats, optional twice-daily ingest | Small each | Ingest < 15 min; DB within free-tier limits |
| **6. Investigate** | Keka / Darwinbox / Zoho official feeds; contact the startup-map owners | Research | A yes or no on each, written down |

Phases 1 and 2 don't depend on new sources and can ship first.

---

## 7. Open questions

1. **Window:** fixed at 14 days by default, with 7 and 30 as options? Should jobs with an unknown posted date be hidden, or shown under a "Date unknown" toggle?
2. **Cities:** is the list in 4.1 right? Should Delhi NCR be one chip, or Delhi / Gurugram / Noida separately?
3. **Remote:** keep `global` remote roles, or India-remote only?
4. **Startup maps:** should I draft an email to the two site owners asking for a company export?
5. **Company list:** do you have target companies (dream list) to put at the top of the discovery CSV?
