# Architecture (v2, as built)

Requirements: `docs/PROMPT.md` (original, section numbers below refer to it) and the v2 plan `docs/PLAN-v2.md` with the amendments in `docs/PLAN-v2-rev4.md`. The **v2 contract** `docs/V2-CONTRACT.md` is authoritative for the v2 decisions, derived values and service interfaces; this file describes the structure of the code as it was built and does not repeat the contract. Python 3.12+.

One FastAPI process (Jinja2 server-rendered pages, plain CSS, small static JS files, no CDN, no inline JS/CSS) with one SQLite database in WAL mode, behind a TLS-terminating reverse proxy.

```
phone ──https──> reverse proxy ──http──> uvicorn (1 worker) ── SQLite file (/data/coffee.db)
                                              └── encrypted mail login (/secrets/mail-login.enc) ──smtp──> postout.lrz.de
```

## Layers and rules

- **Services** (`app/services/`) hold every business rule. Mutating functions take a `Session` opened by the caller with `write_session()` (`BEGIN IMMEDIATE`), never commit and never open their own session. Read functions accept either session type. Typed errors come from `app/services/errors.py` (`NotFound`, `Forbidden`, `UserInactive`, `NoPriceConfigured`, `AlreadyReversed`, `UndoNotAllowed`, `InvalidAmount`, `InvalidInput`, `InvalidPassword`, `Conflict`, `RateLimited`, `MailError`, ...); routes map them to safe pages through the error `code`.
- **Routes** (`app/web/`) parse input, open the session, call services, render templates. Templates hold no rules. Every user-facing string goes through `t("key")` (`app/strings*.py`).
- **Ledger** is append-only: consumption events, payment events, prices, group remittances and the audit log are never edited or deleted (triggers reject changes to the audit log). Undo, corrections and reversals are new compensating rows. Totals are always computed from the ledger.
- **Money** is integer cents (`app/money.py`). Timestamps are UTC internally; display and month boundaries use `LOCAL_TIMEZONE` (`app/timeutil.py`).
- **Keys**: `app/services/keys.py` derives separate keys with HKDF-SHA256 from `SECRET_KEY` (mail-login encryption, token signing per purpose, CSRF, step-up payloads, admin recovery-code MACs).
- **Single process**: rate limits are database-backed, but SQLite's write lock and the in-process mail status assume one worker.

## Modules and ownership

Owners (file ownership during the build; later changes follow the same split): **A** core ledger, **B** core platform and mail, **U** user web, **M** admin, **O** ops. Only the owner edits a file.

| Area | Files | Owner |
| --- | --- | --- |
| Settings, DB, models, money, time | `app/config.py`, `app/db.py`, `app/models.py`, `app/money.py`, `app/timeutil.py`, `migrations/` (one revision, `0001_initial_schema.py`) | A |
| Ledger and groups | `services/ledger.py`, `prices.py`, `users.py`, `chairs.py`, `groups.py`, `overview.py` (read-only site queries), `authz.py`, `audit.py`, `checker.py` | A |
| Passwords and keys | `services/passwords.py`, `services/keys.py` | A (passwords policy shared) |
| Accounts | `services/registration.py` (members, guests, sponsor approval, reassign), `login.py`, `recovery.py`, `account.py` (password change), `sessions.py`, `pending.py`, `onboarding.py` (sign-in helper shared by login and verification), `ratelimit.py` | B |
| Mail | `services/mail.py` (Mailer, backends memory/console/file/smtp, caps, background send), `mail_credentials.py` (Fernet store), `mail_status.py` (cross-process status), `mail_templates.py`, `strings_mail.py` | B |
| Web platform | `web/middleware.py`, `deps.py`, `tokens.py`, `signing.py`, `health.py`, `accesslog.py`, `templating.py`, `main.py`, `strings.py`, `strings_common.py` | B |
| User pages | `web/routes_user.py`, `user_views.py`, `strings_user.py`, `templates/user/`, `static/user.css`, `scan.js` | U |
| Open site | `web/routes_overview.py`, `strings_site.py`, `templates/site/`, `static/site.css` | U |
| Admin | `web/routes_admin.py` and `admin_*.py` (`admin_login`, `admin_users`, `admin_manage`, `admin_roles`, `admin_groups`, `admin_stepup`, `admin_deps`), `services/admin_auth.py`, `stepup.py`, `roles.py`, `stats.py`, `group_report.py`, `exports.py`, `strings_admin.py`, `templates/admin/`, `static/admin*` | M |
| CLI | `app/cli.py` (check-ledger, set-price, purge-expired), `cli_admin.py` (admins, qr), `cli_setup.py` (setup, setup-mail, mail-status, test-mail, clear-mail) | A / M |
| Ops | `Dockerfile`, `docker-compose.yml`, `.env.example`, `scripts/`, `deploy/`, `README.md`, `tests/test_ops_*` | O |

Removed from v1: the access-code gate (`gate.py`), `/join`, `/profiles`, profile selection, `admin_chairs`, `set-admin-chairs`, the `ACCESS_CODE` and `ADMIN_TOTP_REQUIRED` settings.

## Routes

User and platform (`routes_user.py`, `routes_overview.py`, `health.py`):

| Route | Purpose |
| --- | --- |
| `GET /healthz`, `GET /readyz` | process up; database reachable and migrated to the head |
| `GET /` | home |
| `GET|POST /coffee/{single,double}` | GET issues a signed token and writes nothing; POST records (or, without a session, parks a pending action and redirects to `/login`) |
| `GET /coffee/done/{id}`, `POST /coffee/undo/{id}` | result page (balance in words, shots this month), undo within the window |
| `GET|POST /register`, `POST /register/guest` | member and guest registration forms |
| `GET|POST /verify/{token}` | member confirmation and sponsor approve or reject; GET writes nothing |
| `GET|POST /login`, `POST /logout` | username and password sign-in |
| `GET|POST /forgot`, `GET|POST /reset/{token}` | password recovery by username; GET writes nothing |
| `GET /account`, `POST /account/password`, `GET|POST /account/edit` | own account |
| `GET /overview`, `/overview/people`, `/overview/people/{id}`, `/overview/chairs`, `/overview/prices` | open site for signed-in accounts (anonymous: pending `view_site` action, redirect to `/login`); `Cache-Control: private, no-store` |
| `GET /privacy` | privacy statement |
| `GET /profile` | the header's profile button: redirects to the signed-in account's `/overview/people/{id}` |
| `GET /leaderboard?period=YYYY-MM\|all` | ranking by shots (no money) for one local calendar month (default: current) or all time |
| `GET /avatar/{id}` | profile picture (JPEG) for signed-in accounts; 404 otherwise or when there is none |
| `POST /account/status`, `POST /account/avatar`, `POST /account/avatar/remove` | own status text and profile picture (forms on `/account/edit`); `/account/avatar` is the only route with a larger body limit (6 MiB, `middleware.LARGE_BODY_PATHS`) |

Admin (`/admin`, separate cookie `coffee_admin` with `Path=/admin`; a user cookie alone never reaches it; every route is in the route-enumeration test): `login`, `logout`, `totp/setup`, `account` (+ password, session revoke), dashboard (`""`), `users` (+ detail, rename, chair, status, sessions, merge, payment, adjustment, reverse, balance, CSV), `admins` and the group-admin promote/demote buttons, `users/{id}/sponsor`, `groups` (+ remittances and their corrections, CSV), `stepup/confirm` (the only route that executes a balance-changing action), `users/{id}/avatar` (GET), `users/{id}/avatar/remove` and `users/{id}/status-text/remove` (moderation, audited), `chairs`, `prices`, `qr` (four codes, `qr/{kind}.svg`), `audit`, `exports`.

## Schema summary

Single Alembic revision `0001_initial_schema.py` (v2 squashed into the initial migration, `render_as_batch=True`). Tables: `chairs`, `users` (username, `account_kind` member|guest, email for members, `sponsor_id` for guests, `verified_at`, `password_hash`, one `chair_id`), `prices`, `consumption_events`, `payment_events` (`kind` cash|bookkeeping, `idempotency_key`, `collector_admin_id`, `chair_id` at event time), `group_remittances`, `admin_users` (optional `user_id` link for group admins, role `global` | `chair` shown as "group admin"), `admin_sessions` (`stepup_until`), `user_sessions`, `pending_actions` (`single`, `double`, `view_site`), `pending_registrations`, `password_reset_tokens`, `used_nonces`, `rate_limit_events`, `audit_log` (append-only triggers). Revision `0003_user_profiles.py` adds `user_profiles` (one optional row per account: `status_text`, the re-encoded `avatar` JPEG and its timestamp; not part of the ledger, deleted on anonymizing deactivation and on merge). View `effective_users` resolves merged accounts and underlies every total. CHECK constraints in the migration enforce the member/guest rules and the payment kinds. Details: `docs/V2-CONTRACT.md` and `app/models.py`.

## Service interfaces

The interface list (ledger, groups, overview, passwords, authz, mail, mail credentials, registration, recovery, step-up) is in `docs/V2-CONTRACT.md`, "New and changed service interfaces". Unchanged v1 interfaces (prices, sessions, pending, ratelimit, audit, users) keep the names in the code; read the module docstrings. Not repeated here.

## Ops pieces

- `scripts/run.sh` runs `scripts/migrate_on_start.sh` (backup via `backup.sh` when an existing DB is behind head, then `alembic upgrade head`) and starts one uvicorn worker with `--proxy-headers`, `--forwarded-allow-ips $TRUSTED_PROXY_IPS` and `--no-access-log`; the application logs requests itself with one-time tokens redacted (`web/accesslog.py`; proxy side: `deploy/README.md`).
- `scripts/backup.sh`, `restore.sh`, `dbtool.py` use SQLite's backup API on the database file only; the secrets directory is never read.
- `docker-compose.yml`: one `app` service, named volumes `coffee_data` (`/data`) and `secrets` (`/secrets`, mode 0700, owned by the app user, mounted nowhere else), bind mount `./backups`.
- `scripts/smoke.sh` is the HTTP smoke test against a running instance.

## Conventions

- Tests run against a temp-file SQLite database created by Alembic (never `:memory:`, never `create_all`). Mail logic tests use the `memory` backend; protocol tests use a local `aiosmtpd` server with generated certificates. The test configuration uses cheap Argon2 parameters.
- No CDN or external assets. No inline JS/CSS (strict CSP).
- Run the suite from the repository root: `python -m pytest -q`; the Playwright test (`tests/test_e2e_scan.py`) needs Chromium and is skipped when no browser can be launched.

## Public profile extras

- `app/services/profiles.py`: status text (`clean_status`: at most 60 characters, emoji allowed, no control characters) and profile picture (`process_avatar`: Pillow decodes JPEG/PNG/WebP/GIF up to 5 MiB, applies the EXIF orientation, crops the centre square, scales to 256 px and re-encodes as JPEG without any metadata). Admin removal (`admin_remove`) is scope-checked and audited (`user.avatar_removed`, `user.status_text_removed`; the removed text is not logged).
- Every non-admin page gets `header_user` from `templating.render` (two header buttons: own profile and leaderboard); `templates/_profile.html` holds the avatar/initials and icon macros.
- The public profile (`/overview/people/{id}`) shows picture, name, status, balance and Coffees today / this month / total; it no longer shows a dated history (that stays on the owner's private `/account` page).
- Deploy: the reverse-proxy configs in `deploy/` allow 6 MiB for `/account/avatar` only.

## Achievements

- `app/services/achievements.py` defines the 36 approved badges once (key, icon, category, hidden flag; names and descriptions are `t("ach.<key>.name|desc")` in `app/strings_achievements.py`). Rules are evaluated by replaying an account's history in time order (`evaluate`, pure), so unlock times are real. Bavarian public holidays are computed from the Easter date.
- Revision `0004_achievements.py`: `achievement_unlocks` (one row per account and key, with the triggering coffee) and `achievement_showcase` (up to 10 keys per account, in order).
- Hooks: `ledger.record_coffee` (sync the account and sweep queues of the last 7 days), `ledger.undo_coffee` (remove what the undone coffee unlocked, except Schrödinger's Coffee, then sync), `ledger.add_payment` (Ground State) and `registration.approve_guest` (Plus One). Admin reversals never remove a badge.
- Queue badges are awarded only after the queue is over. `python -m app.cli achievements-sync` backfills all accounts and sweeps the whole history: run it once after the upgrade, and nightly (e.g. next to `purge-expired`).
- Pages: `/overview/people/{id}?tab=achievements` (unlocked badges only; dates and the showcase form only for the owner), showcase under the status, `POST /account/showcase`, new badges on `/coffee/done/{id}`.
