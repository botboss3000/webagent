/* global React */
/* WEBAGENT — games shell: manifest, shared hooks, board renderer, menu + modal.
   Loads BEFORE the individual game files so its helpers are globally available. */

window.GAMES = [
  { id: "snake",   name: "Snake",        group: "arcade", desc: "eat · grow · don't bite",     controls: "← ↑ → ↓ move · space restart" },
  { id: "pong",    name: "Pong",         group: "arcade", desc: "first to 7 vs CPU",            controls: "↑ ↓ move paddle · space serve" },
  { id: "breakout",name: "Breakout",     group: "arcade", desc: "clear every brick",            controls: "← → paddle · space launch" },
  { id: "flappy",  name: "Flap",         group: "arcade", desc: "thread the pipes",             controls: "space / ↑ flap" },
  { id: "maze",    name: "Maze",         group: "arcade", desc: "reach the exit ▒",             controls: "← ↑ → ↓ move · space new maze" },
  { id: "invaders",name: "Invaders",     group: "arcade", desc: "defend the base",              controls: "← → move · space fire" },
  { id: "2048",    name: "2048",         group: "grid",   desc: "merge to 2048",                controls: "← ↑ → ↓ slide · space new" },
  { id: "tetris",  name: "Tetris",       group: "grid",   desc: "clear the lines",              controls: "← → move · ↑ rotate · ↓ drop" },
  { id: "mines",   name: "Minesweeper",  group: "grid",   desc: "flag every mine",              controls: "click reveal · right-click flag" },
  { id: "ttt",     name: "Tic-Tac-Toe",  group: "grid",   desc: "beat the CPU",                 controls: "click a cell · space reset" },
  { id: "simon",   name: "Simon",        group: "grid",   desc: "repeat the sequence",          controls: "click pads in order" },
  { id: "lights",  name: "Lights Out",   group: "grid",   desc: "turn them all off",            controls: "click to toggle a cross" },
];

/* ── shared hooks (global via function declaration) ── */
function useTick(cb, ms, active) {
  const ref = React.useRef(cb); ref.current = cb;
  React.useEffect(() => {
    if (active === false) return;
    const id = setInterval(() => ref.current(), ms);
    return () => clearInterval(id);
  }, [ms, active]);
}
function useKeydown(handler) {
  const ref = React.useRef(handler); ref.current = handler;
  React.useEffect(() => {
    const h = (e) => ref.current(e);
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);
}

/* ── char board ── */
const GCOL = { p: "var(--primary)", s: "var(--secondary)", e: "var(--error)",
  t: "var(--tool)", d: "var(--dim)", g: "var(--success)", f: "var(--fg)" };
function makeBoard(W, H) {
  const cells = [];
  for (let y = 0; y < H; y++) { const r = []; for (let x = 0; x < W; x++) r.push({ c: " ", k: "f" }); cells.push(r); }
  return {
    W, H, cells,
    clear() { for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) { cells[y][x].c = " "; cells[y][x].k = "f"; } },
    set(x, y, c, k) { x |= 0; y |= 0; if (x < 0 || x >= W || y < 0 || y >= H) return; cells[y][x].c = c; cells[y][x].k = k || "f"; },
    text(x, y, str, k) { for (let i = 0; i < str.length; i++) this.set(x + i, y, str[i], k); },
    textCenter(y, str, k) { this.text(Math.floor((W - str.length) / 2), y, str, k); },
    border(k) {
      for (let x = 0; x < W; x++) { this.set(x, 0, "─", k); this.set(x, H - 1, "─", k); }
      for (let y = 0; y < H; y++) { this.set(0, y, "│", k); this.set(W - 1, y, "│", k); }
      this.set(0, 0, "┌", k); this.set(W - 1, 0, "┐", k); this.set(0, H - 1, "└", k); this.set(W - 1, H - 1, "┘", k);
    },
  };
}
function renderBoardHTML(board) {
  let html = "";
  for (const row of board.cells) {
    let i = 0;
    while (i < row.length) {
      const k = row[i].k; let run = "";
      while (i < row.length && row[i].k === k) { run += row[i].c; i++; }
      run = run.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      html += '<span style="color:' + (GCOL[k] || GCOL.f) + '">' + run + "</span>";
    }
    html += "\n";
  }
  return html;
}
function CharScreen({ board }) {
  return <pre className="game-screen" dangerouslySetInnerHTML={{ __html: renderBoardHTML(board) }} />;
}

/* ── games menu ── */
function GamesMenu({ onPick, onClose }) {
  return (
    <div className="menu-scrim" onClick={onClose}>
      <div className="menu menu-wide" onClick={(e) => e.stopPropagation()}>
        <div className="menu-top"><span>┤ arcade · select a game ├</span>
          <button className="menu-x" onClick={onClose}>[esc]</button></div>
        <div className="games-grid">
          {window.GAMES.map((g) => (
            <button key={g.id} className="game-card" onClick={() => onPick(g.id)}>
              <span className="gc-name">▸ {g.name}</span>
              <span className="gc-desc">{g.desc}</span>
              <span className={"gc-tag gc-" + g.group}>{g.group}</span>
            </button>
          ))}
        </div>
        <div className="menu-foot">click a cartridge to play · esc to close</div>
      </div>
    </div>
  );
}

/* ── game modal host ── */
function GameModal({ id, onClose }) {
  const [score, setScore] = React.useState("0");
  const g = window.GAMES.find((x) => x.id === id);
  const Comp = window["Game_" + id];
  React.useEffect(() => {
    const h = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", h);
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  return (
    <div className="game-scrim">
      <div className="game-console">
        <div className="game-top">
          <span className="gt-name">▸ {g.name}</span>
          <span className="gt-score">SCORE&nbsp;<b>{score}</b></span>
          <button className="menu-x" onClick={onClose}>[esc]</button>
        </div>
        <div className="game-view">
          {Comp ? <Comp setScore={setScore} onClose={onClose} /> : <div className="game-missing">game “{id}” not found</div>}
        </div>
        <div className="game-foot">{g.controls}</div>
      </div>
    </div>
  );
}

Object.assign(window, { useTick, useKeydown, makeBoard, CharScreen, GamesMenu, GameModal });
