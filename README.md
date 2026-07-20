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

## Running locally

```sh
pip install -r requirements.txt
uvicorn app.main:app --port 8080
# open http://localhost:8080
```

Environment (all optional — the app degrades gracefully):

| Var | Purpose |
|---|---|
| `TIKHUB_API_KEY` | XHS link resolution (authoritative path). Without it, only the free direct-redirect path runs — usually blocked from datacenter IPs. |
| `ANTHROPIC_API_KEY` | Tier-4 adjudication + bilingual run summary. |
| `ANTHROPIC_MODEL` | Defaults to `claude-sonnet-5` (the current Sonnet, verified at build time — the older `claude-sonnet-4-6` works as an override). |
| `APP_PASSWORD` | Enables the login wall (session cookie). Set it — this handles client campaign data. |
| `DATA_DIR` | SQLite + uploads location. Defaults to `/data` when present (Fly volume), else `./data`. |

TikHub endpoints are configurable (`TIKHUB_IMAGE_NOTE_PATH`,
`TIKHUB_VIDEO_NOTE_PATH`) because TikHub versions its API; the defaults were
verified against `api.tikhub.io/openapi.json` at build time. Both accept the
raw `xhslink.com` share URL via `share_text`, so no redirect-following is
required for the authoritative path. Operational limits: 15 s timeout,
3 retries with exponential backoff, 429 `Retry-After` respected, global
concurrency ≤ 4, and a per-run cost counter shown in the UI.

## Deploying on Fly.io

```sh
fly launch --no-deploy         # uses the checked-in fly.toml (region hkg)
fly volumes create data --region hkg --size 1
fly secrets set TIKHUB_API_KEY=... ANTHROPIC_API_KEY=... APP_PASSWORD=...
fly deploy
```

## Evaluation harness

`eval.py` runs the pipeline on the two real source files and diffs column S
against the human reference:

```sh
python eval.py PLOG_DMR_CHECK.xlsx YTD_DMR_MICRO_0720.xlsx PLOG_DMR_CHECK_1.xlsx
```

It prints a confusion matrix and a per-row disagreement report. Two reference
labels are known noise (verified by direct inspection): 兔子糖糖公主Rinrin
(2026-06-26) and 宅鱼日常 (2026-06-29) are marked `无博主` by the human but
exist in the DMR file with exact-date posts. The harness lists them as
*expected* disagreements and the acceptance gate is **≥ 99/101 agreement after
excusing those two**.

## Tests

```sh
python -m pytest tests/ -q
```

Synthetic fixtures reproduce every observed real-data edge case: emoji in
NAME (`一颗鸡蛋🥚`), the quirky headers, NO resetting per campaign, DMR
metadata rows above the header, romanized-only Blogger variants
(`gungunnnnn`), the same-blogger-different-post trap (墨池墨吟 05-13 vs
05-11), Δ4d date drift (饼饼), the 607-vs-14-likes snapshot, a dead link with
a real DMR counterpart (鸡腿子), duplicate bloggers across campaigns, and
out-of-window dates (warn, don't flag).
