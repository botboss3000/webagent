/* WEBAGENT TUI — real-backend bridge.
   Optional transport that connects the terminal to the live webAgent agent.
   When the page is served inside webAgent (same origin as the API) this streams
   real replies; when opened standalone (file://, or no backend) available()
   stays false and app.jsx falls back to the canned REPLIES in tooldata.js.

   Public surface (window.AgentBridge):
     await init()            → probe backend + bootstrap auth (no throw)
     available()             → boolean: is a live backend ready
     userId / sessionId      → current identity (strings)
     newSession()            → start a fresh chat session, returns id
     await send(text, h)     → POST /chat/stream, parse SSE, drive handlers:
        h.onToken(str)       — a streamed text delta for the current bubble
        h.onStep(str)        — a completed assistant step (multi-step turns)
        h.onTool(evt)        — a raw tool_call / tool_result event
        h.onDone()           — turn finished
        h.onError(err)       — failure (caller should surface + maybe fall back)
*/
(function () {
  const LS = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
  };

  function uuid() {
    try { if (crypto && crypto.randomUUID) return crypto.randomUUID(); } catch (e) {}
    return "txxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0; return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
    });
  }

  // Resolve API base. The page is served at /web-terminal/, the API at /api/v1.
  const apiBase = (location.origin && location.origin !== "null") ? location.origin : "";
  const url = (p) => apiBase + p;

  const Bridge = {
    enabled: true,
    backend: null,          // null = unprobed, true/false once known
    token: null,
    userId: null,
    sessionId: null,
    _statusCbs: [],

    available() { return !!(this.enabled && this.backend && this.userId); },
    onStatus(cb) { this._statusCbs.push(cb); },
    _emit(s) { this._statusCbs.forEach((cb) => { try { cb(s); } catch (e) {} }); },

    _readAuth() {
      // shared same-origin localStorage with the parent webAgent app
      this.token = LS.get("auth_token") || null;
      this.userId = LS.get("webagent_active_user_id") || LS.get("auth_user_id") || null;
      this.sessionId = LS.get("webagent.terminal.session") || null;
      if (!this.sessionId) { this.sessionId = uuid(); LS.set("webagent.terminal.session", this.sessionId); }
    },

    async _probe() {
      try {
        const r = await fetch(url("/api/v1/auth/access-mode"), { method: "GET" });
        return r.ok;
      } catch (e) { return false; }
    },

    async _ensureAuth() {
      if (this.token && this.userId) return true;
      try {
        const r = await fetch(url("/api/v1/auth/anonymous"), { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        if (!r.ok) return false;
        const j = await r.json();
        this.token = j.token || j.access_token || j.jwt || null;
        this.userId = j.user_id || j.id || (j.user && j.user.id) || null;
        if (this.token) LS.set("auth_token", this.token);
        if (this.userId) LS.set("auth_user_id", this.userId);
        return !!this.userId;
      } catch (e) { return false; }
    },

    async init() {
      if (!this.enabled) { this.backend = false; return false; }
      this._readAuth();
      this.backend = await this._probe();
      if (this.backend) await this._ensureAuth();
      this._emit(this.available() ? "ready" : "offline");
      return this.available();
    },

    newSession() { this.sessionId = uuid(); LS.set("webagent.terminal.session", this.sessionId); return this.sessionId; },

    async send(text, h) {
      h = h || {};
      if (!this.available()) { h.onError && h.onError(new Error("backend unavailable")); return; }
      let res;
      try {
        // The agent is the one the main app's selector chose (shared same-origin
        // localStorage). Without it the backend has no agent to run → 400, and
        // we fall back to the demo replies below.
        const agentId = LS.get("selectedAgentId");
        const payload = { user_id: this.userId, session_id: this.sessionId, message: text };
        if (agentId) payload.agent_id = agentId;
        res = await fetch(url("/api/v1/chat/stream"), {
          method: "POST",
          headers: { "Content-Type": "application/json", "Authorization": "Bearer " + this.token },
          body: JSON.stringify(payload),
        });
      } catch (e) { h.onError && h.onError(e); return; }
      if (!res.ok || !res.body) { h.onError && h.onError(new Error("stream HTTP " + res.status)); return; }

      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      const handle = (payload) => {
        if (payload === "[DONE]") { h.onDone && h.onDone(); return; }
        let evt; try { evt = JSON.parse(payload); } catch (e) { return; }
        const type = evt.type || evt.event;
        const content = evt.content != null ? evt.content : evt.text;
        if (type === "stream") h.onToken && h.onToken(content || "");
        else if (type === "response" || type === "agent_step_end") { if (content) h.onStep && h.onStep(content); }
        else if (type === "tool_call" || type === "tool_result") h.onTool && h.onTool(evt);
        else if (type === "error") h.onError && h.onError(new Error(content || "agent error"));
        else if (type === "done" || type === "complete") h.onDone && h.onDone();
      };
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          // SSE frames separated by blank line; each frame may have data: lines
          let sep;
          while ((sep = buf.indexOf("\n\n")) >= 0) {
            const frame = buf.slice(0, sep); buf = buf.slice(sep + 2);
            frame.split("\n").forEach((line) => {
              line = line.replace(/\r$/, "");
              if (line.startsWith("data:")) handle(line.slice(5).trim());
            });
          }
        }
      } catch (e) { h.onError && h.onError(e); return; }
      h.onDone && h.onDone();
    },
  };

  window.AgentBridge = Bridge;
})();
