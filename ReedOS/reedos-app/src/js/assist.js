/* timers, alarms, reminders, lists, today's calendar, the glance screen (Lite tab), and the
   calendar feeds (Settings → Connections). everything lives in the brain (assist/api.py). */

const Assist = (() => {
  const $ = (s) => document.querySelector(s);
  let state = null;              // last full Link.assist() picture
  let light = null;              // status.assist: running + ringing, every few seconds
  let lightAt = 0;
  let listName = "shopping";
  let ticker = 0;
  const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  async function refresh() {
    const out = await Link.assist();
    if (out.error) { state = null; renderOffline(out.error); return; }
    state = out;
    render();
  }

  function renderOffline(msg) {
    $("#as-count").textContent = "brain offline";
    $("#as-list").innerHTML = `<p class="card-note">${esc(msg)}</p>`;
  }

  /* ---------- timers & alarms ---------- */
  function items() {
    // the full list knows labels/repeats; light (from status) has fresher seconds
    const base = state ? state.items : [];
    const gone = (Date.now() - lightAt) / 1000;
    const fresh = {};
    (light && light.active || []).forEach(i => { fresh[i.id] = i; });
    return base.filter(i => i.state !== "done").map(i => {
      const f = fresh[i.id];
      const left = f ? (f.state === "paused" ? f.left : Math.max(0, f.left - gone)) : i.left;
      return { ...i, left, state: f ? f.state : i.state };
    });
  }

  function render() {
    renderTimers();
    renderToday();
    renderLists();
    renderGlance();
    renderCalendars();
  }

  function renderTimers() {
    const list = $("#as-list");
    const all = items().filter(i => ["active", "paused"].includes(i.state));
    $("#as-count").textContent = all.length ? `${all.length} running` : "nothing running";
    list.innerHTML = "";
    all.forEach(i => {
      const row = document.createElement("div");
      row.className = "as-row";
      row.dataset.id = i.id;
      const icon = i.kind === "timer" ? "⏱" : i.kind === "alarm" ? "⏰" : "🔔";
      row.innerHTML = `<span class="as-ico" aria-hidden="true">${icon}</span>
        <div class="as-text"><b></b><small></small></div>
        ${i.kind === "timer" ? `<button class="ghost-btn small" data-act="${i.state === "paused" ? "resume" : "pause"}">${i.state === "paused" ? "Resume" : "Pause"}</button>` : ""}
        <button class="ghost-btn small" data-act="cancel" aria-label="Cancel">✕</button>`;
      row.querySelector("b").textContent = title(i);
      row.querySelector("small").textContent = sub(i);
      row.querySelectorAll("[data-act]").forEach(b => b.addEventListener("click", async () => {
        const out = await Link.assist({ action: b.dataset.act, id: i.id });
        if (out.error) App.toast(out.error);
        refresh();
      }));
      list.append(row);
    });
    const ringing = (light && light.ringing) || [];
    $("#as-ring").hidden = !ringing.length;
    if (ringing.length) {
      const r = ringing[0];
      $("#as-ring-text").textContent = r.kind === "timer" ? `${r.label ? cap(r.label) + " timer" : "Timer"} is done`
        : r.kind === "alarm" ? `Alarm${r.label ? ": " + r.label : ""}` : `Reminder: ${r.label}`;
      $("#as-snooze").hidden = r.kind === "timer";
    }
  }

  function tick() {
    // just the countdown text, once a second, without rebuilding rows
    if (!state) return;
    const map = {};
    items().forEach(i => { map[i.id] = i; });
    document.querySelectorAll("#as-list .as-row").forEach(row => {
      const i = map[row.dataset.id];
      if (i && i.kind === "timer") row.querySelector("small").textContent = sub(i);
    });
  }

  function title(i) {
    if (i.kind === "timer") return i.label ? `${cap(i.label)} timer` : "Timer";
    if (i.kind === "alarm") return clock(i.due) + (i.label ? ` · ${i.label}` : "");
    return cap(i.label || "Reminder");
  }

  function sub(i) {
    if (i.kind === "timer") return (i.state === "paused" ? "paused · " : "") + mmss(i.left) + " left";
    const when = i.repeat ? repeatWords(i.repeat) : dayWord(i.due);
    return i.kind === "alarm" ? `Alarm · ${when}` : `${clock(i.due)} · ${when}`;
  }

  function repeatWords(days) {
    const d = [...days].sort().join(",");
    if (d === "0,1,2,3,4,5,6") return "every day";
    if (d === "0,1,2,3,4") return "weekdays";
    if (d === "5,6") return "weekends";
    return days.map(x => DAYS[x]).join(", ");
  }

  function dayWord(ts) {
    const d = new Date(ts * 1000), now = new Date();
    const days = Math.round((new Date(d.toDateString()) - new Date(now.toDateString())) / 864e5);
    if (days === 0) return "today";
    if (days === 1) return "tomorrow";
    if (days < 7) return d.toLocaleDateString(undefined, { weekday: "long" });
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  function clock(ts) {
    const h24 = state && state.settings && state.settings.glance && state.settings.glance.h24;
    return new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", hour12: !h24 });
  }

  /* ---------- today ---------- */
  function renderToday() {
    const evs = (state && state.today) || [];
    const box = $("#as-today");
    box.innerHTML = "";
    const cals = (state && state.settings.calendars) || [];
    $("#as-today-empty").hidden = cals.length > 0 && evs.length > 0;
    $("#as-today-note").textContent = cals.length ? `${cals.length} calendar${cals.length === 1 ? "" : "s"}` : "";
    if (cals.length && !evs.length) {
      box.innerHTML = `<p class="card-note">Nothing else on your calendar today.</p>`;
      return;
    }
    const now = Date.now() / 1000;
    evs.forEach(e => {
      const row = document.createElement("div");
      row.className = "as-ev" + (e.start <= now && now < e.end ? " now" : "") + (e.end <= now ? " past" : "");
      row.innerHTML = `<span class="as-ev-t"></span><div class="as-text"><b></b><small></small></div>`;
      row.querySelector(".as-ev-t").textContent = e.allDay ? "All day" : clock(e.start);
      row.querySelector("b").textContent = e.title;
      row.querySelector("small").textContent = [e.location, e.cal].filter(Boolean).join(" · ");
      box.append(row);
    });
  }

  /* ---------- lists ---------- */
  function renderLists() {
    const box = $("#as-items");
    const things = (state && state.lists[listName]) || [];
    box.innerHTML = "";
    if (!things.length) { box.innerHTML = `<p class="card-note">Empty.</p>`; return; }
    things.forEach(t => {
      const row = document.createElement("label");
      row.className = "as-item";
      row.innerHTML = `<input type="checkbox"><span></span>`;
      row.querySelector("span").textContent = t;
      row.querySelector("input").addEventListener("change", async () => {
        row.classList.add("done");
        await Link.assist({ action: "list.remove", list: listName, text: t });
        setTimeout(refresh, 250);
      });
      box.append(row);
    });
  }

  /* ---------- glance ---------- */
  function renderGlance() {
    const g = (state && state.settings.glance) || {};
    $("#gl-on").checked = g.enabled !== false;
    $("#gl-h24").checked = !!g.h24;
    const every = String(g.every || 120);
    const sel = $("#gl-every");
    if (![...sel.options].some(o => o.value === every)) sel.add(new Option(`${Math.round(every / 60)} minutes`, every));
    sel.value = every;
    document.querySelectorAll("#gl-cards input[data-card]").forEach(cb => {
      cb.checked = (g.cards || []).includes(cb.dataset.card);
    });
  }

  function saveGlance() {
    const cards = [...document.querySelectorAll("#gl-cards input[data-card]")].filter(c => c.checked).map(c => c.dataset.card);
    Link.assist({ action: "settings", glance: {
      enabled: $("#gl-on").checked, every: +$("#gl-every").value, cards, h24: $("#gl-h24").checked,
    } }).then(o => { if (o.error) App.toast(o.error); else refresh(); });
  }

  /* ---------- calendars (Connections page) ---------- */
  function renderCalendars() {
    const list = $("#cal-list");
    if (!list) return;
    const cals = (state && state.settings.calendars) || [];
    list.innerHTML = "";
    cals.forEach(c => {
      const row = document.createElement("div");
      row.className = "hub-row card" + (c.error ? " bad" : "");
      row.innerHTML = `<i class="hub-dot"></i><div class="hub-text"><b></b><small></small></div>
        <button class="ghost-btn small" data-act="test">Test</button>
        <button class="ghost-btn small" data-act="remove">Remove</button>`;
      row.querySelector("b").textContent = c.name;
      row.querySelector("small").textContent = c.error || c.host;
      row.querySelector('[data-act="test"]').addEventListener("click", async (e) => {
        e.target.classList.add("busy");
        const out = await Link.assist({ action: "calendar.test", id: c.id });
        e.target.classList.remove("busy");
        App.toast(out.error ? out.error : `${c.name}: ${out.upcoming} events in the next two weeks`);
      });
      const rm = row.querySelector('[data-act="remove"]');
      rm.addEventListener("click", async () => {
        if (!rm.classList.contains("confirm")) {
          rm.classList.add("confirm"); rm.textContent = "Sure?";
          setTimeout(() => { rm.classList.remove("confirm"); rm.textContent = "Remove"; }, 2500);
          return;
        }
        row.classList.add("leaving");
        await Link.assist({ action: "calendar.remove", id: c.id });
        refresh();
      });
      list.append(row);
    });
  }

  async function addCalendar() {
    const btn = $("#cal-add"), out = $("#cal-out");
    const url = $("#cal-url").value.trim(), name = $("#cal-name").value.trim() || "Calendar";
    if (!url) { out.textContent = "Paste the calendar's iCal link first."; return; }
    btn.classList.add("busy");
    const r = await Link.assist({ action: "calendar.add", name, url });
    btn.classList.remove("busy");
    if (r.error) { out.textContent = r.error; return; }
    $("#cal-url").value = ""; $("#cal-name").value = "";
    out.textContent = `Added ${name}: ${r.upcoming} events in the next two weeks${r.sample && r.sample.length ? ", like " + r.sample.slice(0, 2).join(" and ") : ""}.`;
    refresh();
  }

  /* ---------- helpers ---------- */
  function mmss(s) {
    s = Math.max(0, Math.ceil(s));
    const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
  }
  function cap(s) { return s ? s[0].toUpperCase() + s.slice(1) : s; }
  function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

  function init() {
    $("#as-quick").addEventListener("click", async (e) => {
      const b = e.target.closest("[data-timer]");
      if (!b) return;
      const out = await Link.assist({ action: "timer", seconds: +b.dataset.timer });
      App.toast(out.error || `Timer set for ${b.textContent}`);
      refresh();
    });
    $("#as-say").addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = $("#as-say-text").value.trim();
      if (!text) return;
      const out = await Link.assist({ action: "say", text });
      $("#as-say-out").textContent = out.error || out.reply;
      $("#as-say-text").value = "";
      refresh();
    });
    $("#as-stop").addEventListener("click", () => Link.assist({ action: "stop" }).then(refresh));
    $("#as-snooze").addEventListener("click", () => Link.assist({ action: "snooze" }).then(refresh));

    Seg.bind($("#as-list-seg"), listName, (v) => { listName = v; renderLists(); });
    $("#as-list-add").addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = $("#as-list-text").value.trim();
      if (!text) return;
      $("#as-list-text").value = "";
      await Link.assist({ action: "list.add", list: listName, text });
      refresh();
    });

    ["#gl-on", "#gl-h24", "#gl-every"].forEach(s => $(s).addEventListener("change", saveGlance));
    document.querySelectorAll("#gl-cards input[data-card]").forEach(cb => cb.addEventListener("change", saveGlance));
    document.querySelectorAll(".gl-show").forEach(b => b.addEventListener("click", async (e) => {
      e.preventDefault(); e.stopPropagation();
      const out = await Link.assist({ action: "glance.show", card: b.dataset.show });
      if (out.error) App.toast(out.error);
    }));

    const add = $("#cal-add");
    if (add) add.addEventListener("click", addCalendar);

    let ringWas = 0;
    Link.on("status", (s) => {
      light = s.assist || null;
      lightAt = Date.now();
      const ringing = (light && light.ringing || []).length;
      const count = (light && light.active || []).length;
      const known = state ? items().filter(i => ["active", "paused"].includes(i.state)).length : -1;
      if (ringing !== ringWas || (state && count !== known)) refresh();   // something changed by voice
      else if (state) renderTimers();
      ringWas = ringing;
    });
    clearInterval(ticker);
    ticker = setInterval(() => {
      const v = App.currentView();
      if (v === "lite" && !document.hidden) tick();
    }, 1000);
    setInterval(() => {
      const v = App.currentView();
      if ((v === "lite" || v === "connections") && !document.hidden) refresh();
    }, 30000);
    refresh();
  }

  return { init, refresh };
})();
