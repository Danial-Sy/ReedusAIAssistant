/* the standalone Reedus Lite's cloud relay (ReedusLiteS3/relay): sign in with an account token
   and talk to its /app socket. this is how ReedOS reaches Lite with the Xeon off. when the page
   itself was served by the relay (https://<relay>/), it already knows where the relay is.
   the token lives in this browser's storage; "Sign out" removes it. */

const Relay = (() => {
  const bus = new EventTarget();
  let ws = null, authed = false, account = null, admin = false;
  let served = false, demo = false, connecting = null, lastError = "";
  let nextId = 1;
  const pending = new Map();

  const emit = (t, d) => bus.dispatchEvent(new CustomEvent(t, { detail: d }));
  const on = (t, fn) => bus.addEventListener(t, (e) => fn(e.detail));

  /* "reedus.up.railway.app", "https://x/", "ws://192.168.1.5:8080" -> wss://x/app */
  function normalize(raw) {
    let v = String(raw || "").trim();
    if (!v) return "";
    if (/^https?:\/\//i.test(v)) v = v.replace(/^http/i, "ws");
    if (!/^wss?:\/\//i.test(v)) {
      const local = /^(localhost|127\.|192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)/.test(v);
      v = (local ? "ws://" : "wss://") + v;
    }
    try {
      const u = new URL(v);
      u.pathname = "/app";
      u.search = "";
      u.hash = "";
      return u.toString();
    } catch {
      return "";
    }
  }

  function sameOrigin() {
    const l = window.location;
    return `${l.protocol === "https:" ? "wss" : "ws"}://${l.host}/app`;
  }

  function url() { return Store.app.relayUrl || (served ? sameOrigin() : ""); }

  /* was this page served by a relay? then sign-in only needs the token */
  async function detect() {
    try {
      const r = await fetch("/relay.json", { cache: "no-store" });
      if (r.ok) served = !!(await r.json()).relay;
    } catch { /* not a relay, or file:// */ }
    emit("detect", { served });
    if (Store.app.relayToken && url()) connect();
    return served;
  }

  function connect() {
    if (demo) return Promise.resolve(true);
    if (authed) return Promise.resolve(true);
    if (connecting) return connecting;
    const u = url(), token = Store.app.relayToken;
    if (!u || !token) return Promise.resolve(false);
    connecting = new Promise((resolve) => {
      let sock, settled = false;
      const done = (ok) => { if (!settled) { settled = true; connecting = null; resolve(ok); } };
      try { sock = new WebSocket(u); } catch { lastError = "that relay address isn't valid"; done(false); return; }
      ws = sock;
      const timer = setTimeout(() => { lastError = "the relay didn't answer"; try { sock.close(); } catch { /* */ } }, 8000);
      sock.onopen = () => sock.send(JSON.stringify({ type: "auth", token }));
      sock.onmessage = (ev) => {
        let m;
        try { m = JSON.parse(ev.data); } catch { return; }
        if (m.type === "auth.ok") {
          clearTimeout(timer);
          authed = true; account = m.account; admin = !!m.admin; lastError = "";
          emit("auth", { ok: true, account, admin });
          done(true);
          return;
        }
        if (m.type === "auth.fail") {
          clearTimeout(timer);
          // a saved token that stopped working (revoked, or a new relay database): forget it
          // rather than retry it on every load
          lastError = Store.app.relayToken === token && token
            ? "the relay refused the saved sign-in (was the token replaced?); sign in again"
            : "the relay doesn't know that token";
          Store.setApp("relayToken", "");
          emit("auth", { ok: false, error: lastError });
          return;
        }
        const r = pending.get(m.id);
        if (r) { pending.delete(m.id); r(m); }
      };
      sock.onerror = () => { if (!lastError) lastError = "can't reach the relay at that address"; };
      sock.onclose = () => {
        clearTimeout(timer);
        const was = authed;
        authed = false;
        if (ws === sock) ws = null;
        for (const [, r] of pending) r({ error: "the relay connection dropped" });
        pending.clear();
        done(false);
        emit("down", { was });
        if (was && Store.app.relayToken && !demo) setTimeout(connect, 3000);
      };
    });
    return connecting;
  }

  async function call(action, extra) {
    if (demo) return new Promise((res) => setTimeout(() => res(mockCall(action, extra || {})), action === "chat" ? 800 : 120));
    if (!authed && !(await connect())) return { error: lastError || "can't reach the relay" };
    return new Promise((resolve) => {
      const id = nextId++;
      pending.set(id, resolve);
      try {
        ws.send(JSON.stringify({ id, action, ...(extra || {}) }));
      } catch {
        pending.delete(id);
        resolve({ error: "the relay connection dropped" });
        return;
      }
      setTimeout(() => {
        if (pending.has(id)) { pending.delete(id); resolve({ error: "the relay took too long to answer" }); }
      }, 60000);
    });
  }

  async function signIn(rawUrl, token) {
    const u = rawUrl ? normalize(rawUrl) : (served ? sameOrigin() : "");
    if (!u) return { error: "type your relay's address" };
    if (!String(token || "").trim()) return { error: "paste your app token" };
    signOut(true);
    lastError = "";
    Store.setApp("relayUrl", rawUrl ? u : "");
    Store.setApp("relayToken", String(token).trim());
    if (await connect()) return { ok: true, account };
    Store.setApp("relayToken", "");
    return { error: lastError || "couldn't sign in: check the address and the token" };
  }

  function signOut(quiet) {
    Store.setApp("relayToken", "");
    demo = false;
    authed = false;
    account = null;
    if (ws) { try { ws.close(); } catch { /* */ } }
    ws = null;
    if (!quiet) emit("auth", { ok: false });
  }

  function startDemo() {
    demo = true;
    authed = true;
    account = { id: "demo", name: "Demo", balance: mock.balance, spent: 1.58 };
    emit("auth", { ok: true, account, demo: true });
  }

  /* ---------- demo: a believable relay so the page can be explored ---------- */
  const mock = {
    balance: 8.42,
    keys: { GEMINI_API_KEY: "AIza…demo" },
    billing: { card: null, auto: { enabled: false, below: 2, amount: 10 }, auto_failed: "",
               amounts: [5, 10, 20, 50], below_options: [1, 2, 5], limits: { gap_min: 10, per_day: 3 } },
    llm: { provider: "gemini", model: "gemini-3.7-flash", label: "Gemini 3.7 Flash" },
    devices: [{ id: "d1", name: "Desk Lite", online: true, state: "idle", settings: { volume: 70, brightness: 100 },
                last_seen: Date.now() / 1000, battery: { pct: 82, volts: 3.98 }, status: { rssi: -54 } }],
    bulletin: "- Their name is Danial.\n- Physics exam on Friday, studying optics first.\n- Likes Daft Punk and chess.",
    rev: 3,
    recent: [
      { at: Date.now() / 1000 - 3600, said: "what should I study first for physics", reply: "Start with optics, it's the chunk you said felt shakiest. Twenty minutes, then quiz me." },
      { at: Date.now() / 1000 - 90000, said: "remember I like Daft Punk", reply: "Noted. Discovery on repeat, I assume?" },
    ],
    ledger: [
      { at: Date.now() / 1000 - 3600, usd: -0.0071, what: "Desk Lite: inworld-tts-2 $0.0045, gemini-3.7-flash $0.0026" },
      { at: Date.now() / 1000 - 7200, usd: -0.0064, what: "Desk Lite: inworld-tts-2 $0.0039, gemini-3.7-flash $0.0025" },
      { at: Date.now() / 1000 - 86400 * 3, usd: 10, what: "opening balance" },
    ],
    providers: [
      { id: "gemini", name: "Google Gemini", short: "Gemini", default: "gemini-3.7-flash", env: "GEMINI_API_KEY",
        keyPrefix: "AIza", blurb: "The default. Fast, cheap, and answers live questions with Google Search.",
        keyUrl: "https://aistudio.google.com/apikey", billingUrl: "https://aistudio.google.com/usage",
        steps: ["Open aistudio.google.com/apikey and sign in.", "Create API key (starts with AIza).",
                "Next to it, Set up billing and add a card.", "Paste it below and press Save & test."],
        billing: "Pay-as-you-go on Google Cloud.",
        models: [{ id: "gemini-3.7-flash", label: "Gemini 3.7 Flash", note: "default, fast and cheap" },
                 { id: "gemini-3.1-pro-preview", label: "Gemini 3.1 Pro (preview)", note: "smarter, slower" }] },
      { id: "anthropic", name: "Anthropic Claude", short: "Claude", default: "claude-sonnet-5-5", env: "ANTHROPIC_API_KEY",
        keyPrefix: "sk-ant-", blurb: "Claude Sonnet, Opus or Haiku. Searches the web itself when it needs to.",
        keyUrl: "https://console.anthropic.com/settings/keys", billingUrl: "https://console.anthropic.com/settings/billing",
        steps: ["Open console.anthropic.com and sign up.", "Settings → Billing → buy credits ($5 minimum).",
                "Settings → API keys → Create key (starts with sk-ant-).", "Paste it below and press Save & test."],
        billing: "Prepaid credits.",
        models: [{ id: "claude-sonnet-5-5", label: "Claude Sonnet 5.5", note: "fast, very capable" },
                 { id: "claude-opus-5-5", label: "Claude Opus 5.5", note: "smartest Opus, slower" },
                 { id: "claude-haiku-5-5", label: "Claude Haiku 5.5", note: "quickest and cheapest" }] },
      { id: "openai", name: "OpenAI", short: "OpenAI", default: "gpt-5-mini", env: "OPENAI_API_KEY",
        keyPrefix: "sk-", blurb: "GPT models, with OpenAI's own web search.",
        keyUrl: "https://platform.openai.com/api-keys",
        billingUrl: "https://platform.openai.com/settings/organization/billing/overview",
        steps: ["Sign in at platform.openai.com (separate from ChatGPT).",
                "Billing: add credit ($5 min). ChatGPT Plus doesn't include API use.",
                "API keys: create one (starts with sk-). It's shown once.", "Paste it below."],
        billing: "Prepaid credits.",
        models: [{ id: "gpt-5-mini", label: "GPT-5 mini", note: "fast and cheap" },
                 { id: "gpt-5.1", label: "GPT-5.1", note: "flagship" }] },
    ],
  };

  function mockCall(action, req) {
    switch (action) {
      case "status":
        return { account: { ...account, balance: mock.balance }, balance: mock.balance, low: mock.balance < 1,
                 billing: mock.billing,
                 devices: mock.devices, llm: mock.llm, providers: mock.providers, bulletin_rev: mock.rev, stt: "gemini" };
      case "llm.set": {
        const p = mock.providers.find((x) => x.id === req.provider);
        if (!p) return { error: "this relay has no key for that provider" };
        const m = p.models.find((x) => x.id === req.model);
        mock.llm = { provider: p.id, model: req.model, label: m ? m.label : req.model };
        return { ok: true, ...mock.llm };
      }
      case "memory.get": return { bulletin: mock.bulletin, rev: mock.rev, recent: mock.recent };
      case "memory.set": mock.bulletin = req.bulletin; mock.rev++; return { ok: true, rev: mock.rev };
      case "memory.forget": mock.bulletin = ""; mock.recent = []; mock.rev++; return { ok: true };
      case "device.set": {
        const d = mock.devices.find((x) => x.id === req.device);
        if (!d) return { error: "no such device" };
        for (const k of ["volume", "brightness"]) if (k in req) d.settings[k] = req[k];
        return { ok: true };
      }
      case "ledger": return { ledger: mock.ledger, balance: mock.balance };
      case "chat": {
        const usd = 0.0068;
        mock.balance -= usd;
        mock.ledger.unshift({ at: Date.now() / 1000, usd: -usd, what: "ReedOS chat (demo)" });
        mock.recent.unshift({ at: Date.now() / 1000, said: req.text, reply: "This is the demo relay, so I'm only pretending, but in real life I'd answer that." });
        return { text: "This is the demo relay, so I'm only pretending, but in real life I'd answer that.", usd };
      }
      case "setup.status": {
        const k = mock.keys;
        const brains = mock.providers.map((p) => ({ ...p, env: p.env, keyPrefix: p.keyPrefix,
          key: k[p.env] ? { set: true, hint: k[p.env], source: "app" } : { set: false, hint: "", source: "" } }));
        const svc = (env, extra) => ({ ...extra, env, key: k[env] ? { set: true, hint: k[env], source: "app" } : { set: false } });
        const set = (env) => !!k[env];
        return {
          owner: true, payments: set("STRIPE_SECRET_KEY"), stripe_mode: set("STRIPE_SECRET_KEY") ? "test" : "",
          amounts: [5, 10, 20, 50], public_url: "https://your-relay.up.railway.app",
          ready: brains.some((b) => b.key.set) && set("INWORLD_API_KEY"),
          items: [
            { id: "brain", title: "Brain", done: brains.some((b) => b.key.set), detail: brains.some((b) => b.key.set) ? mock.llm.label : "add a key" },
            { id: "voice", title: "Voice", done: set("INWORLD_API_KEY"), detail: set("INWORLD_API_KEY") ? "Inworld" : "add a key" },
            { id: "lite", title: "Lite", done: true, detail: `${mock.devices.length} added, 1 online` },
            { id: "wake", title: "Wake word", done: true, detail: "ready" },
            { id: "payments", title: "Top-ups", done: set("STRIPE_SECRET_KEY"), optional: true, detail: set("STRIPE_SECRET_KEY") ? "Stripe (test mode)" : "add a Stripe key" },
            { id: "pool", title: "Pool", done: mock.balance > 0, detail: `$${mock.balance.toFixed(2)}` },
          ],
          brains,
          services: [
            svc("INWORLD_API_KEY", { id: "inworld", name: "Inworld", keyUrl: "https://platform.inworld.ai/", billingUrl: "https://platform.inworld.ai/",
              steps: ["Open platform.inworld.ai and sign up.", "Billing: add a card.", "API Keys → create a key, copy the Base64 version.", "Paste it below and press Save & test."],
              billing: "Pay-as-you-go on Inworld." }),
            svc("STRIPE_SECRET_KEY", { id: "stripe", name: "Stripe", keyUrl: "https://dashboard.stripe.com/apikeys", billingUrl: "https://dashboard.stripe.com/settings/payouts",
              steps: ["Open dashboard.stripe.com and sign up.", "Developers → API keys → create a restricted key with Checkout Sessions = Write.", "Paste it below and press Save & test."],
              billing: "Stripe takes its fee per payment; the full amount goes into the pool." }),
          ],
        };
      }
      case "setup.key": {
        if ((req.value || "").length < 8) return { ok: false, saved: false, test: { ok: false, detail: "that key is too short to be real" } };
        mock.keys[req.name] = req.value.slice(0, 4) + "…" + req.value.slice(-4);
        return { ok: true, saved: true, test: { ok: true, detail: "it answered \"Ready.\" in 410 ms (demo)" }, set: true, hint: mock.keys[req.name], source: "app" };
      }
      case "setup.test": return { ok: true, test: { ok: !!mock.keys[req.name], detail: mock.keys[req.name] ? "it works (demo)" : "no key saved" } };
      case "setup.forget": delete mock.keys[req.name]; return { ok: true };
      case "device.create": {
        const d = { id: "d" + (mock.devices.length + 1), name: req.name, online: false, state: null, settings: { volume: 70, brightness: 100 }, last_seen: 0 };
        mock.devices.push(d);
        const token = "rld_demo_" + Math.random().toString(36).slice(2, 10);
        return { device: d, token, secrets_h: `// ReedusLiteS3/firmware/src/secrets.h\n#pragma once\n\n#define WIFI_SSID "your-network-name"   // 2.4 GHz\n#define WIFI_PASS "your-password"\n\n#define RELAY_HOST "${req.host || "your-relay.up.railway.app"}"\n#define RELAY_PORT 443\n#define RELAY_TLS 1\n\n#define LITE_TOKEN "${token}"\n` };
      }
      case "topup.start": {
        // the demo skips Stripe: pretend the payment went through
        mock.balance += req.usd;
        if (req.save_card) {
          mock.billing.card = { brand: "Visa", last4: "4242", exp: "12/30" };
          mock.billing.auto = { enabled: true, below: (req.auto && req.auto.below) || 2, amount: (req.auto && req.auto.amount) || req.usd };
        }
        mock.ledger.unshift({ at: Date.now() / 1000, usd: req.usd, what: "Stripe top-up (demo)" });
        return { demoCredited: req.usd, balance: mock.balance };
      }
      case "topup.confirm": return { error: "demo" };
      case "billing.get": return mock.billing;
      case "billing.set": {
        if (req.enabled && !mock.billing.card) return { error: "save a card first: tick \"save my card\" on your next top-up" };
        mock.billing.auto = { enabled: !!req.enabled, below: req.below ?? mock.billing.auto.below, amount: req.amount ?? mock.billing.auto.amount };
        mock.billing.auto_failed = "";
        return { ok: true, ...mock.billing };
      }
      case "billing.forget_card":
        mock.billing.card = null; mock.billing.auto.enabled = false;
        return { ok: true, ...mock.billing };
      case "topup": return { error: "use topup.start" };
      default: return { error: "unknown action" };
    }
  }

  return {
    detect, connect, call, signIn, signOut, startDemo, normalize, on,
    chat: (text) => call("chat", { text }),
    get active() { return authed; },
    get account() { return account; },
    get admin() { return admin; },
    get served() { return served; },
    get demo() { return demo; },
    get address() { return url(); },
  };
})();
