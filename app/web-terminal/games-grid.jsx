/* global React, useKeydown, useTick */
const { useState: _gS, useRef: _gR, useReducer: _gRed, useEffect: _gE } = React;

/* ════════ 2048 ════════ */
function Game_2048({ setScore }) {
  const [grid, setGrid] = _gS(() => spawn2(spawn2(empty2())));
  const [over, setOver] = _gS(false);
  const sc = _gR(0);
  useKeydown((e) => {
    const map = { ArrowLeft: "L", ArrowRight: "R", ArrowUp: "U", ArrowDown: "D" };
    if (e.key === " ") { sc.current = 0; setScore("0"); setOver(false); setGrid(spawn2(spawn2(empty2()))); e.preventDefault(); return; }
    const dir = map[e.key]; if (!dir) return; e.preventDefault();
    const r = move2(grid, dir);
    if (r.moved) { const ng = spawn2(r.g); sc.current += r.gained; setScore("" + sc.current); setGrid(ng); if (isOver2(ng)) setOver(true); }
  });
  return (
    <div className="grid-game">
      <div className="g2048">
        {grid.flat().map((v, i) => (
          <div key={i} className="t2048" style={tileStyle(v)}>{v || ""}</div>
        ))}
      </div>
      {over && <div className="g-overlay">no moves · <b>space</b> to restart</div>}
    </div>
  );
}
function empty2() { return [[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,0]]; }
function spawn2(g) { const e = []; g.forEach((r, y) => r.forEach((v, x) => { if (!v) e.push([y, x]); })); if (!e.length) return g; const [y, x] = e[Math.random() * e.length | 0]; const ng = g.map((r) => r.slice()); ng[y][x] = Math.random() < 0.9 ? 2 : 4; return ng; }
function slide2(row) { let a = row.filter((v) => v); let gained = 0; for (let i = 0; i < a.length - 1; i++) if (a[i] === a[i + 1]) { a[i] *= 2; gained += a[i]; a.splice(i + 1, 1); } while (a.length < 4) a.push(0); return { row: a, gained }; }
function move2(g, dir) {
  let rows;
  if (dir === "L") rows = g.map((r) => r.slice());
  else if (dir === "R") rows = g.map((r) => r.slice().reverse());
  else if (dir === "U") rows = [0,1,2,3].map((c) => [0,1,2,3].map((r) => g[r][c]));
  else rows = [0,1,2,3].map((c) => [0,1,2,3].map((r) => g[r][c]).reverse());
  let gained = 0; const out = rows.map((r) => { const s = slide2(r); gained += s.gained; return s.row; });
  let ng;
  if (dir === "L") ng = out;
  else if (dir === "R") ng = out.map((r) => r.reverse());
  else if (dir === "U") { ng = empty2(); out.forEach((r, c) => r.forEach((v, i) => ng[i][c] = v)); }
  else { ng = empty2(); out.forEach((r, c) => r.reverse().forEach((v, i) => ng[i][c] = v)); }
  const moved = JSON.stringify(ng) !== JSON.stringify(g);
  return { g: ng, moved, gained };
}
function isOver2(g) { for (let y = 0; y < 4; y++) for (let x = 0; x < 4; x++) { if (!g[y][x]) return false; if (x < 3 && g[y][x] === g[y][x + 1]) return false; if (y < 3 && g[y][x] === g[y + 1][x]) return false; } return true; }
function tileStyle(v) {
  if (!v) return {};
  const pct = Math.min(90, Math.log2(v) * 11);
  return { background: "color-mix(in srgb, var(--primary) " + pct + "%, var(--bg2))",
    color: pct > 45 ? "var(--bg)" : "var(--fg)", borderColor: "var(--borderDim)",
    fontSize: v >= 1024 ? "18px" : v >= 128 ? "22px" : "26px" };
}

/* ════════ TETRIS ════════ */
const TETROMINOES = {
  I: { c: "s", cells: [[0,1],[1,1],[2,1],[3,1]], size: 4 },
  O: { c: "t", cells: [[1,0],[2,0],[1,1],[2,1]], size: 4 },
  T: { c: "p", cells: [[1,0],[0,1],[1,1],[2,1]], size: 3 },
  S: { c: "g", cells: [[1,0],[2,0],[0,1],[1,1]], size: 3 },
  Z: { c: "e", cells: [[0,0],[1,0],[1,1],[2,1]], size: 3 },
  J: { c: "s", cells: [[0,0],[0,1],[1,1],[2,1]], size: 3 },
  L: { c: "t", cells: [[2,0],[0,1],[1,1],[2,1]], size: 3 },
};
function Game_tetris({ setScore }) {
  const CW = 10, CH = 20;
  const st = _gR(null);
  const [, bump] = _gRed((x) => x + 1, 0);
  if (!st.current) st.current = newTetris(CW, CH);
  function rotate(cells, size) { return cells.map(([x, y]) => [size - 1 - y, x]); }
  function collide(s, cells, ox, oy) { return cells.some(([x, y]) => { const gx = ox + x, gy = oy + y; return gx < 0 || gx >= CW || gy >= CH || (gy >= 0 && s.grid[gy][gx]); }); }
  function lock(s) {
    s.piece.cells.forEach(([x, y]) => { const gy = s.oy + y; if (gy >= 0) s.grid[gy][s.ox + x] = s.piece.c; });
    let cleared = 0;
    for (let y = CH - 1; y >= 0; y--) if (s.grid[y].every((c) => c)) { s.grid.splice(y, 1); s.grid.unshift(new Array(CW).fill(0)); cleared++; y++; }
    if (cleared) { s.lines += cleared; s.score += [0, 40, 100, 300, 1200][cleared]; setScore("" + s.score); }
    spawnPiece(s); if (collide(s, s.piece.cells, s.ox, s.oy)) s.over = true;
  }
  function spawnPiece(s) { const keys = Object.keys(TETROMINOES); const t = TETROMINOES[keys[Math.random() * keys.length | 0]]; s.piece = { cells: t.cells.map((c) => c.slice()), c: t.c, size: t.size }; s.ox = 3; s.oy = -1; }
  useKeydown((e) => {
    const s = st.current, k = e.key;
    if (s.over) { if (k === " ") { st.current = newTetris(CW, CH); setScore("0"); bump(); e.preventDefault(); } return; }
    if (k === "ArrowLeft" && !collide(s, s.piece.cells, s.ox - 1, s.oy)) s.ox--;
    else if (k === "ArrowRight" && !collide(s, s.piece.cells, s.ox + 1, s.oy)) s.ox++;
    else if (k === "ArrowDown") { if (!collide(s, s.piece.cells, s.ox, s.oy + 1)) s.oy++; else lock(s); }
    else if (k === "ArrowUp") { const rc = rotate(s.piece.cells, s.piece.size); if (!collide(s, rc, s.ox, s.oy)) s.piece.cells = rc; }
    else if (k === " ") { while (!collide(s, s.piece.cells, s.ox, s.oy + 1)) s.oy++; lock(s); }
    else return; e.preventDefault(); bump();
  });
  useTick(() => { const s = st.current; if (s.over) return; if (!collide(s, s.piece.cells, s.ox, s.oy + 1)) s.oy++; else lock(s); bump(); }, 520);
  const s = st.current;
  const view = s.grid.map((r) => r.slice());
  s.piece.cells.forEach(([x, y]) => { const gy = s.oy + y; if (gy >= 0) view[gy][s.ox + x] = s.piece.c; });
  return (
    <div className="grid-game">
      <div className="tetris" style={{ gridTemplateColumns: "repeat(" + CW + ", 1fr)" }}>
        {view.flat().map((c, i) => <div key={i} className={"tcell " + (c ? "on" : "")} style={c ? { background: "var(--" + tok(c) + ")" } : null} />)}
      </div>
      {s.over && <div className="g-overlay">game over · <b>space</b> to restart</div>}
    </div>
  );
}
function tok(c) { return { p: "primary", s: "secondary", t: "tool", e: "error", g: "success" }[c] || "primary"; }
function newTetris(CW, CH) { const grid = []; for (let y = 0; y < CH; y++) grid.push(new Array(CW).fill(0)); const s = { grid, score: 0, lines: 0, over: false }; const keys = Object.keys(TETROMINOES); const t = TETROMINOES[keys[Math.random() * keys.length | 0]]; s.piece = { cells: t.cells.map((c) => c.slice()), c: t.c, size: t.size }; s.ox = 3; s.oy = -1; return s; }

/* ════════ MINESWEEPER ════════ */
function Game_mines({ setScore }) {
  const N = 12, M = 24;
  const [st, setSt] = _gS(() => newMines(N, M));
  _gE(() => { setScore(st.flags + "/" + M + " ⚑"); }, [st]);
  function reveal(i) {
    if (st.done) return;
    const s = clone(st); const stack = [i];
    if (s.mine[i]) { s.rev = s.rev.map(() => true); s.done = "lose"; setSt(s); return; }
    while (stack.length) { const j = stack.pop(); if (s.rev[j] || s.flag[j]) continue; s.rev[j] = true;
      if (s.num[j] === 0) neighbors(j, N).forEach((k) => { if (!s.rev[k] && !s.mine[k]) stack.push(k); }); }
    if (s.rev.filter((r, k) => r && !s.mine[k]).length === N * N - M) s.done = "win";
    setSt(s);
  }
  function flag(e, i) { e.preventDefault(); if (st.done || st.rev[i]) return; const s = clone(st); s.flag[i] = !s.flag[i]; s.flags += s.flag[i] ? 1 : -1; setSt(s); }
  const numColor = ["", "s", "g", "e", "p", "t", "s", "f", "d"];
  return (
    <div className="grid-game">
      <div className="mines" style={{ gridTemplateColumns: "repeat(" + N + ", 1fr)" }}>
        {Array.from({ length: N * N }).map((_, i) => {
          const r = st.rev[i];
          return (
            <button key={i} className={"mcell " + (r ? "rev" : "")} onClick={() => reveal(i)} onContextMenu={(e) => flag(e, i)}>
              {st.flag[i] && !r ? <span style={{ color: "var(--tool)" }}>⚑</span> :
                r ? (st.mine[i] ? <span style={{ color: "var(--error)" }}>✸</span> :
                  st.num[i] ? <span style={{ color: "var(--" + numColor[st.num[i]] + ")" }}>{st.num[i]}</span> : "") : ""}
            </button>
          );
        })}
      </div>
      {st.done && <div className="g-overlay">{st.done === "win" ? "swept clean! 🎉" : "boom 💥"} · reopen to retry</div>}
    </div>
  );
}
function newMines(N, M) {
  const mine = new Array(N * N).fill(false); let placed = 0;
  while (placed < M) { const i = Math.random() * N * N | 0; if (!mine[i]) { mine[i] = true; placed++; } }
  const num = mine.map((m, i) => m ? 0 : neighbors(i, N).filter((k) => mine[k]).length);
  return { mine, num, rev: new Array(N * N).fill(false), flag: new Array(N * N).fill(false), flags: 0, done: false, N };
}
function neighbors(i, N) { const x = i % N, y = (i / N) | 0; const out = []; for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) { if (!dx && !dy) continue; const nx = x + dx, ny = y + dy; if (nx >= 0 && nx < N && ny >= 0 && ny < N) out.push(ny * N + nx); } return out; }
function clone(s) { return { ...s, rev: s.rev.slice(), flag: s.flag.slice(), mine: s.mine, num: s.num }; }

/* ════════ TIC-TAC-TOE ════════ */
function Game_ttt({ setScore }) {
  const [b, setB] = _gS(() => new Array(9).fill(""));
  const [msg, setMsg] = _gS("your move (X)");
  useKeydown((e) => { if (e.key === " ") { setB(new Array(9).fill("")); setMsg("your move (X)"); setScore("—"); e.preventDefault(); } });
  function win(bd) { const L = [[0,1,2],[3,4,5],[6,7,8],[0,3,6],[1,4,7],[2,5,8],[0,4,8],[2,4,6]]; for (const [a, c, d] of L) if (bd[a] && bd[a] === bd[c] && bd[a] === bd[d]) return bd[a]; return null; }
  function play(i) {
    if (b[i] || win(b)) return;
    const nb = b.slice(); nb[i] = "X";
    if (win(nb)) { setB(nb); setMsg("you win! space=again"); setScore("WIN"); return; }
    if (nb.every((v) => v)) { setB(nb); setMsg("draw · space=again"); setScore("DRAW"); return; }
    const m = best(nb, "O"); nb[m.i] = "O";
    const w = win(nb);
    setB(nb); setMsg(w ? "CPU wins · space=again" : nb.every((v) => v) ? "draw · space=again" : "your move (X)");
    if (w) setScore("LOSE"); else if (nb.every((v) => v)) setScore("DRAW");
  }
  function best(bd, p) {
    const w = win(bd); if (w === "O") return { s: 1 }; if (w === "X") return { s: -1 }; if (bd.every((v) => v)) return { s: 0 };
    let bestM = null;
    bd.forEach((v, i) => { if (v) return; const nb = bd.slice(); nb[i] = p; const r = best(nb, p === "O" ? "X" : "O");
      if (bestM === null || (p === "O" ? r.s > bestM.s : r.s < bestM.s)) bestM = { s: r.s, i }; });
    return bestM;
  }
  return (
    <div className="grid-game">
      <div className="ttt">
        {b.map((v, i) => <button key={i} className="tttcell" onClick={() => play(i)}
          style={{ color: v === "X" ? "var(--primary)" : "var(--secondary)" }}>{v}</button>)}
      </div>
      <div className="g-msg">{msg}</div>
    </div>
  );
}

/* ════════ SIMON ════════ */
function Game_simon({ setScore }) {
  const st = _gR({ seq: [], input: 0, lit: -1, playing: false, lose: false });
  const [, bump] = _gRed((x) => x + 1, 0);
  const PADS = [{ k: "p" }, { k: "s" }, { k: "t" }, { k: "g" }];
  function start() { const s = st.current; s.seq = [Math.random() * 4 | 0]; s.input = 0; s.lose = false; setScore("0"); playSeq(); }
  function playSeq() { const s = st.current; s.playing = true; bump(); let i = 0;
    const step = () => { if (i >= s.seq.length) { s.playing = false; s.lit = -1; bump(); return; } s.lit = s.seq[i]; bump(); setTimeout(() => { s.lit = -1; bump(); i++; setTimeout(step, 180); }, 420); };
    setTimeout(step, 400); }
  function tap(p) { const s = st.current; if (s.playing || s.lose) return; if (s.seq.length === 0) { start(); return; }
    s.lit = p; bump(); setTimeout(() => { s.lit = -1; bump(); }, 160);
    if (p === s.seq[s.input]) { s.input++; if (s.input === s.seq.length) { setScore("" + s.seq.length); s.seq.push(Math.random() * 4 | 0); s.input = 0; playSeq(); } }
    else { s.lose = true; bump(); } }
  const s = st.current;
  return (
    <div className="grid-game">
      <div className="simon">
        {PADS.map((p, i) => <button key={i} className={"simonpad " + (s.lit === i ? "lit" : "")}
          style={{ background: s.lit === i ? "var(--" + p.k + ")" : "color-mix(in srgb, var(--" + p.k + ") 22%, var(--bg2))", borderColor: "var(--" + p.k + ")" }}
          onClick={() => tap(i)} />)}
      </div>
      <div className="g-msg">{s.lose ? "wrong! click a pad to restart" : s.seq.length === 0 ? "click any pad to start" : s.playing ? "watch…" : "your turn — repeat it"}</div>
    </div>
  );
}

/* ════════ LIGHTS OUT ════════ */
function Game_lights({ setScore }) {
  const N = 5;
  const [g, setG] = _gS(() => randLights(N));
  const [moves, setMoves] = _gS(0);
  const won = g.every((v) => !v);
  _gE(() => { setScore(won ? "SOLVED" : moves + " moves"); }, [moves, won]);
  useKeydown((e) => { if (e.key === " ") { setG(randLights(N)); setMoves(0); e.preventDefault(); } });
  function toggle(i) {
    if (won) return;
    const ng = g.slice(); const x = i % N, y = (i / N) | 0;
    [[0,0],[1,0],[-1,0],[0,1],[0,-1]].forEach(([dx, dy]) => { const nx = x + dx, ny = y + dy; if (nx >= 0 && nx < N && ny >= 0 && ny < N) ng[ny * N + nx] = !ng[ny * N + nx]; });
    setG(ng); setMoves((m) => m + 1);
  }
  return (
    <div className="grid-game">
      <div className="lights" style={{ gridTemplateColumns: "repeat(" + N + ", 1fr)" }}>
        {g.map((v, i) => <button key={i} className={"lcell " + (v ? "on" : "")} onClick={() => toggle(i)} />)}
      </div>
      {won && <div className="g-overlay">all out! · <b>space</b> for a new board</div>}
    </div>
  );
}
function randLights(N) { let g = new Array(N * N).fill(false); for (let s = 0; s < 12; s++) { const i = Math.random() * N * N | 0, x = i % N, y = (i / N) | 0; [[0,0],[1,0],[-1,0],[0,1],[0,-1]].forEach(([dx, dy]) => { const nx = x + dx, ny = y + dy; if (nx >= 0 && nx < N && ny >= 0 && ny < N) g[ny * N + nx] = !g[ny * N + nx]; }); } if (g.every((v) => !v)) g[0] = true; return g; }

Object.assign(window, { Game_2048, Game_tetris, Game_mines, Game_ttt, Game_simon, Game_lights });
