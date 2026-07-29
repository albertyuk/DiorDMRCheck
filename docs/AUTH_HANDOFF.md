# Auth & Account Handoff — DMR Reconciler

A self-contained specification of the login / account-creation subsystem, written
so it can be re-implemented in another codebase (any language or framework)
without reading this one. Everything below is the behavior of the running system
as of commit `5d4e054`; file references point at the reference implementation in
this repo for anyone who wants to compare.

Reference implementation (FastAPI + SQLite, stdlib crypto only):

| Concern | File |
|---|---|
| Hashing, sessions, tokens, email validation | `app/auth/service.py` |
| Login/setup throttling | `app/auth/throttle.py` |
| Routes + session-gate middleware | `app/auth/routes.py` |
| Transactional email (Resend) | `app/auth/mailer.py` |
| User/token storage | `app/core/db.py`, `app/core/migrations.py` |
| Config | `app/config.py` |
| Pages | `app/templates/auth/*.html` |
| Tests (executable spec) | `tests/web/test_auth.py`, `tests/web/test_email_accounts.py` |

---

## 1. The mental model

This is a **closed-team** app, not a public sign-up service. Three ideas carry
the whole design:

1. **`APP_PASSWORD` is a *setup code*, not a login password.** It is a server
   secret whose only power is creating (or resetting) an **admin** account via
   `/setup`. Nobody logs in with it day-to-day. Anyone holding it can take over
   the app, so treat it as the root secret.
2. **Accounts are created by admins, never by self-signup.** An admin adds a
   coworker on `/team` — preferably by emailing an invite link so the admin
   never knows the coworker's password. `/setup` is the bootstrap + recovery
   path only.
3. **Email addresses must prove ownership before they can reset passwords.**
   An address is attached *unconfirmed*; a confirmation link sent to that inbox
   flips it to confirmed; only confirmed addresses ever receive reset links.
   (Rationale: an unconfirmed address may be a typo pointing at a stranger's
   inbox, and a reset link there hands them the account.)

Everything degrades gracefully: with no email provider configured, invites and
reset disappear and admins fall back to setting initial passwords by hand; with
no `APP_PASSWORD`, the app refuses to serve (fail-closed) unless an explicit
open-access opt-out is set.

---

## 2. Configuration

| Env var | Default | Meaning |
|---|---|---|
| `APP_PASSWORD` | *(required)* | The setup code. When unset the app fails startup validation; the middleware also independently returns 503 (fail-closed) unless `ALLOW_OPEN_ACCESS` is truthy. |
| `ALLOW_OPEN_ACCESS` | `0` | Explicit opt-out for local no-auth mode. Must never default on — a missing deployment secret must not silently expose data. |
| `APP_SECRET` | *(unset)* | Session-cookie signing key. When unset, a random 64-hex-char secret is generated once and persisted to `DATA_DIR/session_secret` (file mode `0600`) so sessions survive restarts. **Deliberately not derived from `APP_PASSWORD`** — the setup code is shared with teammates and must not let its holders forge cookies. |
| `SESSION_COOKIE_SECURE` | `1` | `Secure` flag on the session cookie. Turn off only for plain-HTTP local dev. |
| `RESEND_API_KEY` | *(unset)* | Email provider key. `email_enabled() == bool(key)` gates every email feature. |
| `EMAIL_FROM` | `DMR Reconciler <onboarding@resend.dev>` | From address; domain must be verified with the provider. |
| `PUBLIC_BASE_URL` | *(unset)* | Absolute origin used to build emailed links, e.g. `https://app.example.com`. **Never build email links from the request `Host` header** — it is attacker-controllable, and a spoofed Host would plant an attacker's domain inside a legitimate reset email. Falls back to the request origin only for local dev. |
| `INVITE_TTL_HOURS` | `72` | Lifetime of invite and email-confirmation links. |
| `RESET_TTL_HOURS` | `2` | Lifetime of password-reset links (short on purpose). |

---

## 3. Data model

Two tables. Timestamps are Unix floats.

```sql
CREATE TABLE users (
    username       TEXT PRIMARY KEY,   -- stored casefolded
    display        TEXT,
    password_hash  TEXT NOT NULL,      -- '' until an invite is accepted
    is_admin       INTEGER DEFAULT 0,
    created_at     REAL NOT NULL,
    email          TEXT,               -- lowercased; NULL when unbound
    email_verified INTEGER DEFAULT 0
);

-- Single-use, time-limited links. Only the SHA-256 of the raw token is
-- stored, so a database read cannot mint a working link.
CREATE TABLE auth_tokens (
    token_hash TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    purpose    TEXT NOT NULL,          -- 'invite' | 'reset' | 'verify'
    email      TEXT,                   -- the address the link was sent to
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at    REAL                    -- NULL until claimed
);
CREATE INDEX idx_auth_tokens_user ON auth_tokens(username);
```

Key invariants:

- **`password_hash = ''` can never authenticate.** The verifier parses
  `salt$hash` and returns False on malformed input, so an invited-but-not-yet-
  activated account is inert rather than special-cased.
- **Email lookups are case-insensitive** (`COLLATE NOCASE`), and addresses are
  normalized to lowercase before storage. One address maps to at most one
  account.
- **`email_verified` resets to 0 on every address change** — confirmation
  belongs to an address, not to an account.

---

## 4. Primitives

### 4.1 Password hashing

- PBKDF2-HMAC-SHA256, **200,000 iterations**, 16-byte random salt.
- Stored as `hex(salt)$hex(derived_key)`.
- Verification recomputes and compares with a **constant-time** comparison.
- ~16 ms per verify on a modern core — cheap enough per login, expensive
  enough that throttling (not hashing) is the real brute-force defense.
- Run the hash **off the request event loop** (threadpool/worker) in async
  frameworks, or a guess burst stalls every other request.

Porting note: substituting argon2id or bcrypt is fine and strictly better;
the interfaces to preserve are `hash(password) -> opaque string` and
`verify(password, stored) -> bool` where malformed `stored` (including `''`)
verifies false without raising.

### 4.2 Session cookies (stateless, signed)

Cookie name `dmr_session`, value:

```
username|expiry_unix|credential_fp|signature
```

- `credential_fp` = first 16 hex chars of `SHA-256(password_hash)` — the
  session is **versioned against the user's current password**. Changing or
  resetting a password changes the fingerprint, which instantly invalidates
  every outstanding session for that user, with no server-side session store.
- `signature` = HMAC-SHA256 over `username|expiry_unix|credential_fp` with the
  signing secret (§2 `APP_SECRET`).
- TTL 7 days. Attributes: `HttpOnly`, `SameSite=Lax`, `Secure` (configurable),
  `max-age` = TTL.
- Validation order: parse (usernames cannot contain `|`, so right-split by `|`
  into 4 parts is unambiguous) → constant-time signature check → expiry check →
  re-fetch user and constant-time compare the *current* credential fingerprint.
  Any failure = anonymous.
- Logout just deletes the cookie (sessions are stateless; there is nothing to
  revoke server-side — password change is the revocation mechanism).

CSRF stance: state-changing routes are plain same-origin form POSTs and the
cookie is `SameSite=Lax`, which blocks cross-site POSTs from carrying it. If
the target platform must support cross-site embedding or cookieless clients,
add explicit CSRF tokens.

### 4.3 Single-use email tokens

- Raw token: `secrets.token_urlsafe(32)` (256 bits). It appears **only** in
  the emailed URL; the database stores **only its SHA-256**.
- Each token has a `purpose` — `invite`, `reset`, or `verify` — and consuming
  checks the purpose, so a reset link can never activate an invite and vice
  versa.
- **Consumption is a single atomic conditional update**:

  ```sql
  UPDATE auth_tokens SET used_at = :now
   WHERE token_hash = :h AND purpose = :p
     AND used_at IS NULL AND expires_at > :now
  ```

  Exactly one concurrent click wins (`rowcount == 1`); everyone else gets the
  "link no longer valid" page. Do not implement this as SELECT-then-UPDATE.
- GET on a token URL does a **non-destructive validity peek** (same WHERE
  clause, read-only) so rendering the form doesn't burn the link; only the
  POST consumes it. Input validation that can fail (e.g. password too short)
  happens **before** consumption, so a typo doesn't kill the link.
- On successful password reset, all remaining `reset` tokens for that user are
  bulk-invalidated (marked used), so an older reset email in the same inbox
  can't overwrite the new password.

### 4.4 Failure throttling

Sliding **5-minute** window over recent failures, kept in process memory
(consistent with a single-process deployment — see §9 for multi-process).

| Bucket | Scope key | Limit / window |
|---|---|---|
| `user` | username (on `/login`) | 5 |
| `ip` | client IP (on `/login`) | 20 |
| `setup` | client IP (on `/setup` **and** `/forgot`) | 5 |

- Client IP comes from the **edge-set** header (`Fly-Client-IP` here — pick
  whatever your proxy sets and strips from inbound traffic), falling back to
  the socket peer. Never trust `X-Forwarded-For` from the client side.
- When blocked, respond `429` with "wait N seconds" where N is time until the
  oldest failure ages out.
- **The check and the record must be one atomic operation** (`reserve()`):
  login does a slow awaited hash between checking the counter and recording
  the outcome, and a separate check-then-register let N concurrent guesses all
  pass a stale count. So: atomically *reserve* a failure slot in **both** the
  user and IP buckets before verifying; on success, *clear* the user bucket
  and *release* (pop one from) the IP bucket — a correct login must not count
  as an IP failure, but clearing the whole IP bucket would erase other users'
  strikes from a shared NAT.
- Memory bound: cap the bucket map (10k keys here), evicting oldest-failure
  first, so distributed abuse can't balloon the process.

---

## 5. The session gate (middleware)

Every request passes through one gate, evaluated in this order:

1. **Public paths pass through.**
   - Exact: `/healthz`, `/login`, `/setup`, `/forgot`
   - Prefix: `/static`, `/lang/`, `/invite/`, `/reset/`, `/verify/`
   (The token prefixes must be public: those links arrive by email, before the
   visitor has any session.)
2. If `APP_PASSWORD` is unset: pass through only when `ALLOW_OPEN_ACCESS` is
   set; otherwise return **503 "Authentication is not configured."** —
   fail-closed even if startup validation was somehow bypassed.
3. Valid session cookie → pass through.
4. No valid session: redirect **303** to `/setup` when zero users exist
   (first-run bootstrap), else to `/login`.

---

## 6. Route-by-route specification

All form POSTs redirect with `303 See Other`. `/team/*` responses carry
feedback via `?msg=`/`?error=` query params on the redirect back to `/team`.
Every user-facing string goes through the i18n layer (EN + ZH here).

### 6.1 `GET /login`, `POST /login`

- Form: `username`, `password`.
- POST sequence:
  1. Normalize username (trim + casefold).
  2. `reserve([("user", username), ("ip", ip)])` — if blocked, 429 with wait
     time.
  3. Fetch user; verify password off the event loop. An unknown username
     still runs to the same generic failure (see note below).
  4. Success: `clear("user", username)`, `release("ip", ip)`, set session
     cookie, 303 to `/`.
  5. Failure: **401** with one generic message — "Wrong username or
     password." Never distinguish unknown-user from wrong-password.
- The login page links to `/setup` ("Create an account") and `/forgot`
  ("Forgot your password?"), with copy explaining that `/setup` needs the
  setup code and that coworker accounts come from an admin.

Note: the reference implementation short-circuits the hash for unknown users,
which is a (accepted, minor) timing side channel on username existence; a
port that cares should verify against a dummy hash instead.

### 6.2 `GET /setup`, `POST /setup` — bootstrap & admin recovery

- Form: `code` (the setup code), `username`, `password`, `display` (optional),
  `email` (optional).
- POST sequence:
  1. If `APP_PASSWORD` unset → error page (feature disabled).
  2. Throttle: `setup` scope per IP; 429 when blocked. Wrong code registers a
     failure; correct code clears the bucket.
  3. Compare the code with **constant-time** equality.
  4. Validate username: casefolded, `^[a-z0-9][a-z0-9._-]{1,31}$` (2–32 chars,
     starts alphanumeric).
  5. Validate password: ≥ 8 chars.
  6. Validate email if present: lowercase-normalized, lax shape check
     (`something@something.tld`, ≤ 254 chars), and **uniqueness** against
     other accounts.
  7. **Upsert** the account as admin. `/setup` doubles as "reset the admin
     account", so:
     - Blank email field → **keep** the address (and its confirmed status)
       already on file; never silently unbind.
     - Same email as on file → keep its confirmed status.
     - New/changed email → store unconfirmed and send a confirmation link.
  8. Set session cookie, 303 to `/`.
- `/setup` is deliberately **always available** (not just at first run): it is
  the recovery path when every admin is locked out. That is exactly why the
  setup code must be treated as the root secret and why it is throttled.

### 6.3 `GET /logout`

Delete the cookie, 303 to `/login`.

### 6.4 `GET /team` — the account-management page

Requires a session (any role). Shows the user table: username, display name,
email (or —), status badge (**Invite pending** when `password_hash=''` /
**Verified** / **Unverified**), role. Admins additionally see per-row actions
and the "Add a coworker" form. Everyone sees a "your email" form and a
"change password" form (admins get a username field on those; members get
their own username fixed in a hidden field — the server re-checks authority
regardless of what the form claims).

### 6.5 `POST /team/add` — admin creates a coworker (admin only)

Two paths, chosen automatically:

- **Invite path** (email given *and* provider configured): create the account
  with `password_hash=''` (cannot log in), email unconfirmed, then issue an
  `invite` token (TTL `INVITE_TTL_HOURS`) and email the link
  `/invite/{raw}` naming the inviting admin. If the send fails, the account
  still exists — surface an error telling the admin to use "Resend invite" or
  set a password by hand.
- **Manual path** (no email, or provider off): require an initial password
  (≥ 8), create the account normally; the admin shares the password privately.
  Email, if provided, is stored unconfirmed.

Validation: username shape + not-taken; email shape + uniqueness. `is_admin`
checkbox sets the role.

### 6.6 `POST /team/resend-invite` (admin only)

Re-issues a fresh invite token for a pending account and re-sends the email.
(Old invite tokens remain valid until expiry — acceptable here; invalidate
prior invites on resend if the target platform prefers.)

### 6.7 `POST /invite/{token}` (public; `GET` renders the form)

- GET: non-destructive peek; expired/used → 404 page with an explanation.
- POST with `password` (≥ 8, validated **before** consuming): atomically
  consume the `invite` token, set the password, **mark the email confirmed**
  (clicking the invite proves ownership of the inbox — one click, not two),
  and sign the user in directly.

### 6.8 `POST /team/delete` (admin only)

Guards, in order: target exists → **cannot delete yourself** → **cannot delete
the last admin** (count admins first). Then delete.

### 6.9 `POST /team/password` — change/reset a password in-app

Self for anyone; any account for admins. ≥ 8 chars. Because sessions are
versioned against the password hash (§4.2), this **logs the user out
everywhere**, including the session that made the change — that is intended
behavior, not a bug.

### 6.10 `POST /team/email` — bind, change, or remove an address

Self for anyone; any account for admins (server-side authority check — the
username field in the form is advisory only). Behavior:

- Empty email → **unbind** the address (sets email NULL, verified 0).
- Otherwise validate shape + uniqueness, store lowercased with
  `email_verified = 0` (**always** — even re-entering the same address drops
  confirmation), and send a confirmation link when the provider is on. The
  flash message explicitly says reset stays unavailable until confirmed.

### 6.11 `POST /team/verify-email` — re-send the confirmation link

Self or admin; requires the account to have an address on file.

### 6.12 `GET /verify/{token}` (public)

Consumes a `verify`-purpose token, then confirms **scoped to the address the
link was sent to**:

```sql
UPDATE users SET email_verified = 1
 WHERE username = :u AND email = :sent_to COLLATE NOCASE
```

If the account switched to a different address after the link was sent, the
update matches nothing and the link renders the same "no longer valid" page —
a confirmation link only proves ownership of the inbox it landed in, so it
must never bless whatever address is *currently* on the account.

### 6.13 `GET /forgot`, `POST /forgot` (public)

- GET: email-entry form; when the provider is off, an explanatory notice
  ("ask an admin to reset your password") instead of the form.
- POST sequence:
  1. Throttle: `setup` scope per IP; **every request counts** (max 5/5 min,
     successful or not) to stop reset-spam and enumeration probing. When
     blocked, render the *success* page anyway (silent).
  2. If provider on + email shape valid + an account holds that address **and
     the address is confirmed**: issue a `reset` token (TTL
     `RESET_TTL_HOURS`, short) and email `/reset/{raw}`.
  3. **Always render the identical "if that email belongs to an account, a
     link is on its way" page** — every branch, including unknown address,
     unconfirmed address, invalid shape, and throttled. No user enumeration
     via response text or status.

### 6.14 `POST /reset/{token}` (public; `GET` renders the form)

- GET: non-destructive peek → form or 404 page.
- POST with `password` (≥ 8, validated before consuming): atomically consume
  the `reset` token, set the new password (rotates the credential fingerprint
  → **all old sessions die**), mark the email confirmed, **invalidate the
  user's other outstanding reset tokens**, and sign the user in.

### 6.15 Status-code summary

| Situation | Code |
|---|---|
| Successful form action | 303 redirect |
| Wrong credentials / wrong setup code | 401 |
| Validation failure (shape, length, duplicates) | 400 (or 303-with-error for `/team/*`) |
| Throttled | 429 (message includes wait seconds) |
| Dead/expired/foreign-purpose token | 404 |
| Auth not configured, no opt-out | 503 |

---

## 7. Email sending

- Provider: **Resend** HTTP API (`POST https://api.resend.com/emails`, bearer
  key, JSON `{from, to, subject, html, text}`), 12 s timeout. Any provider
  with an equivalent "send one transactional message" call slots in.
- `send()` **never raises** — it returns a bool, and every caller has a
  degraded path for `False` (flash an error, fall back to manual password).
- Three messages — invite, confirm-address, reset — all built from one layout:
  wordmark, one-sentence purpose, a single button link, a "works once,
  expires in N hours" note, and the raw URL as fallback text. Bodies are
  bilingual (EN + ZH) because the team is mixed; both HTML and plain-text
  parts are sent, and all interpolated values are HTML-escaped.
- Links are absolute, built from `PUBLIC_BASE_URL` (§2) — never the Host
  header.

---

## 8. Security decisions worth preserving (and why)

Each of these was deliberate; several were bugs found and fixed. A port that
drops one silently regresses.

1. **Setup code ≠ signing secret.** Cookie forgery must not follow from
   knowing the (widely shared) setup code.
2. **Token hashes at rest.** A leaked database or backup cannot mint working
   invite/reset links.
3. **Atomic token consumption.** Single-use must hold under concurrent
   clicks; conditional-UPDATE-and-check-rowcount, never check-then-write.
4. **Atomic throttle reservation.** The slow password hash yields control
   between check and record; reserve-then-release closes the race that let
   concurrent guesses through.
5. **Sessions versioned by credential fingerprint.** Password change/reset is
   total session revocation with zero server-side session state.
6. **Reset links only to *confirmed* addresses.** A typo'd address is a
   stranger's inbox; mailing it a reset link hands over the account.
7. **Verify scoped to the sent-to address.** A stale confirmation link must
   not bless a newer, different address.
8. **No account enumeration.** `/forgot` answers identically for every input;
   `/login` has one generic failure message.
9. **Email links from config, not Host.** Host-header injection into
   password-reset emails is a classic account-takeover vector.
10. **Fail closed.** No setup code configured → 503, unless open access is
    explicitly opted into.
11. **Empty password hash is inert.** Invited-but-unactivated accounts cannot
    authenticate by any path.
12. **Last-admin and self-delete guards.** The team can't lock itself out or
    orphan the instance (and `/setup` remains the break-glass recovery).
13. **Constant-time comparisons** for the setup code, session signature,
    credential fingerprint, and password digest.
14. **Blank fields never unbind.** Recovery-style forms (`/setup`) keep
    existing values when a field is left empty rather than destroying state.

---

## 9. Porting checklist

- [ ] **Storage**: any store with atomic conditional updates works. The two
  atomicity points are token consumption (§4.3) and — only if you move
  throttling out of process memory — throttle reservation (§4.4).
- [ ] **Multi-process/multi-node**: the reference throttle is in-process
  (documented single-process deployment). Behind more than one worker, move
  the buckets to Redis/DB with the same reserve/release semantics, or accept
  per-worker limits (limit × N workers).
- [ ] **Client IP**: swap `Fly-Client-IP` for your edge's trusted header.
- [ ] **Email provider**: reimplement `send(to, subject, html, text) -> bool`;
  keep the never-raises contract and the degraded paths.
- [ ] **Hashing**: PBKDF2-200k minimum; argon2id preferred if a library is
  acceptable.
- [ ] **Framework middleware**: reproduce §5 exactly, including the public
  prefix list — forgetting `/verify/` (or its equivalent) makes emailed links
  redirect to the login page. (This exact bug class is why the list is spec'd.)
- [ ] **CSRF**: `SameSite=Lax` + same-origin forms suffices here; add CSRF
  tokens if your deployment differs (subdomains, embedding, API clients).
- [ ] **i18n**: every string in §6 is user-facing; route them through your
  translation layer from day one.
- [ ] **HTTPS**: `Secure` cookies on by default; the sliding-window numbers
  assume TLS-terminated edge with a trusted client-IP header.

## 10. Test checklist (the behaviors a port must prove)

From `tests/web/test_auth.py` and `tests/web/test_email_accounts.py` (41
tests in the reference suite):

- Login: success sets a working session; wrong password 401; unknown user
  401 with the same message; 6th failure within 5 min → 429; success clears
  the user bucket but leaves other IP strikes.
- Sessions: tampered signature rejected; expired rejected; **password change
  invalidates existing sessions**; cookie flags present.
- Setup: wrong code 401 + throttled; creates admin; re-run resets the admin;
  blank email preserves a confirmed address; new email stored unconfirmed +
  confirmation sent.
- Middleware: anonymous → 303 to `/login` (or `/setup` when zero users);
  `/forgot`, `/invite/x`, `/reset/x`, `/verify/x` reachable logged-out; 503
  when unconfigured.
- Invites: account created passwordless (cannot log in); accept sets
  password, marks email confirmed, signs in; link single-use; expired
  refused; wrong-purpose token refused both directions.
- Reset: only for confirmed addresses; unknown/unconfirmed input → identical
  success page, nothing sent; reset rotates password, kills old link, kills
  old sessions, invalidates sibling reset tokens.
- Verify: confirms once; second click 404; link dead after the account's
  address changed; changing an address drops confirmation.
- Team: non-admin cannot add/delete accounts or touch others' email/password;
  duplicate email rejected across every entry point (add, setup, team/email);
  cannot delete self; cannot delete last admin.
