
const Link = (() => {
  const bus = new EventTarget();
  let ws = null;
  let mode = "mock";            // "live" | "mock"
  let wantUrl = null;
  let reconnectTimer = null;
  let pendingChat = new Map();  // id -> resolve
  let seq = 0;

  const mock = {
    duty: { l: 0, r: 0 },
    rpm: { l: 0, r: 0 },
    counts: { l: 0, r: 0 },
    timer: null,
    start: Date.now(),
    replies: [
      "Hi! Reedus here. My real brain isn't plugged in yet, but I'm in a great mood anyway.",
      "Beep. That tickled my encoders.",
      "When my trained model is wired up, I'll answer this properly. For now: chirp!",
      "My wheels are 76 RPM at full tilt. I have never once used all 76. Cowardice? Wisdom.",
      "I dream of cattails. Tall ones.",
      "Watchdog says I have to stop moving if nobody talks to me for 400 ms. Clingy, honestly.",
    ],
    replyIx: 0,
    brainMode: "local",
    heard: [
      "what's the battery looking like",
      "drive forward a bit",
      "tell me a fact about cattails",
    ],
    heardIx: 0,
  };

  function emit(type, detail) { bus.dispatchEvent(new CustomEvent(type, { detail })); }

  function authedUrl(url) {
    return (window.Auth && Auth.token) ? Auth.withKey(url) : url;
  }

  function mockTick() {
    const RATED = 76;
    ["l", "r"].forEach(w => {
      const target = mock.duty[w] * RATED;
      mock.rpm[w] += (target - mock.rpm[w]) * 0.25;
      if (Math.abs(mock.rpm[w]) > 0.5) mock.rpm[w] += (Math.random() - 0.5) * 1.2;
      mock.counts[w] += Math.round(mock.rpm[w] / 60 * 4400 * 0.1);
    });
    emit("telemetry", {
      rpm: { l: mock.rpm.l, r: mock.rpm.r },
      counts: { l: mock.counts.l, r: mock.counts.r },
      duty: { ...mock.duty },
      latencyMs: 3 + Math.random() * 4,
      pps: 10,
      uptimeS: Math.floor((Date.now() - mock.start) / 1000),
      battery: 87,
    });
  }

  function startMock() {
    if (mock.timer) return;
    mock.timer = setInterval(mockTick, 100);
    emit("status", {
      robot: "mock", phone: "off", chat: "off", brain: "off",
      link: "mock", mode: mock.brainMode, modePending: null, budget: {},
    });
    emit("log", { level: "warn", msg: "reedosd unreachable, running in mock mode" });
  }

  function stopMock() {
    clearInterval(mock.timer); mock.timer = null;
  }

  function connect(url) {
    wantUrl = url || wantUrl || (window.Store ? Store.app.daemonUrl : "ws://127.0.0.1:8765/ws");
    clearTimeout(reconnectTimer);
    try { if (ws) { ws.onclose = null; ws.close(); } } catch { /* already closed */ }

    let sock;
    try { sock = new WebSocket(authedUrl(wantUrl)); }
    catch { startMock(); return; }
    ws = sock;

    sock.onopen = () => {
      mode = "live";
      stopMock();
      emit("log", { level: "info", msg: "linked to reedosd at " + wantUrl });
      send("hello", { client: "reedos-ui", version: "0.1.0" });
      if (window.Store) send("settings.set", Store.robot);
    };

    sock.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      switch (m.type) {
        case "telemetry": emit("telemetry", m.data); break;
        case "status": emit("status", { ...m.data, link: "live" }); break;
        case "mode": emit("mode", m.data); break;
        case "chat.heard": emit("chat-heard", m.data); break;
        case "log": emit("log", m.data); break;
        case "chat.reply": {
          const r = pendingChat.get(m.id);
          if (r) { r(m.data); pendingChat.delete(m.id); }
          break;
        }
        case "routine.progress": emit("routine-progress", m.data); break;
        case "settings": emit("settings", m.data); break;
        case "locked": emit("locked", m.data); break;
      }
    };

    sock.onclose = (ev) => {
      if (ev && ev.code === 4401) {
        mode = "mock";
        startMock();
        emit("log", { level: "warn", msg: "reedosd wants the passcode" });
        emit("locked", { why: "passcode required" });
        return;
      }
      if (mode === "live") emit("log", { level: "err", msg: "lost link to reedosd, falling back to mock" });
      mode = "mock";
      startMock();
      reconnectTimer = setTimeout(() => connect(), 5000);
    };
    sock.onerror = () => { try { sock.close(); } catch { /* noop */ } };
  }

  function send(type, data) {
    if (mode === "live" && ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ type, data, id: ++seq }));
      return seq;
    }
    return null;
  }

  function drive(l, r) {
    l = Math.max(-1, Math.min(1, l));
    r = Math.max(-1, Math.min(1, r));
    if (mode === "live") send("drive", { l, r });
    else { mock.duty.l = l; mock.duty.r = r; }
  }

  /* the Pico's watchdog stops the motors when commands stop, and a held key sends no
     events, so the last twist is resent every 120ms until released */
  let heldTwist = { fwd: 0, turn: 0 };
  let heartbeat = null;

  function twist(fwd, turn) {
    fwd = Math.max(-1, Math.min(1, fwd));
    turn = Math.max(-1, Math.min(1, turn));
    heldTwist.fwd = fwd;
    heldTwist.turn = turn;
    pushTwist();

    const moving = fwd !== 0 || turn !== 0;
    if (moving && !heartbeat) {
      heartbeat = setInterval(pushTwist, 120);
    } else if (!moving && heartbeat) {
      clearInterval(heartbeat);
      heartbeat = null;
    }
  }

  function pushTwist() {
    if (mode === "live") send("twist", heldTwist);
    else {
      mock.duty.l = heldTwist.fwd + heldTwist.turn;
      mock.duty.r = heldTwist.fwd - heldTwist.turn;
    }
  }

  function moveMm(mm) {
    if (mode === "live") send("move", { mm });
    else emit("log", { level: "info", msg: `move ${mm} mm (mock)` });
  }

  function turnDeg(deg) {
    if (mode === "live") send("turn", { deg });
    else emit("log", { level: "info", msg: `turn ${deg}° (mock)` });
  }

  function stop() {
    heldTwist.fwd = 0; heldTwist.turn = 0;
    if (heartbeat) { clearInterval(heartbeat); heartbeat = null; }
    if (mode === "live") send("stop", {});
    mock.duty.l = 0; mock.duty.r = 0;
    emit("log", { level: "warn", msg: "STOP issued" });
  }

  function pushSettings(robotSettings) {
    send("settings.set", robotSettings);
  }

  function chat(text, opts) {
    const o = opts || {};
    return new Promise((resolve) => {
      if (mode === "live") {
        const id = send("chat", { text, speak: !!o.speak, route: o.route || "mobo" });
        pendingChat.set(id, resolve);
        setTimeout(() => {
          if (pendingChat.has(id)) {
            pendingChat.delete(id);
            resolve({ text: "…Reedus is thinking very hard (model endpoint timed out).", tts: false });
          }
        }, 15000);
      } else {
        const delay = 700 + Math.random() * 900;
        const reply = mock.replies[mock.replyIx++ % mock.replies.length];
        setTimeout(() => resolve({ text: reply, tts: !!o.speak, spoke: false,
                                  mode: mock.brainMode }), delay);
      }
    });
  }

  function setMode(target) {
    if (mode === "live") { send("mode.set", { mode: target }); return; }
    mock.brainMode = target === "toggle"
      ? (mock.brainMode === "local" ? "cloud" : "local") : target;
    emit("mode", { accepted: true, mode: mock.brainMode, pending: null });
    emit("log", { level: "info", msg: `mode → ${mock.brainMode} (mock)` });
  }

  function listen(speak) {
    return new Promise((resolve) => {
      if (mode === "live") {
        const id = send("listen", { speak: speak !== false });
        pendingChat.set(id, resolve);
        setTimeout(() => {
          if (pendingChat.has(id)) {
            pendingChat.delete(id);
            resolve({ text: "", error: "no answer from the brain" });
          }
        }, 60000);
        return;
      }
      const heard = mock.heard[mock.heardIx++ % mock.heard.length];
      setTimeout(() => {
        emit("chat-heard", { text: heard });
        const reply = mock.replies[mock.replyIx++ % mock.replies.length];
        setTimeout(() => resolve({ heard, text: reply, tts: !!speak, spoke: false }), 700);
      }, 900);
    });
  }

  function runRoutine(routine) {
    if (mode === "live") {
      send("routine.run", routine);
      return;
    }
    // mock: walk the steps on a timer and emit progress
    let flat = [];
    routine.steps.forEach((s, i) => flat.push({ ...s, ix: i }));
    const repeat = flat.find(s => s.type === "repeat");
    let passes = repeat ? Math.max(1, repeat.times) : 1;
    let queue = [];
    for (let p = 0; p < passes; p++) queue = queue.concat(flat.filter(s => s.type !== "repeat"));
    let k = 0;
    emit("log", { level: "info", msg: `routine "${routine.name}" started (mock)` });
    const step = () => {
      if (k > 0) {
        const prev = queue[k - 1];
        if (prev.type === "move" || prev.type === "turn") { mock.duty.l = 0; mock.duty.r = 0; }
      }
      if (k >= queue.length) {
        emit("routine-progress", { id: routine.id, done: true });
        emit("log", { level: "info", msg: `routine "${routine.name}" finished` });
        return;
      }
      const s = queue[k];
      emit("routine-progress", { id: routine.id, stepIx: s.ix, done: false });
      let dur = 800;
      if (s.type === "move") {
        const d = 0.55 * (s.dir === "back" ? -1 : 1);
        mock.duty.l = d; mock.duty.r = d;
        // mock timing from the real geometry: 204mm per wheel rev at ~35rpm
        const cm = s.cm !== undefined ? s.cm : 50;
        dur = Math.max(300, (cm * 10) / (35 / 60 * 204) * 1000);
      } else if (s.type === "turn") {
        const spin = s.dir === "left" ? -0.35 : 0.35;
        mock.duty.l = spin; mock.duty.r = -spin;
        dur = Math.max(300, (s.degrees || 90) * 8);
      } else if (s.type === "wait") {
        dur = (s.seconds || 1) * 1000;
      } else if (s.type === "say") {
        emit("log", { level: "info", msg: `Reedus says: "${s.text}"` });
        dur = 900;
      } else if (s.type === "sound") {
        emit("log", { level: "info", msg: `*${s.name || "chirp"}*` });
        dur = 500;
      } else if (s.type === "expression") {
        emit("log", { level: "info", msg: `expression -> ${s.mood}` });
        dur = 400;
      } else if (s.type === "speed") {
        dur = 200;
      }
      k++;
      setTimeout(step, dur);
    };
    step();
  }

  function requestSelfTest() {
    if (mode === "live") { send("selftest", {}); return; }
    emit("log", { level: "info", msg: "self-test (mock): LEFT fwd/rev, RIGHT fwd/rev, both fwd" });
    const seqs = [
      ["l", 0.4], ["l", -0.4], ["l", 0],
      ["r", 0.4], ["r", -0.4], ["r", 0],
      ["both", 0.4], ["both", 0],
    ];
    seqs.forEach(([w, d], i) => {
      setTimeout(() => {
        if (w === "both") { mock.duty.l = d; mock.duty.r = d; }
        else mock.duty[w] = d;
        if (i === seqs.length - 1) emit("log", { level: "info", msg: "self-test complete, wheels matched (mock)" });
      }, i * 900);
    });
  }

  return {
    connect, drive, twist, moveMm, turnDeg, stop, chat, runRoutine,
    pushSettings, requestSelfTest, setMode, listen,
    get mode() { return mode; },
    on: (t, fn) => bus.addEventListener(t, (e) => fn(e.detail)),
  };
})();
