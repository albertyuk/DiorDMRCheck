# DMR Reconciler

Reconciles an internal KOL campaign tracker ("PLOG" file) against a DMR
social-listening export ("YTD DMR MICRO" file) for Xiaohongshu (RedBook)
posts, and produces an annotated Excel identical in format to the human-made
reference (`PLOG_DMR_CHECK_1.xlsx`).

## Design principles

1. **Deterministic-first, LLM-last.** The core match is an exact join on
   Xiaohongshu note IDs (Tier 1) and author IDs (Tier 2). Name/date heuristics
   (Tier 3) only *rank candidates* for human review; only Tiers 1–2 may assert
   `MATCH` / `无帖子` / `无博主`. Claude adjudicates only the residue and
   writes human-readable notes — it never decides what an exact ID join can
   decide. Engagement comparison is **never** a decision signal (DMR records a
   first-crawl snapshot; a verified same-post pair reads 607 PLOG likes vs 14
   DMR likes).
2. **Every verdict carries evidence** — deciding tier, matched DMR row/PostID,
   resolved note/author IDs, name-match method, date delta — visible in the UI
   popover and exported in columns T+ and the JSON audit log.
3. **Cache everything external.** SQLite table keyed by short-link URL. A
   resolved `(note_id, author_id)` never changes, so successes are cached
   permanently; failures are retried only after a TTL or on request.
4. **Partial failure is normal.** Dead xhslink, TikHub 4xx, rate limits — each
   becomes a per-row status and the run completes.
5. **Schema-tolerant parsing.** Header rows are located by fingerprint
   (PLOG: `NAME` + `POST LINK`; DMR: `Blogger` + `PostID`), never by fixed
   index, with NFKC + whitespace-collapsed + casefolded header matching
   (handles `FAN BASE（K)` and `TTL  ENGAGEMENT`).

## Pipeline

| Tier | Signal | May assert |
|---|---|---|
| 0 | NFKC normalization, emoji stripping, CJK/ASCII split | — |
| 1 | POST LINK → note id (direct redirect walk, then TikHub) → exact `PostID` join | `MATCH` (+ `有 但是DMR博主名字标注错误` when the DMR Blogger doesn't contain the PLOG name) |
| 2 | resolved author id ∈ DMR `Username` | `无帖子` / `无博主` (name-scan cross-check → `人工复核` on contradiction) |
| 3 | name ladder (CJK substring → norm substring → ASCII fuzzy ≥85 → pinyin bridge), candidates ranked by date proximity (±7d window) | `Check链接错误` with ranked candidates only — never a match |
| 4 | Claude Sonnet, strict JSON, batched | annotation only; `UNSURE`/malformed → `人工复核` |

Column S vocabulary reproduces the human reference exactly:
blank = matched · `无博主` · `无帖子` · `Check链接错误` (+ candidate note) ·
`有 但是DMR博主名字标注错误` · `人工复核`.

## Perimeter cross-check (optional)

Upload the LVMH Micro social perimeter workbook (third slot on the upload
screen) and `无博主` rows are split by perimeter membership — an offline join,
no new external calls:

- resolved `author_id ∈ REDBOOK_ID` set → `无博主但在Perimeter内→无帖子`
  (blogger is monitored yet absent from the export → a genuine DMR gap,
  bucketed with `无帖子`);
- otherwise → `无博主（不在Perimeter内）`, with name-ladder hits recorded as
  evidence only: a single same-name row with a *different* REDBOOK_ID is a
  near-miss note, a single row *without* a REDBOOK_ID gets
  `在Perimeter名单但未登记REDBOOK_ID` (actionable — DMR cannot crawl an
  unregistered account), and multi-hit names (e.g. `esther`, or
  `Ananas吃一半` with 91 same-name rows) are never auto-classified.

**China market only.** This tool evaluates the Chinese market, so the
perimeter is filtered on ingest: rows with `IN_CHINA_REPORTS = YES` when
that column exists (the Macro sheet has it — ready for the future Macro
feature), otherwise rows whose `COUNTRY` is Mainland China (the Micro sheet
has no IN_CHINA flag). On the real file this keeps 6,919 of 58,796 Micro
rows — including **all** 6,140 REDBOOK_ID holders, so membership verdicts
are unaffected; the win is that same-name evidence scans no longer match
tens of thousands of non-China influencers. The kept/total count is shown
wherever the perimeter is displayed; a file with neither column keeps all
rows and warns. (The parse cache is version-salted, so previously cached
unfiltered parses are never served.)

Only the `List Micro` sheet is read (header located by the NAME+REDBOOK_ID
fingerprint; the extraction date is parsed from the metadata and shown —
staleness matters). The parsed 58.8k rows are cached in SQLite by content
hash, so re-runs add under ~2 s. The last uploaded perimeter persists across
runs until replaced or removed; without one, behavior is exactly as before.
Dead-link rows keep their verdict — perimeter hits are annotation only.
`tools/evaluate.py --perimeter <file>` maps both split statuses back to `无博主` so the
reference agreement math is unchanged, and prints the in/out split.

## KOL efficiency report (`/efficiency`)

Upload a PLOG-style tracker on its own and get a chart-based efficiency
presentation: a one-slide 16:9 `.pptx` with **native, editable charts**
(donut of group shares, avg collab price, CPM & CPE bars split SOFT vs PAID
per TIER) plus an HTML report view. No DMR file, TikHub, or Claude involved.

- **Classification** — COOP from `TYPE` (`报备…`→PAID, `软植…`→SOFT), TIER
  from `LEVEL` labels (`尾部`+`底部` merge into BOT — documented judgment
  call, V8) or, via the form toggle, from `FAN BASE` thresholds. A row whose
  LEVEL is missing or unrecognizable falls back to the follower ladder
  (≤200K KOC · 200–400K BOT · 400–1000K MID · 1M+ TOP) and is reported as
  V11; an explicit label always wins over follower count. Rows with neither
  signal land in an UNCLASSIFIED bucket, never guessed. FAN BASE units are
  normalized first: values under 10,000 are thousands (130 → 130K, and
  1741 → 1.74M as in the real file), values at/above are raw follower
  counts ÷1000 (450,000 → 450K) — reported as V12 so units are never
  silently reinterpreted.
- **Metrics** — pooled basis (group Σspend÷Σimpressions ×1000, Σspend÷Σeng)
  by default, per-post average as the alternative; the basis is printed on
  the slide. The source file's CPM column is price per *single* impression —
  ×1000 off standard CPM — and is never reused, only cross-checked (V5).
- **Validation V1–V12** — findings (duplicate links, engagement identity
  breaks, missing values, zero denominators, label conflicts, n<3 groups,
  viral-post concentration >50%) are shown *with* the output and, where they
  bias a number, become on-slide caveats. The source file is never mutated.
- **Verification before download** — metrics are computed twice (raw loop +
  pandas groupby, diffed to 1e-6), totals reconciled, and after the deck is
  built the embedded chart XML's cached values are parsed back out of the
  package and diffed against the metrics. Any mismatch blocks the download.
- **Insights are single-wave only** — winners, premiums, and caveats derived
  from the uploaded file; wave-over-wave comparisons are never generated.
- **Privacy** — the workbook is analyzed in memory, never written to disk or
  the database; the finished report is held in an in-process store and
  expires after two hours.
- **Demo image** — the sample slide shown next to the feature (home card and
  the `/efficiency` form, `app/static/eff_demo_{en,zh}.jpg`) is rendered
  through the real deck builder from *synthetic* data — client workbooks are
  never used for site assets. Regenerate with the scratch script if the deck
  design changes.

## Unfamiliar sheet formats — LLM header mapping with human audit

When an uploaded workbook's headers don't match the deterministic
fingerprint (PLOG, DMR, or the efficiency workbook — e.g. a tracker with
Chinese headers like 博主昵称/笔记链接), the app no longer just fails:

1. Claude is shown a small **structural sample** — sheet names plus the
   first 15 rows — and proposes which sheet/header-row/columns correspond to
   the canonical fields, with per-field confidence and warnings for anything
   a human must double-check (suspected unit differences, rate-vs-count
   columns). It maps columns; it never rewrites data.
2. **A human audits before anything runs.** The proposal renders as an
   editable table: each field shows the original header, example values from
   that column, and the model's confidence (low-confidence picks are
   flagged). Correct any column, or cancel. Required fields must be mapped.
3. On approval, **only the header cells are rewritten** to canonical names
   (in a copy — the uploaded original is kept); every data cell is
   byte-identical and the normal deterministic pipeline runs unchanged. The
   preview/report shows exactly what was remapped and who approved it.
4. Approved mappings are **cached by a signature of the header region** —
   each new format costs one LLM call and one approval ever; identical
   formats later auto-apply with a visible "approved by X on date" note.

Efficiency-report uploads keep their privacy contract: the workbook and the
pending mapping live only in memory. Without `ANTHROPIC_API_KEY` the
behavior is exactly the old one (clear parse error). Genuinely missing data
is out of scope by design — a mapping can rename columns but never conjure
a join key, and the model is instructed to leave unmappable fields empty
rather than guess.

## First-visit guide

A quick-guide popup opens automatically on a user's first visit (suppressed
on the sign-in/setup pages) and can be reopened any time from the **Guide**
button in the top-left corner. It covers what the app does, the four steps of
a reconciliation, how to read the column-S verdicts (including the
name-mislabel and out-of-window nuances), the never-match-on-engagement rule,
and the efficiency report. Dismissal is remembered per browser
(`localStorage`), and the content is fully bilingual via the same `t()`
dictionary as the rest of the UI.

## Interface language (中文界面)

The button in the top-left corner of every page toggles the interface between
English and Chinese (choice remembered in a `dmr_lang` cookie for a year; it
works on the sign-in page too). The Chinese copy is written for the China-side
KOL-operations audience rather than translated literally — a reconciliation
run is 核对, impressions are 曝光量, and domain vocabulary the team already
uses (无博主 / 无帖子 / 人工复核 / 报备 / 软植 / Perimeter / CPM) is kept
verbatim in both languages. English remains the default and renders exactly
as before.

Mechanics (`app/i18n/`): static strings go through `t()` keyed by the
English source text (English is the identity, so a missing translation
degrades to English, never breaks); text that was *stored* in English at run
time — progress lines, parser warnings, run statuses — is translated at
render time by `td()` via exact and regex patterns that carry row numbers and
counts into the Chinese sentence. Data itself (statuses, campaign names,
column-S vocabulary, uploaded content) is never translated. The efficiency
form's slide-language default follows the interface language.

## Running locally

```sh
pip install -r requirements.txt
ALLOW_OPEN_ACCESS=1 SESSION_COOKIE_SECURE=0 uvicorn app.main:app --port 8080
# open http://localhost:8080
```

Authentication is required by default. The command above is the explicit
passwordless opt-out for local-only use; never use it on a shared host.
External API integrations remain optional and degrade gracefully.

| Var | Purpose |
|---|---|
| `TIKHUB_API_KEY` | XHS link resolution (authoritative path). Without it, only the free direct-redirect path runs — usually blocked from datacenter IPs. |
| `ANTHROPIC_API_KEY` | Tier-4 adjudication + bilingual run summary. |
| `ANTHROPIC_MODEL` | Defaults to `claude-sonnet-5` (the current Sonnet, verified at build time — the older `claude-sonnet-4-6` works as an override). |
| `APP_PASSWORD` | The **required-by-default setup code** for the account system. Visit `/setup`, enter the code, and create the admin account; admins add coworkers on `/team`. Startup fails if neither this nor the explicit local opt-out is set. |
| `ALLOW_OPEN_ACCESS` | Set to `1` only for intentional local passwordless use. Defaults off, so a missing deployment secret fails closed. |
| `SESSION_COOKIE_SECURE` | Defaults to `1`; set to `0` only for local plain-HTTP development. |
| `DATA_DIR` | SQLite + uploads location. Defaults to `/data` when present (Fly volume), else `./data`. |
| `MAX_UPLOAD_MB` | Maximum compressed size per workbook; defaults to 25 MB. |
| `MAX_XLSX_UNCOMPRESSED_MB` | Maximum expanded XLSX contents; defaults to 50 MB to reject ZIP bombs. |
| `MAX_XLSX_ENTRIES` | Maximum files inside an XLSX archive; defaults to 2,000. |
| `MAX_XLSX_CELLS` | Maximum populated worksheet cells; defaults to 600,000 to bound `openpyxl` object growth. |
| `UPLOAD_REQUEST_CONCURRENCY` | Maximum upload requests admitted before multipart spooling; defaults to 2 to bound aggregate memory and temporary disk. |
| `UPLOAD_PARSE_CONCURRENCY` | App-wide limit for concurrent workbook parsing/remapping; defaults to 1 to bound memory use. |
| `EXPORT_STREAM_CONCURRENCY` | Maximum concurrent XLSX response streams; defaults to 4 so slow clients cannot accumulate unbounded temporary exports. |
| `UPLOAD_RETENTION_HOURS` | Maximum age for eligible staged/run upload directories; defaults to 720 hours (30 days). |
| `UPLOAD_MAX_TOTAL_MB` | Aggregate upload-directory budget; defaults to 900 MB, with the oldest eligible runs removed first. |
| `MAINTENANCE_INTERVAL_SECONDS` | Interval for expired-token and upload-retention cleanup; defaults to 60 seconds. |

**Resolve-phase speed.** The free direct-to-XHS path sits behind an
adaptive circuit breaker: after 3 consecutive failures (datacenter IPs are
usually blocked) it is skipped — re-probing every 25th link so it can
recover — instead of every link paying the direct timeout before TikHub.
TikHub calls share one pooled HTTPS connection (no per-call TLS handshake),
and the default concurrency is 8 (`TIKHUB_CONCURRENCY`; 429s are still
honored with backoff). Re-runs stay near-instant via the permanent success
cache.

TikHub endpoints are configurable (`TIKHUB_IMAGE_NOTE_PATH`,
`TIKHUB_VIDEO_NOTE_PATH`) because TikHub versions its API; the defaults were
verified against `api.tikhub.io/openapi.json` at build time. Both accept the
raw `xhslink.com` share URL via `share_text`, so no redirect-following is
required for the authoritative path. Operational limits: 15 s timeout,
3 retries with exponential backoff, 429 `Retry-After` respected, global
concurrency ≤ 4, and a per-run cost counter shown in the UI.
Workbook URLs are restricted to `xhslink.com` and `xiaohongshu.com` hosts,
including every direct redirect hop.

## Deploying on Fly.io

All commands must run from the repo root (the directory containing
`fly.toml`) — running them elsewhere produces *"the config for your app is
missing an app name"*. Fly app names are globally unique, so pick your own:

```sh
APP=dmr-reconciler-yourname            # choose a unique name
fly apps create "$APP"
sed -i "s/^app = .*/app = \"$APP\"/" fly.toml   # keep fly.toml in sync
fly volumes create data --region sin --size 1 -a "$APP"
fly secrets set TIKHUB_API_KEY=... ANTHROPIC_API_KEY=... APP_PASSWORD=... -a "$APP"
fly deploy -a "$APP"
```

(`fly launch` also works but interactively rewrites `fly.toml`; the explicit
`fly apps create` + `fly deploy` sequence is deterministic.)

**Run exactly one machine and one uvicorn worker.** Pending header-remap
audits and finished efficiency reports live in process memory
(`app/core/token_store.py`): a second machine or `--workers N` would 404
their token URLs, and a restart drops them. This is deliberate for the
efficiency product — client workbooks are never written to disk, so there
is nothing to persist; scaling out would require a shared backing for the
remap store and a conscious decision to weaken that privacy contract.

## Evaluation harness

`tools/evaluate.py` runs the pipeline on the two real source files and diffs column S
against the human reference:

```sh
python tools/evaluate.py PLOG_DMR_CHECK.xlsx YTD_DMR_MICRO_0720.xlsx PLOG_DMR_CHECK_1.xlsx
```

It prints a confusion matrix and a per-row disagreement report. Two reference
labels are known noise (verified by direct inspection): 兔子糖糖公主Rinrin
(2026-06-26) and 宅鱼日常 (2026-06-29) are marked `无博主` by the human but
exist in the DMR file with exact-date posts. The harness lists them as
*expected* disagreements and the acceptance gate is **≥ 99/101 agreement after
excusing those two**.

## Operational notes

- `fly.toml` uses `auto_stop_machines = true` / `min_machines_running = 0` to
  keep the app free when idle. If Fly stops the machine while a run is in
  flight, the run is marked *interrupted* on the next boot and gets a one-click
  **Retry** (warm cache makes the retry cheap — resolved links are never
  re-fetched).
- The container starts as root only to `chown` the mounted `/data` volume,
  then drops to the unprivileged `appuser` before uvicorn starts.
- **Accounts**: `APP_PASSWORD` is a setup code, not a login password. `/setup`
  (requires the code) creates or resets an admin account; admins manage
  coworker accounts on `/team` (add, remove, reset passwords), and everyone
  can change their own password there. Passwords are stored as salted PBKDF2
  hashes; sessions are signed HMAC cookies, marked Secure in deployment, and
  invalidated when the account password changes. Anyone holding the setup
  code can make themselves admin, so treat it as the root secret.
- **Upload lifecycle**: compressed and expanded workbook sizes are bounded;
  rejected, invalid, and cancelled staging is deleted immediately. Expired
  in-memory reports/remap audits and eligible upload directories are reaped
  at startup and on the configured maintenance interval. Retention enforces
  both a maximum age and aggregate disk budget while protecting queued,
  running, and pending-audit runs; removal also clears matching run history.
- **The export never overwrites populated cells.** An S cell that already
  holds a value in the uploaded file is kept verbatim (when it disagrees
  with the pipeline the verdict is recorded in the evidence Notes column
  instead), and the evidence block shifts right past any column that
  already contains data. A UI override — an explicit action in this tool —
  still writes its value.
- **The DMR export window is editable** on the confirm screen: it prefills
  from the file's metadata, an edit overrides which posts count as
  超出DMR导出窗口 (expected-missing, not DMR gaps), and clearing either date
  disables the window checks. The choice is stored with the run's options
  and survives retries.
- Human overrides are stored per sheet row and win over the pipeline verdict
  in both the UI and the exports; the special choice `已匹配（清空S）` forces a
  blank column S (asserting a match), while clearing the dropdown reverts to
  the pipeline verdict.

## Tests

```sh
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

Synthetic fixtures reproduce every observed real-data edge case: emoji in
NAME (`一颗鸡蛋🥚`), the quirky headers, NO resetting per campaign, DMR
metadata rows above the header, romanized-only Blogger variants
(`gungunnnnn`), the same-blogger-different-post trap (墨池墨吟 05-13 vs
05-11), Δ4d date drift (饼饼), the 607-vs-14-likes snapshot, a dead link with
a real DMR counterpart (鸡腿子), duplicate bloggers across campaigns, and
out-of-window dates (warn, don't flag).
