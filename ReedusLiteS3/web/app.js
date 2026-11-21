/* Reedus Lite: the website the relay serves at /. it talks to the relay's /app socket and
   nothing else: sign in, set the relay up, pair Lites, credit, the AI model, memory, chat.
   the sign-in lives in this browser (localStorage "rl.token"); Sign out removes it. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

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
  const url = () => `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/app`;
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
    setTimeout(() => $("#g-code").focus(), 50);
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
    const code = $("#g-code").value.replace(/\D/g, "");
    if (code.length !== 6) { gateErr("The code is 6 digits."); return; }
    gateSubmit(e.submitter, { type: "redeem", code });
  });
  $("#g-owner-toggle").addEventListener("click", () => {
    $("#g-owner").hidden = !$("#g-owner").hidden;
    if (!$("#g-owner").hidden) $("#g-owner-code").focus();
  });
  $("#g-owner-go").addEventListener("click", (e) => {
    const code = $("#g-owner-code").value.trim();
    if (!code) { gateErr("Paste the setup code (RELAY_ADMIN_TOKEN on Railway)."); return; }
    gateSubmit(e.target, { type: "owner", code });
  });
  $("#g-owner-code").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#g-owner-go").click(); } });
  for (const id of ["#g-code", "#pair-code"]) {
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
  if (S.view === "settings") renderBrain();
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
  const sig = JSON.stringify(devs.map((d) => [d.id, d.name, d.online, d.state, d.battery && d.battery.pct]));
  if (box.dataset.sig === sig && box.children.length) {
    devs.forEach((d) => { const s = $(`[data-dev="${d.id}"] .lite-text small`); if (s) s.textContent = liteLine(d); });
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
        <div class="lite-menu"><button class="ghost-btn small r-name" type="button">Rename</button></div>
      </div>
      <label class="slider"><span>Volume</span><input type="range" min="0" max="100" class="r-vol"><b class="v-vol"></b></label>
      <label class="slider"><span>Brightness</span><input type="range" min="5" max="100" class="r-bri"><b class="v-bri"></b></label>
      <div class="inline r-edit" hidden><input class="r-input" maxlength="40"><button class="cta small r-save" type="button">Save</button>
        <button class="ghost-btn small danger r-remove" type="button">Remove</button></div>`;
    $("b", c).textContent = d.name;
    $("small", c).textContent = liteLine(d);
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
  if (!provs.length) return;
  const ps = $("#provider");
  if (document.activeElement === ps || document.activeElement === $("#model")) return;
  ps.innerHTML = "";
  provs.forEach((p) => { const o = el("option", "", p.name); o.value = p.id; ps.append(o); });
  ps.value = provs.some((p) => p.id === st.llm.provider) ? st.llm.provider : provs[0].id;
  renderModels(ps.value === st.llm.provider ? st.llm.model : null);
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
    const o = el("option", "", m.label + (m.note ? ` · ${m.note}` : ""));
    o.value = m.id;
    sel.append(o);
  });
  const other = el("option", "", "Type any model id…");
  other.value = OTHER;
  sel.append(other);
  sel.value = want || OTHER;
  $("#model-own-row").hidden = sel.value !== OTHER;
  $("#models-more").hidden = !!S.models[pid];
}

function wireBrain() {
  $("#provider").addEventListener("change", () => { say("#brain-out", ""); renderModels(null); });
  $("#model").addEventListener("change", () => {
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
  renderKeys();
  const me = S.status ? S.status.account : (Relay.me && Relay.me.account) || {};
  $("#who").textContent = `Signed in as ${me.name || "…"}${owner() ? ", the relay's owner" : ""}.`;
  if (owner()) loadMembers();
  const mem = await Relay.call("memory.get", { limit: 1 });
  const ta = $("#notes");
  if (!mem.error && document.activeElement !== ta && !ta.dataset.dirty) ta.value = mem.bulletin || "";
}

let loginTimer = null;
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
  $("#login-code").addEventListener("click", async (e) => {
    busy(e.target, true);
    const r = await Relay.call("login.code");
    busy(e.target, false);
    if (r.error) { toast(r.error); return; }
    $("#login-digits").textContent = `${r.code.slice(0, 3)} ${r.code.slice(3)}`;
    $("#login-out").hidden = false;
    let left = r.expires_in;
    clearInterval(loginTimer);
    const tick = () => {
      left--;
      $("#login-note").textContent = left > 0
        ? `Type this on the other device: Reedus Lite's sign-in page. Good for ${Math.ceil(left / 60)} more min, once.`
        : "Expired. Get a new one.";
      if (left <= 0) { clearInterval(loginTimer); $("#login-digits").textContent = "------"; }
    };
    tick();
    loginTimer = setInterval(tick, 1000);
  });
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

/* ======================= boot ======================= */
async function relayInfo() {
  try {
    const r = await fetch("/relay.json", { cache: "no-store" });
    if (r.ok) return await r.json();
  } catch { /* opened from a file, or offline */ }
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
    if (S.view === "credit") {
      $("#balance").textContent = money(st.balance);
      $("#balance-card").classList.toggle("low", !!st.low);
    }
  }, 5000);
}

async function boot() {
  applyTheme();
  wireGate();
  wirePair();
  wireChat();
  wireCredit();
  wireBrain();
  wireSettings();
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
