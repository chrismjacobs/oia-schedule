# CLAUDE.md — OIA Duty Roster

Build brief for a scheduling and attendance app for paid student workers in the
Office of International Affairs (OIA), Ming Chi University of Technology (MCUT).
Read this with `SCHEMA.md` (the data model) and `roster-mockup.html` (the visual
target) alongside it.

> **This is a rebuild.** A first local build exists but was scaffolded on the wrong
> frontend stack (a Node/Vite SPA). Reassess that build against this brief. **Keep
> the stack-independent parts that are correct** — the data model/migrations and the
> OR-Tools allocator (pure Python) — and **redo the frontend scaffold on the pinned
> stack in §3.** Do not carry over `package.json`, `vite.config`, `node_modules`, or
> any npm build step.

---

## 1. Why this exists

Students are paid to staff the office, but **participation is not tracked** — they
get paid whether or not they actually show up. Scheduling is done by hand each
month, and overseeing it is a job nobody wants.

The app has one job: **make oversight nearly effortless.** Every feature exists to
produce a clean overseer view of *who was scheduled, who actually showed up, and
where the gaps are.* When a design choice is ambiguous, resolve it in favour of
reducing the overseer's manual effort.

The single most important output is the **scheduled-vs-recorded gap** — hours a
student was scheduled for vs hours they actually signed in for. This is what the
current system cannot see, and it must be visible on the dashboard from v1.

---

## 2. Users and roles

- **Overseer / admin** — a staff member the director assigns. Sets holidays,
  opens/closes selection, reviews and commits the schedule, approves leave,
  monitors attendance, manages tasks. Primary user; the UX is tuned for them.
- **Student worker** — selects availability, views shifts, signs in/out, files
  per-hour reports, requests leave, claims freed slots and custom tasks.

The roster **changes every semester** — model students as belonging to a semester
rather than a fixed list. Access is **invite-only** (no open registration).
Registration captures: Chinese name, English name, and **student ID (letters and
numbers, any length up to 32)**. *This was originally specified as 8 numeric
digits with no letter prefix; the real roster turned out to carry IDs that don't
fit that shape, so the rule was relaxed. Don't reintroduce the digits-only
assumption.* The overseer can also correct an ID afterwards from the dashboard.

---

## 3. Stack — pinned, do not substitute

- **Backend:** Flask (Python), app-factory pattern.
- **Frontend:** **Flask-served Jinja templates + Vue 3 included from CDN**
  (`vue.global.js`) in progressive mode — a `<script>` tag per page, mounted with
  `Vue.createApp(...).mount(...)`. See §4 for the rules.
- **NOT this:** no Node, no npm, no Vite, no webpack, no `.vue` single-file
  components, no build step, no standalone SPA. If a step seems to need npm, stop
  and use the CDN/Jinja approach instead.
- **DB:** SQLite for v1 (tens of students, a few hundred slots/month). Keep the data
  layer swappable to Postgres/Neon later.
- **File storage:** S3 (time-card photo uploads).
- **Hosting:** Render free tier — see §13 for the scheduler consequences.
- **Allocation solver:** Google OR-Tools (CP-SAT), `pip install ortools`, in-process,
  deterministic. **No LLM/OpenAI API for scheduling** (see §7).

---

## 4. Frontend architecture (the part that went wrong last time)

- Flask renders Jinja templates and serves them. Each page that needs interactivity
  pulls in Vue 3 from CDN and mounts an app on that page's root element.
- **Set Vue `delimiters` to `[[ ]]`** so Vue's interpolation doesn't collide with
  Jinja's `{{ }}`. (Alternatively wrap Vue markup in `{% raw %}`.) Get this right
  once and it stops a whole class of confusing bugs.
- **File layout:** all JS in `static/js/`, all CSS in `static/css/`, templates in
  `templates/` with a shared `base.html`. No bundler output directories.
- Data reaches the page either as a JSON blob Flask injects into the template, or
  via `fetch()` to small Flask JSON endpoints — your call per page, but keep it
  simple; this is an internal CRUD-ish tool, not an SPA.
- This mirrors the existing OIA apps (Li-Ze Academy, English Explorers) — match that
  approach.

---

## 5. Visual design — match the mockup exactly

`roster-mockup.html` is the **source of truth for the look.** Do not invent a new
style; port this one.

- **Extract its `:root` design tokens and component CSS into `static/css/app.css`**
  and apply them app-wide.
- Governing rules from the mockup: **neutral chrome** (greys, off-white, one
  restrained accent ~`#2B4257`); **students are the only vivid colour**; **monospace
  tabular numerals** for all times, hours, and IDs; **status colours reserved and
  separate** from the student palette (amber `#B45309` = shortfall/no-show, muted
  grey `#94A0A9` + hatch = uncovered); the **colour × shape student tokens** (§15);
  and the **bilingual font stack** carrying Latin + Chinese at equal weight.
- Aim: calm, dense-but-legible, "boring in the good way" — a clean duty roster, not
  a consumer app.
- **Quality floor:** responsive to mobile, visible keyboard focus, reduced motion
  respected.

### Mobile navigation (currently missing)
- Add a **hamburger button in the header that opens a slide-in drawer** linking the
  sections: Day, Week, Roster, Tasks, Dashboard, Sign-in.
- Collapse to the hamburger below ~640px; show inline nav on wider screens.
- Keyboard-accessible; closes on outside-tap and on Escape.
- *(Alternative if preferred: a thumb-reachable bottom tab bar on mobile. Ask before
  switching — default is the hamburger drawer.)*

---

## 6. The monthly cycle (state machine)

1. **Setup** — overseer sets the month's **holidays / closed dates** (admin list).
   Closed dates produce no slots. Done before selection opens.
2. **Selection open** — students select **all the hours they want** (no cap). Opens
   at a configured date/time.
3. **Selection closed** — locks at a configured date/time.
4. **Draft generated** — the solver builds a proposed schedule (§7).
5. **Review** — overseer edits the draft (§7).
6. **Committed** — overseer commits → publishes and notifies.
7. **Running** — sign-in/out, leave requests, reopened slots.
8. **Closed** — close-out report (scheduled vs recorded per student, leave summary,
   task completion, uncovered hours).

Open/close dates are **config values**, not hard-coded.

---

## 7. Scheduling and allocation

**The atom is the hour.** Students select individual hours (08:00–12:00, 13:00–17:00,
Mon–Fri → 8 slots/day, 40/week). The solver assigns each hour to at most one
available student. Blocks/contiguity are *preferences*, not fixed units.

### Hard constraints (never violated)
- At most one student per slot.
- Never assign a student an hour they didn't offer.
- No student double-booked.
- Closed dates produce no slots.

### Soft preferences — priority order (higher yields to nothing below)
1. **Coverage** — fill as many slots as possible.
2. **Floor guarantee** — everyone who selected gets at least a minimum before anyone
   gets extra (protects students relying on the pay).
3. **Contiguity** — prefer ~2-hour runs; mildly penalise isolated single hours.
4. **Low churn / consistency** — penalise week-to-week pattern changes. The
   2-weeks-on/2-off rotation should **emerge** from this — do not script it.
5. **Equalise hours** — spread totals as evenly as possible.

All penalty **weights are labelled, tunable config.** The overseer tunes them against
the first real month.

### Draft → review → commit
- Solver output is a **draft, never committed.**
- Overseer reviews in the grid. **Every slot is an editable dropdown listing only the
  students available for that hour** — a manual edit can't create an impossible
  assignment.
- A manual edit is **exactly that one change — no re-solve, no ripple.**
- Committing locks the schedule and fires the "committed" notification.

### Unfilled hours
No offer → **leave uncovered and flag it**, never force-fill.

### v1 vs later
- **v1:** hard constraints + gentle soft prefs. Ship, watch one month.
- **Fast-follow:** turn up consistency/rotation and block-size tuning on real data.
- **v2:** cross-month fairness.

A **greedy round-robin** (next contested hour → eligible student with fewest hours)
is an acceptable fallback if OR-Tools is heavy on the free instance; CP-SAT preferred.

---

## 8. Leave and reopened slots

- Students request leave against specific slots, with a reason.
- **Track patterns:** who requests leave **too often**, and **too late** (short lead
  time). Store request timestamp vs slot start.
- On approval → slot becomes **open**, offered **first-come-first-served**; claimed
  hours count toward the claimer's totals; fires the "slot open" notification.
- FCFS is **only** for reopens. The monthly build is solver-allocated.

---

## 9. Sign-in, attendance, and reports

- **Sign-in opens 10 minutes before** a slot.
- A student on a **consecutive run signs in once** (start) and out (end) — one
  session spanning the run. No per-hour sign-in.
- **At sign-out, a per-hour report:** for each hour, a free-text note and/or ticked
  regular tasks and/or claimed custom tasks.
- **Forgot-to-sign-out → flag for the overseer**, don't auto-close at a guess.
- **Scheduled vs recorded** = assignments vs sessions. Core deliverable (§1).
- Honour-system for v1 (no wifi/IP check). Auto-flag *scheduled but never signed in*
  and *signed in but not scheduled*.

---

## 10. Tasks

- **Regular tasks** — admin-defined, with a **cadence** (daily/weekly/monthly ×
  interval, or **unlimited**). **Done only once per period** — once the fridge is
  done this week it drops off until next week; can't be repeated 5×/day.
  **Unlimited** is the exception: a regular *duty* rather than a cadence — required
  whenever asked in person, any number of times a day, by anyone (e.g. courier
  runs) — it never drops off and never blocks a repeat. Completions log to the
  doer. A task may include an optional **reference photo** the admin uploads (e.g.
  the dirty fridge), shown to the student as what needs doing.
- **Custom tasks** — admin adds ad-hoc, ticked (and thereby claimed) **at
  sign-out**, same as regular tasks — there's no separate advance-claim step; the
  Tasks page is a read-only explainer of what each task involves, not an action
  screen. A custom task can optionally carry an **event_date** — once that date
  arrives it **banners on the sign-in page** for every student until it's marked
  done, in addition to its normal listing on the Tasks page.
- **Proof photos** — when a student completes any task at sign-out, they may upload
  a **completion photo** (e.g. the clean fridge) as evidence, stored in S3. Optional
  by default; the admin can mark a task **photo-required** so it can't be ticked done
  without one.

---

## 11. Image uploads (three distinct kinds)

Keep three separate image purposes distinct: **task reference photo** (admin, on the
task), **task completion/proof photo** (student, on the completion), and **time-card
photo** (periodic evidence, below). All to S3, all separate objects — don't collapse
them into one.

---

## 12. Notifications

**Event-driven:** committed schedule (on commit); leave requested (on submit — generic,
no name or reason, so it both flags the overseer to review and primes students that a
slot may open); slot open (on reopen — manual, approved-leave, or auto_unfilled all
fire the same way).
**Time-driven** (need §13): selection window opens; closing warning (~24h before
close); no-show warning (slot start + grace, if scheduled and not signed in).

- **Recipient:** all go to the **student LINE group** for v1 (no-show gently worded).
  Individual DMs need per-student LINE linking — defer.
- **Channel:** LINE **Messaging API** (LINE Notify was discontinued March 2025 — do
  not use it). Bot added to the group; push to the group ID. **Build notifications
  as a pluggable module** — email for v1, LINE switched on once the Official Account
  is set up. Volume is small; inside the free push quota.
- **Note:** Token ID and sercret added to .env - on build make a feature to manually send a message via the bot for intial testing and later use if necessary

---

## 13. Scheduled work on Render free tier

Free web services spin down after ~15 min idle (cold start ~30–60s) and give **750
free instance-hours/workspace/month.** Therefore:

- **In-process schedulers (APScheduler) are unreliable** — asleep = never fires.
- Use one **token-protected `/tick` endpoint** that runs **all due checks**, driven
  by an **external cron** (cron-job.org or a GitHub Actions scheduled workflow).
- **`/tick` is time-window-aware and idempotent:** on each call it asks *what is due
  and not yet done?* Every notification is gated by a **sent-flag** so it fires once;
  a missed/late/doubled ping self-heals. Compute grace from timestamps.
- **Cron on business hours only** (~07:00–18:00 Mon–Fri) → ~240 h/month, well under
  the 750 cap. A 24/7 ping risks suspension.
- Bulletproof option later: a Render cron job (~$1/month). Free external ping is the
  v1 target.

---

## 14. Bilingual model

**Content is as bilingual as whoever enters it chooses; fixed UI chrome (nav, headers,
buttons) is English-only** — a `zh`/`en` toggle was tried and dropped as dead weight
nobody used, so don't rebuild it without a real request.

- **Structured named content** (task titles; student names already have zh/en
  fields) — **two fields, 中文 + English, English optional.** Show both if present,
  else fall back to whichever is filled.
- **Free-text content** (task descriptions, sign-out reports) — **single box, any
  language(s).**

---

## 15. Student identity: colour × shape tokens

- **8 colourblind-aware colours** (managed palette, not random) × **4 shapes**
  (circle, triangle, square, diamond) = 32 unique tokens.
- **Exhaust colours first**; only reuse a colour differentiated by shape once all 8
  are used.
- **Token is always shown with the student's short name** — three redundant channels
  (colour, shape, text) survive colourblindness, bad monitors, printouts, photos.
- **Consistent across every view.** Overseer can override a colour if two look close.

Reference palette (adjust for legibility on light ground): blue `#0072B2`, orange
`#E69F00`, green `#009E73`, vermilion `#D55E00`, purple `#7E57C2`, teal `#0EA5A5`,
magenta `#C2185B`, brown `#8D6E63`.

---

## 16. Views and dashboard

- **Schedule view** — overseer's default landing; one tab, not separate Day and
  Week tabs. The colour+shape grid (hours as rows, days as columns), grouped and
  labelled week by week, with live status per cell (scheduled / recorded /
  no-show) and an **Advertise** button on any uncovered slot. Weeks that have
  already fully ended are simply not shown — only the current week and what's
  ahead. On narrow screens the grid doesn't switch to a different layout or
  scroll sideways — cell text abbreviates instead (student name to 2 letters,
  "8:00" to "8", etc.) so the same grid always fits.
- **Draft review** — same week-by-week grid, pre-commit: each cell is an
  editable dropdown listing only the students available for that hour (§7).
- **Overseer dashboard** — scheduled-vs-recorded gaps, no-shows, leave patterns
  (too-often / too-late), uncovered slots, task completion.

---

## 17. Demo / seed data (so it looks like the mockup on load)

- Add a **`flask seed-demo` CLI command** that inserts ~9 students (each with an
  assigned colour × shape), their availability, and a committed schedule for the
  current month, so the grid renders populated like `roster-mockup.html`.
- **Flag every seeded row** (`is_demo = true`, see SCHEMA) so demo data is
  distinguishable from real data.
- Add an admin **"Reset demo data"** action that deletes **only** `is_demo` rows —
  so the app can be explored and then wiped cleanly without ever touching real
  students or schedules.

---

## 18. Conventions and non-goals

- **No LLM for allocation** — deterministic, auditable, explainable to a
  disappointed student.
- **Predictable over clever** — manual edits don't re-solve; notifications fire once.
- **Config over hard-coding** — selection dates, grace, solver weights, floor
  minimum, upload cadence, cron window.
- Data layer swappable (SQLite → Postgres); notifications pluggable (email → LINE).

---

## 19. Suggested build order

0. **Scaffold on the pinned stack (§3–§4)** — Flask app factory, `base.html` with the
   mobile nav, `static/css/app.css` ported from `roster-mockup.html`. No Node.
1. Auth + invite; student registration (zh/en name, alphanumeric ID); semester/roster.
2. Slot generation from calendar + holidays/closed dates.
3. Availability selection + open/close window.
4. OR-Tools allocator (hard constraints + gentle soft prefs) → draft.
5. Draft review grid with availability-aware dropdown edits → commit.
6. Colour × shape identity system + day/week views (match the mockup).
7. Sign-in/out with run sessions + per-hour reports.
8. **Scheduled-vs-recorded on the dashboard. (Core — do not defer.)**
9. Leave requests + FCFS reopens + pattern tracking.
10. Regular/custom tasks with cadence guarding.
11. Time-card uploads to S3.
12. `/tick` endpoint + notifications (email first), then LINE Messaging API.
13. Monthly close-out report.
14. `seed-demo` command + reset (can be built early for visual testing).

Build one numbered step at a time and commit between each. Fast-follows after month
one: consistency/rotation tuning, individual LINE DMs. v2: cross-month fairness.
