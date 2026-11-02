
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
    statusTimer: null,
    lite: null,           // the demo Lite, built in startMock
    liteScene: null,
  };

  /* demo Lite: same shape as LiteHub.snapshot() so the real UI code path runs */
  function mockLite() {
    const now = Date.now() / 1000;
    return {
      connected: true, device: "reedus-lite", fw: "0.1.0 (demo)", peer: "192.168.1.42",
      since: now, state: "idle", volume: 70, brightness: 100, face: null,
      battery: { pct: 84, volts: 3.97, at: now },
      status: { rssi: -57, heap: 183400, psram: 7900000, buffered_ms: 0, underruns: 0,
                mic_drops: 0, uptime: 0, head: 0, at: now },
      doa: null,
    };
  }

  function mockStatus() {
    const L = mock.lite;
    if (L) {
      L.status.uptime = Math.floor((Date.now() - mock.start) / 1000);
      L.status.at = Date.now() / 1000;
      if (L.face && L.face.until < Date.now() / 1000) L.face = null;
    }
    return {
      robot: "mock", phone: "off", chat: "off", brain: "off",
      link: "mock", mode: mock.brainMode, modePending: null, budget: {},
      micOwner: "lite", lite: L ? structuredClone(L) : null,
    };
  }

  function mockFace(name, hold) {
    mock.lite.face = { name, until: Date.now() / 1000 + (hold || 2.5) };
  }

  /* a little conversation every so often: perk up, turn, think, answer with a feeling */
  function mockScene() {
    const L = mock.lite;
    if (!L || L.state !== "idle") return;
    const angle = Math.round((Math.random() * 2 - 1) * 70);
    const at = () => Date.now() / 1000;
    const steps = [
      [0, () => { L.state = "listening"; L.doa = { angle, rel: angle - L.status.head, head: L.status.head, speech: true, at: at() }; }],
      [500, () => { L.status.head = angle; if (L.doa) L.doa.head = angle; }],
      [2600, () => { L.state = "thinking"; }],
      [4200, () => { L.state = "speaking"; mockFace(["happy", "laugh", "excited", "smug", "curious"][Math.floor(Math.random() * 5)], 5); }],
      [8200, () => { L.state = "idle"; }],
    ];
    steps.forEach(([ms, fn]) => setTimeout(() => { if (mock.lite === L) { fn(); emit("status", mockStatus()); } }, ms));
  }

  function emit(type, detail) { bus.dispatchEvent(new CustomEvent(type, { detail })); }

  function authedUrl(url) {
    return (typeof Auth !== "undefined" && Auth.token) ? Auth.withKey(url) : url;
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
    if (!mock.lite) mock.lite = mockLite();
    mock.statusTimer = setInterval(() => emit("status", mockStatus()), 3000);
    mock.liteScene = setInterval(mockScene, 19000);
    setTimeout(mockScene, 3500);
    emit("status", mockStatus());
    emit("log", { level: "warn", msg: "reedosd unreachable, running in mock mode" });
  }

  function stopMock() {
    clearInterval(mock.timer); mock.timer = null;
    clearInterval(mock.statusTimer); mock.statusTimer = null;
    clearInterval(mock.liteScene); mock.liteScene = null;
  }

  function connect(url) {
    wantUrl = url || wantUrl || (typeof Store !== "undefined" ? Store.app.daemonUrl : "ws://127.0.0.1:8765/ws");
    clearTimeout(reconnectTimer);
    try { if (ws) { ws.onclose = null; ws.close(); } } catch { /* already closed */ }

    let sock;
    try { sock = new WebSocket(authedUrl(wantUrl)); }
    catch { startMock(); return; }
    ws = sock;
    // a wrong IP on a phone can sit in SYN for 20s+. show the demo meanwhile and let the
    // socket keep trying; onopen swaps the demo out the moment it lands
    const slow = setTimeout(() => { if (sock.readyState === 0 && ws === sock) startMock(); }, 2500);

    sock.onopen = () => {
      clearTimeout(slow);
      mode = "live";
      stopMock();
      emit("log", { level: "info", msg: "linked to reedosd at " + wantUrl });
      send("hello", { client: "reedos-ui", version: "0.2.0" });
      if (typeof Store !== "undefined") send("settings.set", Store.robot);
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
        case "lite.reply":
        case "connections.reply":
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
        if (mock.lite) { mock.lite.state = "thinking"; emit("status", mockStatus()); }
        setTimeout(() => {
          if (mock.lite) {
            mock.lite.state = "speaking"; mockFace("happy", 4); emit("status", mockStatus());
            setTimeout(() => { mock.lite.state = "idle"; emit("status", mockStatus()); }, 3000);
          }
          resolve({ text: reply, tts: !!o.speak, spoke: false, mode: mock.brainMode });
        }, delay);
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

  /* app connections. no action = the list; otherwise {action, id, ...} (connect/manager.py) */
  function connections(req) {
    return new Promise((resolve) => {
      if (mode === "live") {
        const id = send("connections", req || {});
        pendingChat.set(id, resolve);
        setTimeout(() => {
          if (pendingChat.has(id)) { pendingChat.delete(id); resolve({ error: "the brain took too long to answer" }); }
        }, 50000);
        return;
      }
      setTimeout(() => resolve(mockConnections(req)), req && req.action === "add" ? 900 : 120);
    });
  }

  /* demo: a Spotify that "connects" and a hub that "lists" tools, so the page can be explored */
  const mockCx = {
    spotify: { connected: false, account: null, pending: false, playing: true, track: 0, vol: 55, at: Date.now() },
    hubs: [],
    songs: [["Midnight City", "M83"], ["Blinding Lights", "The Weeknd"], ["Digital Love", "Daft Punk"]],
  };
  function mockConnections(req) {
    const sp = mockCx.spotify;
    const presets = {
      composio: { name: "Composio", url: "https://connect.composio.dev/mcp", header: "x-consumer-api-key", blurb: "1000+ apps (Gmail, Calendar, Notion, GitHub, Slack…). Free tier.", keyHelp: "Consumer API key (starts with ck_) from dashboard.composio.dev" },
      zapier: { name: "Zapier", url: "https://mcp.zapier.com/api/v1/connect", header: "Authorization", blurb: "9000+ apps, pick the actions at mcp.zapier.com. Free plan is ~50 calls/month.", keyHelp: "Token from mcp.zapier.com → your server → Connect" },
      homeassistant: { name: "Home Assistant", url: "http://homeassistant.local:8123/api/mcp", header: "Authorization", blurb: "Lights, plugs, thermostats. Needs the MCP Server integration.", keyHelp: "Long-lived access token from your HA profile page" },
      custom: { name: "Custom MCP server", url: "", header: "Authorization", blurb: "Any Streamable HTTP MCP server.", keyHelp: "Whatever the server wants" },
    };
    const status = () => ({
      items: [{ id: "spotify", kind: "spotify", name: "Spotify", connected: sp.connected, account: sp.account,
                product: "premium", clientId: sp.pending || sp.connected, pending: sp.pending,
                redirect: "http://127.0.0.1:8765/connect/callback/spotify" }, ...mockCx.hubs],
      presets,
    });
    if (!req || !req.action) return status();
    const { action, id } = req;
    if (id === "spotify") {
      if (action === "start") {
        if (!/^[0-9a-f]{32}$/i.test(req.clientId || "")) return { error: "that doesn't look like a Spotify Client ID (32 hex characters)" };
        sp.pending = true; return { url: "about:blank#demo-spotify-login" };
      }
      if (action === "finish") { sp.pending = false; sp.connected = true; sp.account = "Danial"; return { ok: true, account: "Danial", product: "premium" }; }
      if (action === "disconnect") { sp.connected = false; sp.account = null; return { ok: true }; }
      if (action === "now") {
        const [track, artist] = mockCx.songs[sp.track % mockCx.songs.length];
        const dur = 214000, prog = sp.playing ? (Date.now() - sp.at) % dur : 61000;
        return { now: { playing: sp.playing, track, artist, album: "", art: null, device: "Danial's iPhone", volume: sp.vol, progress: prog, duration: dur } };
      }
      if (action === "control") {
        if (req.do === "pause") sp.playing = false;
        if (req.do === "resume") sp.playing = true;
        if (req.do === "next") { sp.track++; sp.at = Date.now(); }
        if (req.do === "previous") { sp.track = Math.max(0, sp.track - 1); sp.at = Date.now(); }
        if (req.do === "volume") sp.vol = +req.value;
        return { ok: true };
      }
      if (action === "say") return { reply: /pause/i.test(req.text) ? "Paused." : /skip|next/i.test(req.text) ? "Skipped." : /^play /i.test(req.text) ? `Playing ${req.text.slice(5)}.` : "That isn't a music command I handle directly; Reedus will route it." };
    }
    if (action === "add") {
      const p = presets[req.preset] || presets.custom;
      if (!req.key) return { error: "the server refused the key (HTTP 401)" };
      const hid = (req.name || p.name).toLowerCase().replace(/[^a-z0-9]+/g, "-");
      mockCx.hubs.push({ id: hid, kind: "mcp", preset: req.preset, name: req.name || p.name,
                         host: (req.url || p.url).replace(/^https?:\/\//, "").split("/")[0],
                         enabled: true, connected: true, tools: req.preset === "composio" ? 7 : 23, error: "", server: p.name });
      return { ok: true, id: hid, tools: req.preset === "composio" ? 7 : 23 };
    }
    if (action === "remove") { mockCx.hubs = mockCx.hubs.filter(h => h.id !== id); return { ok: true }; }
    if (action === "test") return { ok: true, tools: 7, sample: ["COMPOSIO_SEARCH_TOOLS", "COMPOSIO_MULTI_EXECUTE_TOOL"] };
    return { error: "unknown action" };
  }

  /* Reedus Lite knobs: {volume, brightness, look, expr, hold}. resolves with the hub's reply */
  function liteSet(req) {
    return new Promise((resolve) => {
      if (mode === "live") {
        const id = send("lite.set", req);
        pendingChat.set(id, resolve);
        setTimeout(() => {
          if (pendingChat.has(id)) { pendingChat.delete(id); resolve({ error: "no answer from the brain" }); }
        }, 8000);
        return;
      }
      const L = mock.lite;
      if (!L) { resolve({ error: "no Lite" }); return; }
      const applied = {};
      if (req.volume != null) applied.volume = L.volume = Math.max(0, Math.min(100, Math.round(req.volume)));
      if (req.brightness != null) applied.brightness = L.brightness = Math.max(0, Math.min(100, Math.round(req.brightness)));
      if (req.look != null) {
        applied.look = Math.max(-90, Math.min(90, +req.look));
        setTimeout(() => { L.status.head = applied.look; emit("status", mockStatus()); }, 450);
      }
      if (req.expr) { applied.expr = req.expr; mockFace(req.expr, req.hold); }
      setTimeout(() => {
        resolve({ applied, lite: structuredClone(L) });
        emit("status", mockStatus());
      }, 60);
    });
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
    pushSettings, requestSelfTest, setMode, listen, liteSet, connections,
    demo: () => { if (mode !== "live") startMock(); },
    get mode() { return mode; },
    on: (t, fn) => bus.addEventListener(t, (e) => fn(e.detail)),
  };
})();
