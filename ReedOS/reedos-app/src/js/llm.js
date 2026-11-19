/* Settings → Cloud brain: pick the LLM Reedus thinks with in cloud mode (Lite is always cloud).
   everything lives in the brain (reedus_companion/cloud/llm.py); keys go in, only a masked
   hint comes back. this page asks and shows */

const LLM = (() => {
  const $ = (s) => document.querySelector(s);
  let state = null;          // GET /api/llm
  let open = null;           // provider shown in the detail card
  const fetched = {};        // provider -> live model list from "Refresh list"
  const OTHER = "__other__";

  const prov = (id) => (state && state.providers.find(p => p.id === id)) || null;
  const keyInfo = (id) => (state && state.keys && state.keys[id]) || {};

  async function refresh() {
    const out = await Link.llm();
    $("#llm-offline").hidden = !out.error;
    $("#llm-now").hidden = !!out.error;
    if (out.error) { renderSub(null); return; }
    state = out;
    if (!open || !prov(open)) open = state.provider;
    render();
  }

  function render() {
    renderNow();
    renderGrid();
    renderDetail();
    renderSub(state);
  }

  function renderNow() {
    $("#llm-now-label").textContent = state.label;
    $("#llm-now-search").textContent = "Web search: " + state.searchSummary;
    const st = $("#llm-now-state");
    st.className = "llm-state" + (state.ready ? "" : " warn");
    st.textContent = state.ready ? "Ready" : (state.why || "Not ready");
    $("#llm-note").textContent = state.ready ? "" : "needs setup";
  }

  function renderGrid() {
    const grid = $("#llm-grid");
    grid.innerHTML = "";
    state.providers.forEach((p, i) => {
      const k = keyInfo(p.id);
      const b = document.createElement("button");
      b.type = "button";
      b.className = "llm-pick" + (p.id === state.provider ? " active" : "") +
        (k.set ? " has-key" : "") + (p.id === open ? " open" : "");
      b.style.animationDelay = i * 30 + "ms";
      b.innerHTML = `<b></b><small></small><span class="llm-tag"></span>`;
      b.querySelector("b").textContent = p.name;
      b.querySelector("small").textContent = p.blurb;
      b.querySelector(".llm-tag").textContent = p.id === state.provider ? "In use"
        : k.set ? "Key saved" : p.keyOptional ? "" : "No key";
      b.addEventListener("click", () => {
        open = p.id;
        renderGrid();
        renderDetail(true);
      });
      grid.append(b);
    });
  }

  function modelList(p) {
    const list = (fetched[p.id] || p.models || []).filter(m => m.id);
    const cur = keyInfo(p.id).model || p.default;
    if (cur && !list.some(m => m.id === cur)) list.unshift({ id: cur, label: cur, note: "saved" });
    return list;
  }

  function renderDetail(animate) {
    const p = prov(open);
    const card = $("#llm-detail");
    if (!p) { card.hidden = true; return; }
    const k = keyInfo(p.id);
    card.hidden = false;
    if (animate) {
      card.classList.remove("rise"); void card.offsetWidth; card.classList.add("rise");
      card.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
    $("#ld-name").textContent = p.name;
    $("#ld-blurb").textContent = p.blurb;
    const st = $("#ld-state");
    const active = p.id === state.provider;
    st.className = "llm-state" + (active ? "" : k.set || p.keyOptional ? "" : " warn");
    st.textContent = active ? "In use" : k.set ? "Key saved" : p.keyOptional ? "" : "Needs a key";

    // how to get a key: open by default until there is one
    $("#ld-short").textContent = p.short;
    const steps = $("#ld-steps");
    steps.innerHTML = "";
    p.steps.forEach(s => { const li = document.createElement("li"); li.textContent = s; steps.append(li); });
    $("#ld-billing").textContent = "💳 You'll need to put money on this account for the key to work. " + p.billing;
    const ku = $("#ld-keyurl"), bu = $("#ld-billurl");
    ku.hidden = !p.keyUrl; ku.href = p.keyUrl || "#";
    bu.hidden = !p.billingUrl; bu.href = p.billingUrl || "#";
    $("#ld-howto").open = !k.set && !p.keyOptional;

    $("#ld-base-row").hidden = !p.needsBaseUrl;
    if (p.needsBaseUrl && document.activeElement !== $("#ld-base")) $("#ld-base").value = k.baseUrl || "";

    $("#ld-key").placeholder = p.keyOptional ? "optional for local servers"
      : `paste the key${p.keyPrefix ? ` (starts with ${p.keyPrefix})` : ""}`;
    $("#ld-keyhint").textContent = k.set
      ? (k.source === "env" ? `from ${p.env} on the run box` : `saved ${k.hint}`) : "";
    $("#ld-forget").hidden = k.source !== "app";

    // model
    const sel = $("#ld-model");
    const cur = k.model || p.default;
    sel.innerHTML = "";
    modelList(p).forEach(m => {
      const o = document.createElement("option");
      o.value = m.id;
      o.textContent = m.label && m.label !== m.id ? `${m.label}${m.note ? " · " + m.note : ""}`
        : `${m.id}${m.note ? " · " + m.note : ""}`;
      sel.append(o);
    });
    const other = document.createElement("option");
    other.value = OTHER; other.textContent = "Other (type a model id)…";
    sel.append(other);
    sel.value = cur || OTHER;
    if (sel.value !== (cur || OTHER)) sel.value = OTHER;
    $("#ld-model-other-row").hidden = sel.value !== OTHER;
    if (sel.value === OTHER) $("#ld-model-other").value = cur || "";
    const err = (fetched[p.id] || []).find(m => m.error);
    $("#ld-model-note").textContent = err ? err.error
      : fetched[p.id] ? `${modelList(p).length} models this key can use.`
      : "Refresh lists every model your key can use.";

    // search
    $("#ld-search").value = state.search || "auto";
    searchNote();
    $("#ld-use").textContent = active ? "Save changes" : `Use ${p.short}`;
    $("#ld-out").textContent = "";
  }

  function searchNote() {
    const p = prov(open);
    if (!p) return;
    const v = $("#ld-search").value;
    $("#ld-search-note").textContent =
      v === "off" ? "He won't look anything up, so live questions get answers from memory."
      : v === "gemini" ? "Reedus decides when a turn needs the web and asks Gemini (with Google Search) for the facts. Needs a Gemini key as well."
      : v === "local" ? "Reedus decides when a turn needs the web and searches with SearXNG or DuckDuckGo. Free."
      : p.search === "native" ? `${p.short} searches the web itself when a question needs it: ${p.searchName}.`
      : `${p.short} can't search by itself, so Reedus's own search router (SearXNG / DuckDuckGo) answers live questions.`;
  }

  function chosenModel() {
    const v = $("#ld-model").value;
    return v === OTHER ? $("#ld-model-other").value.trim() : v;
  }

  function busy(btn, on) {
    btn.disabled = on;
    btn.classList.toggle("busy", on);
  }

  function say(text, bad) {
    const out = $("#ld-out");
    out.textContent = text;
    out.style.color = bad ? "var(--red)" : "";
  }

  function testLine(t) {
    if (!t) return ["", false];
    return t.ok ? [`Works. ${t.model || "It"} answered "${t.reply}" in ${t.ms} ms.`, false]
      : [t.error || "That didn't work.", true];
  }

  function renderSub(s) {
    const label = s && s.label;
    $("#llm-sub").textContent = label ? `${label}${s.ready === false ? " · needs setup" : ""}`
      : "Gemini, Claude, GPT, or any model with an API key";
    const bc = $("#bc-llm");
    if (bc && label) bc.textContent = label;
  }

  function wire() {
    $("#ld-model").addEventListener("change", () => {
      const other = $("#ld-model").value === OTHER;
      $("#ld-model-other-row").hidden = !other;
      if (other) setTimeout(() => $("#ld-model-other").focus(), 50);
    });
    $("#ld-search").addEventListener("change", searchNote);

    $("#ld-key-save").addEventListener("click", async (e) => {
      const key = $("#ld-key").value.trim();
      if (!key) { say("Paste the key first.", true); $("#ld-key").focus(); return; }
      busy(e.target, true);
      say("Saving, then sending one tiny test message…");
      const out = await Link.llm({ action: "key", provider: open, key });
      busy(e.target, false);
      if (out.error) { say(out.error, true); return; }
      $("#ld-key").value = "";
      await refresh();
      const [line, bad] = testLine(out.test);
      say(line ? `Key saved. ${line}` : "Key saved.", bad);
      if (!bad) App.toast(`${prov(open).short} key saved`);
    });

    $("#ld-forget").addEventListener("click", async (e) => {
      const b = e.target;
      if (!b.classList.contains("confirm")) {
        b.classList.add("confirm"); b.textContent = "Sure? It can't be undone";
        setTimeout(() => { b.classList.remove("confirm"); b.textContent = "Forget saved key"; }, 2500);
        return;
      }
      await Link.llm({ action: "forget", provider: open });
      App.toast("Key forgotten");
      refresh();
    });

    $("#ld-refresh").addEventListener("click", async (e) => {
      e.preventDefault();
      const b = e.target;
      b.disabled = true; b.textContent = "Listing…";
      const req = { action: "models", provider: open };
      if (!$("#ld-base-row").hidden) req.baseUrl = $("#ld-base").value.trim();
      const out = await Link.llm(req);
      b.disabled = false; b.textContent = "Refresh list";
      if (out.error) { say(out.error, true); return; }
      fetched[open] = out.models || [];
      renderDetail();
    });

    $("#ld-test").addEventListener("click", async (e) => {
      const model = chosenModel();
      if (!model) { say("Pick or type a model first.", true); return; }
      busy(e.target, true);
      say(`Asking ${model} to say ready…`);
      const req = { action: "test", provider: open, model };
      if (!$("#ld-base-row").hidden) req.baseUrl = $("#ld-base").value.trim();
      const typed = $("#ld-key").value.trim();
      if (typed) req.key = typed;
      const out = await Link.llm(req);
      busy(e.target, false);
      if (out.error && out.ok === undefined) { say(out.error, true); return; }
      const [line, bad] = testLine(out);
      say(line, bad);
    });

    $("#ld-use").addEventListener("click", async (e) => {
      const model = chosenModel();
      if (!model) { say("Pick or type a model first.", true); return; }
      const p = prov(open);
      if ($("#ld-key").value.trim()) {
        say("You pasted a key but didn't save it. Press Save & test first.", true);
        return;
      }
      const req = { action: "set", provider: open, model, search: $("#ld-search").value };
      if (!$("#ld-base-row").hidden) req.baseUrl = $("#ld-base").value.trim();
      busy(e.target, true);
      say("Switching… (if he's mid-sentence this waits for him to finish)");
      const out = await Link.llm(req);
      busy(e.target, false);
      if (out.error) { say(out.error, true); return; }
      await refresh();
      $("#llm-now").classList.remove("swap"); void $("#llm-now").offsetWidth; $("#llm-now").classList.add("swap");
      const note = out.live && out.live.note;
      if (!out.ready) say(`Saved, but not ready yet: ${out.why}`, true);
      else say(note ? `Saved. ${note.charAt(0).toUpperCase() + note.slice(1)}.` : "Saved.");
      App.toast(out.ready ? `Cloud brain: ${out.label}` : `${p.short} saved, needs setup`);
    });
  }

  function init() {
    wire();
    Link.on("status", (s) => {
      if (s.llm) renderSub({ ...s.llm, ready: s.cloudReady });
      const up = s.brain === "on" || s.link === "mock";
      if (up !== init.up) { init.up = up; if (App.currentView() === "llm") refresh(); }
    });
  }

  return { init, refresh };
})();
