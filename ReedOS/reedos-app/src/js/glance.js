/* the Lite panel as a display: timers, ringing, idle cards. a port of
   ReedusLite/firmware/src/glance.cpp so the app preview shows what the panel shows; keep the
   two in step (fonts come from ReedusLite/tools/gen_font.py for both). */

const Glance = (() => {
  const W = 16;
  const MINT = [0x8F, 0xF5, 0xE2], GOLD = [0xFF, 0xC8, 0x40], SKY = [0x6F, 0xA8, 0xFF],
        SNOW = [0xC8, 0xD8, 0xE8], CLOUD = [0x90, 0x9C, 0xA8], CORAL = [0xFF, 0x6A, 0x50],
        PINK = [0xFF, 0x5C, 0x9E];
  let P = null;
  const px = (x, y, c, v = 255) => P(x, y, c, v);

  const small = (ch) => Font.SMALL[ch.toUpperCase()] || Font.SMALL[" "];
  function smallWidth(s, tight) {
    let w = 0;
    for (let i = 0; i < s.length; i++) {
      const colon = s[i] === ":";
      w += small(s[i])[0] + ((tight && (colon || s[i + 1] === ":")) ? 0 : 1);
    }
    return w > 0 ? w - 1 : 0;
  }
  function drawSmall(s, x, y, c, v = 255, tight = false) {
    for (let i = 0; i < s.length; i++) {
      const [w, rows] = small(s[i]);
      if (x > -8 && x < W + 1) {
        for (let r = 0; r < Font.SMALL_H; r++)
          for (let b = 0; b < w; b++) if (rows[r] & (1 << (w - 1 - b))) px(x + b, y + r, c, v);
      }
      const colon = s[i] === ":";
      x += w + ((tight && (colon || s[i + 1] === ":")) ? 0 : 1);
    }
    return x;
  }
  function drawBig(ch, x, y, c, v = 255) {
    const g = Font.BIG[ch];
    if (!g) return;
    const [w, rows] = g;
    for (let r = 0; r < Font.BIG_H; r++)
      for (let b = 0; b < w; b++) if (rows[r] & (1 << (w - 1 - b))) px(x + b, y + r, c, v);
  }
  function drawSmallCentered(s, y, c, v = 255) {
    const tight = smallWidth(s) > W;
    drawSmall(s, Math.floor((W - smallWidth(s, tight) + 1) / 2), y, c, v, tight);
  }
  function scroll(s, y, c, now, t0, v = 255) {
    const w = smallWidth(s);
    if (w <= W) { drawSmallCentered(s, y, c, v); return; }
    const span = w + W + 6;
    const off = Math.floor(Math.max(0, now - t0) * 18 / 1000) % span;
    drawSmall(s, W - off, y, c, v);
  }
  const scrollMs = (s) => { const w = smallWidth(s); return w <= W ? 0 : (w + W + 6) * 1000 / 18; };

  function drawMSS(secs, y, c, v, colon) {
    const m = Math.floor(secs / 60), s = secs % 60;
    drawBig(String(m % 10), 0, y, c, v);
    if (colon) { px(5, y + 2, c, v); px(5, y + 4, c, v); }
    drawBig(String(Math.floor(s / 10)), 7, y, c, v);
    drawBig(String(s % 10), 12, y, c, v);
  }
  function drawPair(n, y, c, v, lead) {
    if (n < 10 && !lead) drawBig(String(n), 6, y, c, v);
    else { drawBig(String(Math.floor(n / 10) % 10), 3, y, c, v); drawBig(String(n % 10), 8, y, c, v); }
  }
  function eyes(now, y, c, wide) {
    const blink = now % 4200 > 4050;
    const gx = Math.floor(now / 2600) % 3 - 1;
    const h = wide ? 3 : 2, top = wide ? y - 1 : y;
    for (let e = 0; e < 2; e++) {
      const x = (e ? 10 : 4) + gx;
      if (blink && !wide) { px(x, y + 1, c, 200); px(x + 1, y + 1, c, 200); }
      else for (let r = 0; r < h; r++) { px(x, top + r, c); px(x + 1, top + r, c); }
    }
  }
  function bell(cx, y, c, tilt) {
    px(cx, y, c);
    for (let x = -1; x <= 1; x++) px(cx + x, y + 1, c);
    for (let r = 2; r <= 4; r++) { const t = r >= 4 ? tilt : 0; for (let x = -2; x <= 2; x++) px(cx + x + t, y + r, c); }
    for (let x = -3; x <= 3; x++) px(cx + x + tilt, y + 5, c);
    px(cx + tilt * 2, y + 6, c, 200);
  }
  function cloud(x, y, c, v = 255) {
    for (let i = 3; i <= 5; i++) px(x + i, y, c, v);
    for (let i = 2; i <= 7; i++) px(x + i, y + 1, c, v);
    for (let i = 1; i <= 8; i++) px(x + i, y + 2, c, v);
    for (let i = 0; i <= 9; i++) px(x + i, y + 3, c, v);
    for (let i = 1; i <= 8; i++) px(x + i, y + 4, c, v);
  }
  function sun(x, y, now, v = 255) {
    for (let r = 0; r < 4; r++) {
      const edge = r === 0 || r === 3;
      for (let i = edge ? 1 : 0; i < (edge ? 3 : 4); i++) px(x + 2 + i, y + 2 + r, GOLD, v);
    }
    const rv = v * (0.55 + 0.45 * (0.5 + 0.5 * Math.sin(now / 300)));
    px(x + 3, y, GOLD, rv); px(x + 4, y, GOLD, rv); px(x + 3, y + 7, GOLD, rv); px(x + 4, y + 7, GOLD, rv);
    px(x, y + 3, GOLD, rv); px(x, y + 4, GOLD, rv); px(x + 7, y + 3, GOLD, rv); px(x + 7, y + 4, GOLD, rv);
    px(x + 1, y + 1, GOLD, rv / 2); px(x + 6, y + 1, GOLD, rv / 2); px(x + 1, y + 6, GOLD, rv / 2); px(x + 6, y + 6, GOLD, rv / 2);
  }
  function weatherIcon(icon, x, y, now) {
    if (icon === "sun") sun(x + 1, y, now);
    else if (icon === "moon") {
      for (let r = 0; r < 7; r++) {
        const a = (r === 0 || r === 6) ? 3 : (r === 1 || r === 5) ? 2 : 1;
        const b = (r === 0 || r === 6) ? 5 : (r === 1 || r === 5) ? 4 : 3;
        for (let i = a; i <= b; i++) px(x + i + 1, y + r, SNOW);
      }
      if (Math.floor(now / 700) % 3 === 0) px(x + 8, y + 1, SNOW, 150);
    } else if (icon === "partly") { sun(x + 3, y - 1, now, 200); cloud(x, y + 3, SNOW); }
    else if (icon === "fog") {
      for (let r = 0; r < 4; r++) {
        const sh = (Math.floor(now / 400) + r) % 2;
        for (let i = sh; i < 10 - (1 - sh); i++) px(x + i, y + 1 + r * 2, CLOUD, 200);
      }
    } else {
      const wet = icon === "rain", snow = icon === "snow", storm = icon === "storm";
      cloud(x, y, storm ? CLOUD : SNOW, storm ? 230 : 255);
      if (wet || snow) for (let k = 0; k < 3; k++) {
        const dy = (Math.floor(now / (snow ? 260 : 140)) + k * 2) % 3;
        px(x + 2 + k * 3, y + 5 + dy, snow ? SNOW : SKY, 230);
      }
      if (storm && Math.floor(now / 180) % 5 !== 0) { px(x + 5, y + 5, GOLD); px(x + 4, y + 6, GOLD); px(x + 5, y + 6, GOLD); px(x + 4, y + 7, GOLD); }
    }
  }
  function note(x, y, c) {
    for (let r = 0; r < 5; r++) px(x + 3, y + r, c);
    px(x + 4, y, c); px(x + 5, y + 1, c);
    for (let i = 1; i <= 3; i++) { px(x + i, y + 5, c); px(x + i, y + 6, c); }
    px(x, y + 5, c, 150); px(x + 4, y + 5, c, 150);
  }

  function clockHM(h24) {
    const d = new Date();
    const h = d.getHours();
    return { h: h24 ? h : (h % 12 || 12), m: d.getMinutes(), s: d.getSeconds(), raw: h };
  }

  function drawTimer(t, n, now) {
    const leftF = t.left;
    const left = leftF <= 0 ? 0 : Math.ceil(leftF - 0.001);
    const v = t.paused && Math.floor(now / 600) % 2 ? 70 : 255;
    const colon = t.paused || now % 1000 < 600;
    const c = left <= 10 && !t.paused ? CORAL : MINT;
    if (left < 600) {
      const cyc = Math.floor(now / 1000) % 12;
      const showLabel = t.label && cyc < 1 + Math.floor(scrollMs(t.label) / 1000);
      if (showLabel) scroll(t.label, 0, GOLD, now, now - cyc * 1000, 200);
      else eyes(now, 2, c, left <= 10 && !t.paused);
      drawMSS(left, 6, c, v, colon);
    } else if (left < 6000) {
      drawPair(Math.floor(left / 60), 0, c, v, true);
      drawPair(left % 60, 8, c, v * 0.55, true);
    } else {
      const h = Math.floor(left / 3600), m = Math.floor(left / 60) % 60;
      drawBig(String(h % 10), 4, 0, c, v);
      drawSmall("H", 9, 2, c, v * 0.7);
      drawPair(m, 8, c, v * 0.55, true);
    }
    if (t.total > 0) {
      const p = Math.max(0, Math.min(1, leftF / t.total));
      const k = Math.ceil(p * W);
      for (let x = 0; x < k; x++) px(x, 15, c, 60);
    }
    for (let k = 1; k < n && k < 4; k++) px(W - k, 0, GOLD, 120);
  }

  function drawRing(r, since, now, h24) {
    const on = since % 600 < 380;
    if (r.kind === "timer") {
      if (r.label) scroll(r.label, 0, GOLD, now, now - since); else eyes(now, 2, CORAL, true);
      drawMSS(0, 6, CORAL, on ? 255 : 50, true);
      for (let x = 0; x < W; x++) px(x, 15, CORAL, on ? 160 : 30);
    } else if (r.kind === "alarm") {
      const q = Math.floor(since / 140) % 4;
      bell(7, 0, GOLD, q === 1 ? 1 : q === 3 ? -1 : 0);
      const { h, m } = clockHM(h24);
      drawSmallCentered(`${h}:${String(m).padStart(2, "0")}`, 9, GOLD, on ? 255 : 90);
      if (r.label && r.label !== "WAKE UP") scroll(r.label, 9, MINT, now, now - since + 2500);
    } else {
      const q = Math.floor(since / 200) % 4;
      bell(7, 0, SKY, q === 1 ? 1 : q === 3 ? -1 : 0);
      scroll(r.label || "REMINDER", 9, MINT, now, now - since);
    }
  }

  function drawCard(cd, now, h24) {
    const t0 = now - cd.age;
    if (cd.card === "clock") {
      const { h, m, s } = clockHM(h24);
      drawPair(h, 0, MINT, 255, h24);
      drawPair(m, 8, MINT, 170, true);
      px(Math.floor(s / 4), 15, GOLD, 90);
    } else if (cd.card === "weather") {
      weatherIcon(cd.icon || "sun", 3, 0, now);
      const buf = (cd.temp < 0 ? "-" : "") + Math.abs(cd.temp | 0);
      const deg = buf.length <= 2;
      const w = buf.length * 5 - 1 + (deg ? 3 : 0);
      const x = Math.floor((W - w + 1) / 2);
      for (let i = 0; i < buf.length; i++) drawBig(buf[i], x + i * 5, 9, MINT);
      if (deg) { const dx = x + buf.length * 5; px(dx, 9, MINT); px(dx + 1, 9, MINT); px(dx, 10, MINT); px(dx + 1, 10, MINT); }
    } else if (cd.card === "event") {
      const top = cd.mins <= 0 ? "NOW" : cd.mins < 60 ? `${cd.mins}M` : (cd.at || "");
      drawSmallCentered(top, 0, GOLD);
      for (let x = 2; x < W - 2; x += 2) px(x, 6, MINT, 50);
      scroll(cd.title || "", 9, MINT, now, t0 + 600);
    } else if (cd.card === "music") {
      note(1, 0, PINK);
      for (let k = 0; k < 3; k++) {
        const ph = now / 170 + k * 1.9;
        const hgt = 2 + Math.floor(2.5 + 2.5 * Math.sin(ph) * Math.sin(ph * 0.37 + k));
        for (let r = 0; r < hgt && r < 7; r++) px(9 + k * 2, 6 - r, MINT, 180);
      }
      scroll(cd.artist ? `${cd.title} - ${cd.artist}` : (cd.title || ""), 9, MINT, now, t0 + 500);
    } else if (cd.card === "text") {
      bell(7, 0, GOLD, 0);
      scroll(cd.title || cd.text || "", 9, MINT, now, t0 + 400);
    }
  }

  /* d: {timers:{items, ring, alarm}, card:{card, ..., age ms}}, with seconds already brought to now.
     returns {own, dim} like glance::draw */
  function draw(pxFn, d, now, idle, asleep, h24) {
    P = pxFn;
    const tm = (d && d.timers) || {};
    const ring = (tm.ring || [])[0];
    if (ring && idle) { drawRing(ring, d.ringFor || 0, now, h24); return { own: true, dim: 1 }; }
    if (!idle) return { own: false, dim: 1 };
    const cd = d && d.card;
    if (cd && (!asleep || cd.card === "clock")) {
      drawCard(cd, now, h24);
      return { own: true, dim: cd.dim || asleep ? 0.45 : 1 };
    }
    const items = (tm.items || []).slice().sort((a, b) => a.left - b.left);
    if (items.length) { drawTimer(items[0], items.length, now); return { own: true, dim: asleep ? 0.55 : 1 }; }
    if (tm.alarm >= 0) px(0, 0, GOLD, 70);
    return { own: false, dim: 1 };
  }

  return { draw };
})();
