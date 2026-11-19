/* Settings → Standalone Lite: the pool, the brain, each Lite, memory and a chat box, all from
   the relay's /app API (js/relay.js). nothing here needs reedosd or the Xeon. */

const Standalone = (() => {
  const $ = (s) => document.querySelector(s);
  let status = null, poll = null, providers = [];
  let setup = null, amount = 10;
  // back from Stripe Checkout: ?topup=<session> (or =cancel), handled once we're signed in
  let returning = new URLSearchParams(window.location.search).get("topup");
  const OTHER = "__other__";

  const money = (v) => (v < 0 ? "-$" : "$") + Math.abs(v).toFixed(Math.abs(v) < 0.1 && v !== 0 ? 4 : 2);
  const ago = (t) => {
    const s = Math.max(0, Date.now() / 1000 - t);
    if (s < 90) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    const d = Math.round(s / 86400);
    return d === 1 ? "yesterday" : `${d} days ago`;
  };

  function busy(btn, on) { btn.disabled = on; btn.classList.toggle("busy", on); }
  function say(sel, text, bad) { const el = $(sel); el.textContent = text; el.style.color = bad ? "var(--red)" : ""; }

  /* ---------- signed in or not ---------- */
  function showSignedIn(on) {
    $("#sa-signin").hidden = on;
    $("#sa-live").hidden = !on;
    $("#sa-lite-card").hidden = !(on || Relay.served);
    if (!on) {
      stopPoll();
      $("#sa-note").textContent = "";
      $("#sa-url-row").hidden = Relay.served;
      $("#sa-served").hidden = !Relay.served;
      const saved = Store.app.relayUrl;
      if (saved && !$("#sa-url").value) $("#sa-url").value = saved.replace(/^wss?:\/\//, "").replace(/\/app$/, "");
      document.querySelectorAll(".sa-sub").forEach((el) => {
        el.textContent = Relay.served ? "sign in to see your pool and your Lites" : "Lite without the Xeon: sign in to your relay";
      });
    }
  }

  async function refresh(opts) {
    if (!Relay.active) {
      if (Store.app.relayToken && (await Relay.connect())) return refresh(opts);
      showSignedIn(false);
      return;
    }
    showSignedIn(true);
    if (returning) await finishTopup();
    const [st, led, mem, su] = await Promise.all([Relay.call("status"), Relay.call("ledger", { limit: 8 }),
                                                   Relay.call("memory.get", { limit: 6 }), Relay.call("setup.status")]);
    if (st.error) { say("#sa-account", st.error, true); return; }
    status = st;
    renderStatus();
    if (!led.error) renderLedger(led.ledger || []);
    if (!mem.error) renderMemory(mem);
    if (!su.error) { setup = su; Setup.render(su, { keepPanel: opts && opts.keepSetup }); renderAmounts(); renderBilling(); }
    if (App.currentView() === "standalone") startPoll();
  }

  /* ---------- top-ups (Stripe Checkout via the relay, funding.py) ---------- */
  function renderAmounts() {
    const box = $("#sa-amounts");
    const amounts = (setup && setup.amounts) || [5, 10, 20, 50];
    if (!amounts.includes(amount)) amount = amounts[1] || amounts[0];
    box.innerHTML = "";
    box.hidden = !(setup && setup.payments);
    amounts.forEach((a) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "sa-chip" + (a === amount ? " on" : "");
      b.textContent = `$${a}`;
      b.setAttribute("aria-pressed", a === amount ? "true" : "false");
      b.addEventListener("click", () => { amount = a; renderAmounts(); });
      box.append(b);
    });
    $("#sa-topup").textContent = setup && setup.payments ? `Top up $${amount}` : "Top up";
    if (setup && setup.payments && setup.stripe_mode === "test" && !$("#sa-topup-out").textContent) {
      say("#sa-topup-out", "Test mode: use card 4242 4242 4242 4242.");
    }
  }

  async function startTopup(btn) {
    if (!setup || !setup.payments) {
      say("#sa-topup-out", setup && setup.owner ? "Add a Stripe key in Setup first." : "Top-ups aren't set up yet. Ask the relay's owner.");
      return;
    }
    busy(btn, true);
    say("#sa-topup-out", "Opening checkout…");
    const save = !$("#sa-save-row").hidden && $("#sa-save-card").checked;
    const r = await Relay.call("topup.start", { usd: amount, return_to: window.location.origin + window.location.pathname,
                                                save_card: save, auto: save ? { below: 2, amount } : undefined });
    busy(btn, false);
    if (r.error) { say("#sa-topup-out", r.error, true); return; }
    if (r.demoCredited) {                    // the demo relay has no Stripe
      App.toast(`$${r.demoCredited} added (demo)`);
      say("#sa-topup-out", `Demo: $${r.demoCredited} added without a real checkout.`);
      refresh();
      return;
    }
    window.location.href = r.url;            // Stripe's page: card, Apple Pay, Google Pay
  }

  const pause = (ms) => new Promise((done) => setTimeout(done, ms));

  async function finishTopup() {
    const sid = returning;
    returning = null;
    try { history.replaceState(null, "", window.location.pathname + "#standalone"); } catch { /* */ }
    if (sid === "cancel") { App.toast("Top-up cancelled, nothing was charged"); return; }
    let r = await Relay.call("topup.confirm", { session: sid });
    // Stripe can take a moment to mark it paid after the redirect
    for (let i = 0; i < 4 && !r.error && !r.credited && !r.already && r.status !== "expired"; i++) {
      await pause(1500);
      r = await Relay.call("topup.confirm", { session: sid });
    }
    if (r.error) say("#sa-topup-out", r.error, true);
    else if (r.credited) {
      App.toast(`$${r.usd.toFixed(2)} added to the pool`);
      say("#sa-topup-out", `$${r.usd.toFixed(2)} added.` + (r.card
        ? ` ${r.card.brand} •••• ${r.card.last4} saved, auto refill on.` : ""));
    } else if (r.already) say("#sa-topup-out", "Already added.");
    else say("#sa-topup-out", "Payment pending. It'll show up within a minute.");
  }

  function renderStatus() {
    const st = status;
    $("#sa-balance").textContent = money(st.balance);
    $("#sa-pool").classList.toggle("low", !!st.low);
    $("#sa-low").hidden = !st.low;
    $("#sa-llm-now").textContent = st.llm ? st.llm.label : "-";
    const online = (st.devices || []).filter((d) => d.online).length;
    $("#sa-note").textContent = `${money(st.balance)} · ${online ? `${online} online` : "no Lite online"}`;
    const sub = `${st.account.name} · ${money(st.balance)} in the pool${online ? ` · ${online} Lite online` : ""}`;
    document.querySelectorAll(".sa-sub").forEach((el) => { el.textContent = sub; });
    $("#sa-account").textContent = `Signed in as ${st.account.name}${Relay.demo ? " (demo)" : ""} · ${
      Relay.demo ? "demo relay" : Relay.address.replace(/^wss?:\/\//, "").replace(/\/app$/, "")}`;
    renderBrain();
    renderDevices();
    renderBilling();
  }

  /* ---------- auto top-up (funding.py: saved card, off-session charges) ---------- */
  function renderBilling() {
    const b = (status && status.billing) || {};
    const payments = !!(setup && setup.payments);
    const card = b.card;
    $("#sa-save-row").hidden = !payments || !!card;
    const box = $("#sa-auto");
    box.hidden = !card;
    if (!card) return;
    $("#sa-card").textContent = `${card.brand} •••• ${card.last4}${card.exp ? `  ·  ${card.exp}` : ""}`;
    const auto = b.auto || {};
    const on = $("#sa-auto-on");
    if (document.activeElement !== on) on.checked = !!auto.enabled;
    const fill = (sel, opts, val) => {
      if (document.activeElement === sel) return;
      const want = opts.includes(+val) ? opts : [...opts, +val].sort((x, y) => x - y);
      sel.innerHTML = "";
      want.forEach((v) => { const o = document.createElement("option"); o.value = v; o.textContent = `$${v}`; sel.append(o); });
      sel.value = String(+val);
    };
    fill($("#sa-auto-below"), b.below_options || [1, 2, 5], auto.below != null ? auto.below : 2);
    fill($("#sa-auto-amount"), b.amounts || [5, 10, 20, 50], auto.amount != null ? auto.amount : 10);
    box.classList.toggle("off", !auto.enabled);
    const failed = $("#sa-auto-failed");
    failed.hidden = !b.auto_failed;
    failed.textContent = b.auto_failed || "";
    const lim = b.limits || { gap_min: 10, per_day: 3 };
    $("#sa-auto-note").textContent = auto.enabled ? `At most once per ${lim.gap_min} min, ${lim.per_day} times a day.` : "Off. Card stays saved.";
  }

  async function saveAuto(patch) {
    const b = (status && status.billing) || {};
    const req = { enabled: $("#sa-auto-on").checked, below: +$("#sa-auto-below").value,
                  amount: +$("#sa-auto-amount").value, ...patch };
    const r = await Relay.call("billing.set", req);
    if (r.error) {
      App.toast(r.error);
      $("#sa-auto-on").checked = !!(b.auto && b.auto.enabled);
      return;
    }
    status.billing = { ...b, card: r.card, auto: r.auto, auto_failed: r.auto_failed };
    renderBilling();
    App.toast(r.auto.enabled ? `Auto refill on: under $${r.auto.below}, add $${r.auto.amount}` : "Auto refill off");
  }

  function renderLedger(rows) {
    const ul = $("#sa-ledger");
    ul.innerHTML = "";
    if (!rows.length) { ul.innerHTML = `<li class="sa-empty">Nothing yet.</li>`; return; }
    rows.slice(0, 8).forEach((r) => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="sa-what"></span><span class="sa-amt"></span>`;
      li.querySelector(".sa-what").textContent = `${ago(r.at)} · ${r.what}`;
      const amt = li.querySelector(".sa-amt");
      amt.textContent = (r.usd > 0 ? "+" : "") + money(r.usd);
      amt.classList.toggle("in", r.usd > 0);
      ul.append(li);
    });
  }

  /* ---------- brain ---------- */
  function renderBrain() {
    providers = status.providers || [];
    const ps = $("#sa-provider");
    const cur = status.llm || {};
    if (document.activeElement === ps || document.activeElement === $("#sa-model")) return;
    ps.innerHTML = "";
    providers.forEach((p) => {
      const o = document.createElement("option");
      o.value = p.id; o.textContent = p.name;
      ps.append(o);
    });
    if (!providers.length) {
      const o = document.createElement("option");
      o.textContent = "No provider keys on the relay yet";
      ps.append(o);
      ps.disabled = true;
    } else {
      ps.disabled = false;
      ps.value = providers.some((p) => p.id === cur.provider) ? cur.provider : providers[0].id;
    }
    renderModels(cur.provider === ps.value ? cur.model : null);
  }

  function renderModels(selected) {
    const p = providers.find((x) => x.id === $("#sa-provider").value);
    const sel = $("#sa-model");
    sel.innerHTML = "";
    if (!p) return;
    const list = [...(p.models || [])];
    const want = selected || p.default;
    if (want && !list.some((m) => m.id === want)) list.unshift({ id: want, label: want, note: "" });
    list.forEach((m) => {
      const o = document.createElement("option");
      o.value = m.id;
      o.textContent = m.label + (m.note ? ` · ${m.note}` : "");
      sel.append(o);
    });
    const other = document.createElement("option");
    other.value = OTHER; other.textContent = "Other (type a model id)…";
    sel.append(other);
    sel.value = want || OTHER;
    $("#sa-model-other-row").hidden = sel.value !== OTHER;
  }

  /* ---------- devices ---------- */
  function renderDevices() {
    const box = $("#sa-devices");
    const devs = status.devices || [];
    if (box.dataset.sig === JSON.stringify(devs.map((d) => [d.id, d.online, d.state, d.battery && d.battery.pct]))
        && box.children.length) return;           // don't rebuild under a moving slider
    box.dataset.sig = JSON.stringify(devs.map((d) => [d.id, d.online, d.state, d.battery && d.battery.pct]));
    box.innerHTML = "";
    if (!devs.length) {
      box.innerHTML = `<div class="card"><p class="hint">No Lite yet. Add one in Setup.</p></div>`;
      return;
    }
    devs.forEach((d, i) => {
      const card = document.createElement("div");
      card.className = "card sa-dev" + (d.online ? " on" : "");
      card.style.animationDelay = i * 40 + "ms";
      const s = d.settings || {};
      card.innerHTML = `
        <div class="sa-dev-head"><i class="hub-dot"></i><div class="hub-text"><b></b><small></small></div></div>
        <label class="field"><span>Volume <b class="v-vol"></b></span><input type="range" min="0" max="100" class="r-vol"></label>
        <label class="field"><span>Face brightness <b class="v-bri"></b></span><input type="range" min="0" max="100" class="r-bri"></label>`;
      card.querySelector("b").textContent = d.name;
      const bits = [];
      if (d.online) bits.push(d.state === "idle" ? "online" : `online, ${d.state}`);
      else bits.push(d.last_seen ? `offline, last seen ${ago(d.last_seen)}` : "never connected");
      if (d.battery && d.battery.pct != null) bits.push(`battery ${d.battery.pct}%`);
      card.querySelector("small").textContent = bits.join(" · ");
      const wire = (cls, key, label) => {
        const r = card.querySelector(cls), v = card.querySelector(label);
        r.value = s[key] != null ? s[key] : (key === "volume" ? 70 : 100);
        v.textContent = r.value + "%";
        r.style.setProperty("--fill", r.value + "%");
        r.addEventListener("input", () => { v.textContent = r.value + "%"; r.style.setProperty("--fill", r.value + "%"); });
        r.addEventListener("change", async () => {
          const out = await Relay.call("device.set", { device: d.id, [key]: +r.value });
          if (out.error) App.toast(out.error);
          else { s[key] = +r.value; App.toast(`${d.name}: ${key} ${r.value}%${d.online ? "" : " (applies when it connects)"}`); }
        });
      };
      wire(".r-vol", "volume", ".v-vol");
      wire(".r-bri", "brightness", ".v-bri");
      box.append(card);
    });
  }

  /* ---------- memory ---------- */
  function renderMemory(mem) {
    const ta = $("#sa-bulletin");
    if (document.activeElement !== ta && !ta.dataset.dirty) ta.value = mem.bulletin || "";
    const ul = $("#sa-recent");
    ul.innerHTML = "";
    const rows = mem.recent || [];
    if (!rows.length) { ul.innerHTML = `<li class="sa-empty">No conversations yet.</li>`; return; }
    rows.slice(0, 6).forEach((r) => {
      const li = document.createElement("li");
      li.innerHTML = `<small></small><p class="you"></p><p class="him"></p>`;
      li.querySelector("small").textContent = ago(r.at);
      li.querySelector(".you").textContent = r.said;
      li.querySelector(".him").textContent = r.reply;
      ul.append(li);
    });
  }

  /* ---------- polling while the page is open ---------- */
  function startPoll() {
    if (poll) return;
    poll = setInterval(async () => {
      if (App.currentView() !== "standalone" || document.hidden || !Relay.active) { stopPoll(); return; }
      const st = await Relay.call("status");
      if (!st.error) { status = st; renderStatus(); }
    }, 5000);
  }
  function stopPoll() { if (poll) { clearInterval(poll); poll = null; } }

  /* ---------- wiring ---------- */
  function wire() {
    $("#sa-go").addEventListener("click", async (e) => {
      busy(e.target, true);
      say("#sa-signin-out", "Signing in…");
      const out = await Relay.signIn(Relay.served ? "" : $("#sa-url").value, $("#sa-token").value);
      busy(e.target, false);
      if (out.error) { say("#sa-signin-out", out.error, true); return; }
      $("#sa-token").value = "";
      App.toast(`Signed in as ${out.account.name}`);
      refresh();
    });
    $("#sa-token").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#sa-go").click(); });
    $("#sa-demo").addEventListener("click", () => { Relay.startDemo(); refresh(); });
    $("#sa-signout").addEventListener("click", () => {
      Relay.signOut();
      status = null;
      App.toast("Signed out of the relay");
      showSignedIn(false);
    });

    $("#sa-topup").addEventListener("click", (e) => startTopup(e.target));
    $("#sa-auto-on").addEventListener("change", () => saveAuto({}));
    $("#sa-auto-below").addEventListener("change", () => saveAuto({}));
    $("#sa-auto-amount").addEventListener("change", () => saveAuto({}));
    $("#sa-forget-card").addEventListener("click", async (e) => {
      const btn = e.target;
      if (!btn.classList.contains("confirm")) {
        btn.classList.add("confirm"); btn.textContent = "Remove? Auto refill stops";
        setTimeout(() => { btn.classList.remove("confirm"); btn.textContent = "Remove card"; }, 3000);
        return;
      }
      btn.classList.remove("confirm"); btn.textContent = "Remove card";
      const r = await Relay.call("billing.forget_card");
      if (r.error) { App.toast(r.error); return; }
      App.toast("Card removed");
      refresh();
    });

    $("#sa-provider").addEventListener("change", () => renderModels(null));
    $("#sa-model").addEventListener("change", () => {
      const other = $("#sa-model").value === OTHER;
      $("#sa-model-other-row").hidden = !other;
      if (other) setTimeout(() => $("#sa-model-other").focus(), 50);
    });
    $("#sa-llm-save").addEventListener("click", async (e) => {
      const provider = $("#sa-provider").value;
      const model = $("#sa-model").value === OTHER ? $("#sa-model-other").value.trim() : $("#sa-model").value;
      if (!model) { say("#sa-llm-out", "Pick or type a model first.", true); return; }
      busy(e.target, true);
      const out = await Relay.call("llm.set", { provider, model });
      busy(e.target, false);
      if (out.error) { say("#sa-llm-out", out.error, true); return; }
      say("#sa-llm-out", `Now thinking with ${out.label}.`);
      App.toast(`Brain: ${out.label}`);
      refresh();
    });

    const ta = $("#sa-bulletin");
    ta.addEventListener("input", () => { ta.dataset.dirty = "1"; });
    $("#sa-bulletin-save").addEventListener("click", async (e) => {
      busy(e.target, true);
      const out = await Relay.call("memory.set", { bulletin: ta.value });
      busy(e.target, false);
      if (out.error) { say("#sa-bulletin-out", out.error, true); return; }
      delete ta.dataset.dirty;
      say("#sa-bulletin-out", "Saved.");
      refresh();
    });
    $("#sa-forget").addEventListener("click", async (e) => {
      const b = e.target;
      if (!b.classList.contains("confirm")) {
        b.classList.add("confirm"); b.textContent = "Sure? Can't undo";
        setTimeout(() => { b.classList.remove("confirm"); b.textContent = "Forget everything"; }, 3000);
        return;
      }
      b.classList.remove("confirm"); b.textContent = "Forget everything";
      const out = await Relay.call("memory.forget");
      App.toast(out.error ? out.error : "Reedus forgot everything");
      delete ta.dataset.dirty;
      refresh();
    });

    $("#sa-chat").addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = $("#sa-chat-text"), text = input.value.trim();
      if (!text) return;
      const out = $("#sa-chat-out");
      out.hidden = false;
      out.textContent = "…";
      input.value = "";
      const r = await Relay.chat(text);
      out.textContent = r.error ? r.error : r.text;
      out.classList.toggle("bad", !!r.error);
      if (!r.error) refresh();
    });
  }

  function init() {
    wire();
    showSignedIn(false);
    Relay.on("auth", (a) => {
      if (!a.ok) {
        showSignedIn(false);
        if (a.error) say("#sa-signin-out", a.error, true);
      } else if (App.currentView() === "standalone") refresh();
      else showSignedIn(true);
    });
    Relay.on("detect", async ({ served }) => {
      showSignedIn(Relay.active);
      // opened from the relay with the Xeon off (or back from Stripe): this is the page that matters
      if ((served && !Store.app.relayToken && !location.hash) || returning) App.navigate("standalone");
      else if (Relay.active || Store.app.relayToken) refresh();
    });
    Relay.detect();
  }

  return { init, refresh };
})();
