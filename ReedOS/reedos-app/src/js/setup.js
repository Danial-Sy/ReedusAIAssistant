/* Standalone Lite → Setup: a checklist of what Lite needs (a brain, his voice, a Lite, the wake
   word, top-ups, money in the pool), and for each one exactly how to get it done: which key,
   where to get it, that the account needs money on it, paste, test, save. keys go to the relay
   (vault.py) and only come back as "sk-a…good". only the relay's owner can change them. */

const Setup = (() => {
  const $ = (s) => document.querySelector(s);
  let st = null, open = null, brainPick = null;
  let note = null;          // {env, text, bad}: survives the re-render a save triggers

  const ICON = { done: "✓", todo: "", optional: "·" };

  function render(status, opts) {
    st = status;
    const keepPanel = opts && opts.keepPanel;
    const list = $("#sa-check");
    list.innerHTML = "";
    const todo = st.items.filter((i) => !i.done && !i.optional).length;
    const state = $("#sa-setup-state");
    state.className = "llm-state" + (todo ? " warn" : "");
    state.textContent = todo ? `${todo} to do` : "All set";
    $("#sa-setup-lead").textContent = st.owner ? "" : "Only the relay's owner can change these.";
    $("#sa-setup").classList.toggle("done", !todo);
    st.items.forEach((it) => {
      const li = document.createElement("li");
      li.className = (it.done ? "done" : it.optional ? "optional" : "todo") + (open === it.id ? " open" : "");
      li.innerHTML = `<button type="button"><i></i><span><b></b><small></small></span><svg class="t-chev"><use href="#i-chev"/></svg></button>`;
      li.querySelector("i").textContent = it.done ? ICON.done : it.optional ? ICON.optional : ICON.todo;
      li.querySelector("b").textContent = it.title + (it.optional ? " (optional)" : "");
      li.querySelector("small").textContent = it.detail;
      li.querySelector("button").addEventListener("click", () => {
        open = open === it.id ? null : it.id;
        render(st);
      });
      list.append(li);
    });
    const panel = $("#sa-panel");
    panel.hidden = !open;
    if (open && !keepPanel) {
      panel.innerHTML = "";
      panel.classList.remove("rise"); void panel.offsetWidth; panel.classList.add("rise");
      ({ brain: brainPanel, voice: servicePanel("INWORLD_API_KEY"), payments: servicePanel("STRIPE_SECRET_KEY"),
         lite: litePanel, wake: wakePanel, pool: poolPanel })[open](panel);
    }
  }

  /* ---------- a key: how to get it, paste, test, save ---------- */
  function keyBlock(panel, o) {
    // o: {env, name, steps, billing, keyUrl, billingUrl, key: {set, hint, source}, moneyNote, keyPrefix, after}
    const box = document.createElement("div");
    box.className = "sa-key";
    box.innerHTML = `
      <details class="llm-howto"><summary></summary><ol class="steps"></ol><p class="llm-bill"></p>
        <div class="row-btns"><a class="ghost-btn small k-url" target="_blank" rel="noopener">Open the key page</a>
        <a class="ghost-btn small b-url" target="_blank" rel="noopener">Open billing</a></div></details>
      <label class="field"><span>Key <b class="k-hint"></b></span>
        <span class="copy-row"><input type="password" autocapitalize="off" spellcheck="false" autocomplete="off">
        <button class="cta k-save" type="button">Save &amp; test</button></span></label>
      <div class="row-btns k-more"><button class="ghost-btn small k-test" type="button">Test</button>
        <button class="ghost-btn small danger-ghost k-forget" type="button">Forget it</button></div>
      <p class="card-note k-out"></p>`;
    const k = o.key || {};
    box.querySelector("summary").textContent = `How to get your ${o.name} key`;
    const ol = box.querySelector("ol");
    o.steps.forEach((s) => { const li = document.createElement("li"); li.textContent = s; ol.append(li); });
    box.querySelector(".llm-bill").textContent = (o.moneyNote === false ? "" :
      "💳 The account needs money on it or the key won't work. ") + o.billing;
    const ku = box.querySelector(".k-url"), bu = box.querySelector(".b-url");
    ku.href = o.keyUrl || "#"; ku.hidden = !o.keyUrl;
    bu.href = o.billingUrl || "#"; bu.hidden = !o.billingUrl;
    box.querySelector("details").open = !k.set;
    box.querySelector(".k-hint").textContent = k.set
      ? (k.source === "railway" ? `set on Railway (${k.hint})` : `saved ${k.hint}`) : "not set";
    const input = box.querySelector("input");
    input.placeholder = o.keyPrefix ? `starts with ${o.keyPrefix}` : "paste the key";
    box.querySelector(".k-more").hidden = !k.set;
    box.querySelector(".k-forget").hidden = k.source !== "app";
    const out = box.querySelector(".k-out");
    const say = (t, bad) => { out.textContent = t; out.style.color = bad ? "var(--red)" : ""; };
    if (note && note.env === o.env) { say(note.text, note.bad); note = null; }

    box.querySelector(".k-save").addEventListener("click", async (e) => {
      const value = input.value.trim();
      if (!value) { say("Paste the key first.", true); input.focus(); return; }
      busy(e.target, true);
      say("Testing…");
      const r = await Relay.call("setup.key", { name: o.env, value });
      busy(e.target, false);
      if (r.error) { say(r.error, true); return; }
      if (!r.saved) { say(`Not saved: ${r.test.detail}`, true); return; }
      input.value = "";
      App.toast(`${o.name} key saved`);
      const d = r.test.detail || "";
      note = { env: o.env, text: `Saved. ${d.charAt(0).toUpperCase() + d.slice(1)}.`, bad: false };
      if (o.after) await o.after(r);
      await Standalone.refresh();
    });
    box.querySelector(".k-test").addEventListener("click", async (e) => {
      busy(e.target, true);
      const r = await Relay.call("setup.test", { name: o.env });
      busy(e.target, false);
      if (r.error) say(r.error, true);
      else say(r.test.ok ? `Works: ${r.test.detail}.` : r.test.detail, !r.test.ok);
    });
    box.querySelector(".k-forget").addEventListener("click", async (e) => {
      const b = e.target;
      if (!b.classList.contains("confirm")) {
        b.classList.add("confirm"); b.textContent = "Sure?";
        setTimeout(() => { b.classList.remove("confirm"); b.textContent = "Forget it"; }, 2500);
        return;
      }
      await Relay.call("setup.forget", { name: o.env });
      App.toast(`${o.name} key forgotten`);
      Standalone.refresh();
    });
    panel.append(box);
    return box;
  }

  function ownerOnly(panel) {
    if (st.owner) return false;
    panel.innerHTML = `<p class="hint">Only the relay's owner can change this.</p>`;
    return true;
  }

  /* ---------- the steps ---------- */
  function brainPanel(panel) {
    if (ownerOnly(panel)) return;
    const brains = st.brains || [];
    const cur = brainPick || (brains.find((b) => !b.key.set) || brains[0] || {}).id;
    const intro = document.createElement("div");
    intro.innerHTML = `<label class="field"><span>Provider</span><select class="b-pick"></select></label>`;
    const sel = intro.querySelector("select");
    brains.forEach((b) => {
      const o = document.createElement("option");
      o.value = b.id;
      o.textContent = `${b.name}${b.key.set ? "  ✓ key set" : ""}`;
      sel.append(o);
    });
    sel.value = cur;
    sel.addEventListener("change", () => { brainPick = sel.value; render(st); });
    panel.append(intro);
    const b = brains.find((x) => x.id === sel.value);
    if (!b) return;
    keyBlock(panel, {
      env: b.env, name: b.name, steps: b.steps, billing: b.billing, keyUrl: b.keyUrl,
      billingUrl: b.billingUrl, key: b.key, keyPrefix: b.keyPrefix,
      after: async () => {
        // the first brain key: start thinking with it
        const s = await Relay.call("status");
        if (s.llm && s.llm.provider !== b.id) {
          const r = await Relay.call("llm.set", { provider: b.id, model: b.default });
          if (!r.error) App.toast(`Reedus now thinks with ${r.label}`);
        }
      },
    });
  }

  function servicePanel(env) {
    return (panel) => {
      if (ownerOnly(panel)) return;
      const svc = (st.services || []).find((s) => s.env === env);
      if (!svc) return;
      keyBlock(panel, { env, name: svc.name, steps: svc.steps, billing: svc.billing, keyUrl: svc.keyUrl,
                        billingUrl: svc.billingUrl, key: svc.key, moneyNote: env !== "STRIPE_SECRET_KEY",
                        keyPrefix: env === "STRIPE_SECRET_KEY" ? "sk_ or rk_" : "" });
    };
  }

  function litePanel(panel) {
    if (ownerOnly(panel)) return;
    panel.innerHTML = `
      <label class="field"><span>Name</span>
        <span class="copy-row"><input type="text" class="l-name" maxlength="40" value="Desk Lite"><button class="cta l-add" type="button">Add a Lite</button></span></label>
      <div class="l-done" hidden>
        <ol class="steps">
          <li>Save as <code>ReedusLiteS3/firmware/src/secrets.h</code>, add your Wi-Fi. Shown once.</li>
          <li><code class="l-ca"></code></li>
          <li><code>cd ReedusLiteS3/firmware &amp;&amp; pio run -t upload</code></li>
        </ol>
        <pre class="sa-code l-code"></pre>
        <div class="row-btns"><button class="ghost-btn l-copy" type="button">Copy secrets.h</button></div>
      </div>
      <p class="card-note l-out"></p>`;
    const host = relayHost();
    panel.querySelector(".l-ca").textContent = `python ReedusLiteS3/tools/fetch_ca.py ${host || "<your relay>"}`;
    panel.querySelector(".l-add").addEventListener("click", async (e) => {
      busy(e.target, true);
      const r = await Relay.call("device.create", { name: panel.querySelector(".l-name").value.trim() || "Reedus Lite", host });
      busy(e.target, false);
      const out = panel.querySelector(".l-out");
      if (r.error) { out.textContent = r.error; out.style.color = "var(--red)"; return; }
      panel.querySelector(".l-code").textContent = r.secrets_h;
      if (/RELAY_TLS 0/.test(r.secrets_h)) {
        panel.querySelector(".l-ca").parentElement.textContent = "No certificate needed on your own network.";
      }
      panel.querySelector(".l-done").hidden = false;
      out.textContent = `${r.device.name} added. It shows up under Your Lites once it's online.`;
      Standalone.refresh({ keepSetup: true });     // the token above is shown once: don't redraw it away
    });
    panel.querySelector(".l-copy").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(panel.querySelector(".l-code").textContent); App.toast("Copied"); }
      catch { App.toast("Couldn't copy, select it by hand"); }
    });
  }

  function wakePanel(panel) {
    panel.innerHTML = `<ol class="steps">
        <li>Copy <code>hey_reedus.onnx</code> into <code>ReedusLiteS3/relay/models/</code> and push.</li>
        <li>Just testing? Set <code>RELAY_WAKE=gate</code> on Railway: any speech wakes him.</li>
      </ol>`;
  }

  function poolPanel(panel) {
    panel.innerHTML = `<p class="hint"></p><div class="row-btns"><button class="cta gold p-go" type="button">Top up now</button></div>`;
    panel.querySelector("p").textContent = st.payments ? "Pick an amount on the Pool card." : "Add a Stripe key under Top-ups first.";
    panel.querySelector(".p-go").addEventListener("click", () => {
      $("#sa-pool").scrollIntoView({ behavior: "smooth", block: "center" });
      $("#sa-pool").classList.remove("flash"); void $("#sa-pool").offsetWidth; $("#sa-pool").classList.add("flash");
    });
  }

  function relayHost() {
    if (Relay.demo) return "your-relay.up.railway.app";
    const a = Relay.address || "";
    try { return new URL(a.replace(/^ws/, "http")).host; } catch { return ""; }
  }

  function busy(btn, on) { btn.disabled = on; btn.classList.toggle("busy", on); }

  return { render, get status() { return st; } };
})();
