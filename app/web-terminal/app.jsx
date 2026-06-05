/* global React, ReactDOM */
const { useState, useEffect, useRef, useCallback } = React;

function applyTheme(id) {
  const t = window.THEMES[id];
  const r = document.documentElement;
  Object.keys(t).forEach((k) => {
    if (k === "label" || k === "tags") return;
    r.style.setProperty("--" + k, t[k]);
  });
  r.style.setProperty("color-scheme", id === "paper" ? "light" : "dark");
}

let replyCursor = 0;

const SLASH = [
  { c: "/clear",   d: "reset the session",     act: "clear" },
  { c: "/model",   d: "switch model",          act: "model" },
  { c: "/diff",    d: "review pending changes" },
  { c: "/compact", d: "compress context"       },
  { c: "/cost",    d: "token + $ usage"        },
  { c: "/undo",    d: "revert last edit"        },
  { c: "/init",    d: "scan & index repo"       },
  { c: "/help",    d: "all commands"            },
];
const QUICK = [
  "Explain the build error",
  "Write tests for tools.ts",
  "Refactor dispatch() to be type-safe",
  "Summarize what changed this session",
];
const MODELS = ["sonnet-4.6", "opus-4.1", "haiku-4"];
const CTX_POOL = ["vite.config.ts", "README.md", "src/lib/agent.ts", "tsconfig.json"];

const ATTACHMENTS = [
  { n: "error-log.txt", s: "4.2 KB" },
  { n: "screenshot-2026.png", s: "188 KB" },
  { n: "schema.sql", s: "1.1 KB" },
];
const DOCS = [
  { n: "README.md", t: "project overview" },
  { n: "ARCHITECTURE.md", t: "system design" },
  { n: "API.md", t: "endpoint reference" },
];
const KEYS = [
  { k: "ANTHROPIC_API_KEY", v: "sk-ant-a83f1c…92d" },
  { k: "GITHUB_TOKEN", v: "ghp_4kQ9x…7Tz" },
  { k: "VITE_API_URL", v: "https://api.webagent.dev" },
];

function App() {
  const [theme, setTheme] = useState("lime");
  const [animIndex, setAnimIndex] = useState(0);
  const [menuOpen, setMenuOpen] = useState(false);
  const [gamesOpen, setGamesOpen] = useState(false);
  const [activeGame, setActiveGame] = useState(null);
  const [designerOpen, setDesignerOpen] = useState(false);
  const [notesOpen, setNotesOpen] = useState(false);
  const [crewOpen, setCrewOpen] = useState(false);
  const [crew, setCrew] = useState(() => { try { const v = JSON.parse(localStorage.getItem("webagent.crew")); return Array.isArray(v) ? v : ["robot", "cat"]; } catch (e) { return ["robot", "cat"]; } });
  const [thinking, setThinking] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [tray, setTray] = useState(false);
  const [revealKeys, setRevealKeys] = useState(false);
  const [mode, setMode] = useState("shell"); // shell | files
  const [items, setItems] = useState(() => JSON.parse(JSON.stringify(window.DEMO)));
  const [input, setInput] = useState("");
  const [tin, setTin] = useState(0);
  const [tout, setTout] = useState(0);
  const [ctx, setCtx] = useState(["src/lib/tools.ts", "src/App.tsx"]);
  const [model, setModel] = useState("sonnet-4.6");
  const [live, setLive] = useState(false); // true once AgentBridge reaches a real backend
  const scrollRef = useRef(null);
  const inputRef = useRef(null);

  const insert = (txt) => {
    setInput((v) => (v ? v.trimEnd() + " " : "") + txt);
    if (inputRef.current) inputRef.current.focus();
  };
  const runSlash = (s) => {
    if (s.act === "clear") { newSession(); setDrawer(false); return; }
    if (s.act === "model") { setMenuOpen(false); setDrawer(true); return; }
    insert(s.c);
  };
  const addCtx = () => setCtx((c) => c.includes(CTX_POOL[c.length % CTX_POOL.length]) ? c : [...c, CTX_POOL[c.length % CTX_POOL.length]]);
  const removeCtx = (i) => setCtx((c) => c.filter((_, k) => k !== i));

  useEffect(() => { applyTheme(theme); }, [theme]);
  useEffect(() => {
    if (window.AgentBridge) window.AgentBridge.init().then((ok) => { setLive(!!ok); }).catch(() => setLive(false));
  }, []);
  useEffect(() => { try { localStorage.setItem("webagent.crew", JSON.stringify(crew)); } catch (e) {} }, [crew]);
  // on mobile, autofocus only works in direct response to a user gesture.
  // capture the very first touch/click and redirect focus to the input.
  useEffect(() => {
    let done = false;
    const handler = () => {
      if (done) return;
      done = true;
      inputRef.current?.focus();
    };
    document.addEventListener("touchstart", handler, { once: true, passive: true });
    document.addEventListener("click", handler, { once: true, passive: true });
    return () => { document.removeEventListener("touchstart", handler); document.removeEventListener("click", handler); };
  }, []);
  const toggleCrew = (id) => setCrew((c) => c.includes(id) ? c.filter((x) => x !== id) : [...c, id]);

  const scrollDown = useCallback(() => {
    const el = scrollRef.current;
    if (el) requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
  }, []);
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    // Only auto-scroll if user is already at or near the bottom (within 60px)
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    if (nearBottom) scrollDown();
  }, [items, scrollDown]);

  const toggle = (idx) => setItems((xs) => xs.map((x, i) => i === idx ? { ...x, expanded: !x.expanded } : x));

  const approve = (idx) => {
    setItems((xs) => xs.map((x, i) => i === idx ? { ...x, status: "running" } : x));
    setTimeout(() => {
      setItems((xs) => xs.map((x, i) => i === idx ? { ...x, status: "done" } : x));
    }, 900);
  };
  const reject = (idx) => setItems((xs) => xs.map((x, i) => i === idx ? { ...x, status: "done", rejected: true, expanded: true } : x));

  const cycleLogo = () => setAnimIndex((i) => (i + 1) % window.ANIMS.length);
  const prevLogo = () => setAnimIndex((i) => (i - 1 + window.ANIMS.length) % window.ANIMS.length);
  const newSession = () => { setItems([]); setTin(0); setTout(0); replyCursor = 0; if (window.AgentBridge && window.AgentBridge.available()) window.AgentBridge.newSession(); inputRef.current?.focus(); };
  const shuffle = () => {
    const ids = window.THEME_ORDER;
    setTheme(ids[Math.floor(Math.random() * ids.length)]);
    setAnimIndex(Math.floor(Math.random() * window.ANIMS.length));
  };

  // ── streaming helpers (live backend) ──
  const streamAppend = (delta) => setItems((xs) => {
    const n = xs.slice();
    for (let i = n.length - 1; i >= 0; i--) {
      if (n[i].type === "agent" && n[i].streaming) { n[i] = { ...n[i], text: (n[i].text || "") + delta }; return n; }
    }
    n.push({ type: "agent", text: delta, streaming: true });
    return n;
  });
  const streamStep = (full) => setItems((xs) => {
    const n = xs.slice();
    for (let i = n.length - 1; i >= 0; i--) {
      if (n[i].type === "agent" && n[i].streaming) { n[i] = { ...n[i], text: (n[i].text && n[i].text.length ? n[i].text : (full || "")), streaming: false }; return n; }
    }
    if (full) n.push({ type: "agent", text: full });
    return n;
  });
  const streamEnd = () => { setItems((xs) => xs.map((x) => x.streaming ? { ...x, streaming: false } : x)); setThinking(false); };
  const addLiveTool = (evt) => setItems((xs) => {
    const name = evt.tool || evt.name || evt.tool_name || "tool";
    if (evt.type === "tool_result") {
      const n = xs.slice();
      for (let i = n.length - 1; i >= 0; i--) {
        if (n[i].type === "tool" && n[i].tool === name && n[i].status === "running") {
          n[i] = { ...n[i], status: "done", data: { ...n[i].data, result: (evt.result != null ? evt.result : evt.output), error: evt.error } };
          return n;
        }
      }
      return n;
    }
    const args = evt.arguments || evt.args || evt.input || {};
    const sum = typeof args === "object" && args ? Object.keys(args).slice(0, 3).join(", ") : String(args).slice(0, 60);
    return [...xs, { type: "tool", tool: name, summary: sum, status: "running", expanded: false, data: { __raw: true, args } }];
  });

  const sendDemo = (text) => {
    const lc = text.toLowerCase();
    let reply = window.REPLIES.find((r) => r.match.some((m) => lc.includes(m)));
    if (!reply) { reply = window.REPLIES[replyCursor % window.REPLIES.length]; replyCursor++; }
    setTimeout(() => {
      setItems((xs) => [...xs, { type: "agent", text: reply.text }]);
      setTout((n) => n + 12);
      if (reply.tool) {
        setTimeout(() => {
          const tc = JSON.parse(JSON.stringify(reply.tool));
          tc.type = "tool"; tc.expanded = true;
          setItems((xs) => [...xs, tc]);
          setTout((n) => n + 40);
          setThinking(false);
        }, 900);
      } else {
        setTimeout(() => setThinking(false), 500);
      }
    }, 360);
  };

  const sendLive = (text) => {
    let got = false;
    window.AgentBridge.send(text, {
      onToken: (d) => { got = true; streamAppend(d); setTout((n) => n + 1); },
      onStep: (s) => { got = true; streamStep(s); },
      onTool: (evt) => { got = true; addLiveTool(evt); },
      onDone: () => streamEnd(),
      onError: (e) => {
        if (!got) { sendDemo(text); }            // backend errored before any output → graceful demo fallback
        else { streamEnd(); setItems((xs) => [...xs, { type: "agent", text: "⚠ stream error: " + ((e && e.message) || e) }]); }
      },
    });
  };

  const send = () => {
    const text = input.trim();
    if (!text) return;
    setInput("");
    setTin((n) => n + Math.max(4, Math.round(text.length / 4)));
    setThinking(true);
    setItems((xs) => [...xs, { type: "user", text }]);
    if (live && window.AgentBridge && window.AgentBridge.available()) sendLive(text);
    else sendDemo(text);
  };

  // keyboard shortcuts
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") { setMenuOpen(false); setDesignerOpen(false); setNotesOpen(false); setTray(false); setCrewOpen(false); return; }
      if (e.ctrlKey && (e.key === "t" || e.key === "T")) { e.preventDefault(); setMenuOpen((o) => !o); }
      if (e.ctrlKey && (e.key === "l" || e.key === "L")) { e.preventDefault(); cycleLogo(); }
      if (e.ctrlKey && (e.key === "n" || e.key === "N")) { e.preventDefault(); newSession(); }
      if (e.ctrlKey && (e.key === "m" || e.key === "M")) { e.preventDefault(); setMode((x) => x === "shell" ? "files" : "shell"); }
      if (e.ctrlKey && (e.key === "g" || e.key === "G")) { e.preventDefault(); setGamesOpen((o) => !o); }
      if (e.ctrlKey && (e.key === "d" || e.key === "D")) { e.preventDefault(); setDesignerOpen((o) => !o); }
      if (e.ctrlKey && (e.key === "b" || e.key === "B")) { e.preventDefault(); setNotesOpen((o) => !o); }
      if (e.ctrlKey && (e.key === "e" || e.key === "E")) { e.preventDefault(); setCrewOpen((o) => !o); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  

  const ctxPct = Math.min(99, Math.round((tin + tout) / 1800 * 100));

  return (
    <div className="screen">
      {/* top status bar */}
      <div className="bar bar-top">
        <span className="bt-title">Admin coding agent</span>
        <span className="bt-modes">
          (<button className={"modepill " + (mode === "shell" ? "on" : "")} onClick={() => setMode("shell")}>shell</button>
          <span className="sep"> · </span>
          <button className={"modepill " + (mode === "files" ? "on" : "")} onClick={() => setMode("files")}>files</button>)
        </span>
        <span className="bt-dash">—</span>
        <button className="bt-link" onClick={newSession}>new session</button>
        <span className="bt-dash">—</span>
        <span className="bt-live"><span className={"live-dot" + (thinking ? " thinking" : "")} />{thinking ? "thinking" : (live ? "agent" : "demo")}</span>
        <span className="bt-spacer" />
        <button className={"bt-theme bt-designer" + (designerOpen ? " on" : "")} onClick={() => setDesignerOpen((o) => !o)} title="talk to your designer (^D)">✎ designer</button>
        <button className="bt-theme" onClick={() => setGamesOpen(true)} title="arcade (^G)">♛ games</button>
        <button className={"bt-theme bt-crew" + (crewOpen ? " on" : "")} onClick={() => setCrewOpen((o) => !o)} title="pick companions (^E)">☺ crew{crew.length ? " · " + crew.length : ""}</button>
        <button className="bt-theme" onClick={cycleLogo} title="cycle ASCII animation (^L)">▶ anim: {window.ANIMS[animIndex].name}</button>
        <button className="bt-theme" onClick={() => setMenuOpen(true)} title="Ctrl+T">◐ theme: {window.THEMES[theme].label}</button>
        <button className="bt-theme" onClick={shuffle} title="randomize the look">⚄ shuffle</button>
        <button className={"bt-theme bt-notes" + (notesOpen ? " on" : "")} onClick={() => setNotesOpen((o) => !o)} title="notes & todos (^B)">▤ notes</button>
      </div>

      {/* scrollable body */}
      <div className="workspace">
      <div className="work-main">
      <div className="body" ref={scrollRef}>
        <div className="logo-wrap">
          <AsciiStage index={animIndex} />
          <div className="logo-ctl">
            <button className="anim-arrow" onClick={prevLogo} title="previous">‹</button>
            <Btn keycap="^L" onClick={cycleLogo} title="next animation">{window.ANIMS[animIndex].name}</Btn>
            <button className="anim-arrow" onClick={cycleLogo} title="next">›</button>
          </div>
        </div>

        <div className="subtitle">
          <span className="st-strong">Admin coding agent</span>
          <span className="st-paren"> ({mode})</span>
          <span className="st-dot"> — </span>
          <span className="st-dim">model:</span> <span className="st-model">{model}</span>
          <span className="st-dot"> — </span>
          <span className="st-dim">2026-05-29</span>
        </div>

        <div className="transcript-header">
          <span className="transcript-title">Transcript</span>
          <button className="transcript-copy" onClick={() => {
            const text = items.map(it => it.type === "tool" ? "" : it.text).filter(Boolean).join('\n\n');
            navigator.clipboard.writeText(text).then(() => {
              const btn = document.querySelector('.transcript-copy');
              if (btn) { btn.textContent = '✓ copied'; setTimeout(() => { btn.textContent = '⎘ copy all'; }, 1500); }
            }).catch(() => {});
          }} title="Copy all agent messages">⎘ copy all</button>
        </div>
        <div className="transcript">
          {items.map((it, i) => {
            if (it.type === "tool") {
              return (
                <div key={i} className={it.rejected ? "rejected-wrap" : ""}>
                  <ToolCard item={it} theme={theme}
                    onToggle={() => toggle(i)} onApprove={() => approve(i)} onReject={() => reject(i)} />
                  {it.rejected && <div className="rejected-note">✗ rejected — change not applied</div>}
                </div>
              );
            }
            return <Msg key={i} idx={i} item={it} />;
          })}
          {items.length === 0 && (
            <div className="empty">new session — type below or hit <b>^N</b>. try “run the tests”, “search the docs”, “make a plan”.</div>
          )}
        </div>
      </div>

      {/* session line */}
      <CrewStage active={crew} thinking={thinking} />
      <div className="sessline">
        <button className={"sess-expand" + (tray ? " on" : "")} onClick={() => { setDrawer(false); setTray((t) => !t); }} title="attachments · documents · keys">{tray ? "▾" : "▸"}</button>
        <span>session <b className="ok">{tin}</b> in / <b className="ok">{tout}</b> out</span>
        <span className="sl-pipe">│</span>
        <span>ctx <b style={{ color: ctxPct > 80 ? "var(--error)" : "var(--primary)" }}>{ctxPct}%</b></span>
        <span className="ctx-track"><span className="ctx-fill" style={{ width: ctxPct + "%" }} /></span>
      </div>

      {/* composer */}
      <div className={"composer-wrap" + (drawer || tray ? " has-drawer" : "")}>
        {tray && (
          <div className="drawer tray">
            <div className="drawer-col">
              <div className="dh">▤ attachments <span className="dh-n">{ATTACHMENTS.length}</span></div>
              <div className="chips chips-col">
                {ATTACHMENTS.map((a) => (
                  <span className="chip" key={a.n}>▣ {a.n}<span className="chip-sz">{a.s}</span></span>
                ))}
                <button className="chip chip-add">+ drop files</button>
              </div>
            </div>
            <div className="drawer-col">
              <div className="dh">☲ documents</div>
              {DOCS.map((d) => (
                <button key={d.n} className="dcmd"><span className="dcmd-c doc">▤ {d.n}</span><span className="dcmd-d">{d.t}</span></button>
              ))}
            </div>
            <div className="drawer-col">
              <div className="dh">⚷ keys &amp; secrets <button className="key-eye" onClick={() => setRevealKeys((r) => !r)}>{revealKeys ? "hide" : "reveal"}</button></div>
              {KEYS.map((k) => (
                <div key={k.k} className="keyrow"><span className="key-name">{k.k}</span><span className="key-val">{revealKeys ? k.v : "•".repeat(12)}</span></div>
              ))}
              <button className="dquick">+ add secret</button>
            </div>
          </div>
        )}
        {drawer && (
          <div className="drawer">
            <div className="drawer-col">
              <div className="dh">/ commands</div>
              {SLASH.map((s) => (
                <button key={s.c} className="dcmd" onClick={() => runSlash(s)}>
                  <span className="dcmd-c">{s.c}</span>
                  <span className="dcmd-d">{s.d}</span>
                </button>
              ))}
            </div>
            <div className="drawer-col">
              <div className="dh">@ context <span className="dh-n">{ctx.length}</span></div>
              <div className="chips">
                {ctx.map((f, i) => (
                  <span className="chip" key={i}>{f}<button onClick={() => removeCtx(i)}>×</button></span>
                ))}
                <button className="chip chip-add" onClick={addCtx}>+ add</button>
              </div>
              <div className="dh dh-mt">model</div>
              <div className="seg">
                {MODELS.map((m) => (
                  <button key={m} className={"segb " + (model === m ? "on" : "")} onClick={() => setModel(m)}>{m}</button>
                ))}
              </div>
            </div>
            <div className="drawer-col">
              <div className="dh">quick actions</div>
              {QUICK.map((q) => (
                <button key={q} className="dquick" onClick={() => { insert(q); setDrawer(false); }}>↳ {q}</button>
              ))}
            </div>
          </div>
        )}
        <div className="composer" onClick={() => inputRef.current?.focus()}>
          <button className={"chev " + (drawer ? "open" : "")} onClick={(e) => { e.stopPropagation(); setTray(false); setDrawer((d) => !d); }} title="open command drawer">{drawer ? "▾" : "▸"}</button>
          <span className="cmp-prompt">❯</span>
          <input
            ref={inputRef}
            value={input}
            autoFocus
            placeholder={mode === "shell" ? "ask, or / for commands · @ for files…" : "describe a file change…"}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") send(); }}
          />
          <div className="cmp-actions">
            <button className="ctool" onClick={() => { setTray(false); setDrawer(true); insert("/"); }} title="slash commands">/</button>
            <button className="ctool" onClick={() => { setTray(false); setDrawer(true); }} title="add context">@</button>
            <button className="ctool ctool-model" onClick={() => { setTray(false); setDrawer(true); }} title="model">⏼ {model}</button>
            {input.length > 0 && <span className="cmp-count">{input.length}</span>}
            <Btn onClick={send} variant="send">send ⏎</Btn>
          </div>
        </div>
      </div>

      {/* shortcut bar */}
      </div>
      <Sidebar open={notesOpen} onClose={() => setNotesOpen(false)} />
      </div>

      <div className="bar bar-bottom">
        <button className="kb" onClick={() => setMenuOpen(true)}><b>^T</b> theme</button>
        <button className="kb" onClick={cycleLogo}><b>^L</b> anim</button>
        <button className="kb" onClick={() => setGamesOpen(true)}><b>^G</b> games</button>
        <button className="kb" onClick={newSession}><b>^N</b> new</button>
        <button className="kb" onClick={() => setMode((x) => x === "shell" ? "files" : "shell")}><b>^M</b> mode</button>
        <button className="kb" onClick={() => inputRef.current && inputRef.current.focus()}><b>^J</b> newline</button>
        <button className="kb" onClick={scrollDown}><b>^~</b> home</button>
        <span className="kb-spacer" />
        <button className="kb" onClick={() => setMenuOpen(false)}><b>Esc</b> exit</button>
      </div>

      {menuOpen && (
        <ThemeMenu current={theme} onPick={(id) => { setTheme(id); }} onClose={() => setMenuOpen(false)} />
      )}
      {gamesOpen && (
        <GamesMenu onPick={(id) => { setActiveGame(id); setGamesOpen(false); }} onClose={() => setGamesOpen(false)} />
      )}
      {crewOpen && (
        <CrewMenu active={crew} onToggle={toggleCrew} onClose={() => setCrewOpen(false)} />
      )}
      {activeGame && (
        <GameModal id={activeGame} onClose={() => setActiveGame(null)} />
      )}
      {designerOpen && (
        <div className="ds-scrim" onClick={() => setDesignerOpen(false)}>
        <DesignerChat
          onClose={() => setDesignerOpen(false)}
          api={{
            currentTheme: theme,
            setTheme: (id) => setTheme(id),
            setAnimByName: (id) => { const i = window.ANIMS.findIndex((a) => a.id === id); if (i >= 0) setAnimIndex(i); },
            cycleAnim: cycleLogo,
            openGame: (id) => { setGamesOpen(false); setActiveGame(id); },
          }}
        />
        </div>
      )}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
