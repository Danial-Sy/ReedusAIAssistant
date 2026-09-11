
const Store = (() => {
  const KEY = "reedos.v1";
  const DAEMON_PORT = 8765;

  function derivedDaemonUrl() {
    try {
      const l = window.location;
      if (l.protocol === "https:") return `wss://${l.host}/ws`;
      if (l.protocol === "http:" && l.hostname) return `ws://${l.hostname}:${DAEMON_PORT}/ws`;
    } catch { /* file:// or a sandboxed frame */ }
    return `ws://127.0.0.1:${DAEMON_PORT}/ws`;
  }

  function isLoopback(url) {
    return /\/\/(127\.0\.0\.1|localhost|\[::1\])[:/]/.test(url || "");
  }

  const defaults = {
    app: {
      theme: "night",
      fullAnimations: true,
      daemonUrl: derivedDaemonUrl(),
      ttsInChat: true,        // is the answer said out loud at all
      chatInput: "type",      // "type" | "talk"
      chatRoute: "mobo",      // "mobo" = his mic + his voice; "device" = this browser's

    },
    robot: {
      voiceProfile: "classic",
      expressiveness: 65,
      speechRate: 100,
      volume: 80,
      llmEnabled: true,
      robotNoises: true,
      idleWander: false,
      wakeWord: "hey reedus",
      speedLimit: 70,
      accelRamp: 40,
      watchdogMs: 400,
      tipStop: true,
      maxSpeed: 60,
      turnSens: 70,
    },
    routines: [
      {
        id: "demo-patrol",
        name: "Morning patrol",
        steps: [
          { type: "say", text: "Starting patrol." },
          { type: "move", dir: "forward", cm: 50 },
          { type: "turn", dir: "left", degrees: 90 },
          { type: "move", dir: "forward", cm: 50 },
          { type: "sound", name: "chirp" },
          { type: "turn", dir: "left", degrees: 90 },
          { type: "repeat", times: 2 },
        ],
        lastRun: null,
      },
      {
        id: "demo-hello",
        name: "Say hello",
        steps: [
          { type: "expression", mood: "happy" },
          { type: "say", text: "Hello! I am Reedus." },
          { type: "turn", dir: "right", degrees: 20 },
          { type: "turn", dir: "left", degrees: 40 },
          { type: "turn", dir: "right", degrees: 20 },
        ],
        lastRun: null,
      },
    ],
  };

  let state = load();

  function load() {
    try {
      const raw = localStorage.getItem(KEY);
      if (!raw) return structuredClone(defaults);
      const saved = JSON.parse(raw);
      const app = { ...defaults.app, ...saved.app };
      if (isLoopback(app.daemonUrl) && !isLoopback(window.location.href)
          && window.location.protocol.startsWith("http")) {
        app.daemonUrl = derivedDaemonUrl();
      }
      return {
        app,
        robot: { ...defaults.robot, ...saved.robot },
        routines: Array.isArray(saved.routines) ? saved.routines : structuredClone(defaults.routines),
      };
    } catch {
      return structuredClone(defaults);
    }
  }

  function persist() {
    try { localStorage.setItem(KEY, JSON.stringify(state)); } catch { /* storage full/blocked: run in-memory */ }
    if (window.Link) Link.pushSettings(state.robot);
  }

  return {
    get app() { return state.app; },
    get robot() { return state.robot; },
    get routines() { return state.routines; },

    setApp(k, v) { state.app[k] = v; persist(); },
    setRobot(k, v) { state.robot[k] = v; persist(); },

    saveRoutine(routine) {
      const i = state.routines.findIndex(r => r.id === routine.id);
      if (i >= 0) state.routines[i] = routine;
      else state.routines.push(routine);
      persist();
    },
    deleteRoutine(id) {
      state.routines = state.routines.filter(r => r.id !== id);
      persist();
    },
    touchRoutine(id) {
      const r = state.routines.find(r => r.id === id);
      if (r) { r.lastRun = Date.now(); persist(); }
    },
    newId() {
      return "r-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 7);
    },
  };
})();
