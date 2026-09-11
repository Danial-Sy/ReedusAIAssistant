/* ReedOS app shell: splash, navigation, status pills, clock, toasts. */

const App = (() => {
  const $ = (s) => document.querySelector(s);
  let current = "home";

  /* navigation */
  function navigate(name) {
    if (name === current) return;
    const from = $("#view-" + current);
    const to = $("#view-" + name);
    if (!to) return;
    from.classList.add("leave-left");
    from.classList.remove("is-active");
    to.classList.remove("leave-left");
    to.classList.add("is-active");
    setTimeout(() => from.classList.remove("leave-left"), 300);
    current = name;
    if (name !== "remote") Link.drive(0, 0);
  }

  function currentView() { return current; }

  /* toasts */
  function toast(msg, ms = 2400) {
    const t = document.createElement("div");
    t.className = "toast";
    t.textContent = msg;
    $("#toasts").append(t);
    setTimeout(() => {
      t.classList.add("gone");
      setTimeout(() => t.remove(), 350);
    }, ms);
  }

  /* clock */
  function tickClock() {
    const d = new Date();
    $("#clock").textContent = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  /* status pills */
  function wireStatus() {
    Link.on("status", (s) => {
      setPill("#pill-robot", s.robot);
      setPill("#pill-phone", s.phone);
      setPill("#pill-chat", s.chat);
      const robotOn = s.robot === "on";
      $("#remote-mode-note").textContent =
        robotOn ? "robot linked ✓" : s.link === "live" ? "robot offline, daemon up" : "no daemon, mock";
      $("#tile-sub-remote").textContent =
        robotOn ? "Robot linked ✓" : s.link === "live" ? "Robot offline" : "Drive Reedus live";
    });
  }

  /* brain mode switch is deferred until the current turn ends so it never yanks the
     audio device mid-sentence; the button shows "pending" until status confirms */
  let brain = { mode: null, pending: null, switching: false, budget: {} };

  function setText(sel, v) { const el = $(sel); if (el) el.textContent = v; }

  function renderBrain(next) {
    brain = { ...brain, ...next };
    const wrap = $("#mode-switch");
    if (!wrap) return;
    const shown = brain.pending || brain.mode || "local";
    const busy = !!brain.pending || !!brain.switching;
    wrap.dataset.mode = shown;
    wrap.classList.toggle("pending", busy);
    wrap.classList.toggle("offline", !brain.mode);
    wrap.querySelectorAll(".seg-btn").forEach(b => b.classList.toggle("on", b.dataset.mode === shown));
    setText("#mode-note", busy ? "switching…" : "");

    const b = brain.budget || {};
    setText("#bc-mode", brain.pending ? `${brain.mode || "?"} → ${brain.pending}`
                                      : (brain.mode || "brain offline"));
    setText("#bc-backend", brain.backend || ", ");
    setText("#bc-voice", brain.voice ? "loaded" : "silent");
    setText("#bc-budget", b.cap ? `$${(b.spent || 0).toFixed(2)} of $${b.cap.toFixed(2)}`
                                + (b.exhausted ? ", spent" : "") : ", ");
    const note = $("#bc-note");
    if (note && brain.cloudWhy) note.textContent = "Cloud unavailable: " + brain.cloudWhy;
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
      if (m.accepted) {
        toast(m.pending ? `Switching to ${m.pending} mode…` : `Now in ${m.mode} mode`);
      } else {
        toast(m.why || "Couldn't change mode.");
        renderBrain({ pending: null });
      }
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

  function setPill(sel, state) {
    const el = $(sel);
    if (!el) return;
    el.dataset.state = state === "on" ? "on" : state === "mock" ? "mock" : "off";
  }

  /* splash / boot */
  const bootLines = [
    "waking up…",
    "counting cattails…",
    "spinning up encoders…",
    "listening for Reedus…",
  ];

  function boot() {
    /* apply saved appearance before anything shows */
    document.body.dataset.theme = Store.app.theme;
    document.body.classList.toggle("reduced-motion", !Store.app.fullAnimations);

    const statusEl = $("#splash-status");
    let i = 0;
    const cycle = setInterval(() => {
      i = (i + 1) % bootLines.length;
      statusEl.textContent = bootLines[i];
    }, 550);

    /* shell prepared behind the splash */
    $("#shell").hidden = false;
    Views.initViews();
    wireStatus();
    wireMode();
    wireLock();
    tickClock();
    setInterval(tickClock, 10000);

    Auth.status().then((a) => {
      if (a.reachable && a.required && !a.authed) showLock();
      else Link.connect(Store.app.daemonUrl);
    });

    /* let the logo animation play, then slide the splash away */
    const total = Store.app.fullAnimations ? 2300 : 300;
    setTimeout(() => {
      clearInterval(cycle);
      $("#splash").classList.add("leaving");
      setTimeout(() => $("#splash").remove(), 750);
    }, total);
  }

  /* passcode gate */
  function showLock(msg) {
    const lock = $("#lock");
    if (!lock) return;
    lock.hidden = false;
    $("#lock-err").textContent = msg || "";
    setTimeout(() => $("#lock-code") && $("#lock-code").focus(), 60);
  }

  function wireLock() {
    const form = $("#lock-form");
    if (!form) return;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = $("#lock-go"), code = $("#lock-code").value;
      btn.disabled = true;
      const out = await Auth.login(code);
      btn.disabled = false;
      if (!out.ok) {
        $("#lock-err").textContent = out.retryAfter
          ? `${out.error}` : (out.error || "wrong passcode");
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

  /* nav buttons */
  function wireNav() {
    document.querySelectorAll("[data-nav]").forEach(el => {
      el.addEventListener("click", () => navigate(el.dataset.nav));
    });
    $("#brand").addEventListener("click", () => navigate("home"));
    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && current !== "home") navigate("home");
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    wireNav();
    boot();
  });

  return { navigate, currentView, toast };
})();
