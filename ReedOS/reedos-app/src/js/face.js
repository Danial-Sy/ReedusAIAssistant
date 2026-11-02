/* Lite's 16x16 face, drawn the way ReedusLite/firmware/src/face.cpp draws it.
   a port of the same eyes, mouths and extras so the preview matches the real panel. */

const Face = (() => {
  const W = 16, H = 16;
  const MINT = [0x8F, 0xF5, 0xE2], PINK = [0xFF, 0x5C, 0x9E], BLUSH = [0xFF, 0x4F, 0x78],
        RED = [0xFF, 0x40, 0x28], SKY = [0x6F, 0xA8, 0xFF], GOLD = [0xFF, 0xC8, 0x40],
        AMBER = [0xFF, 0x90, 0x10], DIMWHITE = [0x60, 0x70, 0x70];
  const blend = (a, b, amt) => a.map((v, i) => Math.round(v + (b[i] - v) * amt / 255));

  /* one frame buffer; px() max-blends like the firmware */
  function makeFrame() { return Array.from({ length: H }, () => Array.from({ length: W }, () => [0, 0, 0])); }
  function px(f, x, y, c, v = 255) {
    x = Math.round(x); y = Math.round(y);
    if (x < 0 || x >= W || y < 0 || y >= H) return;
    const d = f[y][x], k = v / 255;
    d[0] = Math.max(d[0], c[0] * k); d[1] = Math.max(d[1], c[1] * k); d[2] = Math.max(d[2], c[2] * k);
  }
  function row(f, y, x0, x1, c, v) { for (let x = x0; x <= x1; x++) px(f, x, y, c, v); }

  function drawEye(f, e, cx, cy, c, open, side, t) {
    const EDGE = 115, outerX = side < 0 ? cx : cx + 2, innerX = side < 0 ? cx + 2 : cx;
    if (open < 0.2 && !["happy", "heart", "star", "x"].includes(e)) e = "closed";
    switch (e) {
      case "open": case "wide": {
        const top = e === "wide" ? cy - 1 : cy, h = e === "wide" ? 5 : 4;
        const vis = Math.max(1, Math.round(h * open));
        const y0 = top + Math.floor((h - vis + 1) / 2) + Math.floor((h - vis) / 4);
        for (let y = y0; y < y0 + vis; y++) {
          const edge = (y === y0 || y === y0 + vis - 1) && vis > 2;
          px(f, cx, y, c, edge ? EDGE : 255); px(f, cx + 1, y, c); px(f, cx + 2, y, c, edge ? EDGE : 255);
        }
        break;
      }
      case "happy": px(f, cx + 1, cy + 1, c); px(f, cx, cy + 2, c); px(f, cx + 2, cy + 2, c); break;
      case "closed": row(f, cy + 2, cx, cx + 2, c); break;
      case "half": row(f, cy + 2, cx, cx + 2, c, 130); row(f, cy + 3, cx, cx + 2, c); break;
      case "squint": row(f, cy + 1, cx, cx + 2, c, 150); row(f, cy + 2, cx, cx + 2, c); break;
      case "heart": {
        const v = 255 * (0.75 + 0.25 * Math.sin(t * 7)), l = cx - 1;
        px(f, l + 1, cy, c, v); px(f, l + 3, cy, c, v);
        row(f, cy + 1, l, l + 4, c, v); row(f, cy + 2, l + 1, l + 3, c, v); px(f, l + 2, cy + 3, c, v);
        break;
      }
      case "star": {
        px(f, cx + 1, cy + 2, c);
        if (Math.floor(t * 1000 / 220) % 2 === 0) {
          px(f, cx + 1, cy + 1, c, 200); px(f, cx + 1, cy + 3, c, 200);
          px(f, cx, cy + 2, c, 200); px(f, cx + 2, cy + 2, c, 200); px(f, cx + 1, cy, c, 90);
        } else {
          px(f, cx, cy + 1, c, 200); px(f, cx + 2, cy + 1, c, 200);
          px(f, cx, cy + 3, c, 200); px(f, cx + 2, cy + 3, c, 200);
        }
        break;
      }
      case "x":
        px(f, cx, cy + 1, c); px(f, cx + 2, cy + 1, c); px(f, cx + 1, cy + 2, c);
        px(f, cx, cy + 3, c); px(f, cx + 2, cy + 3, c);
        break;
      case "sad":
        row(f, cy + 1, Math.min(cx + 1, innerX), Math.max(cx + 1, innerX), c, 150);
        row(f, cy + 2, cx, cx + 2, c); row(f, cy + 3, cx, cx + 2, c);
        px(f, innerX, cy - 1, c, 150); px(f, cx + 1, cy - 1, c, 90); px(f, outerX, cy, c, 150);
        break;
      case "angry":
        row(f, cy + 1, Math.min(cx + 1, outerX), Math.max(cx + 1, outerX), c, 170);
        row(f, cy + 2, cx, cx + 2, c); row(f, cy + 3, cx, cx + 2, c);
        px(f, outerX, cy - 1, c, 170); px(f, cx + 1, cy - 1, c, 110); px(f, innerX, cy, c, 170);
        break;
      case "ring":
        row(f, cy - 1, cx, cx + 2, c);
        for (let y = cy; y <= cy + 2; y++) { px(f, cx, y, c); px(f, cx + 2, y, c); }
        row(f, cy + 3, cx, cx + 2, c);
        break;
    }
  }

  function drawMouth(f, m, c, level, dy, t) {
    const y = 10 + dy;
    switch (m) {
      case "smile": px(f, 5, y, c); px(f, 10, y, c); row(f, y + 1, 6, 9, c); break;
      case "grin": row(f, y - 1, 5, 10, c); px(f, 5, y, c); px(f, 10, y, c); row(f, y, 6, 9, c, 60); row(f, y + 1, 6, 9, c); break;
      case "flat": row(f, y + 1, 6, 9, c); break;
      case "short": row(f, y + 1, 7, 8, c); break;
      case "small_o": row(f, y, 7, 8, c); px(f, 6, y + 1, c); px(f, 9, y + 1, c); row(f, y + 2, 7, 8, c); break;
      case "big_o": row(f, y - 1, 6, 9, c); px(f, 5, y, c); px(f, 10, y, c); px(f, 5, y + 1, c); px(f, 10, y + 1, c); row(f, y + 2, 6, 9, c); break;
      case "frown": row(f, y, 6, 9, c); px(f, 5, y + 1, c); px(f, 10, y + 1, c); break;
      case "wavy": { const ph = Math.floor(t * 1000 / 300) % 2; for (let x = 5; x <= 10; x++) px(f, x, y + (((x + ph) & 1) ? 0 : 1), c); break; }
      case "smirk": row(f, y + 1, 6, 8, c); px(f, 9, y, c); px(f, 10, y - 1, c, 160); break;
      case "cat": px(f, 5, y, c); px(f, 7, y, c); px(f, 8, y, c); px(f, 10, y, c); px(f, 6, y + 1, c); px(f, 9, y + 1, c); break;
      case "talk": {
        const k = level < 0.10 ? 0 : level < 0.32 ? 1 : level < 0.62 ? 2 : 3;
        if (k === 0) row(f, y + 1, 6, 9, c);
        else if (k === 1) { row(f, y, 7, 8, c); px(f, 6, y + 1, c); px(f, 9, y + 1, c); row(f, y + 2, 7, 8, c); }
        else if (k === 2) { row(f, y, 6, 9, c); px(f, 5, y + 1, c); px(f, 10, y + 1, c); row(f, y + 2, 6, 9, c); }
        else { row(f, y - 1, 6, 9, c); px(f, 5, y, c); px(f, 10, y, c); px(f, 5, y + 1, c); px(f, 10, y + 1, c); row(f, y + 2, 6, 9, c); }
        break;
      }
    }
  }

  function drawExtras(f, x, c, t) {
    if (x.blush) { row(f, 8, 1, 2, BLUSH, 150); row(f, 8, 13, 14, BLUSH, 150); }
    if (x.tear) px(f, 3, 7 + Math.floor((t * 2.5) % 3), SKY, 200);
    if (x.zzz) {
      const dy = Math.sin(t * 1.6) > 0 ? 1 : 0;
      row(f, dy, 12, 14, DIMWHITE, 170); px(f, 13, 1 + dy, DIMWHITE, 170); row(f, 2 + dy, 12, 14, DIMWHITE, 170);
    }
    if (x.dots) { const i = Math.floor(t * 3) % 3; for (let k = 0; k < 3; k++) px(f, 6 + k * 2, 14, c, k === i ? 255 : 70); }
    if (x.question) {
      px(f, 14, 0, GOLD); px(f, 13, 1, GOLD); px(f, 15, 1, GOLD); px(f, 15, 2, GOLD); px(f, 14, 3, GOLD);
      if (Math.floor(t * 1000 / 400) % 2 === 0) px(f, 14, 5, GOLD);
    }
    if (x.hearts) for (let k = 0; k < 2; k++) {
      const p = (t * 0.9 + k * 0.5) % 1;
      px(f, k ? 14 : 1, 13 - Math.floor(p * 13), PINK, 220 * (1 - p));
    }
    if (x.sparkle) {
      const spots = [[0, 1], [15, 2], [1, 13], [14, 12], [7, 0], [0, 8], [15, 8]];
      spots.forEach(([sx, sy], k) => {
        const s = Math.sin(t * 5 + k * 1.7);
        if (s > 0.6) px(f, sx, sy, GOLD, 255 * (s - 0.6) / 0.4);
      });
    }
  }

  /* emotion -> parts, face.cpp applyEmo() */
  const NOBLINK = new Set(["happy", "laugh", "love", "sad", "surprised", "angry", "confused",
                           "excited", "shy", "wink", "bye", "content"]);
  function applyEmo(s, e, speaking, t, sinceS) {
    let m = s.mouth;
    switch (e) {
      case "happy": s.eyeL = s.eyeR = "happy"; m = "smile"; break;
      case "laugh": s.eyeL = s.eyeR = "happy"; m = "grin"; s.extras.bounce = true; break;
      case "love": s.eyeL = s.eyeR = "heart"; s.color = PINK; m = "smile"; s.extras.hearts = s.extras.blush = true; break;
      case "sad": s.eyeL = s.eyeR = "sad"; s.color = blend(MINT, SKY, 150); m = "frown"; s.extras.tear = true; s.gy = 1; break;
      case "surprised": s.eyeL = s.eyeR = "ring"; m = "big_o"; break;
      case "angry": s.eyeL = s.eyeR = "angry"; s.color = blend(MINT, RED, 190); m = "frown"; break;
      case "confused": s.eyeL = "open"; s.eyeR = "squint"; m = "wavy"; s.extras.question = true; break;
      case "sleepy": s.eyeL = s.eyeR = "half"; m = "flat"; s.dim *= 0.7; break;
      case "excited": s.eyeL = s.eyeR = "star"; s.color = blend(MINT, GOLD, 120); m = "grin"; s.extras.sparkle = true; break;
      case "smug": s.eyeL = "half"; s.eyeR = "squint"; m = "smirk"; break;
      case "shy": s.eyeL = s.eyeR = "happy"; m = "cat"; s.extras.blush = true; s.gy = 1; break;
      case "curious": s.eyeL = "wide"; s.eyeR = "open"; m = "small_o"; s.gx += 1; break;
      case "wink": s.eyeL = "open"; s.eyeR = "happy"; m = "smirk"; break;
      case "bye": s.eyeL = "happy"; s.eyeR = Math.floor(t * 1000 / 450) % 2 ? "closed" : "happy"; m = "smile"; break;
      case "dizzy": s.eyeL = s.eyeR = "x"; m = "wavy"; break;
      case "yawn": s.eyeL = s.eyeR = sinceS < 0.4 ? "half" : "closed"; m = sinceS < 0.3 ? "small_o" : "big_o"; s.dim *= 0.85; break;
      case "content": s.eyeL = s.eyeR = "happy"; m = "cat"; break;
    }
    if (NOBLINK.has(e)) s.blinkable = false;
    if (!speaking) s.mouth = m;
  }

  /* everything the live preview needs between frames */
  function createLive() {
    return {
      nextBlink: 0, blinkStart: 0, doubleBlink: false,
      nextFidget: 0, fidgetUntil: 0, fidget: 0, fidgetGx: 2,
      gazeX: 0, gazeY: 0, exStart: 0, lastEx: null,
    };
  }

  /* in: {state, face, connected, brightness, gx}; out: a 16x16 frame */
  function compose(inp, live, nowMs) {
    const t = nowMs / 1000;
    const f = makeFrame();
    const s = { eyeL: "open", eyeR: "open", mouth: "smile", color: MINT, extras: {}, gx: 0, gy: 0, dim: 1, blinkable: true };
    const st = inp.state || "idle";
    const speaking = st === "speaking";

    if (!inp.connected) {
      s.mouth = "flat"; s.gx = 2 * Math.sin(t * 1.3); s.dim = 0.4;
      if (Math.floor(nowMs / 600) % 2) px(f, 15, 15, inp.searchColor === "red" ? RED : AMBER, 180);
    } else {
      switch (st) {
        case "idle": idleFidget(live, nowMs, s); break;
        case "listening": s.eyeL = s.eyeR = "wide"; s.mouth = "short"; break;
        case "thinking": s.gy = -1; s.gx = 1.5 * Math.sin(t * 0.8); s.mouth = "flat"; s.extras.dots = true; break;
        case "speaking": s.mouth = "talk"; break;
        case "sleep":
          s.eyeL = s.eyeR = "closed"; s.mouth = "short"; s.extras.zzz = true;
          s.dim = 0.45 + 0.1 * Math.sin(t * 0.9); s.blinkable = false; break;
      }
      if (inp.gx !== undefined && st !== "sleep") s.gx = inp.gx;
      if (inp.face && st !== "thinking" && st !== "sleep") {
        if (inp.face !== live.lastEx) { live.lastEx = inp.face; live.exStart = nowMs; }
        applyEmo(s, inp.face, speaking, t, (nowMs - live.exStart) / 1000);
      } else live.lastEx = null;
    }

    let open = 1;
    if (s.blinkable) open = blinkOpen(live, nowMs);
    live.gazeX += (s.gx - live.gazeX) * 0.35;
    live.gazeY += (s.gy - live.gazeY) * 0.35;
    const gx = Math.round(live.gazeX), gy = Math.round(live.gazeY);
    const bounce = s.extras.bounce && Math.floor(nowMs / 160) % 2 ? 1 : 0;
    const level = speaking ? Math.abs(Math.sin(t * 9.1) * 0.6 + Math.sin(t * 13.7) * 0.4) : 0;

    drawEye(f, s.eyeL, 3 + gx, 3 + gy - bounce, s.color, open, -1, t);
    drawEye(f, s.eyeR, 10 + gx, 3 + gy - bounce, s.color, open, +1, t);
    drawMouth(f, s.mouth, s.color, level, bounce ? -1 : 0, t);
    drawExtras(f, s.extras, s.color, t);

    const dim = s.dim * (inp.brightness === undefined ? 1 : Math.max(0.3, inp.brightness / 100));
    if (dim < 1) for (const r of f) for (const p of r) { p[0] *= dim; p[1] *= dim; p[2] *= dim; }
    return f;
  }

  function blinkOpen(L, now) {
    if (!L.nextBlink) L.nextBlink = now + 1200 + Math.random() * 3000;
    if (now >= L.nextBlink && !L.blinkStart) L.blinkStart = now;
    if (!L.blinkStart) return 1;
    const dt = now - L.blinkStart;
    if (dt >= 130) {
      L.blinkStart = 0;
      if (L.doubleBlink) { L.doubleBlink = false; L.nextBlink = now + 90; }
      else { L.nextBlink = now + 2200 + Math.random() * 3800; L.doubleBlink = Math.random() < 0.18; }
      return 1;
    }
    return Math.abs(dt / 130 * 2 - 1);
  }

  function idleFidget(L, now, s) {
    if (!L.nextFidget) L.nextFidget = now + 4000;
    if (now >= L.nextFidget) {
      L.fidget = Math.floor(Math.random() * 5);
      L.fidgetUntil = now + 900 + Math.random() * 1400;
      L.fidgetGx = Math.random() < 0.5 ? 2 : -2;
      L.nextFidget = L.fidgetUntil + 6000 + Math.random() * 10000;
    }
    if (now >= L.fidgetUntil) return;
    const p = 1 - (L.fidgetUntil - now) / 2300;
    switch (L.fidget) {
      case 0: s.gx = L.fidgetGx; break;
      case 1: s.gx = p < 0.5 ? -2 : 2; break;
      case 2: applyEmo(s, "content", false, now / 1000, 0); break;
      case 3: s.gy = -1; s.gx = 1; break;
      case 4: s.mouth = "short"; s.gx = L.fidgetGx * 0.5; break;
    }
  }

  /* LEDs as soft round dots with a glow, unlit ones barely there */
  function paint(canvas, frame) {
    const dpr = window.devicePixelRatio || 1;
    const size = canvas.clientWidth || canvas.width;
    if (canvas.width !== Math.round(size * dpr)) { canvas.width = canvas.height = Math.round(size * dpr); }
    const ctx = canvas.getContext("2d");
    const n = canvas.width, cell = n / W, r = cell * 0.36;
    ctx.clearRect(0, 0, n, n);
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      const [R, G, B] = frame[y][x];
      const cx = (x + 0.5) * cell, cy = (y + 0.5) * cell;
      ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2);
      const lit = R + G + B > 12;
      if (lit) {
        ctx.shadowColor = `rgb(${R | 0},${G | 0},${B | 0})`; ctx.shadowBlur = cell * 0.9;
        ctx.fillStyle = `rgb(${Math.min(255, R * 1.05) | 0},${Math.min(255, G * 1.05) | 0},${Math.min(255, B * 1.05) | 0})`;
      } else {
        ctx.shadowBlur = 0; ctx.fillStyle = "rgba(255,255,255,0.045)";
      }
      ctx.fill();
    }
    ctx.shadowBlur = 0;
  }

  /* a still of one expression, for the buttons */
  function still(canvas, name) {
    const live = createLive();
    live.nextBlink = Infinity;
    paint(canvas, compose({ connected: true, state: "idle", face: name }, live, 100));
  }

  return { compose, createLive, paint, still };
})();
