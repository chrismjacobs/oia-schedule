/* Insurance portal console snippet.
 *
 * The university's insurance portal is a separate system we can't post to, so
 * the overseer applies through its own form. This builds the JavaScript they
 * paste into that page's console to fill it from a student's record, instead
 * of retyping the ID, project and hours by hand for every student.
 *
 * ── Why it's one script and not one per stage ─────────────────────────────────
 * The portal is ASP.NET WebForms: every step is a postback that reloads the
 * page, which wipes whatever was pasted into the console. So the snippet
 * doesn't assume where it is — on each run it looks at what's actually on the
 * page, works out which stage that is, and fills that stage. Run it, press the
 * portal's own button, then up-arrow and Enter to run it again. One snippet
 * per student for the whole application.
 *
 * The student's payload is parked in the portal page's own sessionStorage, so
 * it survives the reloads, and with it a cursor saying which day is next.
 *
 * The portal looks the student up itself: the only identifying thing it's
 * given is the national ID, and it fills the name, student ID, birthday and
 * department back into read-only spans. So there is nothing to type for those
 * — but there is something to check, and stage 2 checks it.
 */
(function (window) {
  "use strict";

  var PORTAL = {
    // "Unit" (#ddlDp) is left alone: the portal preselects it from the admin
    // account that logged in, so there is nothing to set and setting it would
    // only risk charging the wrong budget.

    // "Identity" — our students are all MCUT students. Neither radio is
    // checked when the page loads, so this one always has to be set.
    identity: "#rblIdentity_0",

    // "Insurance Type". Daily (日投保) is the page default and the one this
    // flow wants — it's what makes the per-day hours form appear. Monthly
    // carries an onclick __doPostBack and would reload the page, so only the
    // daily radio is ever touched, and only if something has unset it.
    daily: "#rblInsurance_1",

    // Stage 1: the national ID, then Query.
    nationalId: "#txtIdno",
    queryButton: "#btnSearch",
    hoursMessage: "#lblHoursMsg",

    // What the Query fills in — read-only spans, checked against our record
    // rather than written to.
    foundStudentId: "#lblStdno",
    foundName: "#lblCnm1",
    foundDept: "#lblCabr",

    // Stage 2: the job details. #btnAdd relabels itself to "Add Part-time
    // Work Hours" once a student is loaded; it's the same element either way.
    jobName: "#txtJobName",
    funding: "#ddlJobCategory",
    addButton: "#btnAdd",

    // Stage 3: one day's hours. The portal wants yyyy/MM/dd here, not the ISO
    // dates the API hands out, and start/end are four dropdowns rather than a
    // time field — the minute lists offer only 00 and 30, which our
    // on-the-hour slots always satisfy.
    mode: "#ddlMode",          // 0 = Single Day, 1 = Multiple Days, 2 = Period
    count: "#txtCount",
    workDate: "#txtWorkDate",
    startHH: "#ddlSHH",
    startMM: "#ddlSmm",
    endHH: "#ddlEHH",
    endMM: "#ddlEmm",
    hours: "#txtHours",
  };

  var STORE_KEY = "oia.insurance";

  function q(value) {
    return JSON.stringify(value == null ? "" : String(value));
  }

  // Pasted code runs on a page that is not ours, so it can share nothing with
  // it and carries its own helpers.
  //
  //  - `set` fires input and change the way a real keystroke would, because
  //    WebForms hangs validation off those events.
  //  - `setSelect` refuses a value that isn't one of the options: assigning an
  //    unknown value to a <select> silently blanks it, which would submit the
  //    wrong funding source without a word.
  //  - `check` clicks a radio only when it isn't already set, so the daily
  //    insurance radio is never clicked needlessly.
  var HELPERS = [
    "  var K = " + JSON.stringify(STORE_KEY) + ";",
    "  function opt(sel) { return document.querySelector(sel); }",
    "  function pick(sel) {",
    "    var el = opt(sel);",
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
    "  function text(sel) { var el = opt(sel); return el ? el.textContent.trim() : ''; }",
  ].join("\n");

  /** The whole application for one student, as one re-runnable script. */
  function buildScript(payload) {
    var s = payload.student;
    var P = PORTAL;
    var who = s.english_name || s.chinese_name;

    var lines = [];

    lines.push("  var PAYLOAD = " + JSON.stringify({
      student: s, month: payload.month, days: payload.days,
    }) + ";");

    // ---- park or resume ----
    // Progress is kept per student *and* month, not in one slot. A single slot
    // meant that doing Genie, switching to Caramel, then coming back to Genie
    // restarted her at day 1 — re-adding a day already saved, as a duplicate
    // row in a live insurance system. Keyed this way, each application keeps
    // its own cursor and they can be interleaved freely.
    //
    // The cursor is only trusted while the day list is unchanged: if the
    // schedule was edited since, "day 3" no longer means the same thing, so it
    // starts over and says so.
    lines.push(
      "  var key = PAYLOAD.student.id + ':' + PAYLOAD.month.id;",
      "  var all = JSON.parse(sessionStorage.getItem(K) || '{}');",
      "  var state = all[key];",
      "  if (state && JSON.stringify(state.days) !== JSON.stringify(PAYLOAD.days)) {",
      "    console.warn('The schedule changed since this application was started — restarting the day list.');",
      "    state = null;",
      "  }",
      "  if (!state) state = { student: PAYLOAD.student, month: PAYLOAD.month, days: PAYLOAD.days, cursor: 0 };",
      "  else state.student = PAYLOAD.student;",   // pick up name/ID/project corrections
      "  function save() { all[key] = state; sessionStorage.setItem(K, JSON.stringify(all)); }",
      "  save();",
      "  if (state.cursor > 0 && state.cursor < state.days.length) {",
      "    console.log('Resuming ' + PAYLOAD.student.english_name + ' ' + PAYLOAD.month.year_month +",
      "                ' at day ' + (state.cursor + 1) + ' of ' + state.days.length + '.');",
      "  }"
    );

    // ---- stage 1: identify the student, then Query ----
    lines.push(
      "  function stage1() {",
      "    check(" + q(P.identity) + ");",
      "    check(" + q(P.daily) + ");"
    );
    if (!s.insurance_number) {
      // Nothing to look the student up by, so don't press Query on an empty
      // box — that just returns an error and loses the rest of the fill.
      lines.push(
        "    console.warn(" + q("No national ID on record for " + who +
          " — type it into the National ID box and press Query by hand, then run this again.") + ");"
      );
    } else {
      lines.push(
        "    set(" + q(P.nationalId) + ", " + q(s.insurance_number) + ");",
        "    console.log(" + q("Stage 1 — looking up " + s.insurance_number +
          ". The page will reload; then run this again (up-arrow, Enter).") + ");",
        "    pick(" + q(P.queryButton) + ").click();"
      );
    }
    lines.push("  }");

    // ---- stage 2: confirm who was found, then fill the job details ----
    // The check is as much the point of this stage as the filling. The portal
    // is keyed on the national ID alone, so a wrong or stale number on our
    // record insures somebody else entirely — and every screen after this one
    // looks perfectly normal. Comparing the student ID it returns against the
    // one we hold catches that while it is still free to fix.
    lines.push(
      "  function confirmStudent() {",
      "    var found = text(" + q(P.foundStudentId) + ");",
      "    if (found && found.toUpperCase() !== String(state.student.student_id).toUpperCase()) {",
      "      throw new Error('WRONG STUDENT: the portal has ' + found + ' (' + text(" + q(P.foundName) + ") +",
      "        '), but this application is for ' + state.student.student_id + ' (' + state.student.chinese_name +",
      "        '). Check the national ID on their record before going on.');",
      "    }",
      "    return found;",
      "  }",
      "  function fillJob() {"
    );
    if (s.project_name) {
      lines.push("    set(" + q(P.jobName) + ", " + q(s.project_name) + ");");
    } else {
      lines.push("    console.warn(" + q("No project name on record — Activity Name left blank.") + ");");
    }
    // funding_category is null when the overseer hasn't set one. "0" is
    // College — a real choice — so only null skips the field.
    if (s.funding_category == null) {
      lines.push("    console.warn(" + q("No funding source on record — Funding left as the page had it.") + ");");
    } else {
      lines.push("    setSelect(" + q(P.funding) + ", " + q(s.funding_category) + ");");
    }
    lines.push(
      "  }",
      "  function stage2() {",
      "    var found = confirmStudent();",
      "    var msg = text(" + q(P.hoursMessage) + ");",
      "    if (msg) console.warn('Portal says: ' + msg);",
      "    fillJob();",
      "    console.log('Stage 2 — confirmed ' + found + ' · ' + text(" + q(P.foundName) + ") + ' · ' +",
      "                text(" + q(P.foundDept) + ") + '. " + payload.days.length + " day(s), " +
                    payload.total_hours + "h to add.');",
      "    console.log('Check it, then press \\u201c' + (opt(" + q(P.addButton) + ") || {}).value +",
      "                '\\u201d and run this again.');",
      "  }"
    );

    // ---- stage 3: fill the next unadded day ----
    // The double-run guard is the fiddly part. Advancing the cursor on every
    // run would mean running it twice before saving silently skips a day — and
    // a missing day in an insurance application is exactly the kind of thing
    // nobody notices until it matters. So the index filled during this page
    // load is kept on `window`, which the reload wipes: run it twice without
    // saving and it re-fills the same day, run it after a save and it moves on.
    lines.push(
      "  function stage3() {",
      "    confirmStudent();",
      // The job fields sit on the same form; if a postback cleared them, put
      // them back rather than submitting a row against a blank activity name.
      "    var jn = opt(" + q(P.jobName) + ");",
      "    if (jn && !jn.value) { fillJob(); console.log('(re-filled the job details)'); }",
      // Keyed like the parked state, so switching application part-way
      // through a page load doesn't reuse the other one's index.
      "    var mark = window.__oiaFill;",
      "    var i = (mark && mark.key === key) ? mark.i : null;",
      "    if (i == null) {",
      "      i = state.cursor;",
      "      if (state.days[i]) { state.cursor = i + 1; save(); }",
      "      window.__oiaFill = { key: key, i: i };",
      "    }",
      "    var day = state.days[i];",
      "    if (!day) {",
      "      console.log('All ' + state.days.length + ' day(s) done for ' + state.month.year_month + '. Nothing left.');",
      "      return;",
      "    }",
      "    set(" + q(P.workDate) + ", day.date.replace(/-/g, '/'));",
      "    setSelect(" + q(P.mode) + ", '0');",
      "    var cnt = opt(" + q(P.count) + ");",
      "    if (cnt && !cnt.disabled) { cnt.value = '1'; fire(cnt); }",
      "    setSelect(" + q(P.startHH) + ", day.start.slice(0, 2));",
      "    setSelect(" + q(P.startMM) + ", day.start.slice(3, 5));",
      "    setSelect(" + q(P.endHH) + ", day.end.slice(0, 2));",
      "    setSelect(" + q(P.endMM) + ", day.end.slice(3, 5));",
      "    set(" + q(P.hours) + ", String(day.hours));",
      "    console.log('Stage 3 — day ' + (i + 1) + ' of ' + state.days.length + ': ' + day.date + ' ' +",
      "                day.start + '-' + day.end + ' (' + day.hours + 'h).');",
      // Saving here would reload before the overseer could look at the row,
      // and a wrong day submitted to a live insurance system is far more work
      // to undo than one extra keystroke. So it stops at "filled".
      "    console.log('Check it, save the row, then run this again for the next day.');",
      "  }"
    );

    // ---- which stage are we on? ----
    // Decided from what the page actually has, so the same script works
    // wherever it lands. The hours row is checked first because it opens
    // *below* the job fields — both are present at that point, and the hours
    // row is the more specific signal.
    lines.push(
      "  if (opt(" + q(P.workDate) + ")) stage3();",
      "  else if (text(" + q(P.foundStudentId) + ")) stage2();",
      "  else if (opt(" + q(P.nationalId) + ")) stage1();",
      "  else throw new Error('This does not look like the insurance application page — " +
        "no National ID box, no loaded student, no hours row.');"
    );

    return [
      "/* OIA insurance · " + who + " · " + payload.month.year_month,
      "   Run this, press the portal's button, then up-arrow + Enter to run it again.",
      "   It works out which stage the page is on each time. */",
      "(function () {",
      HELPERS,
      lines.join("\n"),
      "})();",
    ].join("\n");
  }

  /** Clears all parked progress — for restarting a day list that ran ahead of
   *  what actually got saved. Switching students doesn't need it: each
   *  application keeps its own cursor. */
  function buildReset() {
    return [
      "/* OIA insurance · reset — forgets how far every application has got */",
      "(function () {",
      "  sessionStorage.removeItem(" + JSON.stringify(STORE_KEY) + ");",
      "  delete window.__oiaFill;",
      "  console.log('Cleared. Every application starts from day 1 again.');",
      "})();",
    ].join("\n");
  }

  /** Lists every field on whatever portal page is open, with its id, name and
   *  nearest label — for when the portal's markup changes and PORTAL above
   *  needs updating. */
  function buildInspector() {
    return [
      "/* OIA insurance · field inspector */",
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
    buildScript: buildScript,
    buildReset: buildReset,
    buildInspector: buildInspector,
  };
})(window);
