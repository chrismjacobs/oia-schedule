# OIA Duty Roster

Scheduling and attendance app for OIA student workers (MCUT). See `CLAUDE.md`
for the product brief, `SCHEMA.md` for the data model, and
`roster-mockup.html` for the visual target (the CSS is ported from it almost
verbatim into `frontend/static/css/app.css`).

## Stack — pinned (CLAUDE.md §3–§4)

- **Backend:** Flask (app-factory pattern) + SQLAlchemy + Flask-Login,
  OR-Tools CP-SAT solver, boto3 (S3).
- **Frontend:** Flask-served Jinja templates + Vue 3 from CDN (`vue.global.prod.js`)
  in progressive mode — one `<script>` per page, `Vue.createApp(...).mount(...)`,
  delimiters set to `[[ ]]` so they don't collide with Jinja's `{{ }}`. **No
  Node, no npm, no build step, no SPA.** Templates in `frontend/templates/`,
  JS in `frontend/static/js/`, CSS in `frontend/static/css/`.
- **DB:** SQLite by default (`backend/oia.db`). Swappable to Postgres/Neon by
  setting `DATABASE_URL` in `.env` — the code has no Postgres-specific SQL.

## Local setup

```bash
cd backend
python -m venv venv
venv/Scripts/pip install -r requirements.txt     # venv/bin/pip on macOS/Linux
venv/Scripts/python seed.py                       # creates tables + the first overseer account from .env
venv/Scripts/python wsgi.py                       # runs on :5057 by default (set PORT to change)
```

Open `http://127.0.0.1:5057/login`. Log in with `ADMIN_EMAIL` / `ADMIN_PASSWORD`
from `.env`. Everything else (semesters, students, months) is invite-only and
set up from **Setup** as that overseer.

`seed.py` is idempotent — safe to re-run; it only creates the overseer account
if one with that email doesn't already exist.

**Local HTTP note:** `SESSION_COOKIE_SECURE` is on by default (production
default, requires HTTPS). Running locally over plain `http://`, set
`FLASK_DEBUG=1` in the environment before starting the server or the session
cookie will be silently dropped by the browser and every page will bounce
back to `/login`.

### See it populated immediately

```bash
FLASK_APP=wsgi.py venv/Scripts/python -m flask seed-demo
```

Inserts 9 demo students (colours/shapes/roster lifted straight from
`roster-mockup.html`), a month of availability, and a real CP-SAT-solved
committed schedule, so Day/Week/Dashboard render populated on first look. The
first weekday of that month is seeded with attendance to reproduce the
mockup's day view exactly (a no-show, an open slot). Every row is flagged
`is_demo` — running `flask reset-demo` (or the "Reset demo data" button on
Setup) deletes only those rows, real data untouched.

## First-time walkthrough (as overseer)

1. **Setup** → create a semester, invite students (each invite returns a
   `/register/<token>` link — send it to the student).
2. **Setup** → create a month (`YYYY-MM`), add closed dates, set the selection
   window, then **Generate slots**.
3. Move the month to `selection_open` so students can submit availability.
4. Once selection closes, go to **Draft review** → **Generate draft** (runs
   the CP-SAT solver) → edit any cells if needed → **Commit**.
5. Move the month to `running`. Students sign in/out from **Sign in/out**.
6. **Dashboard** is the day-to-day view: scheduled-vs-recorded gap, no-shows,
   uncovered slots, leave patterns, task completion. **Day** is the phone-first
   default landing.
7. At month end, `POST /api/admin/months/<id>/close` (from `running`) produces
   the close-out report and locks the month.

## Config

All the tunable values in `CLAUDE.md` §7/§18 are environment variables (see
`backend/app/config.py`) with sane defaults, plus a few — solver weights,
floor hours, timecard cadence — that are also editable at runtime from
**Setup** (stored in the `app_setting` table, no redeploy needed).

## Deploying (Render free tier)

The whole app is one Flask process (`backend/wsgi.py`) — it serves the API
and renders the Jinja pages, so it's a single Render **Web Service**:

- Build command: `pip install -r backend/requirements.txt`
- Start command: `gunicorn -w 2 -b 0.0.0.0:$PORT wsgi:app` (run from `backend/`)
- Env vars: copy everything from `.env` into Render's dashboard. Leave
  `FLASK_DEBUG` unset in production — that keeps `SESSION_COOKIE_SECURE` on,
  which needs HTTPS (Render gives you that by default).
- Run `python seed.py` once (Render shell or a one-off job) to create tables
  and the first overseer account. SQLite on Render's ephemeral disk is fine
  for evaluation but won't survive a redeploy — point `DATABASE_URL` at Neon
  (already provisioned in `.env`) before relying on this for real terms.

### The `/tick` cron (CLAUDE.md §13)

Render's free web service sleeps after ~15 min idle, so an in-process
scheduler won't fire reliably. Instead, an **external** cron pings a
token-protected endpoint:

```
POST https://<your-app>.onrender.com/api/tick
X-Tick-Token: <TICK_TOKEN from .env>
```

Set this up on [cron-job.org](https://cron-job.org) or a GitHub Actions
scheduled workflow, firing every 5–15 minutes, **business hours only**
(~07:00–18:00 Mon–Fri) to stay well under Render's 750 free instance-hours/month.
`/tick` is idempotent — a missed, late, or doubled ping self-heals.

### S3 — three separate image kinds (CLAUDE.md §11)

Task reference photos (admin), task completion/proof photos (student), and
timecard photos never share a key or column, but do share one upload helper
(`app/utils/s3.py`). Bucket = `AWS_S3_BUCKET` / `AWS_S3_REGION` in `.env`; the
IAM user needs `s3:PutObject` / `s3:GetObject`. Viewing uses short-lived
presigned URLs — the bucket does not need to be public.

## Notifications

Pluggable — `NOTIFICATION_BACKEND=email` (default) or `line`. Email needs
`SMTP_HOST`/`SMTP_USER`/`SMTP_PASSWORD`/`NOTIFICATION_TO_EMAIL`. LINE needs
the Messaging API (not LINE Notify — discontinued March 2025):
`LINE_TOKEN` (channel access token, used for push), `LINE_CHANNEL` (channel
ID), `LINE_SECRET` (webhook signature verification — not yet used since there's
no inbound webhook route), and `LINE_GROUP_ID` once the bot's been added to
the student group. Until `LINE_GROUP_ID` is set, use **Setup → Notification
test send** with an explicit target user/group ID to check the wiring.
Nothing configured just logs the message instead of sending.

## Project layout

```
backend/
  app/
    models.py              SQLAlchemy models — mirrors SCHEMA.md exactly
    schedule/solver.py      OR-Tools CP-SAT allocator + greedy fallback
    admin/demo.py            seed-demo / reset-demo (CLAUDE.md §17)
    pages/                   Jinja page routes (redirects to /login, not JSON 401)
    auth/ admin/ availability/ schedule/ attendance/ leave/ tasks/
    timecards/ dashboard/ notifications/       one blueprint each, JSON API
    utils/                   identity tokens, period keys, settings, S3, decorators
  seed.py                    bootstrap script (overseer account)
  wsgi.py                    entrypoint
frontend/
  templates/
    base.html                shared shell: header, hamburger drawer, CDN Vue, i18n toggle
    login.html register.html week.html dashboard.html draft.html
    advanced.html regular_schedule.html availability.html schedule.html attendance.html
    leave.html tasks.html timecards.html
  static/
    css/app.css               design tokens + components ported from roster-mockup.html
    js/
      common.js                shared: fetch wrapper, i18n dict, colour×shape token
                                component, header drawer/lang-toggle behaviour
```

Each page template extends `base.html` and mounts its own small Vue app on a
page-local root div — there is no client-side router and no shared SPA state;
navigation is real page loads, matching the pinned stack in CLAUDE.md §3–4.
