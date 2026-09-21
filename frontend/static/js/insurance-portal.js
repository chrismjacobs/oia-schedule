/* Insurance portal console snippets.
 *
 * The university's insurance portal is a separate system we can't post to, so
 * the overseer applies through its own form. This builds the JavaScript they
 * paste into that page's console to fill it from a student's record, instead
 * of retyping the ID, project and hours by hand for every student.
 *
 * The portal is ASP.NET WebForms and walks through stages, each one a postback
 * that reloads the page — a single pasted script dies at the first one. Hence
 * one snippet per stage, with the student's payload parked in the portal
 * page's own sessionStorage by stage 1 so the later stages still have it after
 * the reloads. Stage 3 keeps a cursor there too, so the same snippet pasted
 * again after each refresh fills the *next* day.
 *
 * The portal looks the student up itself: the only identifying thing it's
 * given is the national ID, and it fills the name, student ID, birthday and
 * department back into read-only spans. So there is nothing to type for those
 * — but there is something to check, and stage 2 checks it (see verifyLookup).
 */
(function (window) {
  "use strict";

  var PORTAL = {
    // "Unit". Left alone: the portal already preselects it from the admin
    // account that logged in, so there is nothing to set and setting it would
    // only risk charging the wrong budget. Putting an option value here
    // ("2Q10" Global Cooperation and Exchange, "2Q20" Overseas Student
    // Affairs) would override that, should it ever be needed.
    unit: { sel: "#ddlDp", value: null, label: "Unit" },

    // "Identity" — our students are all MCUT students. Neither radio is
    // checked when the page loads, so this one always has to be set.
    identity: { mcutStudent: "#rblIdentity_0", label: "Identity" },

    // "Insurance Type". Daily (日投保) is the default and the one this flow
    // wants — it's what makes the per-day hours form appear. Monthly carries
    // an onclick __doPostBack and would reload the page, so the snippet only
    // ever touches the daily radio, and only if something has unset it.
    insuranceType: { daily: "#rblInsurance_1", label: "Insurance type" },

    // Stage 1 ends here: the national ID, then Query.
    query: {
      nationalId: { sel: "#txtIdno", label: "National ID No." },
      button:     { sel: "#btnSearch", label: "Query button" },
      message:    { sel: "#lblHoursMsg", label: "Hours message" },
    },

    // What the Query fills in — read-only spans, checked against our record
    // rather than written to.
    lookup: {
      chineseName: "#lblCnm1",
      studentId:   "#lblStdno",
      birthday:    "#lblBirthday",
      department:  "#lblCabr",
    },

    // Stage 2: the job details, then the button through to the hours form.
    job: {
      activityName: { sel: "#txtJobName", label: "Activity Name" },
      funding:      { sel: "#ddlJobCategory", label: "Funding" },
      addButton:    { sel: "#btnAdd", label: "Add Application Form button" },
      message:      { sel: "#lblMsg", label: "Message" },
    },

    // Stage 3: one day's hours. Note the portal wants yyyy/MM/dd here, not
    // the ISO dates the API hands out, and start/end are four dropdowns
    // rather than a time field — the minute lists offer only 00 and 30, which
    // our on-the-hour slots always satisfy.
    times: {
      open:    { sel: "#btnAdd", label: "Add Part-time Work Hours button" },
      mode:    { sel: "#ddlMode", label: "Date cycle" },
      count:   { sel: "#txtCount", label: "Times" },
      date:    { sel: "#txtWorkDate", label: "Work date" },
      startHH: { sel: "#ddlSHH", label: "Start hour" },
      startMM: { sel: "#ddlSmm", label: "Start minute" },
      endHH:   { sel: "#ddlEHH", label: "End hour" },
      endMM:   { sel: "#ddlEmm", label: "End minute" },
      hours:   { sel: "#txtHours", label: "Hours" },
      // The button that actually saves the filled row isn't in the markup
      // captured so far — it sits below the table. Only the one-click
      // bookmarklet needs it; the normal flow has the overseer press it, so
      // this being TODO doesn't hold anything up.
      submit:  { sel: "#TODO_btnSaveRow", label: "Save row button (one-click variant only)" },
    },
  };

  var STORE_KEY = "oia.insurance";

  // Every selector still carrying a TODO — surfaced in the UI so a snippet is
  // never copied in the belief that it is complete when it isn't.
  function pendingSelectors() {
    var out = [];
    Object.keys(PORTAL).forEach(function (stage) {
      var group = PORTAL[stage];
      Object.keys(group).forEach(function (field) {
        var f = group[field];
        if (f && f.sel && f.sel.indexOf("TODO") !== -1) out.push(f.label);
      });
    });
    return out;
  }

  // ---------------- snippet building ----------------

  // Pasted scripts run on a page that is not ours, so they can share nothing
  // with it: each snippet carries its own copy of these helpers. `set` fires
  // input and change the way a real keystroke would, because WebForms hangs
  // validation off those events. `setSelect` refuses a value that isn't one of
  // the options — assigning an unknown value to a <select> silently blanks it,
  // which would submit the wrong funding source without a word.
  var PRELUDE = [
    "  var K = " + JSON.stringify(STORE_KEY) + ";",
    "  function pick(sel) {",
    "    var el = document.querySelector(sel);",
    "    if (!el) throw new Error('Field not found on this page: ' + sel);",
    "    return el;",
    "  }",
    "  function fire(el) {",
    "    el.dispatchEvent(new Event('input', { bubbles: true }));",
    "    el.dispatchEvent(new Event('change', { bubbles: true }));",
    "  }",
    "  function set(sel, value) { var el = pick(sel); el.value = value; fire(el); return el; }",
    "  function setSelect(sel, value) {",
    "    var el = pick(sel);",
    "    var ok = [].some.call(el.options, function (o) { return o.value === value; });",
    "    if (!ok) throw new Error('No option ' + JSON.stringify(value) + ' in ' + sel);",
    "    el.value = value; fire(el); return el;",
    "  }",
    "  function check(sel) { var el = pick(sel); if (!el.checked) el.click(); return el; }",
    "  function text(sel) { var el = document.querySelector(sel); return el ? el.textContent.trim() : ''; }",
  ].join("\n");

  function q(value) {
    return JSON.stringify(value == null ? "" : String(value));
  }

  function wrap(title, body) {
    return ["/* " + title + " */", "(function () {", PRELUDE, body, "})();"].join("\n");
  }

  /** Stage 1: identify the student and press Query.
   *
   *  Everything the portal needs before the lookup, and nothing after it —
   *  Query is a postback, so anything typed past this point would be filled
   *  in, reloaded away, and have to be done again. */
  function buildLookup(payload) {
    var s = payload.student;
    var lines = [
      // Parked before the click, because the click navigates.
      "  sessionStorage.setItem(K, " + q(JSON.stringify({
        student: s, month: payload.month, days: payload.days, cursor: 0,
      })) + ");",
    ];

    if (PORTAL.unit.value) {
      lines.push("  setSelect(" + q(PORTAL.unit.sel) + ", " + q(PORTAL.unit.value) + ");");
    }  // else: the portal sets Unit from the logged-in admin — untouched.

    lines.push("  check(" + q(PORTAL.identity.mcutStudent) + ");");
    // Daily is the page default; this only clicks if something unset it, and
    // never touches the monthly radio, which would postback.
    lines.push("  check(" + q(PORTAL.insuranceType.daily) + ");");

    if (!s.insurance_number) {
      // Nothing to look the student up by, so don't press Query on an empty
      // box — that just returns an error and loses the rest of the fill.
      lines.push("  console.warn(" + q("No national ID on record for " +
        (s.english_name || s.chinese_name) + " — type it into " +
        PORTAL.query.nationalId.label + " and press Query by hand, then run stage 2.") + ");");
    } else {
      lines.push("  set(" + q(PORTAL.query.nationalId.sel) + ", " + q(s.insurance_number) + ");");
      lines.push("  console.log(" + q("Looking up " + s.insurance_number + " — the page will reload. " +
        "Then paste stage 2.") + ");");
      lines.push("  pick(" + q(PORTAL.query.button.sel) + ").click();");
    }

    return wrap("OIA insurance · stage 1 · identify and Query", lines.join("\n"));
  }

  /** Stage 2: check who the portal found, then fill the job details.
   *
   *  The check is the point of this stage as much as the filling. The portal
   *  is keyed on the national ID alone, so a wrong or stale number on our
   *  record insures somebody else entirely — and every screen after this one
   *  looks perfectly normal. Comparing the student ID it returns against the
   *  one we hold catches that while it's still free to fix. */
  function buildJob(payload) {
    var s = payload.student;
    var job = PORTAL.job;
    var lines = [
      "  var state = JSON.parse(sessionStorage.getItem(K) || 'null');",
      "  if (!state) throw new Error('Run the stage 1 snippet first — nothing is parked for this student.');",
      "  var found = text(" + q(PORTAL.lookup.studentId) + ");",
      "  if (!found) throw new Error('The portal has not looked anyone up yet — press Query first.');",
      "  if (found.toUpperCase() !== String(state.student.student_id).toUpperCase()) {",
      "    throw new Error('WRONG STUDENT: the portal found ' + found + ' (' + text(" +
        q(PORTAL.lookup.chineseName) + ") + '), but this application is for ' +",
      "                    state.student.student_id + ' (' + state.student.chinese_name + '). " +
        "Check the national ID on their record before going on.');",
      "  }",
      "  console.log('Confirmed ' + found + ' · ' + text(" + q(PORTAL.lookup.chineseName) + ") +",
      "              ' · ' + text(" + q(PORTAL.lookup.department) + "));",
      "  var hoursMsg = text(" + q(PORTAL.query.message.sel) + ");",
      "  if (hoursMsg) console.warn('Portal says: ' + hoursMsg);",
    ];

    if (s.project_name) {
      lines.push("  set(" + q(job.activityName.sel) + ", " + q(s.project_name) + ");");
    } else {
      lines.push("  console.warn(" + q("No project name on record — " + job.activityName.label +
        " left blank.") + ");");
    }

    // funding_category is null when the overseer hasn't set one. "0" is
    // College — a real choice — so only null skips the field.
    if (s.funding_category == null) {
      lines.push("  console.warn(" + q("No funding source on record — " + job.funding.label +
        " left as the page had it.") + ");");
    } else {
      lines.push("  setSelect(" + q(job.funding.sel) + ", " + q(s.funding_category) + ");");
    }

    // Once the student is loaded, #btnAdd relabels itself to "Add Part-time
    // Work Hours" — same element, so the message names it by its current text
    // rather than a guess.
    lines.push("  console.log('Filled. Check it, then press \\u201c' + " +
      "(pick(" + q(job.addButton.sel) + ").value || 'Add') + '\\u201d — " +
      payload.days.length + " day(s), " + payload.total_hours + "h to add.');");
    return wrap("OIA insurance · stage 2 · confirm the student and fill the job details",
                lines.join("\n"));
  }

  /** Stage 3: fill the next unadded day.
   *
   *  Run once per day. The cursor lives in the portal page's sessionStorage,
   *  so it survives the reload that saving a row causes and the same code
   *  moves on by itself. Running it again after the last day is a no-op
   *  rather than an error.
   *
   *  `autoSubmit` saves the row too, for the one-click bookmarklet. It needs
   *  PORTAL.times.submit filled in first.
   *
   *  The double-run guard is the fiddly part. Advancing the cursor on every
   *  run would mean two clicks before saving silently skips a day — and a
   *  missing day in an insurance application is exactly the kind of thing
   *  nobody notices until it matters. So the index of the day filled during
   *  this page load is kept on `window`, which the reload wipes: run it twice
   *  without saving and it re-fills the same day, run it after a save and it
   *  moves on. */
  function buildDay(autoSubmit) {
    var t = PORTAL.times;
    var lines = [
      "  var state = JSON.parse(sessionStorage.getItem(K) || 'null');",
      "  if (!state) throw new Error('Run the stage 1 snippet first — nothing is parked for this student.');",
      // A clear instruction beats a confusing "field not found" from pick().
      "  if (!document.querySelector(" + q(t.date.sel) + ")) {",
      "    throw new Error('The hours row is not open — press " + t.open.label.replace(" button", "") + " first.');",
      "  }",
      "  var i = window.__oiaDayIndex;",
      "  if (i == null) {",
      "    i = state.cursor;",
      "    if (state.days[i]) { state.cursor = i + 1; sessionStorage.setItem(K, JSON.stringify(state)); }",
      "    window.__oiaDayIndex = i;",
      "  }",
      "  var day = state.days[i];",
      "  if (!day) { console.log('All ' + state.days.length + ' day(s) done. Nothing left.'); return; }",
      // The portal wants yyyy/MM/dd; the API speaks ISO.
      "  set(" + q(t.date.sel) + ", day.date.replace(/-/g, '/'));",
      "  setSelect(" + q(t.mode.sel) + ", '0');",   // Single Day
      "  var cnt = document.querySelector(" + q(t.count.sel) + ");",
      "  if (cnt && !cnt.disabled) { cnt.value = '1'; fire(cnt); }",
      "  setSelect(" + q(t.startHH.sel) + ", day.start.slice(0, 2));",
      "  setSelect(" + q(t.startMM.sel) + ", day.start.slice(3, 5));",
      "  setSelect(" + q(t.endHH.sel) + ", day.end.slice(0, 2));",
      "  setSelect(" + q(t.endMM.sel) + ", day.end.slice(3, 5));",
      "  set(" + q(t.hours.sel) + ", String(day.hours));",
      "  console.log('Day ' + (i + 1) + ' of ' + state.days.length + ': ' +",
      "              day.date + ' ' + day.start + '-' + day.end + ' (' + day.hours + 'h).');",
    ];

    if (autoSubmit) {
      lines.push("  pick(" + q(t.submit.sel) + ").click();");
    } else {
      // Saving here would reload before the overseer could look at the row,
      // and a wrong day submitted to a live insurance system is far more work
      // to undo than one extra click. So it stops at "filled".
      lines.push("  console.log('Check it, then save the row. After the reload, run this again for the next day.');");
    }
    return wrap("OIA insurance · stage 3 · " + (autoSubmit ? "fill and save the next day" : "fill the next day"),
                lines.join("\n"));
  }

  /** The stage 3 code as a `javascript:` URL, to keep as a bookmark.
   *
   *  This is the answer to the console being wiped by every postback: a
   *  bookmarklet is re-injected by the browser on each click, so it doesn't
   *  care that the page reloaded. One click per day instead of finding the
   *  console and pasting into it. The code is student-agnostic — it reads
   *  whoever stage 1 parked — so the bookmark is made once and kept. */
  function buildBookmarklet(autoSubmit) {
    return "javascript:" + encodeURIComponent(buildDay(autoSubmit) + ";void 0;");
  }

  /** Clears the parked payload — for starting a different student, or
   *  restarting one whose cursor ran ahead of what actually got added. */
  function buildReset() {
    return wrap("OIA insurance · reset", [
      "  sessionStorage.removeItem(K);",
      "  console.log('Cleared. Start again from stage 1.');",
    ].join("\n"));
  }

  /** Lists every field on the portal page with its id, name and nearest
   *  label — what stage 3's TODO selectors need to be filled in from. */
  function buildInspector() {
    return [
      "/* OIA insurance · field inspector — run on the hours form, copy the table */",
      "(function () {",
      "  function labelFor(el) {",
      "    if (el.id) {",
      "      var l = document.querySelector('label[for=\"' + el.id + '\"]');",
      "      if (l) return l.textContent.trim();",
      "    }",
      "    var cell = el.closest('td');",
      "    var prev = cell && cell.previousElementSibling;",
      "    return prev ? prev.textContent.trim().slice(0, 40) : '';",
      "  }",
      "  console.table([].map.call(",
      "    document.querySelectorAll('input, select, textarea, button'),",
      "    function (el) {",
      "      return {",
      "        tag: el.tagName.toLowerCase(), type: el.type || '',",
      "        id: el.id || '', name: el.name || '',",
      "        value: (el.value || '').slice(0, 20), label: labelFor(el),",
      "      };",
      "    }",
      "  ));",
      "})();",
    ].join("\n");
  }

  window.OIAInsurance = {
    PORTAL: PORTAL,
    pendingSelectors: pendingSelectors,
    buildLookup: buildLookup,
    buildJob: buildJob,
    buildDay: buildDay,
    buildBookmarklet: buildBookmarklet,
    buildReset: buildReset,
    buildInspector: buildInspector,
  };
})(window);
