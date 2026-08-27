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

  // ---------------- i18n ----------------
  const STRINGS = {
    "common.loading": { zh: "載入中…", en: "Loading…" },
    "common.save": { zh: "儲存", en: "Save" },
    "common.cancel": { zh: "取消", en: "Cancel" },
    "common.submit": { zh: "送出", en: "Submit" },
    "common.none": { zh: "無", en: "None" },
    "common.approve": { zh: "核准", en: "Approve" },
    "common.deny": { zh: "拒絕", en: "Deny" },
    "common.claim": { zh: "認領", en: "Claim" },
    "common.delete": { zh: "刪除", en: "Delete" },
    "common.upload": { zh: "上傳", en: "Upload" },
    "common.hours": { zh: "小時", en: "hrs" },
    "nav.day": { zh: "當日", en: "Day" },
    "nav.week": { zh: "週表", en: "Week" },
    "nav.dashboard": { zh: "總覽", en: "Dashboard" },
    "nav.draft": { zh: "草案審閱", en: "Draft review" },
    "nav.setup": { zh: "後台設定", en: "Setup" },
    "nav.availability": { zh: "選填時段", en: "Availability" },
    "nav.my_schedule": { zh: "我的班表", en: "My Schedule" },
    "nav.sign_in_out": { zh: "簽到退", en: "Sign in/out" },
    "nav.leave": { zh: "請假", en: "Leave" },
    "nav.tasks": { zh: "任務", en: "Tasks" },
    "nav.timecards": { zh: "工時卡", en: "Timecards" },
    "nav.logout": { zh: "登出", en: "Log out" },
  };
  const LANG_KEY = "oia_lang";
  function getLang() { return localStorage.getItem(LANG_KEY) || "zh"; }
  function setLang(l) {
    localStorage.setItem(LANG_KEY, l);
    document.querySelectorAll("[data-lang-btn]").forEach((b) => {
      b.classList.toggle("on", b.dataset.langBtn === l);
    });
    document.body.dispatchEvent(new CustomEvent("oia:lang-changed", { detail: l }));
  }
  function t(key, vars) {
    const entry = STRINGS[key];
    let str = entry ? (entry[getLang()] || entry.en || key) : key;
    if (vars) {
      Object.keys(vars).forEach((k) => { str = str.replace("{" + k + "}", vars[k]); });
    }
    return str;
  }
  function bilingual(zh, en) {
    if (zh && en) return zh + " " + en;
    return zh || en || "";
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
    app.config.globalProperties.$t = t;
    app.config.globalProperties.$bilingual = bilingual;
  }

  // ---------------- header nav (hamburger drawer + lang toggle), plain JS ----------------
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

    document.querySelectorAll("[data-lang-btn]").forEach((b) => {
      b.classList.toggle("on", b.dataset.langBtn === getLang());
      b.addEventListener("click", () => setLang(b.dataset.langBtn));
    });

    const logoutBtn = document.querySelector("[data-logout]");
    if (logoutBtn) {
      logoutBtn.addEventListener("click", async () => {
        await api.post("/api/auth/logout");
        window.location.href = "/login";
      });
    }
  }
  document.addEventListener("DOMContentLoaded", initHeader);

  window.OIA = { api, t, getLang, setLang, bilingual, shapeSVG, registerGlobals };
})();
