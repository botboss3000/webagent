/* global React */
/* WEBAGENT — right sidebar: todos + notes scratchpad, persisted to localStorage. */

function lsGet(k, def) { try { const v = localStorage.getItem(k); return v == null ? def : JSON.parse(v); } catch (e) { return def; } }
function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }

function Sidebar({ open, onClose }) {
  const { useState, useEffect, useRef } = React;
  const [todos, setTodos] = useState(() => lsGet("webagent.todos", [
    { text: "Try the designer chat (^D)", done: true },
    { text: "Pick a light theme", done: false },
    { text: "Beat my 2048 high score", done: false },
  ]));
  const [notes, setNotes] = useState(() => lsGet("webagent.notes", "// scratchpad\nthemes to try: rosé dawn, ocean\n"));
  const [draft, setDraft] = useState("");
  const draftRef = useRef(null);

  useEffect(() => lsSet("webagent.todos", todos), [todos]);
  useEffect(() => lsSet("webagent.notes", notes), [notes]);

  const add = () => { const t = draft.trim(); if (!t) return; setTodos((x) => [...x, { text: t, done: false }]); setDraft(""); };
  const toggle = (i) => setTodos((x) => x.map((t, k) => k === i ? { ...t, done: !t.done } : t));
  const del = (i) => setTodos((x) => x.filter((_, k) => k !== i));
  const clearDone = () => setTodos((x) => x.filter((t) => !t.done));

  const done = todos.filter((t) => t.done).length;

  return (
    <>
      {open && <div className="sb-scrim" onClick={onClose} />}
      <aside className={"sidebar" + (open ? " open" : "")} aria-hidden={!open}>
      <div className="sb-top">
        <span className="sb-title">▤ notes &amp; todos</span>
        <button className="menu-x" onClick={onClose}>[esc]</button>
      </div>
      <div className="sb-body">
        <div className="sb-sec">
          <div className="sb-h">
            <span>tasks</span>
            <span className="sb-count">{done}/{todos.length}</span>
          </div>
          <div className="sb-add">
            <span className="sb-plus">+</span>
            <input ref={draftRef} value={draft} placeholder="add a task…"
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") add(); }} />
          </div>
          <div className="sb-todos">
            {todos.length === 0 && <div className="sb-empty">no tasks — add one above</div>}
            {todos.map((t, i) => (
              <div key={i} className={"sb-todo" + (t.done ? " done" : "")}>
                <button className="sb-box" onClick={() => toggle(i)}>{t.done ? "[✓]" : "[ ]"}</button>
                <span className="sb-todotext" onClick={() => toggle(i)}>{t.text}</span>
                <button className="sb-del" onClick={() => del(i)}>×</button>
              </div>
            ))}
          </div>
          {done > 0 && <button className="sb-clear" onClick={clearDone}>clear {done} done</button>}
        </div>

        <div className="sb-sec sb-notes-sec">
          <div className="sb-h"><span>notes</span><span className="sb-count">{notes.length} ch</span></div>
          <textarea className="sb-notes" value={notes} placeholder="jot anything…"
            onChange={(e) => setNotes(e.target.value)} spellCheck={false} />
        </div>
      </div>
      <div className="sb-foot">saved locally · persists across reloads</div>
    </aside></>
  );
}

Object.assign(window, { Sidebar });
