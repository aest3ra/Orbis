# Orbis

**AI-ASM — an attack-surface mapper that collects a web target's API endpoints.**

Orbis drives a real browser over a target site, watches the traffic it generates,
reads the JavaScript it ships, and pulls what public archives remember — then
merges all of it into one deduplicated, templated list of API endpoints. The goal
is **maximum endpoint coverage for any site**, with no per-site tuning.

## How it works — the recon triad

Orbis combines three independent discovery layers so that a miss in one is caught
by another:

1. **Live dynamic crawl** (`crawler/`) — Playwright + Chrome DevTools Protocol.
   A BFS frontier walks the site; every page load captures real `xhr`/`fetch`
   traffic, and an **interaction layer** (`interactor.py`) clicks tabs/buttons,
   paginates, and re-runs discovery so endpoints that only fire after a click are
   still seen. This is the highest-confidence source.
2. **Static analysis** (`analysis/`) — external JavaScript is fetched and scanned
   for API URLs (`js_static.py`); OpenAPI/Swagger specs (`openapi.py`) and API
   docs (`docs.py`) are parsed when present.
3. **Passive layer** (`crawler/passive.py`) — the Wayback Machine CDX API is
   queried for every URL ever archived under the host, surfacing endpoints that
   no longer appear in the live UI.

### What makes coverage universal (not overfit)

- **Cardinality-based slug detection** (`crawler/slug.py`) — instead of regex
  guesses about what an ID "looks like," a path position that takes on many
  distinct values across the crawl (default ≥ 8) is collapsed to `{slug}`. This
  generalizes `/api/posts/1`, `/api/posts/2`, … into one template without
  exploding the frontier, and works the same on numeric IDs, hashes, and slugs.
- **Diminishing-returns saturation** — once a template yields no new endpoints
  for N consecutive visits, Orbis stops re-visiting it.
- **Source-priority merge** — the same endpoint seen by multiple layers is kept
  once, preferring the most trustworthy source:
  `dynamic > static_openapi > static_docs > static_js > passive`.
- **Asset filtering** — images, fonts, CSS/JS, and web manifests are never
  enqueued as crawlable pages (they are not endpoints).
- **`discovered_via` tracking** — each dynamic endpoint records which interaction
  surfaced it (or `passive load`).
- **Exact-URL frontier dedup** — a page is visited at most once.

## Install

```bash
pip install -e .
playwright install chromium
```

Requires Python ≥ 3.11.

## Usage

```bash
# Crawl a target and collect endpoints
orbis scan https://example.com

# Tune limits (all optional; sensible defaults otherwise)
orbis scan https://example.com --max-pages 150 --max-duration 1500 --max-scrolls 1

# Crawl presets: quick | deep | exhaustive
orbis scan https://example.com --crawl-mode deep

# Authenticated scan: capture a session, then scan with it
orbis login https://example.com -o auth.json
orbis scan https://example.com --auth auth.json

# Toggle layers
orbis scan https://example.com --no-passive --no-js-analysis --no-probe

# Inspect results
orbis list runs/orbis-...db
orbis list runs/orbis-...db --source dynamic
orbis inspect runs/orbis-...db 42
```

Key `scan` options: `--max-pages`, `--max-depth`, `--max-duration`,
`--per-template`, `--max-scrolls`, `--crawl-mode`, `--auth`,
`--passive/--no-passive`, `--js-analysis/--no-js-analysis`,
`--probe/--no-probe`, `--headless/--no-headless`.

Active probing is on by default and only sends safe in-scope GET requests for
unverified endpoints. With `--auth`, probe requests share browser cookies from
the Playwright storage state; localStorage-only bearer tokens are not replayed.

## Architecture

```
src/orbis/
  cli.py            scan / login / list / inspect commands
  config.py         ScanConfig, LimitsConfig, CRAWL_PRESETS
  safety.py         scope & request safety gates
  crawler/
    runner.py       BFS loop, rate limiting, duration/page caps
    browser.py      Playwright capture (network + DOM)
    interactor.py   click / paginate / re-discover interactions
    frontier.py     dedup queue + saturation + slug templating
    slug.py         cardinality-based {slug} detection
    passive.py      Wayback CDX archive fetch
    scope.py        in-scope host/URL matching
  analysis/
    analyzer.py     classify traffic, extract links, normalize endpoints
    classifier.py   api vs asset/telemetry/page classification
    js_static.py    API-URL extraction from JavaScript
    openapi.py      OpenAPI/Swagger spec parsing
    docs.py         API-doc parsing
    params.py       query/body parameter normalization
    url.py          URL helpers
  storage/
    db.py / repo.py SQLModel persistence
```

## Status

- Endpoints are persisted per scan with source, route kind, params, and
  `discovered_via` provenance.
- Unobserved static/passive endpoints can be actively verified with safe GET
  requests; Orbis records `probe_status` and the actual HTTP `probe_code`.
- Validated against a logged-out manual browse of dreamhack.io: the tool's
  dynamic set was a **strict superset** of everything found by hand (0 misses),
  and roughly 6× the total once passive + static layers are included.
- 384 unit tests pass.

## Roadmap / next

1. **Asset-leak follow-through** — the frontier now drops image/font/manifest
   links; audit remaining non-page link types (e.g. `mailto:`, `tel:`,
   downloadable docs) and confirm none reach the crawl queue.
2. **Smarter frontier ordering** — prioritize high-yield routes (listing/detail
   pages) over low-yield boilerplate (TOS, privacy) so capped crawls spend their
   budget where endpoints actually live.
3. **Write-method discovery** — current dynamic capture is GET-dominated;
   exercise forms/buttons that trigger POST/PUT/DELETE to broaden method
   coverage.
4. **Third-party noise control** — `static_js` can surface off-target SDK
   endpoints (e.g. analytics, social SDKs); add an optional same-host filter
   while keeping them available when full collection is the goal.

> Benchmark/measurement scripts are kept local and intentionally excluded from
> version control.
