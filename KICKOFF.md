# KICKOFF.md — how to restart the build with Claude Code

You have three reference files. Put all of them in the project folder (repo root is
fine) so Claude Code can read them:

- `CLAUDE.md` — the build brief (Claude Code auto-reads a file with this name).
- `SCHEMA.md` — the data model.
- `roster-mockup.html` — the visual target. **This is the source of truth for the
  look** — feeding the actual file works far better than describing the style.

---

## Step 1 — Paste this as your first message

> Reassess this project against `CLAUDE.md`, `SCHEMA.md`, and `roster-mockup.html`,
> which are in the repo.
>
> The current local build was scaffolded on the wrong frontend stack (a Node/Vite
> Vue SPA). I want it redone on the stack pinned in CLAUDE.md §3–§4:
>
> **Flask app (app-factory), frontend = Flask-served Jinja templates + Vue 3 from
> CDN (`vue.global.js`) in progressive mode, Vue `delimiters` set to `[[ ]]`. No
> Node, no npm, no Vite, no build step, no standalone SPA.** JS in `static/js/`, CSS
> in `static/css/`, templates in `templates/` with a shared `base.html`. SQLite,
> OR-Tools for allocation, S3 for uploads.
>
> First, **audit what exists** and tell me: which parts are stack-independent and
> worth keeping (data model / migrations, and the OR-Tools allocator if present),
> and which parts are the Node/SPA scaffold that should be removed. Don't change
> anything yet — give me the plan and wait for my go-ahead.

Read its audit, confirm or adjust, then let it proceed.

---

## Step 2 — Then build in order, one step at a time

Once you approve the plan, drive it through the build order in **CLAUDE.md §19**,
one numbered step per turn, committing between each. A good next message:

> Start with step 0: remove the Node/Vite scaffold, set up the Flask app factory,
> `base.html` with the mobile hamburger nav, and `static/css/app.css` ported from
> the tokens and components in `roster-mockup.html`. Commit when it's running, then
> stop and show me.

Do **not** ask it to build the whole app in one go — a project this size is far more
reliable step by step, and it's how the brief is written.

---

## What "keep vs redo" should land on

- **Keep (stack-independent):** the database models/migrations, and the OR-Tools
  allocator logic — pure Python, the hardest part to get right. Reuse if correct.
- **Redo (frontend scaffold):** anything tied to Node — `package.json`,
  `vite.config.*`, `node_modules/`, the SPA entry, JS build config. Replaced by
  Jinja + CDN Vue.

If the audit reveals the backend is thin and most of the build was frontend, a clean
Flask-first scaffold is the faster path — that's fine, the docs describe the whole
app from scratch.

---

## Guardrails to hold the line

- If Claude Code reaches for `npm`, Vite, or a build step, stop it and point back to
  CLAUDE.md §3–§4. The stack being unstated is what sent it to Node last time; now
  it's pinned, so hold it there.
- The **look must match `roster-mockup.html`** — if a screen doesn't, ask it to port
  the mockup's `app.css` tokens rather than invent styling.
- Build `seed-demo` early (§17) so you can see populated screens and judge the design
  against the mockup while everything else is still coming together.
