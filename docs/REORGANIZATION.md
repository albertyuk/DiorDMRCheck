# Codebase Review & Reorganization Plan

**Scope:** the full repository — `app/` (17 modules, ~7,300 lines), `tests/` (14 files),
`eval.py`, 14 templates, and deployment files (~9,300 lines total).
**Method:** every module was read end-to-end; every finding below was independently
re-verified against the code (file:line references are confirmed, not sampled). One
candidate finding was refuted during verification and is excluded.

**Ground rule for everything that follows:** the migration playbook (Phase 3) is
*move-only* — no behavior changes. Behavioral flaws discovered during the review are
listed in the Appendix and should be scheduled as separate, individually-testable
changes. Mixing "move" and "fix" in one migration is how reorganizations break trust
in the diff.

---

## The one-paragraph diagnosis

This repository contains **two products** — the DMR reconciler (parse → tiered match →
LLM adjudication → annotated export) and the KOL efficiency report (analysis → PPTX
deck) — interleaved in a **single flat package**, wired together by a **983-line
`main.py`** that is simultaneously router, orchestrator for both products, auth
subsystem, and holder of two copy-pasted in-process session stores. The seams between
products run through **private-member imports** (`effreport.py`, `report.py`, and
`eval.py` all import `parsers._cell_str` / `_find_header_row`), and the domain
vocabulary (verdict constants, the Chinese column-S strings) lives inside the matching
algorithm and is **re-hardcoded in three other places**. The code *quality* is high —
docstrings are excellent, design principles are written down and mostly followed — but
the *structure* has not kept up with the second product, and every new feature now has
to thread through one file.

---

# Phase 1 — Analysis & Diagnostics

## 1.1 Architectural weaknesses

### A1. `main.py` is a god module (HIGH — the root cause of most friction)
`app/main.py` (983 lines) holds four unrelated jobs:

| Job | Where |
|---|---|
| Auth middleware, login/setup/logout, team CRUD | `main.py:85–283` |
| DMR upload → remap → preview → run → results → export orchestration | `main.py:288–794` |
| LLM header-remap audit flow (both products) | `main.py:305–649` |
| KOL efficiency flow + its in-memory report store | `main.py:797–968` |

Business decisions live directly in handlers: `POST /upload` (`main.py:451–527`) does
disk persistence, parse orchestration, the cached/LLM-audit/fail remap decision tree,
file rewriting, and token storage; `remap_apply` (`main.py:572–649`) validates the
correction form and then branches into two entirely different continuation flows
(`eff` in-memory at 616–628 vs `run` on-disk at 630–649). Nothing here is testable
without the ASGI app — and the tests prove it by reaching into module privates
(`tests/test_schema_map.py:74` pokes `main_mod._PENDING_MAPS`;
`tests/test_efficiency_web.py:45` pokes `main_mod._EFF_REPORTS`).

### A2. Session-critical state in module-level dicts (HIGH)
`_PENDING_MAPS` (`main.py:313`) and `_EFF_REPORTS` (`main.py:804`) hold raw workbook
bytes (`main.py:519`, `main.py:926`) and finished PPTX bytes (`main.py:890`) in
process-local dicts. Any deployment with >1 uvicorn worker or Fly machine makes the
`/remap/{token}` and `/efficiency/{token}` redirects land on a process that has no such
token; a restart silently loses pending audits and finished reports. This is a hard
single-process constraint that is nowhere declared. The two stores are line-for-line
copies of each other (`main.py:318–338` vs `813–833`) differing only in constants.

### A3. No client boundary around external services (MEDIUM)
The Anthropic integration is implemented **three times**, each with its own client
construction, JSON-fence stripping, and error policy: `schema_map._call_llm`
(`schema_map.py:170–197`), `adjudicator._parse_batch` (`adjudicator.py:51–123`), and
`summarize_run`'s inline call with a bare `except Exception: pass`
(`adjudicator.py:225–251`). `schema_map.py:167` and `adjudicator.py:51` even define the
same `_JSON_RE`. TikHub similarly lives entangled in `resolver.py` behind a
module-level semaphore frozen at import (`resolver.py:39`). Any cross-cutting change —
retries on 529, usage accounting, structured outputs — needs three separate edits.

### A4. Fire-and-forget run threads; recovery logic in the wrong layer (MEDIUM)
`runner.start_run` spawns a daemon thread and drops the handle (`runner.py:21–23`) —
no concurrency cap (N users → N CPU/API-heavy runs), no cancellation. The compensating
orphan-recovery logic is raw SQL in the web layer's startup hook (`main.py:76–81`),
embedding `runs`-table schema knowledge in the router. `fly.toml`'s
`auto_stop_machines` + `min_machines_running = 0` makes the kill-mid-run failure mode
routine, not exceptional.

### A5. Domain vocabulary lives inside the algorithm module and leaks as literals (MEDIUM)
The seven status constants and the Chinese column-S vocabulary `S_TEXT` are defined in
`matcher.py:25–46`, so six modules import the 586-line algorithm just for names — and
consumers have already drifted into copies: `main.py:66` re-types `无博主`, `无帖子`,
`Check链接错误`, `人工复核` as literals in `OVERRIDE_CHOICES` (while using `S_TEXT[...]`
lookups two lines later), `adjudicator.py:221` filters on the raw literal
`("MATCH",)`, and `eval.py:44–60` re-implements the whole taxonomy as substring
heuristics. A wording change in `S_TEXT` silently desynchronizes three copies.

### A6. Implicit, stringly-typed contracts at the seams (MEDIUM)
- `_attempt_remap` returns variant tuples of differing arity — `("cached", dict)` /
  `("audit", prop, choices, sig)` / `("fail", str|None)` — positionally unpacked at
  `main.py:493` and `main.py:924`, with the three-branch dispatch duplicated in both
  flows (`main.py:482–511` vs `915–936`).
- `adjudicator.py:70` decides the LLM question type via
  `v.tier.endswith("name-conflict")` — renaming a tier string silently flips every
  question to `same_post`.
- The run result document (`result_json`) has no owning schema: its shape is hand-built
  in `runner.py:77–102`, its `plog_meta`/`dmr_meta` blocks duplicate `main.py:419–434`
  key-for-key, and `load_verdicts` must `d.pop("column_s", None)` (`runner.py:122`) to
  undo a derived key `Verdict.to_dict` injects (`matcher.py:135`).

### A7. `Verdict` is a ~35-field god record (MEDIUM)
`matcher.py:67–136` mixes row identity, resolution evidence, match evidence, `llm_*`
outputs, `perimeter_*` evidence, the presentation renderer `column_s()`, and a
constant 40-word caveat stored per-instance (`matcher.py:94`) so it is serialized into
every row of every audit JSON. Every subsystem couples to the full record; adding any
tier widens this one class and its three serialization consumers.

### A8. Config as import-time globals (MEDIUM)
All settings materialize at import (`config.py:17–58`), including a filesystem probe
for `DATA_DIR`. Consequences: every test monkeypatches `config` attributes instead of
the environment; `resolver.py:39` freezes a semaphore from `TIKHUB_CONCURRENCY` at
import while `matcher.py:517` sizes a thread pool from the same knob — two mechanisms
for one policy. (`config.py:50`'s `APP_SECRET` fallback is a security flaw — see
Appendix.)

### A9. `db.py` fuses connection, migrations, and unguarded SQL assembly (MEDIUM)
`run_update` builds its SET clause from `**fields` keys via f-string with no whitelist
(`db.py:192–194`); the sibling `run_bump_counter` whitelists via a strippable `assert`
(`db.py:219`). `connect()` performs schema creation, additive ALTERs, and a destructive
`DROP TABLE overrides` behind a process-global `_initialized` flag (`db.py:99–128`),
forcing tests that swap `DB_PATH` to also reset `db._initialized` by hand.

### A10. Hidden state mutation during preview (MEDIUM)
`_finish_upload` calls `perimeter_mod.ingest` while rendering an *unconfirmed* preview
(`main.py:398–399`), and `ingest` unconditionally replaces the app-wide
`current_perimeter` setting (`perimeter.py:252`). Abandoning the preview still swaps
the global perimeter for every other user. The cache-hit path returns a hollow
`PerimeterParse` (`perimeter.py:243–246`) so re-uploads silently drop parse warnings.

### A11. No packaging (MEDIUM)
No `pyproject.toml`/`setup.py`. Tests bootstrap with `sys.path.insert`
(`tests/conftest.py:9`); everything relies on cwd-relative imports; `eval.py` must
physically sit at repo root to import the app — and ships in the production image
(`Dockerfile` COPYs it).

## 1.2 Structural friction (the new-developer experience)

- **S1. No product boundary.** Nothing signals that `effreport.py` + `deck.py` are a
  self-contained second product — the only wiring point is `main.py`'s import list, and
  the efficiency product silently borrows the reconciler's private parser internals
  (`effreport.py:27–28`), so refactoring "the other product" breaks this one.
- **S2. Confusable names.** `report.py` (reconciler xlsx writer), `effreport.py`
  (efficiency *analysis engine* — not a report writer), `deck.py` (efficiency PPTX
  renderer). "Fix the report" has three plausible targets, and the natural guess —
  that `effreport.py` is a variant of `report.py` — is wrong: they share zero code.
- **S3. Agent-noun soup.** `runner` / `matcher` / `resolver` / `adjudicator` give no
  pipeline-order signal (runner orchestrates matcher; matcher calls resolver per row;
  adjudicator is the optional Tier-4 pass). Export logic even hides in the thread
  spawner: `load_verdicts` (`runner.py:115`) is consumed only by the export endpoints.
- **S4. The remap-audit hinge is invisible.** `POST /remap/{token}/apply` serves both
  products and branches on `entry["flow"] == "eff"` (`main.py:616`);
  `schema_map.FIELDS` hardcodes the column schemas of *both* products
  (`schema_map.py:49–96`). A dev tracing either product won't discover this shared
  choke point until a change for one breaks the other's upload flow.
- **S5. Flat templates.** 14 templates for two products, auth, and HTMX partials in
  one directory, with only an underscore convention separating fragments from pages.
  The reconciler landing page embeds efficiency-product marketing copy
  (`index.html:44–60`).
- **S6. The test suite pins the current layout.** Tests import underscore members from
  at least five app modules and one test file imports fixture builders from another
  (`tests/test_efficiency_web.py:13` ← `tests/test_effreport.py`). Any rename breaks
  dozens of tests that never tested public behavior. *(Treat the list of private test
  imports as the checklist of what needs a public seam.)*
- **S7. `eval.py` misleads at first glance.** Repo root, shadows a builtin, looks like
  an entry point, is actually a QA harness — with its own copy of the verdict taxonomy
  and `S_COL = 19` (`eval.py:41` duplicating `report.py:19`).
- **S8. Domain knowledge in the wrong shared layer.** `normalize.py` — the one
  legitimately shared text utility — also owns `HEX24`/`is_hex24`
  (`normalize.py:75–80`), the Xiaohongshu note-id rule, which is pure reconciler
  domain knowledge (and `parsers.py:360` re-inlines the same regex anyway).

## 1.3 Consistency review (duplicate logic & conflicting conventions)

| # | Duplication | Sites | Drift already? |
|---|---|---|---|
| C1 | Column-S verdict vocabulary | `matcher.py:37–45` · `main.py:66` literals · `eval.py:44–60` heuristics · `adjudicator.py:221` raw `"MATCH"` | Yes — mixed literal/lookup style inside one list |
| C2 | TTL+cap token store | `main.py:318–338` vs `main.py:813–833` | Constants only; logic identical |
| C3 | Remap decision tree (cached/audit/fail) | `main.py:482–511` vs `main.py:915–936` | Plumbing differs (disk vs memory) |
| C4 | Excel row-extraction skeleton (cell access, link extraction, forward-fill, blank-run defense) | `parsers.py:170–202` · `parsers.py:347–354` · `effreport.py:160–177` | Yes — DMR's `_extract_link_target` requires `http`, the other two accept any text |
| C5 | `_cell_str` | `parsers.py:29–34` vs `schema_map.py:104–110` | Yes — schema_map's truncates to 60 chars, same name |
| C6 | Name-ladder algorithm (cjk-substring → norm-substring → ascii-fuzzy ≥85 → pinyin) | `matcher.py:174–191` vs `perimeter.py:200–234` | Thresholds owned inconsistently: `FUZZY_CUTOFF=85` named in perimeter, hardcoded twice in matcher |
| C7 | plog/dmr meta dict schema | `main.py:419–434` (preview) vs `runner.py:89–101` (result) | Yes — per-campaign counts vs plain list |
| C8 | Epoch + engagement-count extraction | `resolver.py:156–168` vs `resolver.py:265–283` | Yes — camelCase/snake_case preference order inverted between the two copies |
| C9 | LLM verdict vocabulary `SAME_PERSON…UNSURE` | `adjudicator.py:25` (regex) · `:40` (prompt) · `:154` (schema hint) | Three synchronized string edits per change |
| C10 | `HEADER_SCAN_ROWS` | `parsers.py:21` vs `schema_map.py:34` (`SAMPLE_ROWS = 15  # matches parsers.HEADER_SCAN_ROWS`) | Comment admits the unlinked contract |
| C11 | Bilingual status labels | `S_TEXT` (matcher) · `STATUS_BADGES` (`main.py:57–65`) · i18n catalog | Three mechanisms for one concern; badges invisible to the language toggle |
| C12 | i18n catalog keyed on exact emitted English | `i18n.py` (~420 entries + ~40 regexes) vs every emit site | Yes — `"file(s)"` vs `"file"` already needs two rows with identical Chinese (`i18n.py:65–68`) |

Convention inconsistencies: nine function-local imports where module-level is the
codebase norm, one outright redundant (`deck.py:384` re-imports `VerificationError`
already imported at `deck.py:30`); `runner.py:117` locally imports `Candidate` despite
a module-level matcher import at line 16.

---

# Phase 2 — The Reorganization Proposal

## 2.1 Target folder tree

```
DiorDMRCheck/
├── pyproject.toml                  # NEW — installable package, single dependency source
├── Dockerfile                      # updated paths; eval harness no longer shipped
├── fly.toml
├── entrypoint.sh
├── README.md
├── docs/
│   └── REORGANIZATION.md           # this document
│
├── app/
│   ├── __init__.py
│   ├── main.py                     # create_app() factory + router mounting ONLY (~80 lines)
│   ├── config.py                   # Settings object built once at startup, injectable in tests
│   │
│   ├── core/                       # product-agnostic infrastructure (leaf layer — imports nothing above)
│   │   ├── __init__.py
│   │   ├── db.py                   # connection factory only
│   │   ├── migrations.py           # schema init/ALTERs, explicit init(db_path) — out of connect()
│   │   ├── llm.py                  # THE Anthropic boundary: client, call, fence-strip, JSON parse, retry, counters
│   │   ├── token_store.py          # one parameterized TTL+cap store (replaces both main.py copies)
│   │   ├── textnorm.py             # normalize.py minus HEX24 (nfkc, norm, cjk, ascii_part, header_key)
│   │   └── xlsx.py                 # promoted, public: cell_str, find_header_row, to_date, to_int, to_float,
│   │                               #   extract_link_target, iter_data_rows (blank-run defense), forward_fill
│   │
│   ├── auth/
│   │   ├── __init__.py
│   │   ├── service.py              # sessions, hashing, username/password policy (from auth.py + route inline checks)
│   │   └── routes.py               # /login /logout /setup /team + auth middleware
│   │
│   ├── i18n/
│   │   ├── __init__.py             # get_lang, make_t, make_td, context (the only FastAPI-aware part)
│   │   └── catalog/
│   │       ├── common.py           # auth/shared UI strings
│   │       ├── reconciler.py       # reconciler strings + patterns
│   │       └── efficiency.py       # efficiency strings + patterns   (merged at startup)
│   │
│   ├── remap/                      # shared LLM header-mapping subsystem (used by BOTH products)
│   │   ├── __init__.py
│   │   ├── mapper.py               # schema_map.py core: build_sample, signature, propose, apply_mapping, cache
│   │   ├── registry.py             # FIELDS/KIND_LABELS — each product registers its own entries
│   │   ├── service.py              # attempt_remap decision tree → typed RemapOutcome; pending-map TokenStore
│   │   └── routes.py               # GET/POST /remap/{token} — continuation via per-product callbacks
│   │
│   ├── reconciler/                 # ═══ PRODUCT 1: DMR reconciliation ═══
│   │   ├── __init__.py
│   │   ├── domain.py               # status constants, S_TEXT, NAME_MISLABEL, ENGAGEMENT_CAVEAT,
│   │   │                           #   Verdict (composed of evidence sub-records), Candidate,
│   │   │                           #   HEX24/is_hex24, column_s rendering
│   │   ├── documents.py            # RunResult / PlogMeta / DmrMeta — the ONE owner of result_json shape
│   │   ├── parsers.py              # parse_plog, parse_dmr (on core.xlsx; nothing private leaks)
│   │   ├── pipeline.py             # was matcher.py — tiers 0-3 algorithm only
│   │   ├── links.py                # was resolver.py — TikHubClient class owning throttle+retry, resolution cache
│   │   ├── adjudicator.py          # Tier-4 LLM pass (on core.llm)
│   │   ├── perimeter.py            # parse/cache separated from promote-to-current
│   │   ├── reverse_audit.py
│   │   ├── runs.py                 # was runner.py — execution service: bounded pool, registry,
│   │   │                           #   recover_orphans(), load_verdicts moved OUT (→ export.py)
│   │   ├── export.py               # was report.py + load_verdicts + build_audit_json
│   │   ├── presentation.py         # STATUS_BADGES + OVERRIDE_CHOICES derived from domain.S_TEXT
│   │   └── routes.py               # /, /upload, /runs/*, /perimeter/*
│   │
│   ├── efficiency/                 # ═══ PRODUCT 2: KOL efficiency report ═══
│   │   ├── __init__.py
│   │   ├── analysis.py             # was effreport.py (on core.xlsx; TEXTS moves to deck.py, its only consumer)
│   │   ├── deck.py                 # PPTX renderer (+ TEXTS)
│   │   └── routes.py               # /efficiency* + report TokenStore instance
│   │
│   ├── templates/
│   │   ├── base.html
│   │   ├── shared/                 # error.html, remap_audit.html
│   │   ├── auth/                   # login.html, setup.html, team.html
│   │   ├── reconciler/             # index.html, preview.html, run.html, _results.html,
│   │   │                           #   _progress.html, _error_panel.html
│   │   └── efficiency/             # efficiency.html, efficiency_report.html, _promo_card.html
│   └── static/
│
├── tools/
│   └── evaluate.py                 # was eval.py — imports ONLY public API (domain.S_TEXT, export.S_COL);
│                                   #   not shipped in the Docker image
│
└── tests/
    ├── conftest.py                 # no sys.path hack (editable install)
    ├── fixtures.py                 # + build_eff_bytes/EFF_HEADERS moved from test_effreport.py
    ├── core/                       # test_normalize, test_xlsx (new, for the promoted helpers)
    ├── reconciler/                 # test_parsers, test_matcher, test_perimeter, test_report, test_regressions
    ├── efficiency/                 # test_effreport
    └── web/                        # test_auth, test_i18n, test_guide, test_schema_map, test_efficiency_web
```

## 2.2 Rationale — why these groupings

1. **Product packages first (`reconciler/`, `efficiency/`), layers inside.** The single
   highest-friction fact today is that two products share one flat namespace. A
   feature-first split makes the boundary a *directory*, so "does this change affect
   the other product?" becomes answerable by `git diff --stat`. Inside each product the
   ordering is layered: `routes.py` (HTTP boundary) → orchestration (`runs.py`,
   `service` functions) → domain/algorithms (`pipeline.py`, `analysis.py`) →
   `core/` clients. Routes may import domain; domain never imports routes.

2. **`core/` is a true leaf.** Everything in `core/` is product-agnostic and imports
   nothing from `auth/`, `remap/`, or the product packages. This is what makes the
   private-import problem *structurally impossible* to recreate: `cell_str` and
   `find_header_row` become public members of `core/xlsx.py` with one owner, instead of
   underscore names that three consumers pretend not to depend on. `HEX24` moves *out*
   of the shared text module into `reconciler/domain.py` because it is Xiaohongshu
   domain knowledge, not text normalization.

3. **`domain.py` gives the vocabulary one home.** Six modules currently import the
   586-line matching algorithm just to get constant names, and three more re-type the
   strings. A small leaf `domain.py` (constants + `S_TEXT` + `Verdict`/`Candidate`)
   lets the web layer, adjudicator, export writer, eval harness, and tests import
   vocabulary without pulling in `httpx`/`rapidfuzz` transitively — and makes
   `OVERRIDE_CHOICES`/`STATUS_BADGES` *derivable* instead of re-typed.

4. **`remap/` is promoted to a named shared subsystem.** Today the header-remap flow is
   the hidden hinge fusing both products through one route handler and one `FIELDS`
   dict. Making it a package with a registry (each product registers its own field
   schema) and typed outcomes (`RemapOutcome` dataclass instead of variant tuples)
   turns an invisible coupling into a declared, documented one — and collapses the
   duplicated three-branch tree in `/upload` and `/efficiency` into one service call
   with per-product continuations.

5. **External services get client objects (`core/llm.py`, `links.TikHubClient`).**
   Three hand-rolled Anthropic call sites and a module-level semaphore become two
   client classes owning construction, throttling, retry, JSON extraction, and
   counters. This is the seam that makes model migration, 529-retry policy, and usage
   accounting one-file changes — and makes both clients trivially fakeable in tests.

6. **Templates mirror the package split.** Same mental model in both trees; a dev
   editing `templates/reconciler/` knows the blast radius. The cross-promo card the
   reconciler landing page carries for the efficiency product becomes an includable
   partial *owned by* `templates/efficiency/`.

7. **`tools/` + `pyproject.toml` end the cwd-coupling.** An editable install makes
   `app` importable from anywhere, deletes the `sys.path` hack, lets the eval harness
   live outside the package (and outside the production image), and un-shadows the
   `eval` builtin.

## 2.3 Design patterns to enforce

| Pattern | Where | What it buys |
|---|---|---|
| **App factory + per-feature `APIRouter`** | `main.py` + each `routes.py` | `main.py` shrinks to wiring; each router testable in isolation; FastAPI-idiomatic |
| **Feature-first packaging, layered internally** | `reconciler/`, `efficiency/` | Product blast-radius visible in the tree; layers prevent web→domain leaks |
| **Ports & adapters (lightweight)** | `core/llm.py`, `links.TikHubClient`, `core/db.py` | External services behind constructable clients — swappable, fakeable, one retry policy |
| **Single-owner vocabulary** | `reconciler/domain.py`, `remap/registry.py` | Constants defined once, derived everywhere; ends the four-copy verdict taxonomy |
| **Typed contracts at seams** | `RemapOutcome`, `documents.RunResult`, composed `Verdict` | Variant tuples and hand-mirrored dict shapes become schema-checked dataclasses |
| **Parameterized utility over copy-paste** | `core/token_store.py` | One TTL+cap store, two instances; the single-process constraint lives in ONE class that can later grow a SQLite backing |
| **Explicit lifecycle over import-time side effects** | `config.Settings`, `core/migrations.py`, run pool in `runs.py` | Startup order visible in `create_app()`; tests inject settings instead of monkeypatching module globals |

**Anti-patterns to reject during review:** any new import of an underscore name across
module boundaries; any new string literal from `S_TEXT`'s vocabulary outside
`domain.py`; any route function >40 lines (extract to a service function); any new
`import anthropic`/`httpx` outside the two client modules.

---

# Phase 3 — The Migration Playbook

**Do not execute yet.** Sequencing rules: each step lands as its own PR, green before
the next starts; steps 2–4 are *move-only* (behavior fixes → Appendix); after every
step run the **route-table diff** and the **golden-export check** described in Step 1.

### Step 1 — Initial setup (no code moves yet)

1. Add `pyproject.toml` (setuptools or hatchling; package = `app`; console entry
   optional) with dependencies mirrored from `requirements.txt`. Keep
   `requirements*.txt` during transition; CI/Docker switch later.
2. `pip install -e .` in dev/CI; delete the `sys.path.insert` from
   `tests/conftest.py:8–9` and verify `pytest` still passes from any cwd.
3. Create the empty skeleton: `app/core/`, `app/auth/`, `app/remap/`,
   `app/reconciler/`, `app/efficiency/`, `app/i18n/` (as package), `tools/`,
   `tests/{core,reconciler,efficiency,web}/` — each with `__init__.py`. No moves yet.
4. **Build the two safety harnesses this migration will be judged by:**
   - *Route-table snapshot:* a tiny test that dumps `[(r.path, sorted(r.methods)) for
     r in app.routes]` and compares against a committed snapshot. The route table must
     be **identical** after every step.
   - *Golden export:* run the pipeline on the test fixtures (no network keys, LLM off)
     and snapshot the export.xlsx cell values + audit JSON (normalize timestamps).
     Byte-level equivalence of values is the bar for "move-only".
5. Move `build_eff_bytes`/`EFF_HEADERS` from `tests/test_effreport.py` into
   `tests/fixtures.py` (kills the test→test import that would otherwise break in Step 4).

**Gate:** full suite green; snapshots committed.

### Step 2 — Core utilities and services (bottom-up, so nothing above breaks)

1. `core/textnorm.py`: move `normalize.py` content **minus** `HEX24`/`is_hex24`.
   Leave `app/normalize.py` as a one-line re-export shim (`from .core.textnorm import *`
   plus `from .reconciler.domain import HEX24, is_hex24` once step 2.3 lands) so
   nothing breaks mid-step; shims are deleted in Step 4.
2. `core/xlsx.py`: promote `parsers._cell_str → cell_str`, `_find_header_row →
   find_header_row`, `_to_date/_to_int` (+ a public `to_float` absorbing
   `effreport._to_float`), `HEADER_SCAN_ROWS`, `MAX_CONSECUTIVE_BLANK_ROWS`, plus
   extracted `extract_link_target` and `iter_data_rows` (the blank-run skeleton).
   Point `parsers.py`, `effreport.py`, `report.py:87`, `schema_map.py` (its `_cell_str`
   becomes a truncating wrapper), and `eval.py` at the public names. *Keep the
   http-vs-any-text link divergence (C4) as-is behind two named functions — unifying it
   is a behavior change.*
3. `reconciler/domain.py`: move status constants, `S_TEXT`, `NAME_MISLABEL`,
   `ENGAGEMENT_CAVEAT`, `Verdict`, `Candidate`, `column_s`, `HEX24`/`is_hex24` out of
   `matcher.py`/`normalize.py`. `matcher.py` re-exports temporarily. Re-derive
   `OVERRIDE_CHOICES` and `eval.py`'s `classify` from `S_TEXT`; replace
   `adjudicator.py:221`'s raw `("MATCH",)` with the constant. (Composing `Verdict` into
   sub-records is deferred — it changes serialized shape.)
4. `core/token_store.py`: one `TokenStore(ttl, cap)` class; `main.py`'s two stores
   become two instances (same TTLs/caps as today). Tests that poked
   `main_mod._PENDING_MAPS`/`_EFF_REPORTS` now use the store's public API.
5. `core/llm.py`: extract client construction + text joining + fence-strip/JSON
   extraction + the existing per-site retry behavior; `schema_map`, `adjudicator`, and
   `summarize_run` consume it. *Preserve each site's current error policy exactly*
   (including `summarize_run`'s swallow-all) — tightening it is a behavior change.
6. `core/db.py` + `core/migrations.py`: split `connect()` into a pure connection
   factory and an explicit `migrations.init(db_path)` called from startup (and from
   test fixtures, replacing the `db._initialized` monkeypatching).

**Gate:** suite green; route-table + golden-export snapshots unchanged; `grep -rn
"import _" app/ tools/ tests/` shows no cross-module underscore imports.

### Step 3 — Product packages and feature modules

1. Move + rename, updating imports as you go (one commit per module keeps blame
   useful): `matcher.py → reconciler/pipeline.py`, `resolver.py → reconciler/links.py`,
   `adjudicator.py`, `parsers.py`, `perimeter.py`, `reverse_audit.py` →
   `reconciler/`; `runner.py → reconciler/runs.py` **with `load_verdicts` moving to
   `reconciler/export.py`** (joining `report.py`'s content + `build_audit_json`);
   `effreport.py → efficiency/analysis.py`, `deck.py → efficiency/deck.py` (move
   `TEXTS` into deck.py — its only consumer); `schema_map.py → remap/mapper.py` with
   `FIELDS` split into `remap/registry.py` entries registered by each product.
2. Carve `main.py` into routers: `auth/routes.py` (login/setup/team + middleware +
   `current_user`), `reconciler/routes.py` (dashboard, upload, runs, overrides,
   exports, perimeter), `remap/routes.py` (audit screens; the `_attempt_remap` tree
   becomes `remap/service.py` returning a `RemapOutcome` dataclass, with the eff/run
   continuations passed in by each product's routes), `efficiency/routes.py`.
   `main.py` becomes `create_app()`: settings, templates env, middleware, router
   mounting, startup (`migrations.init` + `runs.recover_orphans()` — the raw SQL moves
   behind a `runs` API). `STATUS_BADGES`/`OVERRIDE_CHOICES` → `reconciler/presentation.py`.
3. Split templates into `shared/ auth/ reconciler/ efficiency/` and update
   `TemplateResponse` names. Extract the index-page promo card into
   `templates/efficiency/_promo_card.html` included from the reconciler index.
4. Split the i18n catalog along the section comments that already exist in `i18n.py`
   into `i18n/catalog/{common,reconciler,efficiency}.py`, merged at startup; move
   `get_lang`/`make_t`/`make_td`/`context` into `i18n/__init__.py`. Coverage tests
   keep passing because the merged dict is unchanged.

**Gate:** suite green; route-table snapshot **identical**; golden export unchanged;
`app/main.py` under ~100 lines.

### Step 4 — Import paths, routing cleanup, and shim removal

1. Delete every re-export shim from Steps 2–3 (`normalize.py`, `matcher.py`,
   `runner.py`, `schema_map.py` stubs). Fix all remaining imports:
   `grep -rn "from app\.\(normalize\|matcher\|runner\|schema_map\|effreport\|resolver\|report\b\)" app/ tests/ tools/`
   must return nothing.
2. Move `eval.py → tools/evaluate.py`; it now imports `reconciler.domain.S_TEXT` and
   `reconciler.export.S_COL` instead of duplicating them, and `core.xlsx` instead of
   parser privates. Update its README mention.
3. Reorganize tests into the mirrored directories; replace remaining private-member
   test imports with the public seams created in Step 2 (any test that *can't* be
   rewritten against a public seam is telling you a seam is still missing — add it,
   don't re-privatize).
4. Update `Dockerfile`: drop `eval.py` from the image, switch to installing the
   package (`pip install .`) or keep requirements.txt — either way `uvicorn
   app.main:app` still works because the factory assigns module-level `app`.
   Verify `entrypoint.sh` and `fly.toml` need no changes (they reference the ASGI
   path and ports only).
5. Hoist the nine function-local imports to module level (delete `deck.py:384`'s
   redundant one outright); keep only the deliberately-lazy `anthropic`/`pandas`
   imports, each with a one-line comment saying why.

**Gate:** suite green from a *fresh clone* (`pip install -e . && pytest`); grep sweeps
clean; `docker build` succeeds and the container boots to `/healthz`.

### Step 5 — Post-migration verification

1. **Automated:** full `pytest` (now organized per package); route-table snapshot
   equality; golden-export equality (values + audit JSON); i18n coverage tests
   (catalog unchanged ⇒ still green); `tools/evaluate.py` run on the reference
   workbook produces the same score as the pre-migration baseline (record it in
   Step 1).
2. **Manual smoke (staging/Fly):** both product flows end-to-end —
   (a) upload PLOG+DMR → preview → run → results → override → export.xlsx +
   export.json; (b) upload an *unfamiliar-header* workbook → LLM remap → audit screen
   → approve → run (exercises the remap hinge, the highest-risk seam moved);
   (c) efficiency upload → report page → deck.pptx download; (d) login/setup/team;
   (e) language toggle on each page.
3. **Deploy watch:** ship to Fly, confirm startup recovery marks no false orphans and
   `/healthz` stays green; keep the previous image tag ready for rollback.
4. Retire `requirements.txt` in favor of `pyproject.toml` extras once CI and the
   Dockerfile both consume the package (optional, can trail).
5. Only after this gate: open the Appendix items as individual issues.

---

## Appendix — Behavioral fixes found during review (schedule SEPARATELY, after the migration)

These are real, verified issues, deliberately **excluded** from the move-only playbook:

1. **`APP_SECRET` derivation (security).** `config.py:50` falls back to
   `"dmr-" + APP_PASSWORD` — the cookie-signing key is derivable from the setup code
   admins hand to coworkers; anyone who ever saw the setup code can forge a session
   cookie for any username (and with both unset the key is the constant `"dmr-"`).
   Require an independent secret or generate-and-persist one.
2. **`run_update` SQL column whitelist.** `db.py:192` — whitelist columns and *raise*
   (the `assert` in `run_bump_counter` at `db.py:219` disappears under `python -O`).
3. **`schema_map.signature()` hashes data rows** (`schema_map.py:113–135`), so the
   same layout with different data misses the approved-mapping cache and re-audits the
   human — defeating the module's core promise. Key the signature on layout only.
4. **Perimeter ingest during preview** (`main.py:398`, `perimeter.py:252`) mutates
   global state before user confirmation and returns a hollow parse on cache hit.
   Separate parse/cache from promote-to-current; tie promotion to run confirmation.
5. **Run concurrency cap + registry** (`runs.py`): bound the worker pool; consider Fly
   `auto_stop` interaction (a run in flight should hold the machine or persist enough
   state to resume).
6. **Token stores under multi-worker deployment:** give `TokenStore` an optional
   SQLite backing so `/remap/{token}` and `/efficiency/{token}` survive restarts and
   worker counts >1 — or document the single-process constraint in `fly.toml` and
   `Dockerfile` explicitly.
7. **Unify the name-ladder policy** (C6): one policy module for rungs + thresholds
   consumed by both `pipeline.py` and `perimeter.py` (behavioral only if the copies
   have already drifted — verify with characterization tests first).
8. **i18n message-ID migration** (C12): long-term, emit message IDs instead of keying
   on exact English text; short-term, add a lint that flags catalog rows with
   identical Chinese values (wording-drift detector).
9. **`Verdict` decomposition** (A7): compose from evidence sub-records and emit
   `ENGAGEMENT_CAVEAT` once per document instead of per row (changes audit JSON shape —
   coordinate with any consumers).

---

# Execution log (2026-07-21)

The playbook above was executed in full on this branch. Every step landed as
its own commit with the full suite green and both safety snapshots
(route table, golden export) byte-identical throughout:

| Step | Commit | Result |
|---|---|---|
| 1 — packaging + harnesses | `cf1f434` | pyproject.toml; route-table + golden-export snapshots; fixture move |
| 2 — core seams | `553270d` | core/{textnorm,xlsx,token_store,llm,migrations}.py; reconciler/domain.py |
| 3a — package moves | `e89152e` | all modules into reconciler/ efficiency/ remap/ core/ (git-mv, history kept) |
| 3b — router carve | `49db212` | main.py → assembly only; auth/ package; web.py; per-product routers; templates split |
| 3c — i18n split | `9fe1558` | i18n package, catalogs merged at import (verified identical: 324 entries, 47 patterns) |
| 4 — cleanup | *(this branch)* | shims deleted; tools/evaluate.py; tests mirrored; imports hoisted |

**Verification performed:** full suite (134 passed, 1 skipped — the skip is
the real-client-workbook golden test, absent by design) from the working
tree AND from a fresh clone with `pip install -e .`; route-table snapshot
identical; golden-export snapshot identical; `uvicorn app.main:app` boots
and serves `/healthz`, `/`, and `/efficiency`; `tools/evaluate.py --help`
runs against the installed package; grep sweeps for every legacy module
path return nothing. **Not verified here:** `docker build` (no daemon in
the execution sandbox) — the only image change is dropping `eval.py` from
a COPY line, but run the build in CI before deploying.

## Deviations from the plan (all deliberate, none behavioral)

1. **`app/web.py` added** (not in the Phase-2 tree): the shared Jinja
   environment, `current_user`, and translator helpers. Routers and
   `main.py` both need these; a separate leaf module avoids a circular
   import between routers and the app assembly.
2. **`core/xlsx.py` does not include `iter_data_rows`/forward-fill.** The
   three parse loops keep their inline blank-run counters — extracting the
   loop skeleton would restructure control flow, which crosses the
   move-only line. Remains open as a follow-up (Appendix/C4).
3. **`reconciler/documents.py` was not created.** Typing the run-result
   document changes its serialized shape risk surface; deferred with the
   Verdict decomposition (Appendix items 9 and A6's document schema).
4. **db init is keyed per DB path** inside `connect()` rather than being a
   startup-only call: the runner thread and the eval harness open
   connections outside web startup, so fully lazy-free init would have
   changed their behavior. The schema/migration logic itself did move to
   `core/migrations.py`, and tests no longer reset a private flag.
5. **i18n:** the sections with clear owners moved to
   `catalog/{reconciler,efficiency}.py`; the per-template static-string
   blob (mixed sources) stayed in `catalog/common.py` pending a
   per-template attribution pass.
6. **`extract_link_target` divergence kept as documented behavior** (strict
   http-only DMR variant in core/xlsx; PLOG/efficiency keep their
   permissive inline extraction) — unifying it is a behavior change (C4).
