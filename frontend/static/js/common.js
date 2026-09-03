/* Shared helpers for every page — plain script (no bundler, no ES modules).
   Loaded before Vue and before each page's own script. Exposes window.OIA. */
(function () {
  "use strict";

  // ---------------- fetch wrapper ----------------
  async function request(method, path, body) {
    const opts = { method, credentials: "same-origin", headers: {} };
    if (body instanceof FormData) {
      opts.body = body;
    } else if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    let data = null;
    const text = await res.text();
    if (text) {
      try { data = JSON.parse(text); } catch (e) { data = text; }
    }
    if (!res.ok) {
      const err = new Error((data && data.message) || (data && data.error) || ("HTTP " + res.status));
      err.status = res.status;
      err.payload = data;
      throw err;
    }
    return data;
  }
  const api = {
    get: (p) => request("GET", p),
    post: (p, b) => request("POST", p, b),
    put: (p, b) => request("PUT", p, b),
    patch: (p, b) => request("PATCH", p, b),
    del: (p) => request("DELETE", p),
  };

  // ---------------- bilingual formatting (student names, task titles) ----------------
  function bilingual(zh, en) {
    if (zh && en) return zh + " " + en;
    return zh || en || "";
  }

  // "2026-09-08" -> "Mon". Built from y/m/d parts (not `new Date(str)`) so it's
  // never off by a day due to the browser parsing the date as UTC midnight.
  const WEEKDAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  function weekdayLabel(dateStr) {
    const [y, m, d] = dateStr.split("-").map(Number);
    return WEEKDAY_LABELS[new Date(y, m - 1, d).getDay()];
  }
  function weekdayIndex(dateStr) {
    const [y, m, d] = dateStr.split("-").map(Number);
    return new Date(y, m - 1, d).getDay(); // 0 Sun .. 6 Sat
  }
  // Python's date.weekday() convention (0=Mon..6=Sun) — matches what the
  // backend returns for weekday/hour patterns (regular hours, last month's
  // hours), so the two sides agree on what "Tuesday" means as a number.
  function mondayWeekday(dateStr) {
    const js = weekdayIndex(dateStr);
    return js === 0 ? 6 : js - 1;
  }

  // ---------------- week-by-week grouping (Regular / Draft / merged schedule grid) ----------------
  // Splits a sorted list of "YYYY-MM-DD" weekday strings into weeks, starting
  // a new group at each Monday. The first/last week of a month can be a
  // partial 1-4 day week — that's expected, not a bug.
  function groupIntoWeeks(dates) {
    const out = [];
    let cur = [];
    dates.forEach((d) => {
      if (weekdayIndex(d) === 1 && cur.length) { out.push(cur); cur = []; }
      cur.push(d);
    });
    if (cur.length) out.push(cur);
    return out;
  }

  // ---------------- narrow-viewport watcher (abbreviate grid text below ~640px) ----------------
  function watchNarrow(onChange, breakpoint) {
    const mq = window.matchMedia("(max-width: " + (breakpoint || 640) + "px)");
    onChange(mq.matches);
    const listener = (e) => onChange(e.matches);
    mq.addEventListener("change", listener);
    return mq;
  }

  // ---------------- grid text abbreviation (same narrow-screen convention everywhere) ----------------
  function shortName(name) { return name ? name.slice(0, 2) : ""; }
  function hourLabel(hour, narrow) { return narrow ? String(hour) : hour + ":00"; }
  const STATE_LABELS = {
    unavailable: { full: "Unavailable", short: "na" },
    unassigned: { full: "Unassigned", short: "--" },
  };
  function stateLabel(state, narrow) {
    const entry = STATE_LABELS[state];
    if (!entry) return state;
    return narrow ? entry.short : entry.full;
  }

  // ---------------- colour x shape identity tokens ----------------
  function shapeSVG(color, shape) {
    const shapes = {
      circle: '<circle cx="12" cy="12" r="9"/>',
      triangle: '<path d="M12 3 L21 20 L3 20 Z"/>',
      square: '<rect x="4" y="4" width="16" height="16" rx="2.5"/>',
      diamond: '<path d="M12 2.5 L21.5 12 L12 21.5 L2.5 12 Z"/>',
    };
    return '<svg viewBox="0 0 24 24" fill="' + color + '" stroke="rgba(0,0,0,.14)" stroke-width="1">' + (shapes[shape] || shapes.circle) + "</svg>";
  }

  // Vue global component: <oia-token :student="s"></oia-token>
  function registerGlobals(app) {
    app.component("oia-token", {
      props: { student: { type: Object, required: true }, size: { type: String, default: "" } },
      template: '<span class="tok" :class="size" v-html="svg"></span>',
      computed: {
        svg() { return shapeSVG(this.student.colour, this.student.shape); },
      },
    });
    app.config.globalProperties.$bilingual = bilingual;
  }

  // ---------------- header nav (hamburger drawer), plain JS ----------------
  function initHeader() {
    const btn = document.querySelector("[data-hamburger]");
    const drawer = document.querySelector("[data-drawer]");
    const backdrop = document.querySelector("[data-drawer-backdrop]");
    function close() {
      if (drawer) drawer.classList.remove("open");
      if (backdrop) backdrop.classList.remove("open");
    }
    function open() {
      if (drawer) drawer.classList.add("open");
      if (backdrop) backdrop.classList.add("open");
    }
    if (btn) btn.addEventListener("click", open);
    if (backdrop) backdrop.addEventListener("click", close);
    document.querySelectorAll("[data-drawer-close]").forEach((el) => el.addEventListener("click", close));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });

    const logoutBtn = document.querySelector("[data-logout]");
    if (logoutBtn) {
      logoutBtn.addEventListener("click", async () => {
        await api.post("/api/auth/logout");
        window.location.href = "/login";
      });
    }
  }
  document.addEventListener("DOMContentLoaded", initHeader);

  window.OIA = {
    api, bilingual, shapeSVG, weekdayLabel, registerGlobals,
    groupIntoWeeks, watchNarrow, shortName, hourLabel, stateLabel, mondayWeekday,
  };
})();
