
const Blocks = {
  move: {
    label: "Move", icon: "↑", hint: "drive",
    // 50cm is 50cm on carpet, on hardwood, and at low battery.
    make: () => ({ type: "move", dir: "forward", cm: 50 }),
    render(s) {
      if (s.cm === undefined) s.cm = 50;      // migrate old seconds-based steps
      return [
        sel(s, "dir", { forward: "Forward", back: "Backward" }),
        num(s, "cm", 1, 500, "cm"),
      ];
    },
    summary: s => `Move ${s.dir} ${s.cm} cm`,
  },
  turn: {
    label: "Turn", icon: "↻", hint: "drive",
    make: () => ({ type: "turn", dir: "left", degrees: 90 }),
    render(s) {
      return [
        sel(s, "dir", { left: "Left", right: "Right" }),
        num(s, "degrees", 5, 360, "°"),
      ];
    },
    summary: s => `Turn ${s.dir} ${s.degrees}°`,
  },
  wait: {
    label: "Wait", icon: "⏳", hint: "flow",
    make: () => ({ type: "wait", seconds: 1 }),
    render(s) { return [num(s, "seconds", 0.1, 300, "s")]; },
    summary: s => `Wait ${s.seconds}s`,
  },
  say: {
    label: "Say", icon: "\u{1F4AC}", hint: "voice",
    make: () => ({ type: "say", text: "Hello!" }),
    render(s) { return [txt(s, "text", "What should Reedus say?")]; },
    summary: s => `Say "${s.text}"`,
  },
  sound: {
    label: "Play sound", icon: "♫", hint: "voice",
    make: () => ({ type: "sound", name: "chirp" }),
    render(s) {
      return [sel(s, "name", { chirp: "Chirp", boop: "Boop", trill: "Trill", purr: "Purr", alarm: "Alarm" })];
    },
    summary: s => `Play ${s.name}`,
  },
  expression: {
    label: "Expression", icon: "☺", hint: "voice",
    make: () => ({ type: "expression", mood: "happy" }),
    render(s) {
      return [sel(s, "mood", { happy: "Happy", curious: "Curious", sleepy: "Sleepy", alert: "Alert", grumpy: "Grumpy" })];
    },
    summary: s => `Feel ${s.mood}`,
  },
  speed: {
    label: "Set speed cap", icon: "⚡", hint: "drive",
    make: () => ({ type: "speed", limit: 60 }),
    render(s) { return [num(s, "limit", 10, 100, "% cap")]; },
    summary: s => `Speed cap ${s.limit}%`,
  },
  repeat: {
    label: "Repeat all", icon: "↺", hint: "flow",
    make: () => ({ type: "repeat", times: 2 }),
    render(s) { return [num(s, "times", 1, 50, "× passes")]; },
    summary: s => `Repeat routine ${s.times}×`,
  },
};

/* param-control builders, return DOM nodes */
function num(step, key, min, max, suffix) {
  const wrap = document.createElement("span");
  const inp = document.createElement("input");
  inp.type = "number"; inp.min = min; inp.max = max; inp.step = "any";
  inp.value = step[key];
  inp.addEventListener("change", () => {
    let v = parseFloat(inp.value);
    if (isNaN(v)) v = min;
    v = Math.max(min, Math.min(max, v));
    inp.value = v; step[key] = v;
  });
  wrap.append(inp, document.createTextNode(" " + suffix));
  return wrap;
}

function sel(step, key, options) {
  const s = document.createElement("select");
  for (const [val, label] of Object.entries(options)) {
    const o = document.createElement("option");
    o.value = val; o.textContent = label;
    if (step[key] === val) o.selected = true;
    s.append(o);
  }
  s.addEventListener("change", () => { step[key] = s.value; });
  return s;
}

function txt(step, key, placeholder) {
  const inp = document.createElement("input");
  inp.type = "text"; inp.value = step[key]; inp.placeholder = placeholder || "";
  inp.style.minWidth = "220px";
  inp.addEventListener("input", () => { step[key] = inp.value; });
  return inp;
}
