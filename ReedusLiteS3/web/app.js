/* Reedus Lite: the website the relay serves at /. it talks to the relay's /app socket and
   nothing else: sign in, set the relay up, pair Lites, credit, the AI model, memory, chat.
   the sign-in lives in this browser (localStorage "rl.token"); Sign out removes it. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

let relayHost = location.host;    // relayInfo() points this at the relay when the page lives elsewhere

const saved = {
  get(k) { try { return localStorage.getItem(k) || ""; } catch { return ""; } },
  set(k, v) { try { if (v) localStorage.setItem(k, v); else localStorage.removeItem(k); } catch { /* private mode */ } },
};

const money = (v) => {
  const a = Math.abs(v || 0);
  return (v < 0 ? "-$" : "$") + a.toFixed(a > 0 && a < 0.1 ? 4 : 2);
};
const ago = (t) => {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  const d = Math.round(s / 86400);
  return d === 1 ? "yesterday" : `${d} days ago`;
};
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};
function busy(btn, on) { if (btn) { btn.disabled = on; btn.classList.toggle("busy", on); } }
function say(sel, text, bad) { const e = $(sel); e.textContent = text || ""; e.classList.toggle("bad", !!bad); }
function toast(text) {
  const t = el("div", "toast", text);
  $("#toasts").append(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 400); }, 2600);
}
/* a button that asks once more before doing something you can't undo */
function confirmFirst(btn, ask, fn) {
  if (!btn.classList.contains("confirm")) {
    const was = btn.textContent;
    btn.classList.add("confirm");
    btn.textContent = ask;
    setTimeout(() => { btn.classList.remove("confirm"); btn.textContent = was; }, 3000);
    return;
  }
  btn.classList.remove("confirm");
  fn();
}

/* ======================= the relay socket ======================= */
const Relay = (() => {
  let ws = null, authed = false, me = null, nextId = 1, retry = null;
  const pending = new Map();
  const hooks = { down: [], up: [], lost: [] };
  const url = () => (relayHost === location.host
    ? `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/app`
    : `wss://${relayHost}/app`);
  const fire = (k, v) => hooks[k].forEach((f) => f(v));

  /* open a socket and sign in with `first` ({type:"auth"|"setup"|"join"|"redeem"|"owner", …}) */
  function open(first) {
    if (ws) { try { ws.close(); } catch { /* */ } }
    clearTimeout(retry);
    return new Promise((resolve) => {
      let sock, done = false;
      const finish = (v) => { if (!done) { done = true; resolve(v); } };
      try { sock = new WebSocket(url()); } catch { finish({ error: "can't reach the relay" }); return; }
      ws = sock;
      const timer = setTimeout(() => { finish({ error: "the relay didn't answer" }); try { sock.close(); } catch { /* */ } }, 10000);
      sock.onopen = () => sock.send(JSON.stringify(first));
      sock.onmessage = (ev) => {
        let m;
        try { m = JSON.parse(ev.data); } catch { return; }
        if (m.type === "auth.ok") {
          clearTimeout(timer);
          if (m.token) saved.set("rl.token", m.token);
          authed = true;
          me = m;
          fire("up", m);
          finish({ ok: true, me: m });
          return;
        }
        if (m.type === "auth.fail") {
          clearTimeout(timer);
          if (first.type === "auth") { saved.set("rl.token", ""); fire("lost", m.error); }
          finish({ error: m.error || "that didn't work" });
          try { sock.close(); } catch { /* */ }
          return;
        }
        const r = pending.get(m.id);
        if (r) { pending.delete(m.id); r(m); }
      };
      sock.onclose = () => {
        clearTimeout(timer);
        const was = authed && ws === sock;
        if (ws === sock) { ws = null; authed = false; }
        for (const [, r] of pending) r({ error: "the connection to the relay dropped" });
        pending.clear();
        finish({ error: "can't reach the relay" });
        if (was) {
          fire("down");
          retry = setTimeout(() => { if (saved.get("rl.token")) connect(); }, 2500);
        }
      };
    });
  }

  function connect() {
    const token = saved.get("rl.token");
    if (!token) return Promise.resolve({ error: "not signed in" });
    return open({ type: "auth", token });
  }

  async function call(action, extra) {
    if (!authed) {
      const r = await connect();
      if (!r.ok) return { error: r.error };
    }
    return new Promise((resolve) => {
      const id = nextId++;
      pending.set(id, resolve);
      try { ws.send(JSON.stringify({ id, action, ...(extra || {}) })); } catch {
        pending.delete(id);
        resolve({ error: "the connection to the relay dropped" });
        return;
      }
      setTimeout(() => { if (pending.has(id)) { pending.delete(id); resolve({ error: "the relay took too long to answer" }); } }, 60000);
    });
  }

  function signOut() {
    saved.set("rl.token", "");
    authed = false;
    clearTimeout(retry);
    if (ws) { try { ws.close(); } catch { /* */ } }
    ws = null;
  }

  return {
    open, connect, call, signOut,
    on: (k, f) => hooks[k].push(f),
    get me() { return me; },
    get up() { return authed; },
  };
})();

/* ======================= state ======================= */
const S = { status: null, setup: null, members: [], models: {}, amount: 10, view: "home", info: null, chatLoaded: false };
const owner = () => !!(Relay.me && Relay.me.owner);
const params = new URLSearchParams(location.search);
let returning = params.get("topup");
const invite = (location.hash.match(/join=([A-Za-z0-9-]+)/) || [])[1] || "";

/* ======================= theme ======================= */
function applyTheme() {
  const pick = saved.get("rl.theme") || "auto";
  const dark = pick === "night" || (pick === "auto" && !matchMedia("(prefers-color-scheme: light)").matches);
  document.body.dataset.theme = dark ? "night" : "day";
  $$("#theme button").forEach((b) => b.classList.toggle("on", b.dataset.val === pick));
  const meta = $('meta[name="theme-color"]');
  if (meta) meta.content = dark ? "#0C1510" : "#EEE8D9";
}
matchMedia("(prefers-color-scheme: light)").addEventListener?.("change", applyTheme);

/* ======================= before signing in ======================= */
function gateErr(t) { $("#g-err").textContent = t || ""; }

function showGate(why) {
  $("#shell").hidden = true;
  $("#gate").hidden = false;
  $$(".g-pane").forEach((p) => { p.hidden = true; });
  gateErr(why || "");
  const info = S.info || {};
  if (invite) {
    $("#g-join").hidden = false;
    setTimeout(() => $("#g-join-name").focus(), 50);
  } else if (info.relay && !info.claimed) {
    $("#g-setup").hidden = false;
    $("#g-setup-code-row").hidden = !info.setup_code;
    setTimeout(() => $("#g-setup-name").focus(), 50);
  } else {
    $("#g-signin").hidden = false;
    setTimeout(() => $("#g-owner-code").focus(), 50);
  }
}

async function gateSubmit(btn, first) {
  gateErr("");
  busy(btn, true);
  const r = await Relay.open(first);
  busy(btn, false);
  if (!r.ok) { gateErr(r.error); return; }
  if (invite) history.replaceState(null, "", location.pathname + "#home");
  enterShell();
}

function wireGate() {
  $("#g-setup").addEventListener("submit", (e) => {
    e.preventDefault();
    const name = $("#g-setup-name").value.trim();
    if (!name) { gateErr("Type your name first."); return; }
    gateSubmit(e.submitter, { type: "setup", name, code: $("#g-setup-code").value.trim() });
  });
  $("#g-join").addEventListener("submit", (e) => {
    e.preventDefault();
    const name = $("#g-join-name").value.trim();
    if (!name) { gateErr("Type your name first."); return; }
    gateSubmit(e.submitter, { type: "join", invite, name });
  });
  $("#g-signin").addEventListener("submit", (e) => {
    e.preventDefault();
    const code = $("#g-owner-code").value.trim();
    if (!code) { gateErr("Type the password (the setup code, RELAY_ADMIN_TOKEN on Railway)."); return; }
    gateSubmit(e.submitter, { type: "owner", code });
  });
  for (const id of ["#pair-code"]) {
    $(id).addEventListener("input", (e) => {
      const d = e.target.value.replace(/\D/g, "").slice(0, 6);
      e.target.value = d.length > 3 ? `${d.slice(0, 3)} ${d.slice(3)}` : d;
    });
  }
}

/* ======================= the signed-in app ======================= */
const VIEWS = ["home", "chat", "credit", "settings"];

function go(view, push = true) {
  if (!VIEWS.includes(view)) view = "home";
  S.view = view;
  $$(".view").forEach((v) => v.classList.toggle("on", v.id === `v-${view}`));
  $$(".tab").forEach((t) => t.classList.toggle("on", t.dataset.go === view));
  if (push && location.hash !== `#${view}`) history.replaceState(null, "", `${location.pathname}#${view}`);
  window.scrollTo(0, 0);
  if (view === "credit") loadLedger();
  if (view === "settings") loadSettings();
  if (view === "chat") loadChat();
}

async function enterShell() {
  $("#gate").hidden = true;
  $("#shell").hidden = false;
  $("#conn").className = "conn on";
  const want = location.hash.replace("#", "");
  go(returning ? "credit" : (VIEWS.includes(want) ? want : "home"), true);
  await refresh();
  if (returning) finishTopup();
}

async function refresh() {
  const [st, su] = await Promise.all([Relay.call("status"), Relay.call("setup.status")]);
  if (st.error) { toast(st.error); return; }
  S.status = st;
  if (!su.error) S.setup = su;
  renderTop();
  renderHome();
  renderCredit();
  if (S.view === "settings") { renderBrain(); renderListen(); }
}

/* ---------- top bar ---------- */
function renderTop() {
  const st = S.status;
  const pill = $("#bal-pill");
  pill.textContent = money(st.balance);
  pill.classList.toggle("low", !!st.low);
}

/* ---------- home ---------- */
const STEP_ACTIONS = {
  brain: () => openKey("GEMINI_API_KEY"),
  hearing: () => openKey("GEMINI_API_KEY"),
  voice: () => openKey("INWORLD_API_KEY"),
  lite: () => openPair(),
  money: () => go("credit"),
  payments: () => openKey("STRIPE_SECRET_KEY"),
};

function renderHome() {
  const st = S.status, su = S.setup;
  $("#hello").textContent = `Hi, ${st.account.name}`;
  if (su) {
    const todo = su.items.filter((i) => !i.done && !i.optional).length;
    $("#setup").hidden = su.ready && su.items.every((i) => i.done || i.optional);
    $("#setup-count").textContent = todo ? `${todo} to go` : "done";
    $("#setup-count").classList.toggle("done", !todo);
    $("#setup-wait").hidden = su.owner || su.relay_ready;
    const ol = $("#setup-steps");
    ol.innerHTML = "";
    su.items.forEach((it, i) => {
      const li = el("li", it.done ? "done" : "");
      const b = el("button");
      b.type = "button";
      const dot = el("span", "dot");
      if (it.done) dot.innerHTML = '<svg><use href="#i-check"/></svg>';
      else dot.textContent = it.optional ? "·" : String(i + 1);
      const txt = el("span");
      txt.append(el("b", "", it.title + (it.optional ? " (optional)" : "")), el("small", "", it.detail));
      const chev = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      chev.setAttribute("class", "chev");
      chev.innerHTML = '<use href="#i-chev"/>';
      b.append(dot, txt, chev);
      b.addEventListener("click", () => (STEP_ACTIONS[it.id] || (() => {}))());
      li.append(b);
      ol.append(li);
    });
  }
  renderLites();
}

function liteLine(d) {
  const bits = [];
  if (d.online) bits.push(d.state && d.state !== "idle" ? d.state : "online");
  else bits.push(d.last_seen ? `offline, seen ${ago(d.last_seen)}` : "hasn't connected yet");
  if (d.battery && d.battery.pct != null) bits.push(`battery ${d.battery.pct}%`);
  return bits.join(" · ");
}

function renderLites() {
  const box = $("#lites");
  const devs = S.status.devices || [];
  const sig = JSON.stringify(devs.map((d) => [d.id, d.name, d.online]));
  if (box.dataset.sig === sig && box.children.length) {
    devs.forEach((d) => {
      const s = $(`[data-dev="${d.id}"] .lite-text small`);
      if (s) s.textContent = liteLine(d);
      const rb = $(`[data-dev="${d.id}"] .r-ring`);
      if (rb) rb.hidden = !d.ringing;
    });
    return;
  }
  if (box.contains(document.activeElement) && document.activeElement.type === "range") return;
  box.dataset.sig = sig;
  box.innerHTML = "";
  if (!devs.length) {
    const c = el("div", "card empty");
    c.innerHTML = '<svg aria-hidden="true"><use href="#i-home"/></svg>';
    c.append(el("p", "", "No Lite yet. Turn yours on and tap Pair a Lite."));
    box.append(c);
    return;
  }
  devs.forEach((d) => {
    const s = d.settings || {};
    const c = el("div", "card lite" + (d.online ? " on" : ""));
    c.dataset.dev = d.id;
    c.innerHTML = `
      <div class="lite-head">
        <span class="lite-face"><svg><use href="#i-home"/></svg></span>
        <div class="lite-text"><b></b><small></small></div>
        <div class="lite-menu"><button class="cta small r-ctl" type="button">Controls</button>
          <button class="ghost-btn small r-name" type="button">Rename</button></div>
      </div>
      <button class="cta gold wide r-ring" type="button" hidden>Stop ringing</button>
      <label class="slider"><span>Volume</span><input type="range" min="0" max="100" class="r-vol"><b class="v-vol"></b></label>
      <label class="slider"><span>Brightness</span><input type="range" min="5" max="100" class="r-bri"><b class="v-bri"></b></label>
      <div class="inline r-edit" hidden><input class="r-input" maxlength="40"><button class="cta small r-save" type="button">Save</button>
        <button class="ghost-btn small danger r-remove" type="button">Remove</button></div>`;
    $("b", c).textContent = d.name;
    $("small", c).textContent = liteLine(d);
    const ringBtn = $(".r-ring", c);
    ringBtn.hidden = !d.ringing;
    ringBtn.addEventListener("click", async () => {
      const r = await Relay.call("device.ring_off", { device: d.id });
      if (r.error) { toast(r.error); return; }
      ringBtn.hidden = true;
      toast("Quiet now");
    });
    const wire = (cls, key, label, def) => {
      const r = $(cls, c), v = $(label, c);
      r.value = s[key] != null ? s[key] : def;
      const paint = () => { v.textContent = `${r.value}%`; r.style.setProperty("--fill", `${(r.value - r.min) / (r.max - r.min) * 100}%`); };
      paint();
      r.addEventListener("input", paint);
      r.addEventListener("change", async () => {
        const out = await Relay.call("device.set", { device: d.id, [key]: +r.value });
        if (out.error) toast(out.error);
        else s[key] = +r.value;
      });
    };
    wire(".r-vol", "volume", ".v-vol", 70);
    wire(".r-bri", "brightness", ".v-bri", 100);
    $(".r-ctl", c).addEventListener("click", () => Ctl.open(d.id));
    $(".r-name", c).addEventListener("click", () => {
      const row = $(".r-edit", c);
      row.hidden = !row.hidden;
      $(".r-input", c).value = d.name;
      if (!row.hidden) $(".r-input", c).focus();
    });
    $(".r-save", c).addEventListener("click", async (e) => {
      busy(e.target, true);
      const out = await Relay.call("device.rename", { device: d.id, name: $(".r-input", c).value });
      busy(e.target, false);
      if (out.error) { toast(out.error); return; }
      refresh();
    });
    $(".r-remove", c).addEventListener("click", (e) => confirmFirst(e.target, "Remove? It'll need pairing again", async () => {
      const out = await Relay.call("device.remove", { device: d.id });
      toast(out.error || `${d.name} removed`);
      refresh();
    }));
    box.append(c);
  });
}

/* ---------- pairing ---------- */
function openPair() {
  $("#pair").hidden = false;
  $("#pair-code").value = "";
  say("#pair-out", S.status && !S.status.pairing
    ? "No Lite is waiting right now. Make sure it's plugged in and on Wi-Fi; the code shows up within a few seconds." : "");
  setTimeout(() => $("#pair-code").focus(), 60);
}
function closeSheets() { $$(".sheet-bg").forEach((s) => { s.hidden = true; }); }

function wirePair() {
  $("#pair-open").addEventListener("click", openPair);
  $$("[data-close]").forEach((b) => b.addEventListener("click", closeSheets));
  $("#pair").addEventListener("click", (e) => { if (e.target.id === "pair") closeSheets(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheets(); });
  $("#pair-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const code = $("#pair-code").value.replace(/\D/g, "");
    if (code.length !== 6) { say("#pair-out", "The code is 6 digits.", true); return; }
    busy(e.submitter, true);
    const r = await Relay.call("pair", { code, name: $("#pair-name").value.trim() });
    busy(e.submitter, false);
    if (r.error) { say("#pair-out", r.error, true); return; }
    closeSheets();
    toast(`${r.device.name} paired. Say "Reedus" to it`);
    setTimeout(refresh, 1500);
  });
}

/* ---------- chat ---------- */
function bubble(cls, text, when) {
  $("#chat-empty").hidden = true;
  const m = el("div", `msg ${cls}`, text);
  if (when) m.append(el("small", "", when));
  $("#thread").append(m);
  return m;
}

async function loadChat() {
  if (S.chatLoaded) return;
  S.chatLoaded = true;
  const mem = await Relay.call("memory.get", { limit: 6 });
  if (mem.error || !(mem.recent || []).length) return;
  [...mem.recent].reverse().forEach((t) => {
    bubble("me", t.said);
    bubble("him", t.reply, ago(t.at));
  });
  window.scrollTo(0, document.body.scrollHeight);
}

function wireChat() {
  $("#chat-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("#chat-input"), text = input.value.trim();
    if (!text) return;
    input.value = "";
    bubble("me", text);
    const wait = bubble("him typing", "•••");
    window.scrollTo(0, document.body.scrollHeight);
    const r = await Relay.call("chat", { text });
    wait.classList.remove("typing");
    wait.textContent = r.error ? r.error : r.text;
    wait.classList.toggle("bad", !!r.error);
    window.scrollTo(0, document.body.scrollHeight);
    if (!r.error) { const st = await Relay.call("status"); if (!st.error) { S.status = st; renderTop(); } }
  });
}

/* ---------- credit ---------- */
function renderCredit() {
  const st = S.status;
  const pay = !!st.payments;
  $("#balance").textContent = money(st.balance);
  $("#balance-card").classList.toggle("low", !!st.low);
  $("#balance-note").textContent = st.balance <= 0
    ? "Out of credit: Lite won't answer until you add some."
    : "Every reply costs about a cent: thinking, his voice, and hearing you.";

  const amounts = st.amounts || [5, 10, 20, 50];
  if (!amounts.includes(S.amount)) S.amount = amounts[1] || amounts[0];
  const box = $("#amounts");
  box.innerHTML = "";
  amounts.forEach((a) => {
    const b = el("button", "chip" + (a === S.amount ? " on" : ""), `$${a}`);
    b.type = "button";
    b.setAttribute("aria-pressed", a === S.amount);
    b.addEventListener("click", () => { S.amount = a; renderCredit(); });
    box.append(b);
  });
  $("#amounts").hidden = !pay;
  $("#pay").hidden = !pay;
  $("#pay").textContent = `Pay $${S.amount}`;
  $("#pay-off").hidden = pay;
  $("#pay-off").textContent = owner()
    ? "Card payments are off. Add a Stripe key in Settings → Service keys, or add credit by hand below."
    : "Card payments aren't set up on this relay. Ask its owner to add credit for you.";
  if (pay && st.stripe_mode === "test" && !$("#pay-out").textContent) {
    say("#pay-out", "Test mode: nothing is really charged. Use card 4242 4242 4242 4242.");
  }

  const b = st.billing || {};
  const card = b.card;
  $("#save-row").hidden = !pay || !!card;
  $("#auto-card").hidden = !card;
  if (card) {
    $("#card-chip").textContent = `${card.brand} •••• ${card.last4}${card.exp ? ` (${card.exp})` : ""}`;
    const auto = b.auto || {};
    if (document.activeElement !== $("#auto-on")) $("#auto-on").checked = !!auto.enabled;
    fillMoney($("#auto-below"), b.below_options || [1, 2, 5], auto.below ?? 2);
    fillMoney($("#auto-amount"), b.amounts || amounts, auto.amount ?? 10);
    $("#auto-card").classList.toggle("off", !auto.enabled);
    $("#auto-failed").hidden = !b.auto_failed;
    $("#auto-failed").textContent = b.auto_failed || "";
  }

  $("#manual-card").hidden = !owner();
  if (owner()) loadMembers();
}

function fillMoney(sel, opts, val) {
  if (document.activeElement === sel) return;
  const list = opts.includes(+val) ? opts : [...opts, +val].sort((x, y) => x - y);
  sel.innerHTML = "";
  list.forEach((v) => { const o = el("option", "", `$${v}`); o.value = v; sel.append(o); });
  sel.value = String(+val);
}

async function loadLedger() {
  const r = await Relay.call("ledger", { limit: 15 });
  const ul = $("#ledger");
  ul.innerHTML = "";
  const rows = r.ledger || [];
  if (!rows.length) { ul.append(el("li", "none", "Nothing yet.")); return; }
  rows.forEach((x) => {
    const li = el("li");
    li.append(el("span", "what", `${ago(x.at)} · ${x.what}`));
    const a = el("span", "amt" + (x.usd > 0 ? " in" : ""), (x.usd > 0 ? "+" : "") + money(x.usd));
    li.append(a);
    ul.append(li);
  });
}

async function loadMembers() {
  const r = await Relay.call("members");
  if (r.error || !S.status) return;
  S.members = r.members || [];
  const sel = $("#manual-who");
  const keep = sel.value;
  sel.innerHTML = "";
  S.members.forEach((m) => {
    const o = el("option", "", `${m.name}${m.id === S.status.account.id ? " (you)" : ""} · ${money(m.balance)}`);
    o.value = m.id;
    sel.append(o);
  });
  sel.value = keep && S.members.some((m) => m.id === keep) ? keep : S.status.account.id;
  renderPeople();
}

async function startPay(btn) {
  busy(btn, true);
  say("#pay-out", "Opening checkout…");
  const save = !$("#save-row").hidden && $("#save-card").checked;
  const r = await Relay.call("topup.start", {
    usd: S.amount, return_to: location.origin + location.pathname,
    save_card: save, auto: save ? { below: 2, amount: S.amount } : undefined,
  });
  busy(btn, false);
  if (r.error) { say("#pay-out", r.error, true); return; }
  location.href = r.url;              // Stripe's page: card, Apple Pay, Google Pay
}

async function finishTopup() {
  const sid = returning;
  returning = null;
  history.replaceState(null, "", location.pathname + "#credit");
  if (sid === "cancel") { toast("Payment cancelled, nothing was charged"); return; }
  say("#pay-out", "Checking the payment…");
  let r = await Relay.call("topup.confirm", { session: sid });
  for (let i = 0; i < 5 && !r.error && !r.credited && !r.already && r.status !== "expired"; i++) {
    await new Promise((ok) => setTimeout(ok, 1500));
    r = await Relay.call("topup.confirm", { session: sid });
  }
  if (r.error) say("#pay-out", r.error, true);
  else if (r.credited) {
    toast(`${money(r.usd)} added`);
    say("#pay-out", `${money(r.usd)} added.` + (r.card ? ` ${r.card.brand} •••• ${r.card.last4} saved, auto refill is on.` : ""));
  } else if (r.already) say("#pay-out", "Already added.");
  else say("#pay-out", "Payment still processing. It'll show up within a minute.");
  refresh();
  loadLedger();
}

async function saveAuto() {
  const b = S.status.billing || {};
  const r = await Relay.call("billing.set", { enabled: $("#auto-on").checked, below: +$("#auto-below").value, amount: +$("#auto-amount").value });
  if (r.error) { toast(r.error); $("#auto-on").checked = !!(b.auto && b.auto.enabled); return; }
  S.status.billing = { ...b, card: r.card, auto: r.auto, auto_failed: r.auto_failed };
  renderCredit();
  toast(r.auto.enabled ? `Auto refill on: under $${r.auto.below}, add $${r.auto.amount}` : "Auto refill off");
}

function wireCredit() {
  $("#pay").addEventListener("click", (e) => startPay(e.target));
  ["#auto-on", "#auto-below", "#auto-amount"].forEach((s) => $(s).addEventListener("change", saveAuto));
  $("#card-forget").addEventListener("click", (e) => confirmFirst(e.target, "Remove card? Auto refill stops", async () => {
    const r = await Relay.call("billing.forget_card");
    toast(r.error || "Card removed");
    refresh();
  }));
  $("#manual-go").addEventListener("click", async (e) => {
    const usd = +$("#manual-usd").value;
    busy(e.target, true);
    const r = await Relay.call("credit.add", { account: $("#manual-who").value, usd });
    busy(e.target, false);
    if (r.error) { toast(r.error); return; }
    toast(`${money(usd)} added`);
    refresh();
    loadLedger();
  });
}

/* ---------- settings: the AI model ---------- */
const OTHER = "__other__";

function renderBrain() {
  const st = S.status;
  if (!st) return;
  const provs = st.providers || [];
  $("#brain-pick").hidden = !provs.length;
  $("#brain-none").hidden = !!provs.length;
  $("#brain-now").textContent = provs.length ? `Reedus is thinking with ${st.llm.label}.` : "";
  $("#think-row").hidden = !provs.length;
  const cur = provs.find((p) => p.id === st.llm.provider);
  $("#say-search-note").textContent = cur && cur.search !== "native"
    ? `${cur.short || cur.name} can't search the web by itself, so the search phrases do nothing for now. Gemini, Claude and OpenAI can.`
    : "";
  paintThink(st.think || "fast");
  if (!provs.length) $("#think-note").textContent = "";
  if (!provs.length) return;
  const ps = $("#provider");
  if (document.activeElement === ps || document.activeElement === $("#model")) return;
  ps.innerHTML = "";
  provs.forEach((p) => { const o = el("option", "", p.name); o.value = p.id; ps.append(o); });
  ps.value = provs.some((p) => p.id === st.llm.provider) ? st.llm.provider : provs[0].id;
  renderModels(ps.value === st.llm.provider ? st.llm.model : null);
}

function paintThink(level) {
  const choices = (S.status && S.status.think_choices) || [];
  choices.forEach((c) => {
    const b = $(`#think-pick button[data-val="${c.id}"]`);
    if (b) b.textContent = c.label;
  });
  $$("#think-pick button").forEach((b) => b.classList.toggle("on", b.dataset.val === level));
  const c = choices.find((x) => x.id === level);
  $("#think-note").textContent = (c ? c.note + " " : "") +
    "For one hard question, say \u201cthink about this before you answer\u201d first.";
}

function renderModels(selected) {
  const pid = $("#provider").value;
  const p = (S.status.providers || []).find((x) => x.id === pid);
  const sel = $("#model");
  sel.innerHTML = "";
  if (!p) return;
  const list = [];
  const seen = new Set();
  [...(p.models || []), ...(S.models[pid] || [])].forEach((m) => {
    if (m.id && !seen.has(m.id)) { seen.add(m.id); list.push(m); }
  });
  const want = selected || p.default;
  if (want && !seen.has(want)) list.unshift({ id: want, label: want, note: "" });
  list.forEach((m) => {
    const think = m.thinking && m.thinking !== "unknown" ? ` · ${m.thinking_label}` : "";
    const o = el("option", "", m.label + (m.note ? ` · ${m.note}` : "") + think);
    o.value = m.id;
    sel.append(o);
  });
  const other = el("option", "", "Type any model id…");
  other.value = OTHER;
  sel.append(other);
  sel.value = want || OTHER;
  $("#model-own-row").hidden = sel.value !== OTHER;
  $("#models-more").hidden = !!S.models[pid];
  paintModelInfo();
}

// how quick the picked model is for talking: can it skip thinking, and its first-word time
// (from real replies for the one in use, or from the Test speed button)
S.probes = S.probes || {};
function pickedModel() {
  const pid = $("#provider").value;
  const model = $("#model").value === OTHER ? $("#model-own").value.trim() : $("#model").value;
  return { pid, model };
}
function paintModelInfo() {
  const { pid, model } = pickedModel();
  const out = $("#model-info");
  if (!model) { out.textContent = ""; return; }
  const p = (S.status.providers || []).find((x) => x.id === pid) || {};
  const meta = [...(p.models || []), ...(S.models[pid] || [])].find((m) => m.id === model) || {};
  const probe = S.probes[`${pid}/${model}`];
  const cur = S.status.llm || {};
  const live = cur.provider === pid && cur.model === model ? cur.info || {} : {};
  const level = (probe && probe.thinking) || live.thinking || meta.thinking || "unknown";
  const labels = { off: "No thinking before it answers: quickest.",
    minimal: "Minimal thinking before it answers: quick.",
    low: "Always thinks a little before it answers: adds a second or so.",
    medium: "Always thinks before it answers: slower.",
    always: "A reasoning model: always thinks first, slowest to start." };
  const bits = [labels[level] || "Thinking: not tested yet."];
  if (live.first_word_s) bits.push(`In your conversations its first word takes about ${live.first_word_s} s (last ${live.samples}).`);
  if (probe) bits.push(`Speed test: first word in ${probe.first_word_s} s.`);
  else if (!live.first_word_s) bits.push("Tap Test speed to find out how quick it is.");
  out.textContent = bits.join(" ");
}

function wireBrain() {
  $("#provider").addEventListener("change", () => { say("#brain-out", ""); renderModels(null); });
  $("#model-own").addEventListener("input", paintModelInfo);
  $("#model-test").addEventListener("click", async (e) => {
    const { pid, model } = pickedModel();
    if (!model) { say("#brain-out", "Pick or type a model first.", true); return; }
    busy(e.target, true);
    say("#brain-out", `Testing ${model}…`);
    const r = await Relay.call("llm.probe", { provider: pid, model });
    busy(e.target, false);
    if (r.error) { say("#brain-out", r.error, true); return; }
    S.probes[`${pid}/${model}`] = r;
    say("#brain-out", "");
    paintModelInfo();
  });
  $("#model").addEventListener("change", () => {
    paintModelInfo();
    const other = $("#model").value === OTHER;
    $("#model-own-row").hidden = !other;
    if (other) setTimeout(() => $("#model-own").focus(), 50);
  });
  $("#models-more").addEventListener("click", async (e) => {
    const pid = $("#provider").value;
    busy(e.target, true);
    const r = await Relay.call("models", { provider: pid });
    busy(e.target, false);
    if (r.error) { say("#brain-out", r.error, true); return; }
    S.models[pid] = r.models || [];
    const cur = $("#model").value;
    renderModels(cur === OTHER ? null : cur);
    say("#brain-out", r.error_note ? `Couldn't list them: ${r.error_note}` : `${S.models[pid].length} models available.`, !!r.error_note);
  });
  $$("#think-pick button").forEach((b) => b.addEventListener("click", async () => {
    const level = b.dataset.val;
    paintThink(level);
    const r = await Relay.call("think.set", { think: level });
    if (r.error) { say("#brain-out", r.error, true); paintThink((S.status && S.status.think) || "fast"); return; }
    if (S.status) S.status.think = level;
    toast(`Thinking: ${r.label}`);
  }));
  $("#brain-save").addEventListener("click", async (e) => {
    const provider = $("#provider").value;
    const model = $("#model").value === OTHER ? $("#model-own").value.trim() : $("#model").value;
    if (!model) { say("#brain-out", "Pick or type a model first.", true); return; }
    busy(e.target, true);
    const r = await Relay.call("llm.set", { provider, model });
    busy(e.target, false);
    if (r.error) { say("#brain-out", r.error, true); return; }
    say("#brain-out", `Done. Every Lite on your account now thinks with ${r.label}.`);
    refresh();
  });
}

/* ---------- settings: listening ---------- */
function renderListen() {
  const L = (S.status && S.status.listen) || null;
  if (!L) return;
  const paint = (sel, choices, cur, noteSel) => {
    choices.forEach((c) => {
      const b = $(`${sel} button[data-val="${c.id}"]`);
      if (b) b.textContent = c.label;
    });
    $$(`${sel} button`).forEach((b) => b.classList.toggle("on", b.dataset.val === cur));
    $(noteSel).textContent = (choices.find((c) => c.id === cur) || {}).note || "";
  };
  paint("#mode-pick", L.mode_choices || [], L.mode, "#mode-note");
  paint("#patience-pick", L.patience_choices || [], L.patience, "#patience-note");
  paint("#head-pick", L.head_choices || [], L.head, "#head-note");
  const cut = $("#wake-cut");
  if (document.activeElement !== cut) cut.value = Math.round((L.wake || 0.9) * 100);
  paintWake();
  // the S3 reports when it almost heard its name: how close the last one came tells you
  // which setting would have caught it
  const near = (S.status.devices || []).map((d) => d.wake_near && { ...d.wake_near, name: d.name })
    .filter(Boolean).sort((a, b) => b.at - a.at)[0];
  const needs = L.wake || 0.9;
  let txt = "";
  if (near && Date.now() / 1000 - near.at < 6 * 3600) {
    const when = new Date(near.at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const pct = Math.floor(near.p * 100);
    const min = Math.round((L.wake_min || 0.6) * 100);
    txt = `At ${when} ${near.name} almost heard his name: ${pct}% sure, and it needs ${Math.round(needs * 100)}%.` +
      (pct >= min ? ` Set it to ${pct}% or lower to catch that.`
        : ` That's under ${min}%, so it's too far or too noisy for the wake word, whatever the setting.`);
  } else {
    txt = "Tip: have someone call him from where they'd normally be. If he misses it, this shows how close it came.";
  }
  $("#wake-near").textContent = txt;
}

function paintWake() {
  const cut = $("#wake-cut");
  const v = +cut.value;
  $("#wake-cut-v").textContent = `${v}%`;
  cut.style.setProperty("--fill", `${(v - cut.min) / (cut.max - cut.min) * 100}%`);
  const def = Math.round(((S.status && S.status.listen && S.status.listen.wake_default) || 0.9) * 100);
  $("#wake-note").textContent =
    v >= 93 ? "Only when it's very sure. Fewest mistakes, but you'll need to be close." :
    v >= 86 ? `About the default (${def}%). Fine within a few metres.` :
    v >= 76 ? "Hears you from further away. Wakes by mistake now and then." :
    v >= 66 ? "Across the room. Expect the odd false wake from TV or similar words." :
    "Hears almost anything that sounds like his name. Expect false wakes.";
}

function wireListen() {
  $("#wake-cut").addEventListener("input", paintWake);
  $("#wake-cut").addEventListener("change", async (e) => {
    const r = await Relay.call("listen.set", { wake: +e.target.value / 100 });
    if (r.error) { toast(r.error); return; }
    if (S.status) S.status.listen = r.listen;
    toast(`He wakes at ${e.target.value}% sure`);
    renderListen();
  });
  const hook = (sel, key) => $$(`${sel} button`).forEach((b) => b.addEventListener("click", async () => {
    const r = await Relay.call("listen.set", { [key]: b.dataset.val });
    if (r.error) { toast(r.error); return; }
    if (S.status) S.status.listen = r.listen;
    renderListen();
  }));
  hook("#mode-pick", "mode");
  hook("#patience-pick", "patience");
  hook("#head-pick", "head");
}

/* ---------- settings: service keys (owner) ---------- */
const KEY_ROLE = {
  GEMINI_API_KEY: "thinking + hearing · recommended",
  OPENAI_API_KEY: "thinking + hearing",
  INWORLD_API_KEY: "his voice · required",
  STRIPE_SECRET_KEY: "card payments · optional",
};
const KEY_ORDER = ["GEMINI_API_KEY", "INWORLD_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"];
let openKeyName = "", keyNote = null;

function keyEntries() {
  const su = S.setup || {};
  const rows = [...(su.brains || []).map((b) => ({ ...b, role: KEY_ROLE[b.env] || "thinking" })),
                ...(su.services || []).map((s) => ({ ...s, role: KEY_ROLE[s.env] || "" }))];
  const rank = (r) => { const i = KEY_ORDER.indexOf(r.env); return r.env === "STRIPE_SECRET_KEY" ? 99 : i < 0 ? 50 : i; };
  return rows.sort((a, b) => rank(a) - rank(b));
}

function renderKeys() {
  $("#keys-card").hidden = !owner();
  if (!owner()) return;
  const box = $("#keys");
  box.innerHTML = "";
  keyEntries().forEach((k) => {
    const set = k.key && k.key.set;
    const row = el("div", "key" + (openKeyName === k.env ? " open" : ""));
    row.dataset.env = k.env;
    const head = el("button");
    head.type = "button";
    const name = el("span");
    name.append(el("b", "", k.name), el("small", "", k.role));
    const state = el("span", "state" + (set ? " set" : ""), set ? `✓ ${k.key.source === "railway" ? "on Railway" : k.key.hint}` : "not set");
    const chev = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    chev.setAttribute("class", "chev");
    chev.innerHTML = '<use href="#i-chev"/>';
    head.append(name, state, chev);
    head.addEventListener("click", () => { openKeyName = openKeyName === k.env ? "" : k.env; renderKeys(); });
    row.append(head);
    if (openKeyName === k.env) row.append(keyBody(k));
    box.append(row);
  });
}

function keyBody(k) {
  const set = k.key && k.key.set;
  const body = el("div", "key-body");
  const ol = el("ol");
  (k.steps || []).forEach((s) => ol.append(el("li", "", s)));
  body.append(ol);
  if (k.billing) body.append(el("p", "note", k.billing));
  const links = el("div", "links");
  if (k.keyUrl) { const a = el("a", "", "Get a key ↗"); a.href = k.keyUrl; a.target = "_blank"; a.rel = "noopener"; links.append(a); }
  if (k.billingUrl && k.billingUrl !== k.keyUrl) { const a = el("a", "", "Billing ↗"); a.href = k.billingUrl; a.target = "_blank"; a.rel = "noopener"; links.append(a); }
  if (links.children.length) body.append(links);
  const row = el("div", "inline");
  const input = el("input");
  input.type = "password";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.placeholder = k.keyPrefix ? `starts with ${k.keyPrefix}` : (k.env === "STRIPE_SECRET_KEY" ? "starts with sk_ or rk_" : "paste the key");
  input.style.flex = "1";
  const save = el("button", "cta", set ? "Replace" : "Save & test");
  save.type = "button";
  row.append(input, save);
  body.append(row);
  const out = el("p", "note");
  if (keyNote && keyNote.env === k.env) { out.textContent = keyNote.text; out.classList.toggle("bad", keyNote.bad); keyNote = null; }
  const more = el("div", "inline");
  if (set) {
    const test = el("button", "ghost-btn small", "Test");
    test.type = "button";
    test.addEventListener("click", async () => {
      busy(test, true);
      const r = await Relay.call("setup.test", { name: k.env });
      busy(test, false);
      out.textContent = r.error || (r.test.ok ? `Works: ${r.test.detail}.` : r.test.detail);
      out.classList.toggle("bad", !!r.error || !r.test.ok);
    });
    more.append(test);
    if (k.key.source === "app") {
      const forget = el("button", "ghost-btn small danger", "Forget");
      forget.type = "button";
      forget.addEventListener("click", () => confirmFirst(forget, "Sure?", async () => {
        await Relay.call("setup.forget", { name: k.env });
        toast(`${k.name} key forgotten`);
        await refresh();
        renderKeys();
      }));
      more.append(forget);
    }
  }
  if (more.children.length) body.append(more);
  body.append(out);
  const submit = async () => {
    const value = input.value.trim();
    if (!value) { out.textContent = "Paste the key first."; out.classList.add("bad"); input.focus(); return; }
    busy(save, true);
    out.textContent = "Testing it…";
    out.classList.remove("bad");
    const r = await Relay.call("setup.key", { name: k.env, value });
    busy(save, false);
    if (r.error) { out.textContent = r.error; out.classList.add("bad"); return; }
    if (!r.saved) { out.textContent = `Not saved: ${r.test.detail}`; out.classList.add("bad"); return; }
    const d = r.test.detail || "";
    keyNote = { env: k.env, text: `Saved. ${d.charAt(0).toUpperCase()}${d.slice(1)}.`, bad: false };
    toast(`${k.name} key saved`);
    await refresh();
    renderKeys();
  };
  save.addEventListener("click", submit);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  setTimeout(() => { if (!set) input.focus(); }, 60);
  return body;
}

function openKey(env) {
  if (!owner()) { go("settings"); return; }
  openKeyName = env;
  go("settings");
  setTimeout(() => { const r = $(`.key[data-env="${env}"]`); if (r) r.scrollIntoView({ behavior: "smooth", block: "center" }); }, 120);
}

/* ---------- settings: people, memory, this device ---------- */
function renderPeople() {
  $("#people-card").hidden = !owner();
  if (!owner()) return;
  const ul = $("#people");
  ul.innerHTML = "";
  S.members.forEach((m) => {
    const li = el("li");
    const n = el("span", "", m.name);
    if (m.owner) n.append(el("span", "owner", "owner"));
    li.append(n, el("span", "note", `${money(m.balance)} left`));
    ul.append(li);
  });
}

async function loadSettings() {
  renderBrain();
  renderListen();
  renderKeys();
  loadVoice();
  renderGlance();
  const me = S.status ? S.status.account : (Relay.me && Relay.me.account) || {};
  $("#who").textContent = `Signed in as ${me.name || "…"}${owner() ? ", the relay's owner" : ""}.`;
  if (owner()) loadMembers();
  const mem = await Relay.call("memory.get", { limit: 1 });
  const ta = $("#notes");
  if (!mem.error && document.activeElement !== ta && !ta.dataset.dirty) ta.value = mem.bulletin || "";
}

function wireSettings() {
  $("#invite").addEventListener("click", async (e) => {
    busy(e.target, true);
    const r = await Relay.call("invite.create");
    busy(e.target, false);
    if (r.error) { toast(r.error); return; }
    $("#invite-link").textContent = `${location.origin}/#join=${r.code}`;
    $("#invite-out").hidden = false;
  });
  $("#invite-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("#invite-link").textContent); toast("Invite link copied"); }
    catch { toast("Couldn't copy, select it by hand"); }
  });
  const ta = $("#notes");
  ta.addEventListener("input", () => { ta.dataset.dirty = "1"; });
  $("#notes-save").addEventListener("click", async (e) => {
    busy(e.target, true);
    const r = await Relay.call("memory.set", { bulletin: ta.value });
    busy(e.target, false);
    if (r.error) { say("#notes-out", r.error, true); return; }
    delete ta.dataset.dirty;
    say("#notes-out", "Saved. Your Lites have it now.");
  });
  $("#forget").addEventListener("click", (e) => confirmFirst(e.target, "Sure? This can't be undone", async () => {
    const r = await Relay.call("memory.forget");
    toast(r.error || "Reedus forgot everything");
    ta.value = "";
    delete ta.dataset.dirty;
    $("#thread").querySelectorAll(".msg").forEach((m) => m.remove());
    $("#chat-empty").hidden = false;
  }));
  $$("#theme button").forEach((b) => b.addEventListener("click", () => { saved.set("rl.theme", b.dataset.val); applyTheme(); }));
  $("#signout").addEventListener("click", async () => {
    await Relay.call("signout");
    Relay.signOut();
    toast("Signed out");
    S.status = null;
    S.chatLoaded = false;
    $("#thread").querySelectorAll(".msg").forEach((m) => m.remove());
    $("#chat-empty").hidden = false;
    S.info = await relayInfo();
    showGate();
  });
}

/* ---------- voice ---------- */
const V = { list: [], loaded: false, models: [], model: "inworld-tts-2" };

async function loadVoice() {
  const r = await Relay.call("voices");
  if (r.error) { say("#voice-out", r.error, true); return; }
  V.list = r.voices || [];
  const cur = r.voice || {};
  const pick = $("#voice-pick");
  pick.innerHTML = "";
  const now = cur.id || r.default || "Theodore";
  if (!V.list.some((v) => v.id === now)) V.list.unshift({ id: now, name: now, desc: "" });
  V.list.forEach((v) => {
    const o = el("option", "", v.name + (v.id === r.default ? " (default)" : ""));
    o.value = v.id;
    pick.append(o);
  });
  pick.value = now;
  $("#voice-speed").value = cur.speed || 1.0;
  V.models = r.models || [];
  V.model = cur.model || r.model_default || "inworld-tts-2";
  V.models.forEach((m) => {
    const b = $(`#voice-model button[data-val="${m.id}"]`);
    if (b) b.textContent = m.label;
  });
  paintVoice();
  const engine = (V.models.find((m) => m.id === V.model) || {}).label;
  $("#voice-now").textContent = `He's speaking as ${now}${cur.speed ? ` at ${cur.speed}x` : ""}` +
    `${engine ? ` (${engine.toLowerCase()} engine)` : ""}.` +
    (r.source === "builtin" ? " (Inworld's full list couldn't be loaded, these are the built-in voices.)" : "");
}

function paintVoice() {
  const v = V.list.find((x) => x.id === $("#voice-pick").value);
  $("#voice-desc").textContent = (v && v.desc) || "";
  const sp = +$("#voice-speed").value;
  $("#voice-speed-v").textContent = `${sp.toFixed(2)}x`;
  const r = $("#voice-speed");
  r.style.setProperty("--fill", `${(sp - r.min) / (r.max - r.min) * 100}%`);
  $$("#voice-model button").forEach((b) => b.classList.toggle("on", b.dataset.val === V.model));
  $("#voice-model-note").textContent = (V.models.find((m) => m.id === V.model) || {}).note || "";
}

function voiceReq() {
  return { voice: $("#voice-pick").value, speed: +$("#voice-speed").value, model: V.model };
}

function wireVoice() {
  $("#voice-pick").addEventListener("change", paintVoice);
  $("#voice-speed").addEventListener("input", paintVoice);
  $$("#voice-model button").forEach((b) => b.addEventListener("click", () => {
    V.model = b.dataset.val;
    paintVoice();
  }));
  let audio = null;
  $("#voice-play").addEventListener("click", async (e) => {
    busy(e.target, true);
    say("#voice-out", "");
    const r = await Relay.call("voice.preview", voiceReq());
    busy(e.target, false);
    if (r.error) { say("#voice-out", r.error, true); return; }
    if (audio) audio.pause();
    audio = new Audio(`data:audio/wav;base64,${r.wav}`);
    audio.play().catch(() => say("#voice-out", "Your browser blocked the sound. Tap again.", true));
  });
  $("#voice-save").addEventListener("click", async (e) => {
    busy(e.target, true);
    const r = await Relay.call("voice.set", voiceReq());
    busy(e.target, false);
    if (r.error) { say("#voice-out", r.error, true); return; }
    say("#voice-out", "Done. He uses it from his next sentence.");
    loadVoice();
  });
}

/* ---------- idle screen: weather ---------- */
function renderGlance() {
  const g = (S.status && S.status.glance) || {};
  $("#wx-on").checked = !!g.weather;
  if (document.activeElement !== $("#wx-place")) $("#wx-place").value = g.place || "";
  $$("#wx-unit button").forEach((b) => b.classList.toggle("on", b.dataset.val === (g.unit || "F")));
  $("#wx-every").value = String(g.every || 0);
  if (!$("#wx-every").value) $("#wx-every").value = "0";
  $("#wx-opts").hidden = !g.weather && !$("#wx-on").checked;
}

function wireGlance() {
  const save = async (btn, patch) => {
    busy(btn, true);
    say("#wx-out", patch.place ? "Looking it up…" : "");
    const r = await Relay.call("glance.set", patch);
    busy(btn, false);
    if (r.error) { say("#wx-out", r.error, true); return false; }
    if (S.status) S.status.glance = r.glance;
    const w = r.now;
    say("#wx-out", r.glance.weather
      ? (w ? `On. ${w.place}: ${w.temp}°${w.unit}, ${w.cond}. It shows after he's been idle a little while.` : "On.")
      : "Off.");
    return true;
  };
  $("#wx-on").addEventListener("change", async (e) => {
    $("#wx-opts").hidden = !e.target.checked;
    const place = $("#wx-place").value.trim();
    if (e.target.checked && !place) { $("#wx-place").focus(); say("#wx-out", "Type where you are, then Save."); return; }
    const ok = await save(null, { weather: e.target.checked });
    if (!ok) e.target.checked = !e.target.checked;
  });
  $$("#wx-unit button").forEach((b) => b.addEventListener("click", () => {
    $$("#wx-unit button").forEach((x) => x.classList.toggle("on", x === b));
  }));
  $("#wx-save").addEventListener("click", (e) => {
    const unit = ($("#wx-unit button.on") || {}).dataset?.val || "F";
    save(e.target, { weather: true, place: $("#wx-place").value.trim(), unit, every: +$("#wx-every").value })
      .then((ok) => { if (ok) $("#wx-on").checked = true; });
  });
}

/* ---------- a Lite's controls ---------- */
const EXPRS = [
  ["happy", "Happy", "😊"], ["laugh", "Laugh", "😆"], ["love", "Love", "😍"], ["excited", "Excited", "🤩"],
  ["shy", "Shy", "😳"], ["smug", "Smug", "😏"], ["curious", "Curious", "🤔"], ["surprised", "Wow", "😮"],
  ["confused", "Huh?", "😕"], ["sad", "Sad", "😢"], ["angry", "Grr", "😠"], ["sleepy", "Sleepy", "😪"],
  ["wink", "Wink", "😉"], ["content", "Content", "😌"], ["yawn", "Yawn", "🥱"], ["dizzy", "Dizzy", "😵"], ["bye", "Bye", "👋"],
];
const PALETTE = ["#8ff5e2", "#ffffff", "#ff4028", "#ff5c9e", "#ffc840", "#ff9010", "#7fe04a", "#6fa8ff", "#9b6bff", "#606f70"];
const W = 16, H = 16;

const Ctl = (() => {
  let dev = null, tab = "face";
  let frames = [new Uint8Array(W * H * 3)], cur = 0, color = PALETTE[0], tool = "pen";
  let painting = false, playTimer = null, fwId = "", lookTarget = null;
  let fwCheck = null, fwCheckAt = 0;   // which commit the latest build is from (fw.check)

  const device = () => (S.status && S.status.devices || []).find((d) => d.id === dev);
  const hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));

  function open(id) {
    dev = id;
    const d = device();
    $("#ctl-title").textContent = d ? d.name : "Lite";
    $("#ctl").hidden = false;
    $("[data-tab=fw]", $("#ctl-tabs")).hidden = !owner();
    show(tab === "fw" && !owner() ? "face" : tab);
    tick();
  }

  function show(t) {
    tab = t;
    $$("#ctl-tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
    $$(".ctl-pane").forEach((p) => { p.hidden = p.id !== `ctl-${t}`; });
    if (t === "draw") paint();
    if (t === "head") renderDial();
    if (t === "fw") { renderFw(); checkFw(); }
  }

  async function send(action, extra, okText) {
    const r = await Relay.call(action, { device: dev, ...extra });
    if (r.error) toast(r.error);
    else if (okText) toast(okText);
    return r;
  }

  /* ----- head dial (top-down; + = his left = screen left) ----- */
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
    let drag = false;
    const angleAt = (e) => {
      const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
      const p = pt.matrixTransform(svg.getScreenCTM().inverse());
      const a = Math.atan2(CX - p.x, CY - p.y) * 180 / Math.PI;
      return Math.round(Math.max(-90, Math.min(90, a)) / 5) * 5;
    };
    svg.addEventListener("pointerdown", (e) => { drag = true; svg.setPointerCapture(e.pointerId); lookTarget = angleAt(e); renderDial(); });
    svg.addEventListener("pointermove", (e) => { if (drag) { lookTarget = angleAt(e); renderDial(); } });
    svg.addEventListener("pointerup", (e) => { if (!drag) return; drag = false; look(angleAt(e)); });
    svg.addEventListener("pointercancel", () => { drag = false; });
    $("#look-center").addEventListener("click", () => look(0));
    $("#look-voice").addEventListener("click", () => {
      const d = device();
      if (d && d.doa && d.doa.angle != null) look(Math.max(-90, Math.min(90, Math.round(d.doa.angle))));
      else toast("He hasn't heard anyone lately");
    });
  }
  function headNow() {
    const d = device();
    if (lookTarget != null) return lookTarget;
    if (d && d.doa && d.doa.head != null && (!d.status || (d.doa.at || 0) * 1000 > Date.now() - 15000)) return +d.doa.head;
    return d && d.status ? +d.status.head || 0 : 0;
  }
  function renderDial() {
    const head = headNow();
    $("#dial-needle").style.transform = `rotate(${-head}deg)`;
    $("#head-read").textContent = Math.abs(head) < 3 ? "Looking straight ahead" : `Looking ${Math.abs(Math.round(head))}° to his ${head > 0 ? "left" : "right"}`;
    const d = device(), doa = $("#dial-doa");
    const fresh = d && d.doa && (d.doa.at || 0) * 1000 > Date.now() - 20000;
    if (fresh) {
      const [x, y] = toXY(Math.max(-90, Math.min(90, d.doa.angle)));
      doa.setAttribute("cx", x); doa.setAttribute("cy", y); doa.setAttribute("opacity", "1");
    } else doa.setAttribute("opacity", "0");
  }
  async function look(angle) {
    lookTarget = angle;
    renderDial();
    await send("device.look", { angle });
    setTimeout(() => { lookTarget = null; }, 12000);
  }

  /* ----- drawing ----- */
  const cv = () => $("#pix");
  function paint() {
    const c = cv(), g = c.getContext("2d"), f = frames[cur], s = c.width / W;
    g.fillStyle = "#050806"; g.fillRect(0, 0, c.width, c.height);
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      const i = (y * W + x) * 3;
      const lit = f[i] | f[i + 1] | f[i + 2];
      g.fillStyle = lit ? `rgb(${f[i]},${f[i + 1]},${f[i + 2]})` : "#141c17";
      g.beginPath();
      g.roundRect ? g.roundRect(x * s + 2, y * s + 2, s - 4, s - 4, 4) : g.rect(x * s + 2, y * s + 2, s - 4, s - 4);
      g.fill();
    }
    renderFrames();
  }
  function thumb(f) {
    const c = document.createElement("canvas"); c.width = W; c.height = H;
    const g = c.getContext("2d"), img = g.createImageData(W, H);
    for (let p = 0; p < W * H; p++) { img.data[p * 4] = f[p * 3]; img.data[p * 4 + 1] = f[p * 3 + 1]; img.data[p * 4 + 2] = f[p * 3 + 2]; img.data[p * 4 + 3] = 255; }
    g.putImageData(img, 0, 0);
    return c;
  }
  function renderFrames() {
    const box = $("#frames");
    box.innerHTML = "";
    frames.forEach((f, i) => {
      const b = el("button", "frame" + (i === cur ? " on" : ""));
      b.type = "button";
      b.title = `Frame ${i + 1}`;
      b.append(thumb(f));
      b.addEventListener("click", () => { stopPlay(); cur = i; paint(); });
      box.append(b);
    });
    $("#fr-del").disabled = frames.length < 2;
  }
  function cellAt(e) {
    const r = cv().getBoundingClientRect();
    const x = Math.floor((e.clientX - r.left) / r.width * W), y = Math.floor((e.clientY - r.top) / r.height * H);
    return x >= 0 && x < W && y >= 0 && y < H ? [x, y] : null;
  }
  function setPx(x, y, rgb) { const i = (y * W + x) * 3; frames[cur][i] = rgb[0]; frames[cur][i + 1] = rgb[1]; frames[cur][i + 2] = rgb[2]; }
  function fill(x, y, rgb) {
    const f = frames[cur], i0 = (y * W + x) * 3, from = [f[i0], f[i0 + 1], f[i0 + 2]];
    if (from.every((v, k) => v === rgb[k])) return;
    const st = [[x, y]];
    while (st.length) {
      const [a, b] = st.pop();
      if (a < 0 || a >= W || b < 0 || b >= H) continue;
      const i = (b * W + a) * 3;
      if (f[i] !== from[0] || f[i + 1] !== from[1] || f[i + 2] !== from[2]) continue;
      setPx(a, b, rgb);
      st.push([a + 1, b], [a - 1, b], [a, b + 1], [a, b - 1]);
    }
  }
  function stroke(e) {
    const c = cellAt(e);
    if (!c) return;
    if (tool === "fill") fill(c[0], c[1], hex(color));
    else setPx(c[0], c[1], tool === "erase" ? [0, 0, 0] : hex(color));
    paint();
  }
  function stopPlay() { if (playTimer) { clearInterval(playTimer); playTimer = null; $("#fr-play").innerHTML = "&#9654; Preview"; } }
  const b64 = (u8) => { let s = ""; for (let i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]); return btoa(s); };
  const unb64 = (s) => Uint8Array.from(atob(s), (ch) => ch.charCodeAt(0));

  /* animations made in code: each returns a list of frames */
  function blank() { return new Uint8Array(W * H * 3); }
  function put(f, x, y, rgb) { if (x < 0 || x >= W || y < 0 || y >= H) return; const i = (y * W + x) * 3; f[i] = rgb[0]; f[i + 1] = rgb[1]; f[i + 2] = rgb[2]; }
  const hsv = (h, s = 1, v = 1) => { const f = (n) => { const k = (n + h / 60) % 6; return Math.round(255 * (v - v * s * Math.max(0, Math.min(k, 4 - k, 1)))); }; return [f(5), f(3), f(1)]; };
  const HEART = ["..XX...XX..", ".XXXX.XXXX.", "XXXXXXXXXXX", "XXXXXXXXXXX", ".XXXXXXXXX.", "..XXXXXXX..", "...XXXXX...", "....XXX....", ".....X....."];
  const PRESETS = {
    "Heartbeat": () => [1, 0.75, 1, 0.55, 0.55, 0.55].map((v, k) => {
      const f = blank(), big = k === 0 || k === 2;
      HEART.forEach((row, y) => [...row].forEach((ch, x) => { if (ch === "X") put(f, x + 2, y + (big ? 3 : 4), [Math.round(255 * v), Math.round(60 * v), Math.round(110 * v)]); }));
      return f;
    }),
    "Rainbow": () => Array.from({ length: 16 }, (_, t) => { const f = blank(); for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) put(f, x, y, hsv(((x + y) * 14 + t * 22.5) % 360, 1, 0.9)); return f; }),
    "Spinner": () => Array.from({ length: 12 }, (_, t) => {
      const f = blank();
      for (let k = 0; k < 12; k++) { const a = (k / 12) * 2 * Math.PI, age = (t - k + 12) % 12, v = Math.max(0, 1 - age / 6); put(f, Math.round(7.5 + 5.5 * Math.cos(a)), Math.round(7.5 + 5.5 * Math.sin(a)), [Math.round(143 * v), Math.round(245 * v), Math.round(226 * v)]); }
      return f;
    }),
    "Sparkles": () => { let seed = 7; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647; const pts = Array.from({ length: 22 }, () => [Math.floor(rnd() * W), Math.floor(rnd() * H), Math.floor(rnd() * 10), hsv(rnd() * 360, 0.5, 1)]);
      return Array.from({ length: 10 }, (_, t) => { const f = blank(); pts.forEach(([x, y, ph, c]) => { const v = Math.max(0, Math.sin(((t + ph) % 10) / 10 * Math.PI)); put(f, x, y, c.map((q) => Math.round(q * v))); }); return f; }); },
    "Pac-Man": () => Array.from({ length: 8 }, (_, t) => {
      const f = blank(), open = [0, 0.25, 0.5, 0.25][t % 4];
      for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) { const dx = x - 6.5, dy = y - 7.5; if (dx * dx + dy * dy > 30) continue; const a = Math.abs(Math.atan2(dy, dx)); if (a < open * Math.PI * 0.8) continue; put(f, x, y, [255, 210, 0]); }
      for (let k = 0; k < 3; k++) { const px = 12 + k * 4 - (t % 4); if (px > 9 && px < 16) put(f, px, 7, [255, 220, 200]), put(f, px, 8, [255, 220, 200]); }
      return f;
    }),
    "Rain": () => { const drops = [[2, 0], [5, 6], [9, 3], [12, 10], [14, 1], [7, 12], [0, 8], [11, 14]];
      return Array.from({ length: 16 }, (_, t) => { const f = blank(); drops.forEach(([x, y0]) => { const y = (y0 + t) % 16; put(f, x, y, [111, 168, 255]); put(f, x, (y + 15) % 16, [40, 70, 130]); }); return f; }); },
    "Smiley": () => [0, 0, 0, 1].map((wink) => {
      const f = blank(), Y = [255, 200, 64];
      for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) { const d = Math.hypot(x - 7.5, y - 7.5); if (d < 7.6 && d > 6.4) put(f, x, y, Y); }
      put(f, 5, 5, Y); put(f, 5, 6, Y);
      if (wink) { put(f, 9, 6, Y); put(f, 10, 6, Y); put(f, 11, 6, Y); } else { put(f, 10, 5, Y); put(f, 10, 6, Y); }
      [[4, 9], [5, 10], [6, 11], [7, 11], [8, 11], [9, 11], [10, 10], [11, 9]].forEach(([x, y]) => put(f, x, y, Y));
      return f;
    }),
  };

  function savedList() { try { return JSON.parse(saved.get("rl.drawings") || "{}"); } catch { return {}; } }
  function renderSaved() {
    const sel = $("#art-saved"), list = savedList();
    sel.innerHTML = '<option value="">My drawings…</option>';
    Object.keys(list).sort().forEach((n) => { const o = el("option", "", n); o.value = n; sel.append(o); });
  }

  /* ----- firmware ----- */
  // CI builds carry their commit in the version (0.3.0-s3.6c5a42c), and fw.check says which
  // commit the latest build is from, so the two can be compared without downloading anything
  async function checkFw(force) {
    if (!force && Date.now() - fwCheckAt < 60000) return;
    fwCheckAt = Date.now();
    const r = await Relay.call("fw.check");
    fwCheck = r && !r.error && r.sha ? r : null;
    renderFw();
  }
  function upToDate(d) {
    return !!(fwCheck && d && d.fw && d.fw.endsWith("." + fwCheck.sha));
  }
  function renderFw() {
    const d = device();
    const fw = d && d.fw ? d.fw : "unknown (offline)";
    const can = d && (d.caps || []).includes("ota");
    const latest = upToDate(d);
    let note = "";
    if (d && d.online && !can) note = " This firmware can't update itself yet: flash this new version over USB once, and after that every update can come from here.";
    else if (latest) note = " That's the latest build, so he's up to date.";
    else if (fwCheck && d && d.fw) note = ` A newer build is out (${fwCheck.sha}).`;
    $("#fw-now").textContent = `Running ${fw}.` + note;
    $("#fw-latest").disabled = !can || latest;
    $("#fw-latest").textContent = latest ? "Up to date \u2713" : "Get the latest build";
    $("#fw-file").disabled = !can;
    if (latest && !fwId) { $("#fw-install").hidden = true; }
    const o = d && d.ota;
    const bar = $("#fw-bar");
    if (o && o.at && Date.now() / 1000 - o.at < 300 && o.state !== "error") {
      bar.hidden = false;
      $("i", bar).style.width = `${o.state === "done" ? 100 : (o.pct || 0)}%`;
      say("#fw-out", o.state === "sent" ? "Sent. He's starting the download…" : o.state === "downloading" ? "Downloading and installing… he'll restart when it's done." : "");
    } else if (o && o.state === "error" && Date.now() / 1000 - o.at < 600) {
      bar.hidden = true;
      say("#fw-out", `The update didn't go through: ${o.error || "unknown error"}. He's still on the old firmware.`, true);
    } else bar.hidden = true;
  }
  async function uploadFile(file) {
    const buf = new Uint8Array(await file.arrayBuffer());
    const CH = 512 * 1024, count = Math.ceil(buf.length / CH);
    let upload = "", r = null;
    for (let i = 0; i < count; i++) {
      say("#fw-ready", `Uploading… ${Math.round(i / count * 100)}%`);
      r = await Relay.call("fw.upload", { upload, index: i, count, data: b64(buf.subarray(i * CH, (i + 1) * CH)) });
      if (r.error) { say("#fw-ready", r.error, true); return; }
      upload = r.upload;
    }
    ready(r);
  }
  function ready(r) {
    const d = device();
    if (d && d.fw && r.version && d.fw === r.version) {
      fwId = "";
      $("#fw-install").hidden = true;
      say("#fw-ready", `He's already running ${r.version}. Nothing to install.`);
      return;
    }
    fwId = r.fw;
    say("#fw-ready", `Ready: ${r.version || "firmware"} (${Math.round(r.size / 1024)} KB).`);
    $("#fw-install").hidden = false;
  }

  function wire() {
    $("#ctl").addEventListener("click", (e) => { if (e.target.id === "ctl") { stopPlay(); closeSheets(); } });
    $$("#ctl-tabs button").forEach((b) => b.addEventListener("click", () => show(b.dataset.tab)));
    const grid = $("#expr-grid");
    EXPRS.forEach(([name, label, emoji]) => {
      const b = el("button", "expr");
      b.type = "button";
      b.append(el("span", "e", emoji), el("small", "", label));
      b.addEventListener("click", () => send("device.expr", { name, hold: 3 }));
      grid.append(b);
    });
    $$("#state-row button").forEach((b) => b.addEventListener("click", () => send("device.state", { state: b.dataset.state })));
    buildDial();
    // drawing
    const pal = $("#palette");
    PALETTE.forEach((c, i) => {
      const b = el("button", "sw" + (i === 0 ? " on" : ""));
      b.type = "button"; b.style.background = c; b.setAttribute("aria-label", c);
      b.addEventListener("click", () => { color = c; $$(".sw", pal).forEach((x) => x.classList.toggle("on", x === b)); if (tool === "erase") setTool("pen"); });
      pal.append(b);
    });
    $("#pix-color").addEventListener("input", (e) => { color = e.target.value; $$(".sw", pal).forEach((x) => x.classList.remove("on")); if (tool === "erase") setTool("pen"); });
    const setTool = (t) => { tool = t; $$("#tools [data-tool]").forEach((x) => x.classList.toggle("on", x.dataset.tool === t)); };
    $$("#tools [data-tool]").forEach((b) => b.addEventListener("click", () => setTool(b.dataset.tool)));
    $("#pix-clear").addEventListener("click", () => { frames[cur] = blank(); paint(); });
    const c = cv();
    c.addEventListener("pointerdown", (e) => { stopPlay(); painting = true; c.setPointerCapture(e.pointerId); stroke(e); e.preventDefault(); });
    c.addEventListener("pointermove", (e) => { if (painting && tool !== "fill") stroke(e); });
    ["pointerup", "pointercancel"].forEach((k) => c.addEventListener(k, () => { painting = false; }));
    $("#fr-add").addEventListener("click", () => { stopPlay(); if (frames.length >= 32) return toast("32 frames at most"); frames.splice(cur + 1, 0, blank()); cur++; paint(); });
    $("#fr-dup").addEventListener("click", () => { stopPlay(); if (frames.length >= 32) return toast("32 frames at most"); frames.splice(cur + 1, 0, frames[cur].slice()); cur++; paint(); });
    $("#fr-del").addEventListener("click", () => { stopPlay(); if (frames.length < 2) return; frames.splice(cur, 1); cur = Math.max(0, cur - 1); paint(); });
    const fps = $("#fr-fps");
    const paintFps = () => { $("#fr-fps-v").textContent = `${fps.value} fps`; fps.style.setProperty("--fill", `${(fps.value - 1) / 19 * 100}%`); };
    fps.addEventListener("input", () => { paintFps(); if (playTimer) { stopPlay(); $("#fr-play").click(); } });
    paintFps();
    $("#fr-play").addEventListener("click", () => {
      if (playTimer) return stopPlay();
      if (frames.length < 2) return toast("Add a second frame to animate");
      $("#fr-play").textContent = "■ Stop";
      playTimer = setInterval(() => { cur = (cur + 1) % frames.length; paint(); }, 1000 / +fps.value);
    });
    const ps = $("#art-preset");
    Object.keys(PRESETS).forEach((n) => { const o = el("option", "", n); o.value = n; ps.append(o); });
    ps.addEventListener("change", () => {
      if (!ps.value) return;
      stopPlay();
      frames = PRESETS[ps.value]();
      cur = 0;
      paint();
      ps.value = "";
    });
    $("#art-send").addEventListener("click", async (e) => {
      busy(e.target, true);
      const r = await send("device.draw", { frames: frames.map(b64), fps: +fps.value, hold: +$("#art-hold").value, loop: true });
      busy(e.target, false);
      if (!r.error) toast(frames.length > 1 ? "Playing on his face" : "On his face now");
    });
    $("#art-clear").addEventListener("click", () => send("device.draw.clear", {}, "Back to his face"));
    renderSaved();
    $("#art-save").addEventListener("click", () => {
      const name = (prompt("Name this drawing") || "").trim().slice(0, 30);
      if (!name) return;
      const list = savedList();
      list[name] = { fps: +fps.value, frames: frames.map(b64) };
      try { saved.set("rl.drawings", JSON.stringify(list)); toast("Saved on this device"); } catch { toast("Couldn't save (storage full?)"); }
      renderSaved();
      $("#art-saved").value = name;
    });
    $("#art-saved").addEventListener("change", (e) => {
      const it = savedList()[e.target.value];
      if (!it) return;
      stopPlay();
      frames = it.frames.map(unb64);
      cur = 0;
      fps.value = it.fps || 6;
      paintFps();
      paint();
    });
    $("#art-del").addEventListener("click", () => {
      const n = $("#art-saved").value;
      if (!n) return;
      const list = savedList();
      delete list[n];
      saved.set("rl.drawings", JSON.stringify(list));
      renderSaved();
    });
    // firmware
    $("#fw-latest").addEventListener("click", async (e) => {
      busy(e.target, true);
      say("#fw-ready", "Fetching the latest build…");
      const r = await Relay.call("fw.latest");
      busy(e.target, false);
      if (r.error) { say("#fw-ready", r.error, true); return; }
      ready(r);
    });
    $("#fw-file").addEventListener("change", (e) => { const f = e.target.files[0]; e.target.value = ""; if (f) uploadFile(f); });
    $("#fw-install").addEventListener("click", (e) => confirmFirst(e.target, "Install now? He'll restart", async () => {
      const r = await send("device.ota", { fw: fwId });
      if (!r.error) {
        $("#fw-install").hidden = true;
        fwId = "";
        say("#fw-ready", "");
        say("#fw-out", "Sent. He's starting the download…");
      }
    }));
  }

  function tick() {
    if ($("#ctl").hidden || !dev) return;
    const d = device();
    $("#ctl-offline").hidden = !!(d && d.online);
    if (tab === "head") renderDial();
    if (tab === "fw") renderFw();
  }

  return { open, wire, tick };
})();

/* ======================= boot ======================= */
/* the relay serves this page itself, or the page lives elsewhere (Vercel) and the
   <meta name="relay"> tag says where the relay is */
async function relayInfo() {
  const ask = async (base) => {
    try {
      const r = await fetch(`${base}/relay.json`, { cache: "no-store" });
      if (!r.ok) return null;
      const j = await r.json();
      return j && j.relay ? j : null;
    } catch { return null; }   // not a relay (or an HTML fallback page), or offline
  };
  const here = await ask("");
  if (here) { relayHost = location.host; return here; }
  const meta = ($('meta[name="relay"]') || {}).content || "";
  if (meta && meta !== location.host) {
    relayHost = meta;
    return (await ask(`https://${meta}`)) || {};
  }
  return {};
}

function poll() {
  setInterval(async () => {
    if (document.hidden || !Relay.up || $("#shell").hidden) return;
    const st = await Relay.call("status");
    if (st.error) return;
    S.status = st;
    renderTop();
    if (S.view === "home") renderLites();
    Ctl.tick();
    if (S.view === "credit") {
      $("#balance").textContent = money(st.balance);
      $("#balance-card").classList.toggle("low", !!st.low);
    }
  }, 5000);
  // quicker while a Lite's controls are open (the head dial, an update's progress)
  setInterval(async () => {
    if (document.hidden || !Relay.up || $("#ctl").hidden) return;
    const st = await Relay.call("status");
    if (st.error) return;
    S.status = st;
    Ctl.tick();
  }, 1500);
}

async function boot() {
  applyTheme();
  wireGate();
  wirePair();
  wireChat();
  wireCredit();
  wireBrain();
  wireListen();
  wireSettings();
  wireVoice();
  wireGlance();
  Ctl.wire();
  $$("[data-go]").forEach((b) => b.addEventListener("click", () => go(b.dataset.go)));
  window.addEventListener("hashchange", () => {
    const v = location.hash.replace("#", "");
    if (!$("#shell").hidden && VIEWS.includes(v) && v !== S.view) go(v, false);
  });
  Relay.on("down", () => { $("#conn").className = "conn bad"; });
  Relay.on("up", () => { $("#conn").className = "conn on"; });
  Relay.on("lost", async (why) => {
    S.lostWhy = why;
    if (!$("#shell").hidden) { S.info = await relayInfo(); showGate(why); }
  });

  S.info = await relayInfo();
  let ok = false;
  if (saved.get("rl.token") && !invite) ok = (await Relay.connect()).ok;
  $("#boot").classList.add("gone");
  setTimeout(() => { $("#boot").hidden = true; }, 400);
  if (ok) enterShell();
  else showGate(S.lostWhy || (S.info.relay ? "" : "Can't reach the relay. Check your connection and reload."));
  poll();
}

boot();
