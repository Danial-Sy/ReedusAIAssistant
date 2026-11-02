/* ReedOS app shell: splash, tabs + per-tab page stacks, brain mode, lock, toasts. */

const App = (() => {
  const $ = (s) => document.querySelector(s);
  const TABS = ["reedus", "lite", "chat", "settings"];
  const TAB_LABEL = { reedus: "Reedus", lite: "Reedus Lite", chat: "Chat", settings: "Settings" };
  const EASE = "cubic-bezier(.2,.8,.2,1)";

  /* each tab remembers the page it was on, like a phone app */
  const stack = { reedus: "reedus", lite: "lite", chat: "chat", settings: "settings" };
  let tab = "reedus";
  let current = "reedus";
  let booting = true;

  const motion = () => !document.body.classList.contains("reduced-motion");
  const viewEl = (name) => $("#view-" + name);
  const tabOf = (name) => (viewEl(name) && viewEl(name).dataset.tab) || "reedus";

  /* dir: 1 push (in from the right), -1 back, 0 tab switch (fade + rise) */
  function swap(fromName, toName, dir) {
    const from = viewEl(fromName), to = viewEl(toName);
    if (!to || from === to) return;
    if (from) from.classList.remove("is-active");
    to.classList.add("is-active");
    to.classList.remove("entering"); void to.offsetWidth; to.classList.add("entering");
    current = toName;
    if (!motion() || booting) return;          // the splash hides the first switch anyway
    const dx = dir * 36;
    const inFrom = dir ? `translateX(${dx}px)` : "translateY(10px) scale(.995)";
    to.animate([{ opacity: 0, transform: inFrom }, { opacity: 1, transform: "none" }],
               { duration: dir ? 320 : 280, easing: EASE });
    if (from) {
      from.animate([{ opacity: 1, transform: "none", visibility: "visible" },
                    { opacity: 0, transform: dir ? `translateX(${-dx * 0.4}px)` : "none", visibility: "visible" }],
                   { duration: dir ? 240 : 160, easing: EASE });
    }
  }

  function afterShow(name) {
    if (name !== "remote") Link.twist(0, 0);
    document.body.dataset.view = name;
    const narrow = window.matchMedia("(max-width: 760px)").matches;
    const label = narrow && tab === "lite" ? "Lite" : TAB_LABEL[tab];
    // on a phone the crumb is the page title; the tab bar already says which device
    $("#crumb").textContent = name === tab ? label
      : narrow ? viewEl(name).dataset.title : `${label} · ${viewEl(name).dataset.title}`;
    Lite.wake();
    requestAnimationFrame(Seg.refreshAll);
  }

  function selectTab(t, opts) {
    if (!TABS.includes(t)) t = "reedus";
    const was = tab;
    if (t === was && !(opts && opts.force)) {
      // tapping the tab you're on goes back to its root
      if (current !== t) { const from = current; stack[t] = t; swap(from, t, -1); afterShow(t); }
      else viewEl(t).scrollTo({ top: 0, behavior: motion() ? "smooth" : "auto" });
      return;
    }
    tab = t;
    document.body.dataset.tab = t;
    document.querySelectorAll(".nav-tab").forEach(b => {
      const on = b.dataset.tabGo === t;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", on);
    });
    moveNavIndicator();
    swap(current, stack[t], 0);
    afterShow(stack[t]);
    Store.setApp("tab", t);
  }

  function navigate(name) {
    if (!viewEl(name) || name === current) return;
    const t = tabOf(name);
    if (t !== tab) { stack[t] = name; selectTab(t); return; }
    const back = name === t;       // the tab root is "back"
    stack[t] = name;
    swap(current, name, back ? -1 : 1);
    afterShow(name);
  }

  function back() {
    if (current !== tab) navigate(tab);
  }

  function currentView() { return current; }

  function moveNavIndicator() {
    const ind = $(".nav-ind"), on = $(".nav-tab.on");
    if (!ind || !on) return;
    const vertical = getComputedStyle($("#nav")).flexDirection === "column";
    ind.style.width = on.offsetWidth + "px";
    ind.style.height = on.offsetHeight + "px";
    ind.style.transform = `translate(${on.offsetLeft}px, ${on.offsetTop}px)`;
    ind.dataset.axis = vertical ? "y" : "x";
  }

  /* toasts */
  function toast(msg, ms = 2400) {
    const t = document.createElement("div");
    t.className = "toast";
    t.textContent = msg;
    const box = $("#toasts");
    box.append(t);
    while (box.children.length > 3) box.firstChild.remove();
    setTimeout(() => {
      t.classList.add("gone");
      setTimeout(() => t.remove(), 350);
    }, ms);
  }

  function tickClock() {
    $("#clock").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  /* theme + motion */
  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
  const reduceQuery = window.matchMedia("(prefers-reduced-motion: reduce)");

  function applyTheme() {
    const pick = Store.app.theme === "auto" ? (darkQuery.matches ? "night" : "day") : Store.app.theme;
    document.body.dataset.theme = pick === "day" ? "day" : "night";
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = pick === "day" ? "#F3EEE3" : "#0E1812";
  }
  function applyMotion() {
    document.body.classList.toggle("reduced-motion", !Store.app.fullAnimations || reduceQuery.matches);
  }
  darkQuery.addEventListener && darkQuery.addEventListener("change", applyTheme);
  reduceQuery.addEventListener && reduceQuery.addEventListener("change", applyMotion);

  /* connection status: nav dots, footer, demo pill */
  function wireStatus() {
    Link.on("status", (s) => {
      const body = Views.brainBody(s);
      dot("#dot-reedus", s.robot === "on" ? "on" : s.robot === "mock" ? "mock" : "off");
      dot("#dot-lite", s.lite && s.lite.connected ? "on" : s.lite ? "mock" : "off");
      dot("#dot-chat", s.chat === "on" ? "on" : s.link === "mock" ? "mock" : "off");
      const demo = s.link !== "live";
      $("#demo-pill").hidden = !demo;
      document.body.classList.toggle("is-demo", demo);
      const conn = $("#conn");
      conn.dataset.state = demo ? "mock" : s.brain === "on" ? "on" : "half";
      $("#conn-text").textContent = demo ? "Demo mode"
        : s.brain !== "on" ? "Run box up, brain off"
        : body === "lite" ? "Brain in Lite" : "Brain in Reedus";
    });
    $("#demo-pill").addEventListener("click", () => {
      navigate("settings");
      setTimeout(() => { const a = $("#as-daemon"); a.focus(); a.select(); }, 350);
    });
  }
  function dot(sel, state) { const el = $(sel); if (el) el.dataset.state = state; }

  /* brain mode switch is deferred until the current turn ends so it never yanks the
     audio device mid-sentence; the button shows "pending" until status confirms */
  let brain = { mode: null, pending: null, switching: false, budget: {} };
  const setText = (sel, v) => { const el = $(sel); if (el) el.textContent = v; };

  function renderBrain(next) {
    brain = { ...brain, ...next };
    const wrap = $("#mode-switch");
    const shown = brain.pending || brain.mode || "local";
    const busy = !!brain.pending || !!brain.switching;
    wrap.dataset.mode = shown;
    wrap.classList.toggle("pending", busy);
    wrap.classList.toggle("offline", !brain.mode);
    Seg.set(wrap, shown);

    const b = brain.budget || {};
    setText("#bc-mode", brain.pending ? `${brain.mode || "?"} → ${brain.pending}` : (brain.mode || "brain offline"));
    setText("#bc-backend", brain.backend || "-");
    setText("#bc-voice", brain.voice ? "loaded" : "silent");
    setText("#bc-budget", b.cap ? `$${(b.spent || 0).toFixed(2)} of $${b.cap.toFixed(2)}` + (b.exhausted ? ", spent" : "") : "-");
    if (brain.cloudWhy) setText("#bc-note", "Cloud unavailable: " + brain.cloudWhy);
  }

  function wireMode() {
    document.querySelectorAll("#mode-switch .seg-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        const want = btn.dataset.mode;
        if (want === brain.mode && !brain.pending) return;
        Link.setMode(want);
        renderBrain({ pending: want });          // optimistic; status corrects it
      });
    });
    Link.on("mode", (m) => {
      if (!m) return;
      if (m.accepted) toast(m.pending ? `Switching to ${m.pending} mode…` : `Now in ${m.mode} mode`);
      else { toast(m.why || "Couldn't change mode."); renderBrain({ pending: null }); }
    });
    Link.on("status", (s) => renderBrain({
      mode: s.mode || null,
      pending: s.modePending || null,
      switching: !!s.modeSwitching,
      budget: s.budget || {},
      backend: s.backend,
      voice: s.voice,
      cloudWhy: s.cloudReady === false ? (s.cloudWhy || "") : "",
    }));
  }

  /* splash / boot */
  const bootLines = ["waking up…", "counting cattails…", "finding both bodies…", "listening for Reedus…"];

  function connect() {
    Auth.status().then((a) => {
      if (a.reachable && a.required && !a.authed) { showLock(); return; }
      if (!a.reachable) Link.demo();             // nobody home: demo now, keep dialing
      Link.connect(Store.app.daemonUrl);
    });
  }

  function boot() {
    applyTheme();
    applyMotion();

    const statusEl = $("#splash-status");
    let i = 0;
    const cycle = setInterval(() => { i = (i + 1) % bootLines.length; statusEl.textContent = bootLines[i]; }, 550);

    $("#shell").hidden = false;
    Views.initViews();
    Lite.init();
    Connections.init();
    wireStatus();
    wireMode();
    wireLock();
    tickClock();
    setInterval(tickClock, 10000);
    selectTab(Store.app.tab, { force: true });
    // deep links, e.g. the Spotify callback page sends you back to /#connections
    const deep = location.hash.slice(1);
    if (deep && viewEl(deep)) { navigate(deep); history.replaceState(null, "", location.pathname); }
    booting = false;
    connect();

    window.addEventListener("resize", moveNavIndicator);
    const total = motion() ? 1900 : 200;
    setTimeout(() => {
      clearInterval(cycle);
      $("#splash").classList.add("leaving");
      moveNavIndicator();
      Seg.refreshAll();
      setTimeout(() => $("#splash").remove(), 750);
    }, total);
  }

  /* passcode gate */
  function showLock(msg) {
    $("#lock").hidden = false;
    $("#lock-err").textContent = msg || "";
    setTimeout(() => $("#lock-code").focus(), 60);
  }

  function wireLock() {
    $("#lock-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = $("#lock-go"), code = $("#lock-code").value;
      btn.disabled = true;
      let out;
      try { out = await Auth.login(code); } catch { out = { ok: false, error: "can't reach reedosd" }; }
      btn.disabled = false;
      if (!out.ok) {
        $("#lock-err").textContent = out.error || "wrong passcode";
        $("#lock-form").classList.remove("shake"); void $("#lock-form").offsetWidth;
        $("#lock-form").classList.add("shake");
        $("#lock-code").select();
        return;
      }
      $("#lock-code").value = "";
      $("#lock-err").textContent = "";
      $("#lock").hidden = true;
      Link.connect(Store.app.daemonUrl);
      toast("Unlocked");
    });
    Link.on("locked", async () => {
      const a = await Auth.status();
      if (a.reachable && a.authed) { Link.connect(Store.app.daemonUrl); return; }
      Auth.forget();
      showLock("The passcode changed, enter the new one.");
    });
  }

  function wireNav() {
    document.querySelectorAll("[data-tab-go]").forEach(el => el.addEventListener("click", () => selectTab(el.dataset.tabGo)));
    document.querySelectorAll("[data-nav]").forEach(el => el.addEventListener("click", () => navigate(el.dataset.nav)));
    window.addEventListener("keydown", (e) => {
      if (e.target.matches("input, textarea, select")) return;
      if (e.key === "Escape") back();
      // 1-4 jump between tabs on a keyboard
      if (!e.ctrlKey && !e.metaKey && !e.altKey && /^[1-4]$/.test(e.key) && current !== "remote") selectTab(TABS[+e.key - 1]);
    });
    /* edge swipe from the left goes back, like iOS */
    let sx = null, sy = 0;
    $("#viewport").addEventListener("touchstart", (e) => {
      const t = e.touches[0];
      sx = t.clientX < 24 && current !== tab ? t.clientX : null; sy = t.clientY;
    }, { passive: true });
    $("#viewport").addEventListener("touchend", (e) => {
      if (sx === null) return;
      const t = e.changedTouches[0];
      if (t.clientX - sx > 70 && Math.abs(t.clientY - sy) < 60) back();
      sx = null;
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    wireNav();
    boot();
  });

  return { navigate, back, currentView, toast, applyTheme, applyMotion, connect, selectTab };
})();
