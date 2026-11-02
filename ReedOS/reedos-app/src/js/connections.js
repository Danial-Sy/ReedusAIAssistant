/* Settings → Connections: Spotify setup + now playing, and MCP app hubs.
   everything lives in the brain (connect/manager.py); this page only asks and shows */

const Connections = (() => {
  const $ = (s) => document.querySelector(s);
  let state = { items: [], presets: {} };
  let formPreset = null;
  let poll = null;
  let now = null;

  const spItem = () => state.items.find(i => i.id === "spotify") || {};
  const hubs = () => state.items.filter(i => i.kind === "mcp");

  async function refresh() {
    const out = await Link.connections();
    $("#cx-offline").hidden = !out.error;
    if (out.error) { $("#cx-note").textContent = ""; renderSub(); return; }
    state = out;
    render();
  }

  function render() {
    const sp = spItem();
    $("#sp-redirect").textContent = sp.redirect || $("#sp-redirect").textContent;
    $("#sp-state").textContent = sp.connected
      ? `Connected as ${sp.account || "you"}${sp.product && sp.product !== "premium" ? " · not Premium, playback control won't work" : ""}`
      : sp.pending ? "Waiting for the Spotify login…" : "Not connected";
    $("#sp-card").classList.toggle("on", !!sp.connected);
    $("#sp-setup").hidden = !!sp.connected;
    $("#sp-live").hidden = !sp.connected;
    $("#sp-disconnect").hidden = !sp.connected;
    $("#sp-finish").hidden = !sp.pending;

    const list = $("#hub-list");
    list.innerHTML = "";
    hubs().forEach((h, i) => {
      const row = document.createElement("div");
      row.className = "hub-row card" + (h.error ? " bad" : "");
      row.style.animationDelay = i * 40 + "ms";
      row.innerHTML = `<i class="hub-dot"></i>
        <div class="hub-text"><b></b><small></small></div>
        <button class="ghost-btn small" data-act="test">Test</button>
        <button class="ghost-btn small" data-act="remove">Remove</button>`;
      row.querySelector("b").textContent = h.name;
      row.querySelector("small").textContent = h.error ? h.error
        : `${h.tools} tool${h.tools === 1 ? "" : "s"} · ${h.host}`;
      row.querySelector('[data-act="test"]').addEventListener("click", async (e) => {
        busy(e.target, true);
        const out = await Link.connections({ action: "test", id: h.id });
        busy(e.target, false);
        App.toast(out.error ? out.error : `${h.name} answers: ${out.tools} tools`);
        refresh();
      });
      const rm = row.querySelector('[data-act="remove"]');
      rm.addEventListener("click", async () => {
        if (!rm.classList.contains("confirm")) {
          rm.classList.add("confirm"); rm.textContent = "Sure?";
          setTimeout(() => { rm.classList.remove("confirm"); rm.textContent = "Remove"; }, 2500);
          return;
        }
        row.classList.add("leaving");
        await Link.connections({ action: "remove", id: h.id });
        App.toast(`${h.name} removed`);
        refresh();
      });
      list.append(row);
    });

    const grid = $("#preset-grid");
    if (!grid.children.length && Object.keys(state.presets).length) {
      for (const [key, p] of Object.entries(state.presets)) {
        const b = document.createElement("button");
        b.className = "preset";
        b.innerHTML = `<b></b><small></small><span class="pr-add">+ Add</span>`;
        b.querySelector("b").textContent = p.name;
        b.querySelector("small").textContent = p.blurb;
        b.addEventListener("click", () => openForm(key));
        grid.append(b);
      }
    }
    const n = (sp.connected ? 1 : 0) + hubs().filter(h => h.connected).length;
    $("#cx-note").textContent = n ? `${n} connected` : "";
    renderSub();
    if (App.currentView() === "connections") startPoll();
  }

  function renderSub() {
    const sp = spItem();
    const names = [sp.connected ? "Spotify" : null, ...hubs().filter(h => h.connected).map(h => h.name)].filter(Boolean);
    $("#conn-sub").textContent = names.length ? names.join(" · ") + " connected"
      : "Spotify, Gmail, Calendar, smart home…";
  }

  /* ---------- Spotify ---------- */
  function wireSpotify() {
    $("#sp-copy").addEventListener("click", async () => {
      const text = $("#sp-redirect").textContent;
      try { await navigator.clipboard.writeText(text); App.toast("Copied"); }
      catch { App.toast("Couldn't copy, select it by hand"); }
    });
    $("#sp-start").addEventListener("click", async (e) => {
      const id = $("#sp-client").value.trim();
      busy(e.target, true);
      const out = await Link.connections({ action: "start", id: "spotify", clientId: id });
      busy(e.target, false);
      if (out.error) { App.toast(out.error); return; }
      // a new tab keeps ReedOS open for the paste-back on a phone
      const w = window.open(out.url, "_blank", "noopener");
      if (!w && !out.url.startsWith("about:")) location.href = out.url;
      if (out.url.startsWith("about:")) App.toast("Demo mode: pretend you logged in, then press Finish");
      $("#sp-finish").hidden = false;
      refresh();
    });
    $("#sp-finish-go").addEventListener("click", async (e) => {
      busy(e.target, true);
      const out = await Link.connections({ action: "finish", id: "spotify", url: $("#sp-paste").value.trim() });
      busy(e.target, false);
      if (out.error) { App.toast(out.error); return; }
      $("#sp-paste").value = "";
      App.toast(`Spotify connected as ${out.account}`);
      refresh();
    });
    $("#sp-disconnect").addEventListener("click", async () => {
      await Link.connections({ action: "disconnect", id: "spotify" });
      App.toast("Spotify disconnected");
      refresh();
    });

    document.querySelectorAll("[data-sp]").forEach(b => b.addEventListener("click", async () => {
      let act = b.dataset.sp;
      if (act === "toggle") act = now && now.playing ? "pause" : "resume";
      b.classList.remove("pop"); void b.offsetWidth; b.classList.add("pop");
      if (now && (act === "pause" || act === "resume")) { now.playing = act === "resume"; paintNow(); }
      const out = await Link.connections({ action: "control", id: "spotify", do: act });
      if (out.error) App.toast(out.error);
      setTimeout(pollNow, 350);
    }));

    const vol = $("#np-vol");
    let vt = null;
    vol.addEventListener("input", () => {
      $("#np-vol-val").textContent = vol.value + "%";
      clearTimeout(vt);
      vt = setTimeout(async () => {
        const out = await Link.connections({ action: "control", id: "spotify", do: "volume", value: +vol.value });
        if (out.error) App.toast(out.error);
      }, 250);
    });

    $("#sp-try").addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = $("#sp-try-text").value.trim();
      if (!text) return;
      $("#sp-try-out").textContent = "…";
      const out = await Link.connections({ action: "say", id: "spotify", text });
      $("#sp-try-out").textContent = out.error || out.reply;
      setTimeout(pollNow, 500);
    });
  }

  async function pollNow() {
    if (!spItem().connected || App.currentView() !== "connections") return;
    const out = await Link.connections({ action: "now", id: "spotify" });
    if (out.error) return;
    now = out.now;
    paintNow();
  }

  function paintNow() {
    const n = now || {};
    $("#np-track").textContent = n.track || "Nothing playing";
    $("#np-artist").textContent = n.track ? (n.artist || "") : "Open Spotify on any device";
    $("#np-dev").textContent = n.device ? `on ${n.device}` : "";
    const art = $("#np-art");
    art.style.backgroundImage = n.art ? `url("${n.art}")` : "";
    art.classList.toggle("spin", !!n.playing && !n.art);
    $("#np-icon").innerHTML = n.playing
      ? '<rect x="6.5" y="5" width="4" height="14" rx="1.2" fill="currentColor"/><rect x="13.5" y="5" width="4" height="14" rx="1.2" fill="currentColor"/>'
      : '<path d="M8 5v14l11-7z" fill="currentColor"/>';
    $("#np-prog").style.width = n.duration ? Math.min(100, n.progress / n.duration * 100) + "%" : "0%";
    if (n.volume != null && document.activeElement !== $("#np-vol")) {
      const v = $("#np-vol"); v.value = n.volume;
      v.style.setProperty("--fill", n.volume + "%");
      $("#np-vol-val").textContent = n.volume + "%";
    }
  }

  function startPoll() {
    if (poll || !spItem().connected) return;
    pollNow();
    poll = setInterval(() => {
      if (App.currentView() !== "connections") { clearInterval(poll); poll = null; return; }
      pollNow();
    }, 4000);
  }

  /* ---------- hubs ---------- */
  function openForm(key) {
    const p = state.presets[key];
    formPreset = key;
    $("#hf-title").textContent = `Add ${p.name}`;
    $("#hf-blurb").textContent = p.blurb;
    $("#hf-key-label").textContent = p.keyHelp;
    const custom = key === "custom";
    $("#hf-name-row").hidden = !custom;
    $("#hf-header-row").hidden = !custom;
    $("#hf-url-row").hidden = !(custom || key === "homeassistant");
    $("#hf-name").value = "";
    $("#hf-url").value = p.url || "";
    $("#hf-header").value = p.header || "Authorization";
    $("#hf-key").value = "";
    $("#hf-out").textContent = "";
    const form = $("#hub-form");
    form.hidden = false;
    form.classList.remove("rise"); void form.offsetWidth; form.classList.add("rise");
    form.scrollIntoView({ block: "nearest", behavior: "smooth" });
    setTimeout(() => (custom ? $("#hf-name") : $("#hf-key")).focus(), 250);
  }

  function wireForm() {
    $("#hf-cancel").addEventListener("click", () => { $("#hub-form").hidden = true; });
    $("#hf-add").addEventListener("click", async (e) => {
      const req = { action: "add", preset: formPreset, key: $("#hf-key").value.trim() };
      if (formPreset === "custom") {
        req.name = $("#hf-name").value.trim() || "Custom";
        req.header = $("#hf-header").value.trim();
      }
      if (!$("#hf-url-row").hidden) req.url = $("#hf-url").value.trim();
      busy(e.target, true);
      $("#hf-out").textContent = "Connecting and listing its tools…";
      const out = await Link.connections(req);
      busy(e.target, false);
      if (out.error) { $("#hf-out").textContent = out.error; return; }
      $("#hub-form").hidden = true;
      App.toast(`Connected · ${out.tools} tools`);
      refresh();
    });
  }

  function busy(btn, on) {
    btn.disabled = on;
    btn.classList.toggle("busy", on);
  }

  function init() {
    wireSpotify();
    wireForm();
    Link.on("status", (s) => {
      // the brain coming or going changes what this page can do
      const up = s.brain === "on" || s.link === "mock";
      if (up !== init.up) { init.up = up; refresh(); }
    });
    document.querySelector('[data-nav="connections"]').addEventListener("click", () => setTimeout(refresh, 50));
  }

  return { init, refresh };
})();
