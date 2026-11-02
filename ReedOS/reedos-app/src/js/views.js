/* ReedOS views: behavior for each screen. app.js calls initViews() once. */

/* segmented controls with a sliding thumb. .seg > .seg-ind + .seg-btn[data-val|data-mode] */
const Seg = (() => {
  function key(b) { return b.dataset.val ?? b.dataset.mode; }
  function place(el) {
    const ind = el.querySelector(".seg-ind"), on = el.querySelector(".seg-btn.on");
    if (!ind) return;
    if (!on || !on.offsetWidth) { ind.style.opacity = on ? ind.style.opacity : "0"; return; }
    ind.style.opacity = "1";
    ind.style.width = on.offsetWidth + "px";
    ind.style.transform = `translateX(${on.offsetLeft - 3}px)`;
  }
  function set(el, value) {
    if (!el) return;
    el.dataset.val = value;
    el.querySelectorAll(".seg-btn").forEach(b => b.classList.toggle("on", key(b) === value));
    place(el);
  }
  function bind(el, value, onPick) {
    if (!el) return;
    set(el, value);
    el.querySelectorAll(".seg-btn").forEach(b => b.addEventListener("click", () => {
      if (b.disabled) return;
      const v = key(b);
      if (onPick(v) !== false) set(el, v);
    }));
  }
  function refreshAll() { document.querySelectorAll(".seg").forEach(place); }
  window.addEventListener("resize", refreshAll);
  return { bind, set, place, refreshAll };
})();

const Views = (() => {
  const $ = (s) => document.querySelector(s);
  const AVATAR = `<svg><use href="#badge"/></svg>`;
  let status = {};

  /* who has the brain: "robot" | "lite" | null */
  function brainBody(s) {
    if (!s || s.brain !== "on" && s.link !== "mock") return null;
    return s.lite ? "lite" : (s.brain === "on" ? "robot" : null);
  }

  /* --- Reedus home --- */
  function initReedusHome() {
    Link.on("status", (s) => {
      const drive = s.robot === "on" ? "on" : s.robot === "mock" ? "wait" : "off";
      setChip("#chip-drive", drive, s.robot === "on" ? "Drive linked" : s.robot === "mock" ? "Drive (demo)" : "Drive offline");
      const body = brainBody(s);
      setChip("#chip-brain", body === "robot" ? "on" : body === "lite" ? "away" : "off",
              body === "robot" ? `Brain · ${s.mode || "on"}` : body === "lite" ? "Brain is in Lite" : "Brain offline");
      setChip("#chip-cam", s.cameraUrl ? "on" : "off", s.cameraUrl ? "Camera live" : "No camera");
      $("#reedus-line").textContent =
        s.robot === "on" ? (body === "robot" ? "Linked and listening." : "Linked. Drive-only while the brain is elsewhere.")
        : s.robot === "mock" ? "Demo mode, nothing here moves a real robot."
        : s.link === "live" ? "reedosd is up, but the robot isn't on the network." : "Looking for the robot…";
      $("#tile-sub-remote").textContent =
        s.robot === "on" ? "Linked ✓ joystick, keys, wheel speeds" : "Joystick, keys, live wheel speeds";
      $("#remote-mode-note").textContent =
        s.robot === "on" ? "linked ✓" : s.link === "live" ? "robot offline" : "demo";
    });
  }

  function setChip(sel, state, text) {
    const el = $(sel);
    if (!el) return;
    el.dataset.state = state;
    el.lastChild.textContent = text;
  }

  /* --- remote --- */
  let joy = { active: false };

  function initRemote() {
    const pad = $("#joypad"), knob = $("#joyknob");
    const maxR = () => pad.clientWidth / 2 - knob.clientWidth / 2 - 6;
    const setKnob = (dx, dy) => { knob.style.transform = `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px))`; };

    function apply(dx, dy) {
      const r = maxR();
      const maxS = Store.robot.maxSpeed / 100, sens = Store.robot.turnSens / 100;
      Link.twist((-dy / r) * maxS, (dx / r) * sens * maxS);       // up = +forward
    }

    function onMove(e) {
      if (!joy.active) return;
      const rect = pad.getBoundingClientRect();
      let dx = e.clientX - (rect.left + rect.width / 2);
      let dy = e.clientY - (rect.top + rect.height / 2);
      const r = maxR(), d = Math.hypot(dx, dy);
      if (d > r) { dx = dx / d * r; dy = dy / d * r; }
      setKnob(dx, dy);
      apply(dx, dy);
    }

    pad.addEventListener("pointerdown", (e) => {
      joy.active = true;
      pad.classList.add("dragging");
      pad.setPointerCapture(e.pointerId);
      if (navigator.vibrate) navigator.vibrate(8);
      onMove(e);
    });
    pad.addEventListener("pointermove", onMove);
    const release = () => {
      if (!joy.active) return;
      joy.active = false;
      pad.classList.remove("dragging");
      setKnob(0, 0);
      Link.twist(0, 0);
    };
    pad.addEventListener("pointerup", release);
    pad.addEventListener("pointercancel", release);

    const keys = new Set();
    const KEYMAP = { w: "fwd", arrowup: "fwd", s: "back", arrowdown: "back",
                     a: "left", arrowleft: "left", d: "right", arrowright: "right" };

    function keyDrive() {
      if (App.currentView() !== "remote") return;
      const maxS = Store.robot.maxSpeed / 100, sens = Store.robot.turnSens / 100;
      const fy = (keys.has("fwd") ? 1 : 0) + (keys.has("back") ? -1 : 0);
      const fx = (keys.has("right") ? 1 : 0) + (keys.has("left") ? -1 : 0);
      Link.twist(fy * maxS, fx * sens * maxS);
      const r = maxR();
      setKnob(fx * r * 0.7, -fy * r * 0.7);
    }

    window.addEventListener("keydown", (e) => {
      if (App.currentView() !== "remote" || e.target.matches("input, textarea, select")) return;
      const k = e.key.toLowerCase();
      if (KEYMAP[k]) {
        e.preventDefault();                 // stop arrow keys scrolling the view
        if (!e.repeat) { keys.add(KEYMAP[k]); keyDrive(); }
      }
      if (k === " ") { e.preventDefault(); Link.stop(); App.toast("Stopped"); }
    });
    window.addEventListener("keyup", (e) => {
      const k = e.key.toLowerCase();
      if (KEYMAP[k] && keys.delete(KEYMAP[k])) keyDrive();
    });
    /* dropping the keys when the window loses focus prevents a stuck throttle */
    window.addEventListener("blur", () => {
      if (keys.size) { keys.clear(); Link.twist(0, 0); setKnob(0, 0); }
    });

    bindRange("#maxspeed", "#maxspeed-val", v => v + "%", (v) => Store.setRobot("maxSpeed", v), Store.robot.maxSpeed);
    bindRange("#turnsens", "#turnsens-val", v => v + "%", (v) => Store.setRobot("turnSens", v), Store.robot.turnSens);

    $("#estop").addEventListener("click", () => {
      Link.stop();
      if (navigator.vibrate) navigator.vibrate([30, 40, 30]);
      App.toast("Motors stopped");
    });

    Link.on("telemetry", (t) => {
      if (App.currentView() !== "remote") return;
      const RATED = 76;
      $("#rpm-l").textContent = Math.round(Math.abs(t.rpm.l));
      $("#rpm-r").textContent = Math.round(Math.abs(t.rpm.r));
      $("#rpm-l-bar").style.width = Math.min(100, Math.abs(t.rpm.l) / RATED * 100) + "%";
      $("#rpm-r-bar").style.width = Math.min(100, Math.abs(t.rpm.r) / RATED * 100) + "%";
      $("#lat").textContent = t.latencyMs.toFixed(1);
      $("#lat-bar").style.width = Math.min(100, t.latencyMs / 50 * 100) + "%";
      if (t.headingDeg !== undefined) {
        $("#head-deg").textContent = (t.headingDeg > 0 ? "+" : "") + t.headingDeg.toFixed(1);
        $("#head-bar").style.width = Math.min(100, Math.abs(t.headingDeg) / 10 * 100) + "%";
      }
    });
  }

  /* --- routines --- */
  let editing = null;   // routine object being edited
  let runningId = null;

  function renderRoutineList() {
    const list = $("#routine-list");
    list.innerHTML = "";
    if (!Store.routines.length) {
      list.innerHTML = `<p class="hint">No routines yet. Tap "+ New" and teach Reedus a trick.</p>`;
    }
    Store.routines.forEach((r, i) => {
      const card = document.createElement("div");
      card.className = "routine-card" + (r.id === runningId ? " running" : "");
      card.style.animationDelay = (i * 50) + "ms";
      const last = r.lastRun ? new Date(r.lastRun).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "never";
      card.innerHTML = `
        <h4></h4>
        <span class="r-meta">${r.steps.length} steps · last run ${last}</span>
        <div class="r-actions">
          <button class="cta gold r-run">▶ Run</button>
          <button class="ghost-btn r-edit">Edit</button>
          <button class="ghost-btn r-del" aria-label="Delete">Delete</button>
        </div>`;
      card.querySelector("h4").textContent = r.name;
      card.querySelector(".r-run").addEventListener("click", () => {
        runningId = r.id;
        Store.touchRoutine(r.id);
        Link.runRoutine(r);
        renderRoutineList();
        App.toast(`Running "${r.name}"`);
      });
      card.querySelector(".r-edit").addEventListener("click", () => openEditor(structuredClone(r)));
      const del = card.querySelector(".r-del");
      del.addEventListener("click", () => {
        // two taps: phones fat-finger
        if (!del.classList.contains("confirm")) {
          del.classList.add("confirm"); del.textContent = "Sure?";
          setTimeout(() => { del.classList.remove("confirm"); del.textContent = "Delete"; }, 2500);
          return;
        }
        card.classList.add("leaving");
        setTimeout(() => { Store.deleteRoutine(r.id); renderRoutineList(); }, 220);
        App.toast("Routine deleted");
      });
      list.append(card);
    });
    $("#tile-sub-routines").textContent = Store.routines.length
      ? `${Store.routines.length} saved · tap to run` : "Build & run sequences";
  }

  function openEditor(routine) {
    editing = routine || { id: Store.newId(), name: "", steps: [], lastRun: null };
    $("#routine-name").value = editing.name;
    $("#routine-list").hidden = true;
    $("#routine-new").hidden = true;
    $("#routine-editor").hidden = false;
    renderSteps();
  }

  function closeEditor() {
    editing = null;
    $("#routine-editor").hidden = true;
    $("#routine-list").hidden = false;
    $("#routine-new").hidden = false;
    renderRoutineList();
  }

  function renderSteps() {
    const ol = $("#steps-list");
    ol.innerHTML = "";
    $("#steps-empty").hidden = editing.steps.length > 0;
    editing.steps.forEach((s, i) => {
      const def = Blocks[s.type];
      const li = document.createElement("li");
      li.className = "step-item";
      li.dataset.ix = i;
      li.style.animationDelay = (i * 25) + "ms";
      const ico = document.createElement("span");
      ico.className = "st-icon"; ico.textContent = def.icon;
      const body = document.createElement("div");
      body.className = "st-body";
      const label = document.createElement("b");
      label.textContent = def.label;
      body.append(label, ...def.render(s));
      const tools = document.createElement("div");
      tools.className = "st-tools";
      tools.append(mkBtn("▲", "Move up", () => moveStep(i, -1)),
                   mkBtn("▼", "Move down", () => moveStep(i, +1)),
                   mkBtn("✕", "Remove", () => { editing.steps.splice(i, 1); renderSteps(); }));
      li.append(ico, body, tools);
      ol.append(li);
    });
  }

  function mkBtn(txt, label, fn) {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = txt; b.setAttribute("aria-label", label);
    b.addEventListener("click", fn);
    return b;
  }

  function moveStep(i, d) {
    const j = i + d;
    if (j < 0 || j >= editing.steps.length) return;
    const [s] = editing.steps.splice(i, 1);
    editing.steps.splice(j, 0, s);
    renderSteps();
  }

  function initRoutines() {
    const pal = $("#block-palette");
    for (const [, def] of Object.entries(Blocks)) {
      const b = document.createElement("button");
      b.className = "block-btn";
      b.innerHTML = `<span class="b-ico">${def.icon}</span> ${def.label} <small>${def.hint}</small>`;
      b.addEventListener("click", () => {
        if (!editing) return;
        editing.steps.push(def.make());
        renderSteps();
        const last = $("#steps-list").lastElementChild;
        if (last) last.scrollIntoView({ block: "nearest", behavior: "smooth" });
      });
      pal.append(b);
    }

    $("#routine-new").addEventListener("click", () => openEditor(null));
    $("#routine-cancel").addEventListener("click", closeEditor);
    $("#routine-save").addEventListener("click", () => {
      if (!editing) return;
      editing.name = $("#routine-name").value.trim() || "Untitled routine";
      Store.saveRoutine(structuredClone(editing));
      App.toast(`Saved "${editing.name}"`);
      closeEditor();
    });
    $("#routine-run").addEventListener("click", () => {
      if (!editing) return;
      editing.name = $("#routine-name").value.trim() || "Untitled routine";
      Link.runRoutine(editing);
      App.toast(`Running "${editing.name}"`);
    });

    Link.on("routine-progress", (p) => {
      if (editing) {
        document.querySelectorAll(".step-item").forEach(li => {
          li.classList.toggle("active-step", !p.done && +li.dataset.ix === p.stepIx);
        });
      }
      if (p.done) { runningId = null; if (!editing) renderRoutineList(); }
    });

    renderRoutineList();
  }

  /* --- camera --- */
  function initCamera() {
    const img = $("#cam-stream"), off = $("#cam-offline");
    let streamUrl = null;

    Link.on("status", (s) => {
      if (s.cameraUrl && s.cameraUrl !== streamUrl) {
        streamUrl = s.cameraUrl;
        img.src = Auth.withKey(s.cameraUrl);
        img.hidden = false; off.hidden = true;
        $("#cam-note").textContent = "live";
      } else if (!s.cameraUrl) {
        streamUrl = null;
        img.hidden = true; off.hidden = false;
        $("#cam-note").textContent = "offline";
      }
      $("#tile-sub-camera").textContent = s.cameraUrl ? "Live ✓" : "See what he sees";
    });
    img.addEventListener("error", () => { img.hidden = true; off.hidden = false; });

    $("#cam-snap").addEventListener("click", () => {
      if (img.hidden) { App.toast("No stream to snapshot"); return; }
      const c = document.createElement("canvas");
      c.width = img.naturalWidth || 640; c.height = img.naturalHeight || 480;
      c.getContext("2d").drawImage(img, 0, 0);
      const a = document.createElement("a");
      a.download = "reedus-" + Date.now() + ".png";
      a.href = c.toDataURL("image/png");
      a.click();
      App.toast("Snapshot saved");
    });
    $("#cam-full").addEventListener("click", () => {
      const stage = $("#cam-stage");
      if (document.fullscreenElement) document.exitFullscreen();
      else if (stage.requestFullscreen) stage.requestFullscreen();
      else if (img.webkitEnterFullscreen) img.webkitEnterFullscreen();
    });
  }

  /* --- chat (shared brain) --- */
  function initChat() {
    const thread = $("#chat-thread"), form = $("#chat-form"), input = $("#chat-input");
    const micBtn = $("#chat-mic"), hint = $("#chat-hint");
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition || null;
    let rec = null;         // in-flight browser recognition
    let greeted = false;

    Seg.bind($("#chat-input-seg"), Store.app.chatInput, v => { Store.setApp("chatInput", v); applyChoice(); });
    Seg.bind($("#chat-route-seg"), Store.app.chatRoute, v => { Store.setApp("chatRoute", v); applyChoice(); });
    $("#chat-tts").checked = Store.app.ttsInChat;
    $("#chat-tts").addEventListener("change", (e) => { Store.setApp("ttsInChat", e.target.checked); applyChoice(); });

    function applyChoice() {
      const talk = Store.app.chatInput === "talk";
      const mobo = Store.app.chatRoute === "mobo";
      const body = brainBody(status);
      $("#route-body").textContent = body === "lite" ? "Lite's speaker" : "His speaker";
      Seg.place($("#chat-route-seg"));
      $("#chat-body").textContent = body === "lite" ? "speaking through Reedus Lite"
        : body === "robot" ? "speaking through the robot"
        : status.link === "mock" ? "demo replies" : "brain offline";
      micBtn.hidden = !talk;
      input.placeholder = talk ? (mobo ? "or type, his mic is on the box" : "or type, tap the mic to speak")
                               : "Say something to Reedus";
      let msg = "";
      if (talk && !mobo && !SR) msg = "This browser can't record. Use his speaker and mic, or type.";
      else if (talk && mobo && status.micOwner === "lite") msg = "Lite's mic is always on, just say “hey Reedus” near it.";
      else if (talk && mobo && status.micOwner === "voiceloop") msg = "He already has his mic open, you can just talk to him out loud.";
      else if (mobo && status.brain === "on" && status.voice === false) msg = "No voice loaded on the box, replies get read out here instead.";
      hint.textContent = msg;
    }

    function bubble(who, text, speaking) {
      $("#chat-empty").hidden = true;
      const m = document.createElement("div");
      m.className = "msg " + who;
      const av = who === "reedus" ? `<span class="avatar">${AVATAR}</span>` : "";
      m.innerHTML = `${av}<div class="msg-col"><div class="bubble"></div>${speaking ? '<div class="speaking"><i></i><i></i><i></i> speaking</div>' : ""}</div>`;
      m.querySelector(".bubble").textContent = text;
      thread.append(m);
      thread.scrollTo({ top: thread.scrollHeight, behavior: "smooth" });
      return m;
    }

    function typing() {
      $("#chat-empty").hidden = true;
      const m = document.createElement("div");
      m.className = "msg reedus typing";
      m.innerHTML = `<span class="avatar">${AVATAR}</span><div class="bubble"><span class="tdot"></span><span class="tdot"></span><span class="tdot"></span></div>`;
      thread.append(m);
      thread.scrollTo({ top: thread.scrollHeight, behavior: "smooth" });
      return m;
    }

    /* browser TTS only when the daemon didn't already speak it (reply.tts). that flag is
       decided server-side so he can never talk over himself */
    function speak(text) {
      if (!Store.app.ttsInChat || !window.speechSynthesis) return false;
      try {
        const u = new SpeechSynthesisUtterance(text);
        u.rate = Store.robot.speechRate / 100;
        u.volume = Store.robot.volume / 100;
        u.pitch = { chirpy: 1.5, lowfi: 0.7, whisper: 0.9 }[Store.robot.voiceProfile] || 1.0;
        speechSynthesis.speak(u);
        return true;
      } catch { return false; }
    }

    async function ask(text) {
      bubble("me", text);
      const t = typing();
      const reply = await Link.chat(text, { speak: Store.app.ttsInChat, route: Store.app.chatRoute });
      t.remove();
      const here = reply.tts ? speak(reply.text) : false;
      bubble("reedus", reply.text, here || reply.spoke);
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      await ask(text);
    });

    micBtn.addEventListener("click", async () => {
      if (Store.app.chatRoute === "mobo") {
        micBtn.classList.add("rec");
        hint.textContent = "listening on his mic…";
        const out = await Link.listen(Store.app.ttsInChat);
        micBtn.classList.remove("rec");
        applyChoice();
        if (out.error) { App.toast(out.error); return; }
        if (out.heard) bubble("me", out.heard);
        if (out.text) {
          const here = out.tts ? speak(out.text) : false;
          bubble("reedus", out.text, here || out.spoke);
        }
        return;
      }
      if (!SR) { App.toast("No speech recognition in this browser."); return; }
      if (rec) { rec.stop(); return; }
      rec = new SR();
      rec.lang = "en-GB";
      rec.interimResults = false;
      rec.maxAlternatives = 1;
      micBtn.classList.add("rec");
      rec.onresult = (e) => {
        const said = (e.results[0][0].transcript || "").trim();
        if (said) ask(said);
      };
      rec.onerror = () => App.toast("Didn't catch that.");
      rec.onend = () => { rec = null; micBtn.classList.remove("rec"); };
      rec.start();
    });

    Link.on("status", (s) => {
      const notice = $("#chat-notice");
      notice.hidden = s.chat === "on" || s.link === "mock";
      notice.textContent = "Brain not running. Start the voice loop (or run_lite.sh) on the run box.";
      applyChoice();
      if (!greeted && s.link === "mock") {
        greeted = true;
        setTimeout(() => bubble("reedus", "Hey Danial. Demo brain here until you point me at the run box."), 500);
      }
    });

    applyChoice();
  }

  /* --- robot settings --- */
  function initRobotSettings() {
    const r = Store.robot;
    const save = (k) => (v) => { Store.setRobot(k, v); synced("#rs-sync"); };
    bindRange("#rs-speed", "#rs-speed-val", v => v + "%", save("speedLimit"), r.speedLimit);
    bindRange("#rs-accel", "#rs-accel-val", v => v + "%", save("accelRamp"), r.accelRamp);
    bindRange("#rs-wd", "#rs-wd-val", v => v + " ms", save("watchdogMs"), r.watchdogMs);
    bindSwitch("#rs-noises", "robotNoises", "#rs-sync");
    bindSwitch("#rs-idle", "idleWander", "#rs-sync");
    bindSwitch("#rs-tipstop", "tipStop", "#rs-sync");
  }

  /* --- shared + app settings --- */
  function initSettings() {
    const r = Store.robot;
    const save = (k) => (v) => { Store.setRobot(k, v); synced("#set-sync"); };
    $("#rs-voice").value = r.voiceProfile;
    $("#rs-voice").addEventListener("change", (e) => save("voiceProfile")(e.target.value));
    bindRange("#rs-expr", "#rs-expr-val", v => v + "%", save("expressiveness"), r.expressiveness);
    bindRange("#rs-rate", "#rs-rate-val", v => v + "%", save("speechRate"), r.speechRate);
    bindRange("#rs-vol", "#rs-vol-val", v => v + "%", save("volume"), r.volume);
    bindSwitch("#rs-llm", "llmEnabled", "#set-sync");
    $("#rs-wake").value = r.wakeWord;
    $("#rs-wake").addEventListener("change", (e) => save("wakeWord")(e.target.value.trim() || "hey reedus"));

    /* brain card: which body holds it */
    Link.on("status", (s) => {
      const body = brainBody(s);
      $("#body-pick").dataset.body = body || "none";
      $("#bc-body").textContent = body === "lite" ? "The brain is in Reedus Lite right now."
        : body === "robot" ? "The brain is in the robot right now."
        : "The brain is offline. It lives in one body at a time.";
    });

    /* appearance */
    Seg.bind($("#as-theme"), Store.app.theme, (v) => { Store.setApp("theme", v); App.applyTheme(); });
    $("#as-anim").checked = Store.app.fullAnimations;
    $("#as-anim").addEventListener("change", (e) => {
      Store.setApp("fullAnimations", e.target.checked);
      App.applyMotion();
    });

    /* connection */
    const addr = $("#as-daemon");
    addr.value = prettyAddr(Store.app.daemonUrl);
    const connect = () => {
      const url = Store.normalizeDaemon(addr.value);
      Store.setApp("daemonUrl", url);
      addr.value = prettyAddr(url);
      App.connect();
      App.toast("Connecting to " + prettyAddr(url) + "…");
    };
    $("#as-reconnect").addEventListener("click", connect);
    addr.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); connect(); } });

    Link.on("status", (s) => {
      $("#as-conn-state").textContent = s.link === "live"
        ? `Linked to reedosd at ${prettyAddr(Store.app.daemonUrl)}`
        : `Demo mode, nothing answered at ${prettyAddr(Store.app.daemonUrl)}`;
      if (s.hostIp) $("#as-ip").textContent = s.hostIp;
      if (s.link === "live") lockState(s.locked);
    });

    function lockState(required) {
      const el = $("#as-lock-state");
      el.textContent = required ? "Passcode required to connect."
                                : "No passcode, anyone on this network can drive Reedus.";
      el.classList.toggle("warn", !required);
    }
    Auth.status().then((a) => {
      if (a.reachable) lockState(a.required);
      else $("#as-lock-state").textContent = "Shows once ReedOS reaches the run box.";
    });

    $("#as-passcode-save").addEventListener("click", async () => {
      const code = $("#as-passcode").value.trim();
      if (code && code.length < 4) { App.toast("Use at least 4 characters"); return; }
      let out;
      try { out = await Auth.setPasscode(code); } catch { out = { ok: false, error: "reedosd isn't reachable" }; }
      if (!out.ok) { App.toast(out.error); return; }
      $("#as-passcode").value = "";
      lockState(out.required);
      App.toast(out.required ? "Passcode set, other devices must sign in again" : "Passcode removed, ReedOS is open");
    });

    $("#pw-quit").hidden = !(window.__TAURI__ && window.__TAURI__.window);
    $("#pw-quit").addEventListener("click", () => window.__TAURI__.window.getCurrentWindow().close());
  }

  function prettyAddr(url) {
    try {
      const u = new URL(url);
      return u.port === "8765" && u.pathname === "/ws" ? u.hostname : u.host;
    } catch { return url || ""; }
  }

  /* --- diagnostics --- */
  const chart = { data: [], max: 120 };

  function initDiagnostics() {
    const cvs = $("#rpm-chart");
    const ctx = cvs.getContext("2d");

    Link.on("telemetry", (t) => {
      chart.data.push({ l: t.rpm.l, r: t.rpm.r });
      if (chart.data.length > chart.max) chart.data.shift();
      $("#tile-sub-diag").textContent = `${t.latencyMs.toFixed(0)} ms · ${Link.mode === "live" ? "live" : "demo"}`;
      if (App.currentView() !== "diagnostics") return;
      $("#d-lat").textContent = t.latencyMs.toFixed(1);
      $("#d-pps").textContent = Math.round(t.pps);
      $("#d-cl").textContent = t.counts.l.toLocaleString();
      $("#d-cr").textContent = t.counts.r.toLocaleString();
      $("#d-up").textContent = fmtUp(t.uptimeS);
      $("#d-mode").textContent = Link.mode === "live" ? "live" : "demo";
      drawChart(ctx, cvs);
    });

    Link.on("log", (e) => {
      const con = $("#log-console");
      const line = document.createElement("div");
      const cls = e.level === "err" ? "lv-err" : e.level === "warn" ? "lv-warn" : "lv-info";
      const ts = new Date().toLocaleTimeString();
      line.innerHTML = `<span class="${cls}">[${ts}] ${e.level.toUpperCase()}</span> `;
      line.append(document.createTextNode(e.msg));
      con.append(line);
      while (con.children.length > 200) con.firstChild.remove();
      con.scrollTop = con.scrollHeight;
    });

    $("#diag-selftest").addEventListener("click", () => {
      Link.requestSelfTest();
      App.toast("Self-test started");
    });
  }

  function drawChart(ctx, cvs) {
    const dpr = window.devicePixelRatio || 1;
    const w = cvs.clientWidth, h = 120;
    if (cvs.width !== Math.round(w * dpr)) { cvs.width = Math.round(w * dpr); cvs.height = h * dpr; cvs.style.height = h + "px"; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const css = getComputedStyle(document.body);
    ctx.strokeStyle = css.getPropertyValue("--line");
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();
    const plot = (key, color) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.beginPath();
      chart.data.forEach((d, i) => {
        const x = i / (chart.max - 1) * w;
        const y = h / 2 - (d[key] / 80) * (h / 2 - 6);
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      ctx.stroke();
    };
    plot("l", css.getPropertyValue("--green-hi"));
    plot("r", css.getPropertyValue("--gold"));
  }

  function fmtUp(s) {
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
    return Math.floor(s / 3600) + "h " + Math.floor(s % 3600 / 60) + "m";
  }

  /* --- power --- */
  function initPower() {
    $("#pw-sleep").addEventListener("click", () => { Link.stop(); App.toast("Reedus sleeping, motors off"); });
    $("#pw-reboot").addEventListener("click", () => App.toast(Link.mode === "live" ? "Reboot sent to robot" : "Robot not linked"));
    $("#pw-daemon").addEventListener("click", () => App.toast(Link.mode === "live" ? "reedosd restarting" : "Daemon not linked"));
  }

  /* --- shared helpers --- */
  function bindRange(sel, valSel, fmt, onChange, initial) {
    const el = document.querySelector(sel), out = document.querySelector(valSel);
    const fill = () => el.style.setProperty("--fill", ((el.value - el.min) / (el.max - el.min) * 100) + "%");
    if (initial !== undefined) { el.value = initial; out.textContent = fmt(initial); }
    fill();
    el.addEventListener("input", () => {
      const v = +el.value;
      out.textContent = fmt(v);
      fill();
      onChange(v);
    });
  }

  function bindSwitch(sel, key, noteSel) {
    const el = $(sel);
    el.checked = Store.robot[key];
    el.addEventListener("change", () => { Store.setRobot(key, el.checked); synced(noteSel); });
  }

  const syncTimers = {};
  function synced(sel) {
    const el = $(sel);
    if (!el) return;
    el.textContent = Link.mode === "live" ? "saved ✓" : "saved here, syncs when linked";
    el.classList.add("flash");
    clearTimeout(syncTimers[sel]);
    syncTimers[sel] = setTimeout(() => { el.textContent = ""; el.classList.remove("flash"); }, 2000);
  }

  function initViews() {
    Link.on("status", (s) => { status = s; });
    initReedusHome();
    initRemote();
    initRoutines();
    initCamera();
    initChat();
    initRobotSettings();
    initSettings();
    initDiagnostics();
    initPower();
    /* every range gets a filled track, including ones bound elsewhere */
    document.querySelectorAll('input[type="range"]').forEach(el => {
      const fill = () => el.style.setProperty("--fill", ((el.value - el.min) / (el.max - el.min) * 100) + "%");
      fill();
      el.addEventListener("input", fill);
      new MutationObserver(fill).observe(el, { attributes: true, attributeFilter: ["value"] });
    });
  }

  return { initViews, renderRoutineList, brainBody };
})();
