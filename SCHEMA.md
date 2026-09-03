# SCHEMA.md — OIA Duty Roster data model

Companion to `CLAUDE.md`. Field types are indicative (SQLite v1, Postgres-ready).
Timestamps are ISO 8601 with timezone (Asia/Taipei). IDs are surrogate integers/uuids
unless noted; `student_id` (the university number) is a separate business key.

---

## Entity overview

```
semester ──< student ──< availability >── slot >── assignment
                 │                          │
                 ├──< attendance_session ──< hourly_report
                 ├──< leave_request ── (reopens) ── slot
                 ├──< task_completion >── regular_task
                 ├──< custom_task (claimed_by)
                 └──< timecard_upload

month ──< selection_window
month ──< closed_date
month ──< schedule (draft→committed) ──< assignment
month ──< regular_slot >── student
regular_slot_template >── student  (persistent weekly pattern, not month-scoped)

notification_log   ui_string   user(auth)
```

---

## `semester`
Roster period — the student list changes each semester.

| field | type | notes |
|---|---|---|
| id | pk | |
| name | text | e.g. "2026 Fall" |
| starts_on / ends_on | date | |
| is_active | bool | |

---

## `student`

| field | type | notes |
|---|---|---|
| id | pk | |
| semester_id | fk → semester | roster membership |
| chinese_name | text | required |
| english_name | text | required |
| student_id | char(8) | **8 digits, numeric only, no letter prefix**; validate |
| colour | text | hex from the managed 8-colour palette (CLAUDE §15) |
| shape | enum | circle / triangle / square / diamond |
| line_user_id | text null | optional; only if individual DMs are enabled later |
| is_active | bool | leaving mid-semester deactivates rather than deletes |
| is_demo | bool | seeded demo row; **"Reset demo data" deletes only these** |
| created_at | ts | |

The `(colour, shape)` pair is unique **within a semester**, assigned colours-first.

---

## Calendar & month

### `month`
| field | type | notes |
|---|---|---|
| id | pk | |
| year_month | char(7) | "2026-09" |
| state | enum | setup / selection_open / selection_closed / draft / review / committed / running / closed |

### `closed_date`
Holidays / breaks — no slots generated.

| field | type | notes |
|---|---|---|
| id | pk | |
| date | date | |
| reason | text | |
| set_by | fk → user | overseer |

### `selection_window`
| field | type | notes |
|---|---|---|
| id | pk | |
| month_id | fk → month | |
| opens_at | ts | config |
| closes_at | ts | config |

---

## Regular schedule (standing slots)

Some students work the same hour every week. Rather than let the solver
guess a rotation, the overseer can name it directly, and the solver locks it
in before it runs anything else. Students still log availability every month
regardless — a regular slot is never assigned unless the student actually
offers that hour.

### `regular_slot_template`
The persistent weekly pattern — **not month-scoped**. Edited once by the
overseer; copied into `regular_slot` whenever a month is populated. Editing
the template only affects months populated afterward, so a mid-semester
change never silently rewrites a month already in progress.

| field | type | notes |
|---|---|---|
| id | pk | |
| weekday | int | 0=Mon .. 4=Fri |
| hour | int | one of the 8 slot hours |
| state | enum | unavailable / unassigned / assigned |
| student_id | fk → student null | set only when state=assigned |

`(weekday, hour)` unique.

### `regular_slot`
One month's instance of the pattern, per `(date, hour)` — the grid the
overseer actually edits, shown week by week for that month. Populated from
`regular_slot_template` (fills gaps only — safe to re-run, never clobbers a
hand edit), then hand-edited freely without touching the template.

| field | type | notes |
|---|---|---|
| id | pk | |
| month_id | fk → month | |
| date | date | |
| hour | int | |
| state | enum | unavailable / unassigned / assigned |
| student_id | fk → student null | set only when state=assigned |

`(date, hour)` unique. Drives `slot` generation directly (below) and feeds
the solver's regular-lock pre-pass (SCHEMA §`assignment`, `slot.source`
notes in `CLAUDE.md` §7 discussion).

---

## `slot`
The atomic assignable unit: one date + one hour, one seat.

| field | type | notes |
|---|---|---|
| id | pk | |
| date | date | Mon–Fri, not a closed_date |
| hour | int | 8,9,10,11,13,14,15,16 (start hour; 1-hour duration) |
| period | enum | morning / afternoon (derived, for grouping) |
| state | enum | open (unassigned) / assigned / reopened |

Generated for the month from the calendar minus `closed_date`s **and** any
`(date, hour)` whose `regular_slot` is `unavailable` — no slot at all is
created for those (coverage need varies month to month, not every hour needs
staffing). Cells with no `regular_slot` row fall back to plain generation
exactly as before the feature existed. `(date, hour)` unique.

---

## `availability`
An hour a student offered during selection. No per-student cap.

| field | type | notes |
|---|---|---|
| id | pk | |
| student_id | fk → student | |
| slot_id | fk → slot | |
| submitted_at | ts | |

`(student_id, slot_id)` unique. Solver input.

---

## `schedule`
A generated schedule version for a month (draft, then committed).

| field | type | notes |
|---|---|---|
| id | pk | |
| month_id | fk → month | |
| status | enum | draft / committed |
| generated_at | ts | |
| committed_at | ts null | |
| solver_weights | json | the tunable penalty weights used (audit trail) |
| is_demo | bool | seeded schedule; cleared by "Reset demo data" |

---

## `assignment`
One student in one slot. The scheduled truth.

| field | type | notes |
|---|---|---|
| id | pk | |
| schedule_id | fk → schedule | |
| slot_id | fk → slot | |
| student_id | fk → student | |
| source | enum | solver / manual_edit / claimed (FCFS reopen) / regular_lock |
| created_at | ts | |

`(schedule_id, slot_id)` unique → one student per slot. A manual edit replaces the
student on one assignment only (no re-solve). `regular_lock` marks a standing
`regular_slot` claim the solver locked in during its pre-pass, before running
CP-SAT on everything else — see "Regular schedule" above.

---

## `leave_request`

| field | type | notes |
|---|---|---|
| id | pk | |
| student_id | fk → student | |
| slot_id | fk → slot | slot being dropped |
| reason | text | |
| requested_at | ts | for lead-time / too-late analysis |
| lead_time_hours | int | derived: slot.start − requested_at |
| status | enum | pending / approved / denied |
| decided_by | fk → user null | |

On approval → the slot is reopened (below).

---

## `reopened_slot`
A slot freed by approved leave, offered first-come-first-served.

| field | type | notes |
|---|---|---|
| id | pk | |
| slot_id | fk → slot | |
| leave_request_id | fk → leave_request | |
| opened_at | ts | when advertised |
| claimed_by | fk → student null | first eligible claimer |
| claimed_at | ts null | |

On claim → write a new `assignment` with `source = claimed`.

---

## Attendance

### `attendance_session`
One sign-in→sign-out episode across a contiguous run of the student's slots.

| field | type | notes |
|---|---|---|
| id | pk | |
| student_id | fk → student | |
| date | date | |
| signed_in_at | ts | sign-in opens 10 min before first slot |
| signed_out_at | ts null | null + past end ⇒ **forgot-to-sign-out flag** |
| flagged | bool | forgot-to-sign-out, or signed-in-but-not-scheduled |

### `hourly_report`
Per-hour detail captured at sign-out (hourly even though sign-in is per-run).

| field | type | notes |
|---|---|---|
| id | pk | |
| session_id | fk → attendance_session | |
| slot_id | fk → slot | the hour this line covers |
| note | text null | free-text, any language |
| (task ticks recorded via task_completion / custom_task links) | | |

---

## Tasks

### `regular_task`
| field | type | notes |
|---|---|---|
| id | pk | |
| title_zh | text | |
| title_en | text null | optional; show both if present, else fallback |
| description | text null | free-text, single box |
| frequency | enum | daily / weekly / monthly / unlimited |
| interval | int | every N periods (default 1); ignored for unlimited |
| is_active | bool | |
| reference_s3_key | text null | admin's "what to do" photo (e.g. dirty fridge) |
| photo_required | bool | if true, completion needs a proof photo |

`unlimited` is for duties that are regular but not cadence-bound — required
whenever asked in person, any number of times a day, by anyone (e.g. courier
runs). It skips the once-per-period behaviour below entirely: every
completion gets its own unique `period_key`, so the task never drops off the
list and never blocks a second completion the same day.

### `task_completion`
Logs a regular task done — and enforces once-per-period (except `unlimited`
tasks, see above).

| field | type | notes |
|---|---|---|
| id | pk | |
| regular_task_id | fk → regular_task | |
| student_id | fk → student | |
| session_id | fk → attendance_session | |
| slot_id | fk → slot null | |
| completed_at | ts | |
| period_key | text | e.g. "2026-W37" (weekly) / "2026-09-08" (daily) / a random key (unlimited) |
| proof_s3_key | text null | student's completion photo (e.g. clean fridge) |

`(regular_task_id, period_key)` unique → task drops off the list once done for its
period (the fridge can't be cleaned 5× in a day) — moot for `unlimited` tasks,
whose period_key is never reused.

### `custom_task`
| field | type | notes |
|---|---|---|
| id | pk | |
| title_zh | text | |
| title_en | text null | optional |
| description | text null | free-text |
| created_by | fk → user | overseer |
| status | enum | open / claimed / done |
| claimed_by | fk → student null | |
| claimed_at | ts null | |
| event_date | date null | when set, the task is "due" — banners on the student's
  sign-in page from that date on, until it's marked done |
| reference_s3_key | text null | admin's "what to do" photo (e.g. dirty fridge) |
| photo_required | bool | if true, completion needs a proof photo |


---

## `timecard_upload`
Periodic photo of the physical time card — evidence, separate from reports.

| field | type | notes |
|---|---|---|
| id | pk | |
| student_id | fk → student | |
| period_label | text | e.g. "2026-09" or "2026-W37" |
| s3_key | text | object in S3 |
| uploaded_at | ts | |
| cadence | enum | per_session / weekly / monthly (config) |

---

## `notification_log`
Every notification, gated for once-only delivery (idempotent `/tick`).

| field | type | notes |
|---|---|---|
| id | pk | |
| type | enum | selection_open / closing_warning / committed / leave_requested / slot_open / no_show |
| target | enum | group / individual / overseer |
| related_type / related_id | text / int | e.g. slot, leave_request, month |
| sent_at | ts | |
| sent_flag | bool | set true once delivered → never resend |

Uniqueness on `(type, related_type, related_id, target)` prevents duplicates even if
`/tick` fires twice.

---

## `ui_string` (i18n)
Fixed UI text, bilingual. Can be a static JSON file rather than a table.

| field | type | notes |
|---|---|---|
| key | text pk | e.g. "btn.sign_in" |
| zh | text | |
| en | text | |

---

## `user` (auth)

| field | type | notes |
|---|---|---|
| id | pk | |
| student_id | fk → student null | null for overseer/admin accounts |
| role | enum | overseer / student |
| invite_token / auth fields | — | invite-only |

---

## Derived values (not stored — computed for the dashboard)

- **Scheduled hours** (student, month) = count of `assignment`s.
- **Recorded hours** (student, month) = covered hours from `attendance_session`s.
- **Gap** = scheduled − recorded. **The core dashboard number.**
- **No-show** = `assignment` for a past slot with no covering session.
- **Signed-in-but-not-scheduled** = session hour with no matching `assignment`.
- **Leave-too-often** = approved `leave_request`s per student per month.
- **Leave-too-late** = `leave_request`s with small `lead_time_hours`.
- **Uncovered slots** = committed `slot`s with no `assignment`.
- **Coverage %** = assigned slots / total slots for a day or week.

---

## Demo / seed data

- `flask seed-demo` inserts ~9 students, their availability, and a committed schedule
  for the current month so the UI renders populated like `roster-mockup.html`.
- Every seeded row carries `is_demo = true` (`student`, `schedule`; child rows —
  availability, assignment, session, etc. — trace to demo via their demo parent, or
  carry their own `is_demo` if simpler).
- **"Reset demo data"** deletes only `is_demo` rows, in FK-safe order, leaving real
  data untouched.

---

## Notes for implementation

- Enforce `student_id` as exactly 8 numeric digits at model and form layers.
- One-student-per-slot is a DB uniqueness constraint, not just app logic — solver,
  manual edits, and FCFS claims all funnel through `assignment`.
- Store `solver_weights` per schedule so a committed schedule is reproducible.
- Keep `timecard_upload` and `hourly_report` fully separate.
- `period_key` on `task_completion` makes cadence guarding trivial: compute the key
  from the task's frequency at completion time and rely on the unique index.
- `regular_slot_template` is the source of truth for the recurring pattern;
  `regular_slot` rows are a month's copy of it, safe to hand-edit per month
  without ever mutating the template. Never derive one from the other at
  read time — `populate` is the only copy point, and it's idempotent.
- Task images have three roles — `regular_task.reference_s3_key` /
  `custom_task.reference_s3_key` (admin, before), `task_completion.proof_s3_key`
  (student, after), and `timecard_upload.s3_key` (periodic evidence). Keep them in
  their own columns/objects; reuse the same S3 upload helper but don't share keys.
