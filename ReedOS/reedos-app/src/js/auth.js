const Auth = (() => {
  const KEY = "reedos.token";
  let token = "";
  try { token = localStorage.getItem(KEY) || ""; } catch { /* private mode */ }

  function base() {
    const ws = (typeof Store !== "undefined" && Store.app.daemonUrl) || "";
    try {
      const u = new URL(ws);
      return `${u.protocol === "wss:" ? "https:" : "http:"}//${u.host}`;
    } catch {
      return window.location.origin;
    }
  }

  function withKey(url) {
    if (!token) return url;
    return url + (url.includes("?") ? "&" : "?") + "k=" + encodeURIComponent(token);
  }

  function save(t) {
    token = t || "";
    try { token ? localStorage.setItem(KEY, token) : localStorage.removeItem(KEY); } catch { /* */ }
  }

  async function status() {
    try {
      const r = await fetch(withKey(base() + "/api/auth"), {
        credentials: "include",
        signal: AbortSignal.timeout ? AbortSignal.timeout(3000) : undefined,
      });
      if (r.status === 401) return { reachable: true, required: true, authed: false };
      if (!r.ok) return { reachable: false, required: false, authed: true };   // not reedosd
      return { reachable: true, ...(await r.json()) };
    } catch {
      return { reachable: false, required: false, authed: true };   // no daemon → mock mode
    }
  }

  async function login(code) {
    const r = await fetch(base() + "/api/login", {
      method: "POST", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok) { save(data.token); return { ok: true }; }
    return { ok: false, error: data.error || "wrong passcode", retryAfter: data.retryAfter };
  }

  async function setPasscode(code) {
    const r = await fetch(withKey(base() + "/api/passcode"), {
      method: "POST", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok) { save(data.token || ""); return { ok: true, required: data.required }; }
    return { ok: false, error: data.error || "could not change it" };
  }

  return {
    get token() { return token; },
    base, withKey, status, login, setPasscode, save,
    forget: () => save(""),
  };
})();
