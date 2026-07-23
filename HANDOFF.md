# HANDOFF — read this first in a new session

This file briefs a fresh Claude session (or human) taking over development.
It captures state, architecture, hard invariants, judgment calls, and the
working conventions this project was built under. Last updated: 2026-07-23.

## What this is

**DMR Reconciler** — a FastAPI web app for a China-market KOL operations
team. Two products in one app:

1. **Reconciliation** (`/`): upload the internal KOL tracker ("PLOG" xlsx) +
   the DMR social-listening export → resolve每个帖子链接 → match by
   Xiaohongshu note-ID/author-ID → produce the annotated Excel the team
   otherwise fills by hand (column S verdicts: blank=matched · 无帖子 ·
   无博主 · Check链接错误 · 人工复核 · 有 但是DMR博主名字标注错误), with
   per-row evidence, human overrides, JSON audit export, and an optional
   LVMH "Perimeter" membership split of 无博主 rows.
2. **KOL efficiency report** (`/efficiency`): upload a PLOG alone → validated
   metrics (CPM/CPE by TIER × 报备/软植) → one-slide native-chart `.pptx` +
   HTML report. Fully in-memory (client-data privacy).

Owner: GitHub `albertyuk/DiorDMRCheck`. Deployed by the user manually on
Fly.io (app `dmr-reconciler`, region `sin`, single machine — see
constraints). UI is bilingual (EN default, 中文 via top-left toggle).

## Current state

- **Integration provenance**: remote feature commit
  `36911ed51656ef18f85a4ddecf3fbb16be742da1` was integrated with the
  reviewed hardening checkpoint
  `2a92bf5` (`agent/hardening-checkpoint-36911ed`) on local branch
  `agent/integrate-hardening-36911ed`. The work was done in the sibling
  `DiorDMRCheck-integration` worktree so neither input was overwritten.
- **Publishing rule**: do **not** push multiple branches or deploy directly
  from an arbitrary working tree. Review the integration diff, run the full
  gate below, then publish the single intended branch through the normal PR
  or user-approved deployment workflow. No instruction in this file grants
  permission to push, merge, or deploy.
- **Tests**: in this integration environment on 2026-07-23, 405 passed and
  1 real-client efficiency golden test skipped. The full gate is
  `python -m pytest tests/ -q`, `ruff check app tests tools`, and
  `git diff HEAD --check`; the fixture skip is expected when its gitignored
  input is unavailable, and the rubric syntax test also skips if Node.js is
  not installed.
- **Deploy**: deploy only the exact reviewed commit. Authentication is
  fail-closed; production must have `APP_PASSWORD` and secure cookies.
- **Real data** for validation belongs in `data/real/` (gitignored) and is
  available only in environments where it has been supplied:
  `PLOG_DMR_CHECK.xlsx` (101 rows), the human reference
  `PLOG_DMR_CHECK_1.xlsx`, `YTD_DMR_MICRO_0720.xlsx`, and `Perimeters.xlsx`
  (58.8k Micro rows + a Macro sheet). It was not present for the 2026-07-23
  integration gate, so the golden test skipped. A newer 417-row PLOG and
  1555-row DMR exist only on the user's machine (they described its quirks:
  YY/MM/DD dates, 17 campaigns, 2025 window).

## Architecture map (post-reorg package layout)

    app/main.py                 assembly only: middleware, lifespan, routers
    app/web.py                  Jinja env (context_processors=[i18n.context], filters: fromjson, ts)
    app/config.py               all env vars; validate_runtime() = fail-closed auth
    app/core/                   db.py (SQLite, WAL), migrations.py (idempotent, applied at startup),
                                textnorm.py (nfkc, header_key…), xlsx.py (to_date/to_int/find_header_row),
                                uploads.py (body limits, zip-bomb caps, admission gates, retention),
                                token_store.py (TTL in-memory stores), llm.py
    app/reconciler/             parsers.py (PLOG/DMR, fingerprint headers), pipeline.py (tiered matcher),
                                links.py (HTTPS-only resolver, canonical single-flight, independent
                                network breakers, pooled TikHub client), adjudicator.py (Claude tier-4,
                                annotation only),
                                perimeter.py (China-filter, content-hash cache with _PARSER_VERSION salt),
                                runs.py (bounded run scheduler + apply_window_override), export.py
                                (annotated xlsx — never overwrites populated cells), routes.py
    app/efficiency/             analysis.py (parse→classify→validate V1–V13→metrics→verify→insights),
                                deck.py (python-pptx + lxml chart-XML patching; assert_chart_cache),
                                routes.py (in-memory store EFF_REPORTS)
    app/remap/                  LLM header-mapping: mapper.py (sample→Claude proposal→validated),
                                registry.py (approved-mapping cache by header-layout signature),
                                service.py (PENDING_MAPS), routes.py (audit screen)
    app/auth/                   service.py (PBKDF2, HMAC sessions, persisted signing secret),
                                throttle.py (sliding-window login/setup limits), routes.py
    app/i18n/                   __init__.py (t()/td()), catalog/{common,reconciler,efficiency}.py
    app/templates/              base.html (all CSS/JS: guide modal, stepper, dropzones, busy submits,
                                results filters) + per-product dirs + shared/ (_steps.html, remap_audit)
    tests/                      mirrors the package layout; tests/fixtures.py has the synthetic
                                PLOG/DMR/efficiency builders and fake_resolutions
    tools/evaluate.py           eval harness vs the human reference (≥99/101 acceptance gate)
    tools/make_demo_assets.py   regenerates app/static/eff_demo_{en,zh}.jpg from synthetic data
    tools/make_rubric.js        generates docs/DMR_Reconciler_File_Rubric.docx (bilingual file spec)
    tools/make_templates.py     generates docs/*_Template.xlsx (verified to parse cleanly)
    docs/                       the generated rubric + Excel templates + REORGANIZATION.md

## Hard invariants — do not break these

1. **Deterministic-first, LLM-last.** Only note-ID/author-ID joins assert
   MATCH/无帖子/无博主. Name heuristics only rank candidates. Claude only
   annotates residue and writes the summary — it never decides a verdict.
   **Engagement numbers are NEVER a matching signal** (DMR is a first-crawl
   snapshot; a verified same-post pair reads 607 vs 14 likes).
2. **Golden gates.** `tools/evaluate.py` vs the human reference: ≥99/101
   after two documented reference-noise exclusions. Efficiency golden test
   (skipped when `data/real/` absent): spend ¥1,049,345 · impressions
   17,820,424 · engagements 216,300 · 7 groups (no TOP PAID in that file).
3. **Efficiency privacy.** Uploaded efficiency workbooks are analyzed from
   memory and NEVER written to disk or DB; reports live in an in-process
   TTL store. Consequence: **single process, single machine** — never add
   uvicorn workers or a second Fly machine (documented in fly.toml).
4. **Deck verification.** Every chart's cached XML values are re-parsed and
   diffed against computed metrics before a download is offered
   (`assert_chart_cache`). Metrics are computed twice (loop + pandas).
   OOXML gotchas already fixed once — keep: integer EMU only in slide
   geometry; per-point donut labels carry their own numFmt; ser-level dLbls
   must be fully specified; DONUT_ORDER/COLORS must cover all 8 TIER×COOP.
5. **Never mutate source data; never reuse the source CPM column** (it's
   price per single impression — ×1000 off standard). **Never generate
   cross-wave comparisons** in insights.
6. **Export preserves source data and records provenance.** A valid UI
   override wins; otherwise a populated source-S value is preserved, with
   formula-like text neutralized before writing. Raw source S, disposition,
   overrides, perimeter provenance, and evidence are recorded in
   collision-safe audit metadata. Evidence uses a whole-sheet-empty column
   block or a collision-safe hidden `_DMR_EVIDENCE` fallback at XFD.
7. **Header remap**: the LLM proposes column mappings from a structural
   sample only; NOTHING applies without human approval on the audit screen;
   only header cells are rewritten; approved mappings cached by
   header-layout signature.
8. **i18n contract**: English source text is the key; en rendering is the
   identity. Every `t("…")` in templates MUST have a ZH entry (a test scans
   and enforces). Runtime-stored English (progress lines, warnings,
   findings) is translated at render time by `td()` exact+regex patterns —
   when you change such a message, update its pattern. A lint flags two
   keys mapping to the same Chinese (allowlist in tests/web/test_i18n.py).
9. **Security posture** (from PR #1 + follow-ups): fail-closed auth
   (`APP_PASSWORD` required unless `ALLOW_OPEN_ACCESS=1`), login/setup
   throttling (app/auth/throttle.py), sessions versioned to password hash,
   SSRF allowlist for outbound fetches (xhslink/xiaohongshu only, per-hop
   revalidation), upload body/zip-bomb caps before parsing.
10. **Cache-version discipline**: parsed-perimeter cache is content-hashed —
    if you change perimeter parse semantics, bump `_PARSER_VERSION` in
    app/reconciler/perimeter.py or stale rows will be served. Same idea for
    the remap registry (keyed by header layout).

## Domain judgment calls (deliberate, documented — don't "fix" silently)

- 尾部 and 底部 LEVEL labels merge into BOT (V8 warns when both coexist).
- Tier ladder (approved fallback and fanbase mode): ≥1000K TOP · ≥400K MID ·
  ≥200K BOT · otherwise KOC. Boundaries belong to the higher band.
  In label mode, recognized explicit labels win; fanbase mode deliberately
  applies the follower ladder to every row.
- LEVEL fallback is deliberately narrow: only blank, `待定`, and `?` use
  FAN BASE and report V12. Any other unknown label stays UNCLASSIFIED/V7.
- FAN BASE has one explicit unit for the entire workbook: `k` (default) or
  `raw`. Raw mode divides every valid row by 1,000 and reports V13. There is
  no per-row magnitude cutoff or unit guessing.
- Perimeter is China-market only: IN_CHINA_REPORTS=YES when the column
  exists (Macro sheet — future feature hook), else COUNTRY=Mainland China.
  Verified: all 6,140 REDBOOK_ID rows in the real Micro sheet are Mainland
  China, so membership verdicts can't flip.
- DMR export window: parsed from the metadata "From … To …" line and editable
  on the confirm screen. Omitted fields preserve the detected window;
  clearing either atomically clears both; invalid or reversed ISO ranges are
  rejected. Options survive retries.
- Date parsing: separators never change interpretation. Ambiguous numeric
  dates use an explicit source policy where known; DMR metadata is day-first,
  YY/MM/DD tracker values such as `24/11/27` are supported, and generic
  ambiguous input retains the historical month-first default.
- Resolver: canonical Xiaohongshu note URLs are extracted without I/O.
  Redirect resolution and detail enrichment have separate breakers that trip
  only on transport failures (3 failures; re-probe every 25th skip). URL-keyed
  single-flight coalesces equivalent concurrent requests and forced retries.
  TikHub is authoritative, uses one pooled client closed at app shutdown, and
  `TIKHUB_CONCURRENCY` defaults to 8 with a validated 1–32 range.
- Column S vocabulary and 超出DMR导出窗口/Perimeter split suffixes reproduce
  the human reference exactly — never rename them.

## Working conventions in this repo

- **Bilingual by construction**: any new user-visible string → wrap in
  `t()` → add zh to the right catalog file. Chinese is written natively for
  a 小红书/KOL ops audience (核对 = a reconciliation run, 曝光量, 判定依据,
  存疑行…), never literal translation. Domain tokens (无博主/报备/Perimeter/
  CPM…) stay verbatim in both languages.
- **UI kit lives in base.html**: flow stepper (`{% set flow_step %}` +
  include shared/_steps.html), `.dropzone` file inputs, `data-busy` forms,
  results filter bar, first-visit guide modal, Dior-esque monochrome CSS
  (--ink/--paper/--line; serif wordmark; no rounded corners).
- **Every substantive change ships with tests** and, for UI, a Playwright
  drive-through (pattern: scratchpad scripts that boot uvicorn in-process
  with `config.ALLOW_OPEN_ACCESS=True` and fake `pipeline.resolve_link`
  from tests/fixtures.fake_resolutions).
- **Findings codes**: V1–V13 are stable public vocabulary.
  New validation = next number + zh pattern + README row.
- **Container QA deps** (not preinstalled; apt-get when needed):
  `libreoffice-impress libreoffice-writer libreoffice-calc poppler-utils`,
  pip `defusedxml`. Playwright uses `/opt/pw-browsers/chromium`.
- Commit style: imperative summary + a body that says *why*. Never push,
  merge, or deploy unless the user explicitly asks for that state change.

## Config quick reference (app/config.py)

`APP_PASSWORD` (setup code, required in prod) · `ALLOW_OPEN_ACCESS` ·
`APP_SECRET` (optional; else persisted random) · `SESSION_COOKIE_SECURE` ·
`TIKHUB_API_KEY` + `TIKHUB_CONCURRENCY=8` (valid 1–32) + timeouts ·
`ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL` (default claude-sonnet-5; adjudicator,
run summary, header-map proposals) · `MAX_UPLOAD_MB=25` and zip caps ·
`RUN_MAX_CONCURRENT=2` · `DATA_DIR` (/data on Fly volume).

## Known gaps / likely next features

- **Macro perimeter** ("will be added later" — the user said so): the
  ingest filter already understands `IN_CHINA_REPORTS`; what's missing is
  reading the "List Macro" sheet (currently only List Micro is picked) and
  deciding how Macro membership feeds the pipeline.
- Insights on the zh efficiency deck: chart titles are localized but the
  insight bullet lines render in English on the slide (TEXTS covers titles;
  bullets are built in English in analysis.build_insights).
- The 1900-01-06 junk-date artifact (bare numbers in POST DATE) parses
  "successfully" — a loud warning for pre-2015 dates was discussed, not built.
- `docs/` deliverables (rubric + templates) are generated by `tools/`.
  Templates: `python tools/make_templates.py`. Rubric:
  `cd tools && npm ci && npm run make:rubric`; `docx` is pinned in the lock.

## 2026-07-23 integration change map

This section is the concrete review map for the feature-preserving hardening
integration. It is intentionally path-specific.

- `app/auth/{routes,service,throttle}.py`, `app/core/{db,migrations,uploads,
  token_store,xlsx}.py`, `app/config.py`, `entrypoint.sh`, `Dockerfile`:
  retained fail-closed auth, atomic admin/session and cache behavior,
  POST-only same-origin logout, upload/ZIP/workbook/result/storage limits,
  safer container startup, finite numeric coercion, explicit date-order
  parsing, bounded concurrency, and fail-fast validation for finite timeouts,
  nonnegative retries/date windows, and positive cache TTLs.
- `app/reconciler/{routes,runs,parsers}.py` and reconciler templates:
  preserved the new editable export window while distinguishing an omitted
  override from an intentional clear, rejecting malformed/reversed ranges,
  parsing DMR metadata day-first, preserving retry choices, enforcing admin
  perimeter promotion, quarantining malformed persisted run options instead
  of stranding queued work, and returning bounded/sanitized failures.
- `app/reconciler/links.py`, `app/main.py`: combined pooled TikHub HTTP with
  canonical URL single-flight, strict HTTPS/host/identity checks, independent
  transport-only breakers, note-scoped author extraction, safe `Retry-After`
  handling, zero-I/O canonical-note extraction, sanitized public errors,
  forced-retry coalescing, deterministic pool shutdown, and same-origin
  anti-framing response headers.
- `app/reconciler/adjudicator.py`, `app/remap/mapper.py`: retained provider
  details in server logs while replacing user-visible exception text with
  stable, non-sensitive failure messages.
- `app/reconciler/export.py`: kept source S unless a valid UI override wins,
  preserved its value type/formatting, sanitized newly written external text,
  recorded raw/disposition provenance, excluded populated and merged
  whole-sheet columns from evidence placement, and added an XFD-safe hidden
  evidence-sheet fallback.
- `app/efficiency/{analysis,deck,routes}.py` and efficiency templates:
  preserved the new reports/UI while replacing magnitude-based FAN BASE
  guessing with one explicit workbook unit, keeping finite and row-limit
  checks, using inclusive ≥200/400/1000 boundaries, restricting fallback to
  blank/待定/?, and carrying V12/V13 provenance into HTML and PPTX.
- `app/i18n/catalog/{common,efficiency}.py`: added bilingual strings and
  runtime patterns for the new window/unit/finding behavior.
- `tools/make_demo_assets.py`, `tools/make_templates.py`,
  `tools/make_rubric.js`, `tools/package{,-lock}.json`: removed machine-local
  paths, made the generators portable and re-runnable, pinned the Word
  generator dependency, and added smoke coverage in
  `tests/test_generators.py` (templates execute and parse; the demo imports
  and builds; the rubric lock and JavaScript syntax are checked). The
  generated DOCX received separate rendered-page visual QA.
- Tests under `tests/core`, `tests/reconciler`, `tests/efficiency`, and
  `tests/web` cover the concurrency, breaker, provenance, sparse-XFD, date,
  window, unit, auth, storage, generator, and regression paths above.

## How to start (for the next session)

1. Read this file, then README.md (user-facing behavior + validation summary).
2. Run `python -m pytest tests/ -q`, `ruff check app tests tools`, and
   `git diff HEAD --check`; in the 2026-07-23 integration environment the
   baseline was 405 passed, with one skip because the real efficiency fixture
   was absent. A machine without Node.js also skips the rubric syntax check.
3. Skim `docs/REORGANIZATION.md` if you need the deep architecture rationale.
4. Inspect `git status`, `git diff --stat`, and the integration commit before
   changing anything. Publishing or deploying is a separate, explicit action.
