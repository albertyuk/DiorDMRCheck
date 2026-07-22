# HANDOFF — read this first in a new session

This file briefs a fresh Claude session (or human) taking over development.
It captures state, architecture, hard invariants, judgment calls, and the
working conventions this project was built under. Last updated: 2026-07-22.

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

- **Branches**: `claude/dmr-reconciler-webapp-9bwvwn` (the user's deploy
  branch) and `main` are kept in lockstep — **push every change to BOTH**
  (`git push origin <branch>` then `git push origin HEAD:main`). A third
  branch `claude/codebase-reorganization-review-15xt85` was a big
  reorg+hardening PR (#1), already merged; don't touch it.
- **Tests**: 238 passing (`python -m pytest tests/ -q`). Keep it that way.
- **Deploy**: the user runs, on their Mac:
  `cd ~/DiorDMRCheck && git pull origin claude/dmr-reconciler-webapp-9bwvwn && fly deploy`
  (always name the branch — a bare `git pull` once shipped a stale image).
  `APP_PASSWORD` is set as a Fly secret (auth is fail-closed without it).
- **Real data** for validation lives in `data/real/` (gitignored, present in
  the dev container): `PLOG_DMR_CHECK.xlsx` (101 rows), the human reference
  `PLOG_DMR_CHECK_1.xlsx`, `YTD_DMR_MICRO_0720.xlsx`, `Perimeters.xlsx`
  (58.8k Micro rows + a Macro sheet). A newer 417-row PLOG and 1555-row DMR
  exist only on the user's machine (they described its quirks: YY/MM/DD
  dates, 17 campaigns, 2025 window).

## Architecture map (post-reorg package layout)

    app/main.py                 assembly only: middleware, lifespan, routers
    app/web.py                  Jinja env (context_processors=[i18n.context], filters: fromjson, ts)
    app/config.py               all env vars; validate_runtime() = fail-closed auth
    app/core/                   db.py (SQLite, WAL), migrations.py (idempotent, applied at startup),
                                textnorm.py (nfkc, header_key…), xlsx.py (to_date/to_int/find_header_row),
                                uploads.py (body limits, zip-bomb caps, admission gates, retention),
                                token_store.py (TTL in-memory stores), llm.py
    app/reconciler/             parsers.py (PLOG/DMR, fingerprint headers), pipeline.py (tiered matcher),
                                links.py (resolver: direct path + TikHub, SSRF allowlist, DIRECT_BREAKER,
                                pooled client), adjudicator.py (Claude tier-4, annotation only),
                                perimeter.py (China-filter, content-hash cache with _PARSER_VERSION salt),
                                runs.py (bounded run scheduler + apply_window_override), export.py
                                (annotated xlsx — never overwrites populated cells), routes.py
    app/efficiency/             analysis.py (parse→classify→validate V1–V12→metrics→verify→insights),
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
6. **Export never overwrites populated cells** — pre-filled column-S values
   are kept (disagreement recorded in evidence Notes), the evidence block
   shifts right past populated columns, A–R untouched. UI overrides win.
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
- Tier ladder (fallback when LEVEL missing/unclear, and fanbase mode):
  ≤200K KOC · 200–400K BOT · 400K–1M MID · 1M+ TOP; boundary values belong
  to the band below, exactly 1000K is TOP. Explicit label always wins
  (V11 reports fallback rows).
- FAN BASE units: values <10,000 are thousands (130→130K, 1741→1.74M —
  real-file semantics); ≥10,000 are raw counts ÷1000 (V12 reports). Cutoff
  chosen against real data: all real values are 34–1741 K.
- Perimeter is China-market only: IN_CHINA_REPORTS=YES when the column
  exists (Macro sheet — future feature hook), else COUNTRY=Mainland China.
  Verified: all 6,140 REDBOOK_ID rows in the real Micro sheet are Mainland
  China, so membership verdicts can't flip.
- DMR export window: parsed from the metadata "From … To …" line, editable
  on the confirm screen (stored in run options; blank side disables checks).
- Date parsing: YY/MM/DD ("24/11/27") accepted, but the two-digit-year
  formats sit LAST in core/xlsx.py so ambiguous strings keep historical
  readings ("05/06/25" is still %m/%d/%y).
- Resolver: free direct-to-XHS path sits behind DIRECT_BREAKER (3 failures
  trip, re-probe every 25th) because datacenter IPs are blocked; TikHub is
  authoritative and uses one pooled client; TIKHUB_CONCURRENCY default 8.
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
- **Findings codes**: V1–V12 are stable public vocabulary (README table).
  New validation = next number + zh pattern + README row.
- **Container QA deps** (not preinstalled; apt-get when needed):
  `libreoffice-impress libreoffice-writer libreoffice-calc poppler-utils`,
  pip `defusedxml`. Playwright uses `/opt/pw-browsers/chromium`.
- Commit style: imperative summary + a body that says *why*; push to BOTH
  branches (see Current state).

## Config quick reference (app/config.py)

`APP_PASSWORD` (setup code, required in prod) · `ALLOW_OPEN_ACCESS` ·
`APP_SECRET` (optional; else persisted random) · `SESSION_COOKIE_SECURE` ·
`TIKHUB_API_KEY` + `TIKHUB_CONCURRENCY=8` + timeouts ·
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
- `docs/` deliverables (rubric + templates) are generated by tools/ scripts —
  regenerate and re-commit when formats change.

## How to start (for the next session)

1. Read this file, then README.md (user-facing behavior + validation table).
2. `python -m pytest tests/ -q` — expect 238 passed (1 skip if data/real absent).
3. Skim `docs/REORGANIZATION.md` if you need the deep architecture rationale.
4. Make changes → tests → push to BOTH branches → remind the user:
   `git pull origin claude/dmr-reconciler-webapp-9bwvwn && fly deploy`.
