/* the Reedus Lite tab: live face, expressions, head, speaker/brightness, status.
   everything comes from status.lite (LiteHub.snapshot() via reedosd) and goes out as lite.set */

const Lite = (() => {
  const $ = (s) => document.querySelector(s);
  const EXPRS = [
    ["happy", "Happy"], ["laugh", "Laugh"], ["love", "Love"], ["excited", "Excited"],
    ["shy", "Shy"], ["smug", "Smug"], ["curious", "Curious"], ["surprised", "Wow"],
    ["confused", "Huh?"], ["sad", "Sad"], ["angry", "Grr"], ["sleepy", "Sleepy"],
    ["wink", "Wink"], ["content", "Content"], ["yawn", "Yawn"], ["dizzy", "Dizzy"], ["bye", "Bye"],
  ];

  let lite = null;          // last status.lite (null: the brain isn't running Lite)
  let status = {};
  let localFace = null;     // {name, until} we just sent, so the preview reacts before the next status
  let lookTarget = null;    // {angle, until, sentAt}: held until the device reports a newer head
  const live = Face.createLive();
  let raf = 0;

  /* what Lite can do right now */
  function mode() {
    if (!status.link) return "pending";              // no status yet, don't cry wolf
    if (status.link !== "live" && status.link !== "mock") return "nodaemon";
    if (!lite) return status.brain === "on" ? "robot" : "brainoff";
    return lite.connected ? "live" : "waiting";
  }

  function render() {
    const m = mode();
    const help = $("#lite-help"), title = $("#lite-help-title"), body = $("#lite-help-body");
    const copy = {
      nodaemon: ["Can't reach the run box", "ReedOS couldn't find reedosd. Check the address in Settings → Connection."],
      brainoff: ["The brain is asleep", "Start Lite's brain on the run box: <code>bash reedus_companion/deploy/run_lite.sh</code>"],
      robot: ["The brain is in the robot", "Reedus and Lite share one brain, one body at a time. Stop <code>reedus-voice</code> and start <code>run_lite.sh</code> to move it into Lite."],
      waiting: ["Waiting for Lite", "The brain is ready and listening on :8767. Power Lite on, it finds the run box by itself."],
    };
    help.hidden = !copy[m];
    if (copy[m]) { title.textContent = copy[m][0]; body.innerHTML = copy[m][1]; }

    const on = m === "live";
    document.querySelectorAll(".lite-card").forEach(c => c.classList.toggle("is-off", !on));
    // volume/brightness are kept by the hub and sent on connect, so they work while it's away
    $("#lite-knobs-card").classList.toggle("is-off", !lite);

    const lines = {
      live: { idle: "Hanging out on the desk.", listening: "Listening…", thinking: "Thinking it over…",
              speaking: "Talking.", sleep: "Asleep." }[lite && lite.state] || "Awake.",
      waiting: lite && lite.last_seen ? `Last seen ${ago(lite.last_seen)}.` : "Not connected yet.",
      robot: "The brain is with Reedus right now.",
      brainoff: "Brain offline.",
      nodaemon: "Run box not found.",
      pending: "Looking for Lite…",
    };
    $("#lite-line").textContent = lines[m];

    chip("#chip-lite-link", on ? "on" : lite ? "wait" : "off", on ? "Connected" : lite ? "Waiting" : "Offline");
    const b = lite && lite.battery;
    chip("#chip-lite-batt", b ? (b.pct <= 15 ? "warn" : "on") : "off", b ? `${Math.round(b.pct)}%` : "Battery");
    const w = lite && lite.status;
    chip("#chip-lite-wifi", w && on ? (w.rssi < -75 ? "warn" : "on") : "off", w && on ? wifiWord(w.rssi) : "Wi-Fi");

    if (!document.activeElement || document.activeElement.id !== "lite-vol") setRange("#lite-vol", "#lite-vol-val", lite ? lite.volume : 70);
    if (!document.activeElement || document.activeElement.id !== "lite-bri") setRange("#lite-bri", "#lite-bri-val", lite ? lite.brightness : 100);

    kv("#ls-state", on ? cap(lite.state || "idle") : "-");
    kv("#ls-batt", b ? `${Math.round(b.pct)}% · ${(+b.volts).toFixed(2)} V` : "-");
    kv("#ls-wifi", w && w.rssi != null ? `${w.rssi} dBm · ${wifiWord(w.rssi)}` : "-");
    kv("#ls-up", w && w.uptime != null ? dur(w.uptime) : "-");
    kv("#ls-buf", w && w.buffered_ms != null ? `${w.buffered_ms} ms` : "-");
    kv("#ls-drops", w ? `${w.underruns ?? 0} / ${w.mic_drops ?? 0}` : "-");
    kv("#ls-fw", lite && lite.fw ? lite.fw : "-");
    kv("#ls-peer", lite && lite.peer ? String(lite.peer).replace(/[()' ]/g, "").split(",")[0] : "-");

    renderDial();
  }

  /* ---------- face ---------- */
  function faceInput() {
    const now = Date.now() / 1000;
    let face = lite && lite.face && lite.face.until > now ? lite.face.name : null;
    if (localFace && localFace.until > now) face = localFace.name;
    const doa = lite && lite.doa;
    const gx = doa && doa.at && now - doa.at < 2 ? Math.max(-2, Math.min(2, (doa.rel || 0) / 45 * 2)) : undefined;
    return {
      connected: mode() === "live",
      state: lite ? lite.state : "idle",
      face, gx,
      brightness: lite ? lite.brightness : 100,
      searchColor: lite ? "red" : "amber",
    };
  }

  function frame() {
    raf = 0;
    if (!visible()) return;
    Face.paint($("#lite-face"), Face.compose(faceInput(), live, performance.now()));
    raf = requestAnimationFrame(frame);
  }

  function visible() { return App.currentView() === "lite" && !document.hidden; }
  function wake() { if (!raf && visible()) raf = requestAnimationFrame(frame); }

  function buildExpressions() {
    const grid = $("#expr-grid");
    EXPRS.forEach(([name, label]) => {
      const b = document.createElement("button");
      b.className = "expr";
      b.innerHTML = `<canvas width="48" height="48"></canvas><span>${label}</span>`;
      b.addEventListener("click", async () => {
        b.classList.remove("pop"); void b.offsetWidth; b.classList.add("pop");
        localFace = { name, until: Date.now() / 1000 + 2.5 };
        const out = await Link.liteSet({ expr: name, hold: 2.5 });
        if (out.error) { localFace = null; App.toast(out.error); }
      });
      grid.append(b);
      requestAnimationFrame(() => Face.still(b.querySelector("canvas"), name));
    });
  }

  /* ---------- head dial: top-down, Lite faces up. + bearing = its left = screen left ---------- */
  const CX = 130, CY = 130, R = 110;
  const toXY = (deg, r = R) => [CX - r * Math.sin(deg * Math.PI / 180), CY - r * Math.cos(deg * Math.PI / 180)];

  function buildDial() {
    const g = $("#dial-ticks");
    for (let a = -90; a <= 90; a += 15) {
      const [x1, y1] = toXY(a, R - (a % 45 ? 5 : 10)), [x2, y2] = toXY(a, R + 0.5);
      const l = document.createElementNS("http://www.w3.org/2000/svg", "line");
      l.setAttribute("x1", x1); l.setAttribute("y1", y1); l.setAttribute("x2", x2); l.setAttribute("y2", y2);
      l.setAttribute("class", a % 45 ? "dial-tick" : "dial-tick major");
      g.append(l);
    }
    const svg = $("#look-dial");
    let dragging = false;
    const angleAt = (e) => {
      const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
      const p = pt.matrixTransform(svg.getScreenCTM().inverse());
      const a = Math.atan2(CX - p.x, CY - p.y) * 180 / Math.PI;
      return Math.round(Math.max(-90, Math.min(90, a)) / 5) * 5;
    };
    const preview = (a) => { lookTarget = { angle: a, until: Date.now() + 1500 }; renderDial(); };
    svg.addEventListener("pointerdown", (e) => {
      if (mode() !== "live") return;
      dragging = true; svg.setPointerCapture(e.pointerId); svg.classList.add("dragging"); preview(angleAt(e));
    });
    svg.addEventListener("pointermove", (e) => { if (dragging) preview(angleAt(e)); });
    const up = (e) => {
      if (!dragging) return;
      dragging = false; svg.classList.remove("dragging");
      look(angleAt(e));
    };
    svg.addEventListener("pointerup", up);
    svg.addEventListener("pointercancel", () => { dragging = false; svg.classList.remove("dragging"); });

    $("#look-center").addEventListener("click", () => look(0));
    $("#look-voice").addEventListener("click", () => {
      if (lite && lite.doa) look(Math.round(lite.doa.angle));
    });
  }

  async function look(angle) {
    // the device reports its head every 10 s (faster only while someone talks), so the
    // target stands in for it until a report newer than the command shows up
    lookTarget = { angle, until: Date.now() + 12000, sentAt: Date.now() / 1000 };
    renderDial();
    const out = await Link.liteSet({ look: angle });
    if (out.error) { lookTarget = null; renderDial(); App.toast(out.error); }
  }

  function headNow() {
    const s = lite && lite.status, d = lite && lite.doa;
    if (d && s && d.at > (s.at || 0)) return +d.head || 0;
    return s ? +s.head || 0 : 0;
  }

  function headStamp() {
    const s = lite && lite.status, d = lite && lite.doa;
    return Math.max((s && s.at) || 0, (d && d.at) || 0);
  }

  function renderDial() {
    if (lookTarget && lookTarget.sentAt && headStamp() > lookTarget.sentAt + 0.8) lookTarget = null;
    const tgt = lookTarget && lookTarget.until > Date.now() ? lookTarget.angle : null;
    const head = tgt ?? headNow();
    $("#dial-needle").style.transform = `rotate(${-head}deg)`;
    $("#head-read").textContent = Math.abs(head) < 3 ? "centered" : `${Math.abs(Math.round(head))}° ${head > 0 ? "left" : "right"}`;

    const tg = $("#dial-target");
    if (tgt !== null) {
      const [x, y] = toXY(tgt);
      tg.firstElementChild.setAttribute("cx", x); tg.firstElementChild.setAttribute("cy", y);
      tg.setAttribute("opacity", "1");
    } else tg.setAttribute("opacity", "0");

    const doa = lite && lite.doa, dot = $("#dial-doa");
    const fresh = doa && doa.at && Date.now() / 1000 - doa.at < 20;
    if (fresh) {
      const [x, y] = toXY(Math.max(-90, Math.min(90, doa.angle)), R);
      dot.setAttribute("cx", x); dot.setAttribute("cy", y);
    }
    dot.setAttribute("opacity", fresh ? "1" : "0");
    $("#look-voice").disabled = !fresh || mode() !== "live";
  }

  /* ---------- knobs ---------- */
  function bindKnob(sel, valSel, key) {
    const el = $(sel);
    let last = 0, timer = null;
    const push = () => { last = Date.now(); Link.liteSet({ [key]: +el.value }).then(o => o.error && App.toast(o.error)); };
    el.addEventListener("input", () => {
      $(valSel).textContent = el.value + "%";
      if (key === "brightness" && lite) lite.brightness = +el.value;   // preview follows the thumb
      clearTimeout(timer);
      if (Date.now() - last > 180) push(); else timer = setTimeout(push, 180);
    });
    el.addEventListener("change", () => { clearTimeout(timer); push(); });
  }

  /* ---------- helpers ---------- */
  function chip(sel, state, text) {
    const el = $(sel);
    el.dataset.state = state;
    const t = el.querySelector("span");
    if (t) t.textContent = text; else el.lastChild.textContent = text;
  }
  function kv(sel, v) { $(sel).textContent = v; }
  function setRange(sel, valSel, v) {
    const el = $(sel);
    el.value = v;
    el.style.setProperty("--fill", ((v - el.min) / (el.max - el.min) * 100) + "%");
    $(valSel).textContent = v + "%";
  }
  function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }
  function wifiWord(rssi) { return rssi >= -55 ? "great" : rssi >= -67 ? "good" : rssi >= -75 ? "fair" : "weak"; }
  function dur(s) {
    s = Math.floor(s);
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
    return Math.floor(s / 3600) + "h " + Math.floor(s % 3600 / 60) + "m";
  }
  function ago(ts) {
    const s = Math.max(0, Date.now() / 1000 - ts);
    return s < 60 ? "just now" : s < 3600 ? `${Math.floor(s / 60)} min ago` : `${Math.floor(s / 3600)} h ago`;
  }

  function init() {
    buildExpressions();
    buildDial();
    bindKnob("#lite-vol", "#lite-vol-val", "volume");
    bindKnob("#lite-bri", "#lite-bri-val", "brightness");
    Link.on("status", (s) => {
      status = s;
      lite = s.lite || null;
      render();
    });
    document.addEventListener("visibilitychange", wake);
    render();
  }

  return { init, wake, get mode() { return mode(); }, get snapshot() { return lite; } };
})();
