/* ReedOS views: behavior for each screen. app.js calls initViews() once. */

const Views = (() => {
  const $ = (s) => document.querySelector(s);
  const REEDUS_AVATAR = `<svg viewBox="14 14 132 132" width="34" height="34"><circle cx="80" cy="80" r="58" fill="#F3EEE3" stroke="#1F4D3A" stroke-width="4"/><path d="M80,128 C60,112 50,88 55,58 C61,88 70,108 81,123 Z" fill="#1F4D3A"/><path d="M80,128 C96,115 104,96 102,72 C100,98 92,114 79,125 Z" fill="#1F4D3A"/><path d="M78.5,128 C78,104 78.2,84 79,62 L81,62 C81.8,84 82,104 81.5,128 Z" fill="#1F4D3A"/><rect x="73.5" y="34" width="13" height="32" rx="6.5" fill="#6B4226"/></svg>`;

  /* --- remote --- */
  let joy = { active: false, x: 0, y: 0 };

  function initRemote() {
    const pad = $("#joypad"), knob = $("#joyknob");
    const maxR = () => pad.clientWidth / 2 - knob.clientWidth / 2 - 6;

    function setKnob(dx, dy) {
      knob.style.transform = `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px))`;
    }

    function apply(dx, dy) {
      const r = maxR();
      const fx = dx / r, fy = -dy / r;                      // fy: up = +forward
      const maxS = Store.robot.maxSpeed / 100;
      const sens = Store.robot.turnSens / 100;
      Link.twist(fy * maxS, fx * sens * maxS);
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
      onMove(e);
    });
    pad.addEventListener("pointermove", onMove);
    const release = () => {
      if (!joy.active) return;
      joy.active = false;
      pad.classList.remove("dragging");
      setKnob(0, 0);
      Link.drive(0, 0);
    };
    pad.addEventListener("pointerup", release);
    pad.addEventListener("pointercancel", release);

    const keys = new Set();
    const KEYMAP = {
      w: "fwd", arrowup: "fwd",
      s: "back", arrowdown: "back",
      a: "left", arrowleft: "left",
      d: "right", arrowright: "right",
    };

    function keyDrive() {
      if (App.currentView() !== "remote") return;
      const maxS = Store.robot.maxSpeed / 100;
      const sens = Store.robot.turnSens / 100;
      const fy = (keys.has("fwd") ? 1 : 0) + (keys.has("back") ? -1 : 0);
      const fx = (keys.has("right") ? 1 : 0) + (keys.has("left") ? -1 : 0);
      Link.twist(fy * maxS, fx * sens * maxS);
      showKeyHint(fy, fx);
    }

    function showKeyHint(fy, fx) {
      const knob = document.querySelector("#joyknob");
      const pad = document.querySelector("#joypad");
      if (!knob || !pad) return;
      const r = pad.clientWidth / 2 - knob.clientWidth / 2 - 6;
      knob.style.transform =
        `translate(calc(-50% + ${fx * r * 0.7}px), calc(-50% + ${-fy * r * 0.7}px))`;
    }

    window.addEventListener("keydown", (e) => {
      if (App.currentView() !== "remote") return;
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
      if (keys.size) { keys.clear(); Link.twist(0, 0); showKeyHint(0, 0); }
    });

    /* sliders */
    bindRange("#maxspeed", "#maxspeed-val", v => v + "%", (v) => Store.setRobot("maxSpeed", v), Store.robot.maxSpeed);
    bindRange("#turnsens", "#turnsens-val", v => v + "%", (v) => Store.setRobot("turnSens", v), Store.robot.turnSens);

    $("#estop").addEventListener("click", () => {
      Link.stop();
      App.toast("Motors stopped");
    });

    /* telemetry into meters */
    Link.on("telemetry", (t) => {
      const RATED = 76;
      $("#rpm-l").textContent = Math.round(Math.abs(t.rpm.l));
      $("#rpm-r").textContent = Math.round(Math.abs(t.rpm.r));
      $("#rpm-l-bar").style.width = Math.min(100, Math.abs(t.rpm.l) / RATED * 100) + "%";
      $("#rpm-r-bar").style.width = Math.min(100, Math.abs(t.rpm.r) / RATED * 100) + "%";
      $("#lat").textContent = t.latencyMs.toFixed(1);
      $("#lat-bar").style.width = Math.min(100, t.latencyMs / 50 * 100) + "%";
      const hd = $("#head-deg");
      if (hd && t.headingDeg !== undefined) {
        hd.textContent = (t.headingDeg > 0 ? "+" : "") + t.headingDeg.toFixed(1);
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
      list.innerHTML = `<p class="hint">No routines yet, tap "+ New routine" and teach Reedus a trick.</p>`;
    }
    Store.routines.forEach((r, i) => {
      const card = document.createElement("div");
      card.className = "routine-card" + (r.id === runningId ? " running" : "");
      card.style.animationDelay = (i * 60) + "ms";
      const last = r.lastRun ? new Date(r.lastRun).toLocaleString() : "never";
      card.innerHTML = `
        <h4></h4>
        <span class="r-meta">${r.steps.length} steps · last run: ${last}</span>
        <div class="r-actions">
          <button class="cta gold r-run">▶ Run</button>
          <button class="ghost-btn r-edit">Edit</button>
          <button class="ghost-btn r-del">Delete</button>
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
      card.querySelector(".r-del").addEventListener("click", () => {
        Store.deleteRoutine(r.id);
        renderRoutineList();
        App.toast("Routine deleted");
      });
      list.append(card);
    });
    $("#tile-sub-routines").textContent = Store.routines.length
      ? `${Store.routines.length} saved · tap to run`
      : "Build & run sequences";
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
      li.style.animationDelay = (i * 30) + "ms";
      const ico = document.createElement("span");
      ico.className = "st-icon"; ico.textContent = def.icon;
      const body = document.createElement("div");
      body.className = "st-body";
      const label = document.createElement("b");
      label.textContent = def.label;
      body.append(label, ...def.render(s));
      const tools = document.createElement("div");
      tools.className = "st-tools";
      const up = mkBtn("▲", () => moveStep(i, -1));
      const dn = mkBtn("▼", () => moveStep(i, +1));
      const del = mkBtn("✕", () => { editing.steps.splice(i, 1); renderSteps(); });
      tools.append(up, dn, del);
      li.append(ico, body, tools);
      ol.append(li);
    });
  }

  function mkBtn(txt, fn) {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = txt;
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
    /* block palette */
    const pal = $("#block-palette");
    for (const [key, def] of Object.entries(Blocks)) {
      const b = document.createElement("button");
      b.className = "block-btn";
      b.innerHTML = `<span class="b-ico">${def.icon}</span> ${def.label} <small>${def.hint}</small>`;
      b.addEventListener("click", () => {
        if (!editing) return;
        editing.steps.push(def.make());
        renderSteps();
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
        streamUrl = Auth.withKey(s.cameraUrl);
        img.src = streamUrl;
        img.hidden = false; off.hidden = true;
        $("#cam-note").textContent = "live";
      } else if (!s.cameraUrl && Link.mode === "mock") {
        img.hidden = true; off.hidden = false;
        $("#cam-note").textContent = "offline";
      }
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
      else stage.requestFullscreen && stage.requestFullscreen();
    });
  }

  /* --- chat --- */
  function initChat() {
    const thread = $("#chat-thread"), form = $("#chat-form"), input = $("#chat-input");
    const micBtn = $("#chat-mic"), hint = $("#chat-hint");
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition || null;
    let status = {};        // last status frame, for the hints
    let rec = null;         // in-flight browser recognition

    function seg(sel, value, onPick) {
      const el = $(sel);
      if (!el) return;
      el.dataset.val = value;
      el.querySelectorAll(".seg-btn").forEach(b => {
        b.classList.toggle("on", b.dataset.val === value);
        b.addEventListener("click", () => {
          el.dataset.val = b.dataset.val;
          el.querySelectorAll(".seg-btn").forEach(x => x.classList.toggle("on", x === b));
          onPick(b.dataset.val);
        });
      });
    }

    seg("#chat-input-seg", Store.app.chatInput, v => { Store.setApp("chatInput", v); applyChoice(); });
    seg("#chat-route-seg", Store.app.chatRoute, v => { Store.setApp("chatRoute", v); applyChoice(); });
    $("#chat-tts").checked = Store.app.ttsInChat;
    $("#chat-tts").addEventListener("change", (e) => {
      Store.setApp("ttsInChat", e.target.checked); applyChoice();
    });

    function applyChoice() {
      const talk = Store.app.chatInput === "talk";
      const mobo = Store.app.chatRoute === "mobo";
      micBtn.hidden = !talk;
      input.placeholder = talk ? (mobo ? "or type, his mic is on the box"
                                       : "or type, tap the mic to speak")
                               : "Say something to Reedus";
      let msg = "";
      if (talk && !mobo && !SR) {
        msg = "This browser can't record, point the audio at Reedus, or type.";
      } else if (talk && mobo && status.micOwner === "voiceloop") {
        msg = "He already has his mic open, you can just talk to him out loud.";
      } else if (mobo && status.brain === "on" && status.voice === false) {
        msg = "No voice loaded on the box, replies get read out here instead.";
      }
      hint.textContent = msg;
    }

    function bubble(who, text, speaking) {
      const m = document.createElement("div");
      m.className = "msg " + who;
      const av = who === "reedus" ? `<span class="avatar">${REEDUS_AVATAR}</span>` : "";
      m.innerHTML = `${av}<div><div class="bubble"></div>${speaking ? '<div class="speaking">🔊 speaking…</div>' : ""}</div>`;
      m.querySelector(".bubble").textContent = text;
      thread.append(m);
      thread.scrollTop = thread.scrollHeight;
      return m;
    }

    function typing() {
      const m = document.createElement("div");
      m.className = "msg reedus typing";
      m.innerHTML = `<span class="avatar">${REEDUS_AVATAR}</span><div class="bubble"><span class="tdot"></span><span class="tdot"></span><span class="tdot"></span></div>`;
      thread.append(m);
      thread.scrollTop = thread.scrollHeight;
      return m;
    }

    /* browser TTS only when the daemon didn't already speak it (reply.tts). that flag is
       decided server-side so he can never talk over himself */
    function speak(text) {
      if (!Store.app.ttsInChat) return false;
      try {
        const u = new SpeechSynthesisUtterance(text);
        u.rate = Store.robot.speechRate / 100;
        u.volume = Store.robot.volume / 100;
        u.pitch = Store.robot.voiceProfile === "chirpy" ? 1.5 :
                  Store.robot.voiceProfile === "lowfi" ? 0.7 :
                  Store.robot.voiceProfile === "whisper" ? 0.9 : 1.0;
        speechSynthesis.speak(u);
        return true;
      } catch { return false; }
    }

    async function ask(text) {
      bubble("me", text);
      const t = typing();
      const reply = await Link.chat(text, {
        speak: Store.app.ttsInChat,
        route: Store.app.chatRoute,
      });
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
        hint.textContent = "listening on Reedus's mic…";
        const out = await Link.listen(Store.app.ttsInChat);
        micBtn.classList.remove("rec");
        applyChoice();
        if (out.error) { App.toast(out.error); bubble("reedus", out.error); return; }
        if (out.heard) bubble("me", out.heard);
        if (out.text) {
          const here = out.tts ? speak(out.text) : false;
          bubble("reedus", out.text, here || out.spoke);
        }
        return;
      }
      if (!SR) { App.toast("No speech recognition in this browser, use Reedus's mic."); return; }
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
      status = s;
      const live = s.chat === "on";
      $("#chat-notice").hidden = live;
      $("#chat-notice").textContent = s.brain === "on"
        ? "Reedus's brain is connected."
        : "Brain not running, canned test replies. Start the voice loop on the box.";
      $("#tile-sub-chat").textContent = live
        ? (s.mode ? `Model online · ${s.mode}` : "Model online")
        : "Talk with Reedus";
      applyChoice();
    });

    applyChoice();

    /* greeting */
    setTimeout(() => bubble("reedus", "Hey Danial. Systems up, mock brain online until you plug me in."), 1400);
  }

  /* --- robot settings --- */
  function initRobotSettings() {
    const r = Store.robot;
    $("#rs-voice").value = r.voiceProfile;
    $("#rs-voice").addEventListener("change", (e) => { Store.setRobot("voiceProfile", e.target.value); synced(); });
    bindRange("#rs-expr", "#rs-expr-val", v => v + "%", v => { Store.setRobot("expressiveness", v); synced(); }, r.expressiveness);
    bindRange("#rs-rate", "#rs-rate-val", v => v + "%", v => { Store.setRobot("speechRate", v); synced(); }, r.speechRate);
    bindRange("#rs-vol", "#rs-vol-val", v => v + "%", v => { Store.setRobot("volume", v); synced(); }, r.volume);
    bindRange("#rs-speed", "#rs-speed-val", v => v + "%", v => { Store.setRobot("speedLimit", v); synced(); }, r.speedLimit);
    bindRange("#rs-accel", "#rs-accel-val", v => v + "%", v => { Store.setRobot("accelRamp", v); synced(); }, r.accelRamp);
    bindRange("#rs-wd", "#rs-wd-val", v => v + " ms", v => { Store.setRobot("watchdogMs", v); synced(); }, r.watchdogMs);

    bindSwitch("#rs-llm", "llmEnabled");
    bindSwitch("#rs-noises", "robotNoises");
    bindSwitch("#rs-idle", "idleWander");
    bindSwitch("#rs-tipstop", "tipStop");

    $("#rs-wake").value = r.wakeWord;
    $("#rs-wake").addEventListener("change", (e) => { Store.setRobot("wakeWord", e.target.value); synced(); });

    function bindSwitch(sel, key) {
      const el = $(sel);
      el.checked = Store.robot[key];
      el.addEventListener("change", () => { Store.setRobot(key, el.checked); synced(); });
    }
    let t;
    function synced() {
      const el = $("#rs-sync");
      el.textContent = Link.mode === "live" ? "synced to Reedus ✓" : "saved (will sync when linked)";
      clearTimeout(t);
      t = setTimeout(() => el.textContent = "", 2200);
    }
  }

  /* --- diagnostics --- */
  const chart = { data: [], max: 120 };

  function initDiagnostics() {
    const cvs = $("#rpm-chart");
    const ctx = cvs.getContext("2d");

    Link.on("telemetry", (t) => {
      $("#d-lat").textContent = t.latencyMs.toFixed(1);
      $("#d-pps").textContent = Math.round(t.pps);
      $("#d-cl").textContent = t.counts.l.toLocaleString();
      $("#d-cr").textContent = t.counts.r.toLocaleString();
      $("#d-up").textContent = fmtUp(t.uptimeS);
      $("#d-mode").textContent = Link.mode;
      $("#tile-sub-diag").textContent = `${t.latencyMs.toFixed(0)} ms · ${Link.mode}`;

      chart.data.push({ l: t.rpm.l, r: t.rpm.r });
      if (chart.data.length > chart.max) chart.data.shift();
      if (App.currentView() === "diagnostics") drawChart(ctx, cvs);
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
    const w = cvs.width = cvs.clientWidth;
    const h = cvs.height;
    ctx.clearRect(0, 0, w, h);
    const css = getComputedStyle(document.body);
    ctx.strokeStyle = css.getPropertyValue("--line");
    ctx.beginPath(); ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();
    const plot = (key, color) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
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

  /* --- app settings --- */
  function initAppSettings() {
    $("#as-theme").value = Store.app.theme;
    $("#as-theme").addEventListener("change", (e) => {
      Store.setApp("theme", e.target.value);
      document.body.dataset.theme = e.target.value;
    });
    $("#as-anim").checked = Store.app.fullAnimations;
    $("#as-anim").addEventListener("change", (e) => {
      Store.setApp("fullAnimations", e.target.checked);
      document.body.classList.toggle("reduced-motion", !e.target.checked);
    });
    $("#as-daemon").value = Store.app.daemonUrl;
    $("#as-daemon").addEventListener("change", (e) => Store.setApp("daemonUrl", e.target.value.trim()));
    $("#as-reconnect").addEventListener("click", () => {
      Link.connect(Store.app.daemonUrl);
      App.toast("Reconnecting…");
    });
    Link.on("status", (s) => {
      $("#as-conn-state").textContent = s.link === "live" ? "linked to reedosd" : "mock mode, daemon not found";
      if (s.hostIp) $("#as-ip").textContent = s.hostIp;
      lockState(s.locked);
    });

    function lockState(required) {
      const el = $("#as-lock-state");
      if (!el) return;
      el.textContent = required
        ? "Passcode required to connect."
        : "No passcode, anyone on this network can drive Reedus.";
      el.style.color = required ? "var(--ink-faint)" : "var(--gold)";
    }
    Auth.status().then((a) => { if (a.reachable) lockState(a.required); });

    $("#as-passcode-save").addEventListener("click", async () => {
      const code = $("#as-passcode").value.trim();
      if (code && code.length < 4) { App.toast("Use at least 4 characters"); return; }
      const out = await Auth.setPasscode(code);
      if (!out.ok) { App.toast(out.error); return; }
      $("#as-passcode").value = "";
      lockState(out.required);
      App.toast(out.required ? "Passcode set, other devices must sign in again"
                             : "Passcode removed, ReedOS is open");
    });
  }

  /* --- power --- */
  function initPower() {
    $("#pw-sleep").addEventListener("click", () => { Link.stop(); App.toast("Reedus sleeping, motors off"); });
    $("#pw-reboot").addEventListener("click", () => App.toast(Link.mode === "live" ? "Reboot sent to robot" : "Robot not linked"));
    $("#pw-daemon").addEventListener("click", () => App.toast(Link.mode === "live" ? "reedosd restarting" : "Daemon not linked"));
    $("#pw-quit").addEventListener("click", () => {
      if (window.__TAURI__ && window.__TAURI__.window) {
        window.__TAURI__.window.getCurrentWindow().close();
      } else {
        App.toast("Running in browser, close the tab to quit");
      }
    });
  }

  /* --- shared helpers --- */
  function bindRange(sel, valSel, fmt, onChange, initial) {
    const el = document.querySelector(sel), out = document.querySelector(valSel);
    if (initial !== undefined) { el.value = initial; out.textContent = fmt(initial); }
    el.addEventListener("input", () => {
      const v = +el.value;
      out.textContent = fmt(v);
      onChange(v);
    });
  }

  function initViews() {
    initRemote();
    initRoutines();
    initCamera();
    initChat();
    initRobotSettings();
    initDiagnostics();
    initAppSettings();
    initPower();
  }

  return { initViews, renderRoutineList };
})();
