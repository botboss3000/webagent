/* global React */
const { useState, useEffect, useRef, useCallback } = React;

/* ───────────────── Animated ASCII stage (12 animations) ───────────────── */
function AsciiStage({ index }) {
  const preRef = useRef(null);
  const rafRef = useRef(0);
  const stRef = useRef({});

  useEffect(() => {
    stRef.current = {};                 // reset per-animation state
    const W = window.STAGE_W, H = window.STAGE_H;
    const buf = window.makeBuf(W, H);
    const anim = window.ANIMS[index] || window.ANIMS[0];
    const t0 = performance.now();
    const frame = (t) => {
      window.clearBuf(buf);
      try { anim.fn(buf, t, stRef.current); } catch (e) { /* keep last frame */ }
      if (preRef.current) preRef.current.textContent = window.bufStr(buf);
    };
    frame(0.05);                        // synchronous first frame (robust if rAF is throttled)
    const tick = (now) => {
      frame((now - t0) / 1000);
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    // fallback timer in case rAF is paused (offscreen/preview); harmless when rAF runs
    const iv = setInterval(() => frame((performance.now() - t0) / 1000), 66);
    return () => { cancelAnimationFrame(rafRef.current); clearInterval(iv); };
  }, [index]);

  return <pre className="stage-pre" ref={preRef} aria-label="WEBAGENT" />;
}

/* ───────────────────────── ASCII button ───────────────────────── */
function Btn({ children, onClick, variant = "default", active, title, keycap }) {
  return (
    <button
      className={"abtn abtn-" + variant + (active ? " abtn-active" : "")}
      onClick={onClick}
      title={title}
    >
      {keycap && <span className="abtn-key">{keycap}</span>}
      <span className="abtn-l">[</span>
      <span className="abtn-t">{children}</span>
      <span className="abtn-r">]</span>
    </button>
  );
}

/* ───────────────────────── Tool output renderers ───────────────────────── */
function ToolBody({ tool, data }) {
  if (tool === "read_file") {
    return (
      <div className="tb">
        <div className="tb-path">{data.path}</div>
        <div className="codeblock">
          {data.lines.map((ln, i) => (
            <div className="cl" key={i}>
              <span className="cl-n">{String(data.start + i).padStart(3, " ")}</span>
              <span className="cl-t">{ln || " "}</span>
            </div>
          ))}
        </div>
      </div>
    );
  }
  if (tool === "run_shell") {
    return (
      <div className="tb">
        <div className="sh-cmd"><span className="sh-prompt">$</span> {data.cmd}</div>
        <div className="codeblock">
          {data.out.map((o, i) => (
            <div className={"sh-line sh-" + (o.k || "out")} key={i}>{o.t || " "}</div>
          ))}
        </div>
        <div className={"sh-exit " + (data.code === 0 ? "ok" : "err")}>
          ⏎ exit {data.code}
        </div>
      </div>
    );
  }
  if (tool === "edit_file" || tool === "write_file") {
    return (
      <div className="tb">
        <div className="tb-path">{data.path}</div>
        <div className="codeblock diff">
          {(data.diff || data.content).map((d, i) => {
            const t = d.t || " ";
            return (
              <div className={"dl dl-" + (t === "+" ? "add" : t === "-" ? "del" : "ctx")} key={i}>
                <span className="dl-g">{t === " " ? "·" : t}</span>
                <span className="dl-t">{d.text || " "}</span>
              </div>
            );
          })}
        </div>
      </div>
    );
  }
  if (tool === "web_search") {
    return (
      <div className="tb">
        <div className="tb-q">◍ {data.query}</div>
        {data.results.map((r, i) => (
          <div className="ws" key={i}>
            <div className="ws-t">{r.title}</div>
            <div className="ws-u">{r.url}</div>
            <div className="ws-s">{r.snippet}</div>
          </div>
        ))}
      </div>
    );
  }
  if (tool === "grep") {
    return (
      <div className="tb">
        <div className="tb-q">⌕ /{data.pattern}/</div>
        <div className="codeblock">
          {data.matches.map((m, i) => (
            <div className="gl" key={i}>
              <span className="gl-f">{m.file}</span>
              <span className="gl-n">:{m.line}</span>
              <span className="gl-t">  {m.text}</span>
            </div>
          ))}
        </div>
      </div>
    );
  }
  if (tool === "glob") {
    return (
      <div className="tb">
        <div className="tb-q">✲ {data.pattern}</div>
        <div className="codeblock">
          {data.matches.map((m, i) => (
            <div className="gl" key={i}><span className="gl-f">{m.file}</span></div>
          ))}
        </div>
      </div>
    );
  }
  if (tool === "memory_search") {
    return (
      <div className="tb">
        <div className="tb-q">❖ {data.query}</div>
        {data.hits.map((h, i) => (
          <div className="mem" key={i}><span className="mem-b">›</span> {h}</div>
        ))}
      </div>
    );
  }
  if (tool === "memory_save") {
    return <div className="tb"><div className="mem"><span className="mem-b">+</span> {data.note}</div></div>;
  }
  if (tool === "todo") {
    return (
      <div className="tb">
        {data.items.map((it, i) => (
          <div className={"todo " + (it.done ? "todo-done" : "")} key={i}>
            <span className="todo-box">[{it.done ? "✓" : " "}]</span> {it.text}
          </div>
        ))}
      </div>
    );
  }
  if (data && data.__raw) {
    // generic renderer for real backend tool_call / tool_result events
    const fmt = (v) => { try { return typeof v === "string" ? v : JSON.stringify(v, null, 2); } catch (e) { return String(v); } };
    const hasRes = ("result" in data) || ("error" in data);
    return (
      <div className="tb">
        <div className="tb-q">args</div>
        <div className="codeblock"><div className="cl"><span className="cl-t">{fmt(data.args)}</span></div></div>
        {hasRes && <div className="tb-q" style={{ marginTop: 8 }}>result</div>}
        {hasRes && (
          <div className="codeblock"><div className="cl">
            <span className="cl-t" style={data.error ? { color: "var(--error)" } : null}>{fmt(data.error != null ? data.error : data.result)}</span>
          </div></div>
        )}
      </div>
    );
  }
  return null;
}

/* ───────────────────────── Tool card ───────────────────────── */
function ToolCard({ item, onToggle, onApprove, onReject, theme }) {
  const meta = window.TOOL_META[item.tool] || { glyph: "•", color: "tool", verb: "" };
  const colorVar = "var(--" + meta.color + ")";
  const pending = item.status === "pending";
  const running = item.status === "running";
  const statusTxt = pending ? "awaiting approval" : running ? "running…" : "done";

  return (
    <div className={"tool " + (pending ? "tool-pending" : "")} style={{ "--tc": colorVar }}>
      <div className="tool-head" onClick={onToggle}>
        <span className="tool-caret">{item.expanded ? "▾" : "▸"}</span>
        <span className="tool-glyph">{meta.glyph}</span>
        <span className="tool-name">{">> " + item.tool}</span>
        <span className="tool-sum">{item.summary}</span>
        <span className={"tool-status st-" + item.status}>
          {running && <span className="spin" />}
          {statusTxt}
        </span>
      </div>
      {item.expanded && (
        <div className="tool-body">
          <ToolBody tool={item.tool} data={item.data} />
          {pending && (
            <div className="approve-row">
              <span className="approve-q">apply this {meta.verb}?</span>
              <Btn variant="accept" keycap="y" onClick={onApprove}>accept</Btn>
              <Btn variant="reject" keycap="n" onClick={onReject}>reject</Btn>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ─────────────────── Lightweight markdown → HTML ─────────────────── */
function mdToHtml(src) {
  if (!src) return "";
  // Step 1: extract and protect code blocks so inner content isn't processed
  const blocks = [];
  let h = src.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const idx = blocks.length;
    const cls = lang ? ' class="md-lang-' + lang + '"' : '';
    blocks.push('<pre class="md-pre"><code' + cls + '>' + escHtml(code.trim()) + '</code></pre>');
    return '\x00BLOCK' + idx + '\x00';
  });
  // Step 2: inline markdown
  h = h
    .replace(/`([^`]+)`/g, '<code class="md-code">$1</code>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/__(.+?)__/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/_(.+?)_/g, '<em>$1</em>')
    .replace(/~~(.+?)~~/g, '<del>$1</del>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a class="md-link" href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/^### (.+)$/gm, '<h4 class="md-h">$1</h4>')
    .replace(/^## (.+)$/gm, '<h3 class="md-h">$1</h3>')
    .replace(/^# (.+)$/gm, '<h2 class="md-h">$1</h2>')
    .replace(/^> (.+)$/gm, '<blockquote class="md-bq">$1</blockquote>')
    .replace(/^[\s]*[-*] (.+)$/gm, '<li class="md-li">$1</li>')
    .replace(/^[\s]*\d+\. (.+)$/gm, '<li class="md-li">$1</li>')
    .replace(/^---+/gm, '<hr class="md-hr" />');
  // Step 3: split into paragraphs on double newlines
  const parts = h.split(/\n\n+/).filter(Boolean);
  h = parts.map(function(p) {
    p = p.replace(/\n/g, '<br />');
    // Don't wrap block-level elements in <p>
    if (/^<(pre|h[234]|blockquote|li|hr)/.test(p)) return p;
    return '<p class="md-p">' + p + '</p>';
  }).join('\n');
  // Step 4: restore code blocks
  h = h.replace(/\x00BLOCK(\d+)\x00/g, function(_, i) { return blocks[+i]; });
  return h;
}
function escHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/* ───────────────────────── Chat messages ───────────────────────── */
function Msg({ item, idx }) {
  const copyMsg = () => {
    navigator.clipboard.writeText(item.text).then(() => {
      const btn = document.querySelector(`.copy-btn-${idx}`);
      if (btn) { btn.textContent = '✓'; setTimeout(() => { btn.textContent = '⎘'; }, 1500); }
    }).catch(() => {});
  };
  if (item.type === "user") {
    return (
      <div className="msg msg-user">
        <div className="msg-bar" />
        <div className="msg-body">
          <div className="msg-who">you</div>
          <div className="md-box">
            <button className={`copy-btn copy-btn-${idx}`} onClick={copyMsg} title="Copy this message">⎘</button>
            <div className="msg-text">{item.text}</div>
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className="msg msg-agent">
      <div className="md-box">
        <button className={`copy-btn copy-btn-${idx}`} onClick={copyMsg} title="Copy this message">⎘</button>
        <div className="msg-text agent" dangerouslySetInnerHTML={{ __html: mdToHtml(item.text) }} />
      </div>
    </div>
  );
}

/* ───────────────────────── Theme menu ───────────────────────── */
function ThemeMenu({ current, onPick, onClose }) {
  const order = window.THEME_ORDER;
  return (
    <div className="menu-scrim" onClick={onClose}>
      <div className="menu" onClick={(e) => e.stopPropagation()}>
        <div className="menu-top">
          <span>┤ color scheme ├</span>
          <button className="menu-x" onClick={onClose}>[esc]</button>
        </div>
        <div className="menu-list">
          {order.map((id) => {
            const t = window.THEMES[id];
            const sel = id === current;
            return (
              <button key={id} className={"menu-row " + (sel ? "sel" : "")} onClick={() => onPick(id)}>
                <span className="menu-cur">{sel ? "›" : " "}</span>
                <span className="sw" style={{ background: t.bg, borderColor: t.border }}>
                  <i style={{ background: t.primary }} />
                  <i style={{ background: t.secondary }} />
                  <i style={{ background: t.tool }} />
                </span>
                <span className="menu-label">{t.label}</span>
                <span className="menu-tag">{t.tags}</span>
              </button>
            );
          })}
        </div>
        <div className="menu-foot">↑↓ move · ⏎ apply · click a swatch</div>
      </div>
    </div>
  );
}

Object.assign(window, { AsciiStage, Btn, ToolCard, ToolBody, Msg, ThemeMenu });
