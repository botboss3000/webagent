/* global React, makeBoard, CharScreen, useTick, useKeydown */
const { useRef: _uR, useReducer: _uRed, useState: _uS, useEffect: _uE } = React;

/* ════════ SNAKE ════════ */
function Game_snake({ setScore }) {
  const W = 44, H = 22;
  const st = _uR(null);
  const [, bump] = _uRed((x) => x + 1, 0);
  if (!st.current) st.current = newSnake(W, H);
  function place(s) { while (true) { const f = { x: 1 + (Math.random() * (W - 2) | 0), y: 1 + (Math.random() * (H - 2) | 0) }; if (!s.snake.some((p) => p.x === f.x && p.y === f.y)) { s.food = f; return; } } }
  useKeydown((e) => {
    const s = st.current, k = e.key;
    if (k === "ArrowUp" && s.dir.y === 0) s.ndir = { x: 0, y: -1 };
    else if (k === "ArrowDown" && s.dir.y === 0) s.ndir = { x: 0, y: 1 };
    else if (k === "ArrowLeft" && s.dir.x === 0) s.ndir = { x: -1, y: 0 };
    else if (k === "ArrowRight" && s.dir.x === 0) s.ndir = { x: 1, y: 0 };
    else if (k === " " || k === "Enter") { if (s.dead) { st.current = newSnake(W, H); setScore("0"); bump(); } }
    else return;
    e.preventDefault();
  });
  useTick(() => {
    const s = st.current; if (s.dead) return;
    if (s.ndir) { s.dir = s.ndir; s.ndir = null; }
    const head = { x: ((s.snake[0].x - 1 + s.dir.x + (W - 2)) % (W - 2)) + 1, y: ((s.snake[0].y - 1 + s.dir.y + (H - 2)) % (H - 2)) + 1 };
    if (s.snake.some((p) => p.x === head.x && p.y === head.y)) { s.dead = true; bump(); return; }
    s.snake.unshift(head);
    if (head.x === s.food.x && head.y === s.food.y) { s.score += 10; setScore("" + s.score); place(s); }
    else s.snake.pop();
    bump();
  }, 85);
  const s = st.current, b = makeBoard(W, H); b.border("d");
  b.set(s.food.x, s.food.y, "●", "e");
  s.snake.forEach((p, i) => b.set(p.x, p.y, i === 0 ? "█" : "▓", i === 0 ? "p" : "g"));
  if (s.dead) { b.textCenter(H / 2 - 1, " GAME OVER ", "e"); b.textCenter(H / 2 + 1, " press space ", "d"); }
  return <CharScreen board={b} />;
}
function newSnake(W, H) { return { snake: [{ x: 8, y: (H / 2) | 0 }, { x: 7, y: (H / 2) | 0 }], dir: { x: 1, y: 0 }, ndir: null, food: { x: 28, y: (H / 2) | 0 }, dead: false, score: 0 }; }

/* ════════ PONG ════════ */
function Game_pong({ setScore }) {
  const W = 48, H = 24;
  const st = _uR(null);
  const [, bump] = _uRed((x) => x + 1, 0);
  if (!st.current) st.current = { py: H / 2 - 2, cy: H / 2 - 2, ph: 4, bx: W / 2, by: H / 2, vx: -0.8, vy: 0.4, ps: 0, cs: 0, up: 0, dn: 0 };
  useKeydown((e) => {
    const s = st.current, k = e.key;
    if (k === "ArrowUp") s.up = 1; else if (k === "ArrowDown") s.dn = 1;
    else if (k === " ") { s.bx = W / 2; s.by = H / 2; s.vx = (Math.random() < 0.5 ? -1 : 1) * 0.85; s.vy = (Math.random() - 0.5); }
    else return; e.preventDefault();
  });
  _uE(() => { const up = (e) => { const s = st.current; if (e.key === "ArrowUp") s.up = 0; if (e.key === "ArrowDown") s.dn = 0; }; window.addEventListener("keyup", up); return () => window.removeEventListener("keyup", up); }, []);
  useTick(() => {
    const s = st.current;
    if (s.up) s.py = Math.max(1, s.py - 1.2); if (s.dn) s.py = Math.min(H - 1 - s.ph, s.py + 1.2);
    const tgt = s.by - s.ph / 2; s.cy += Math.sign(tgt - s.cy) * Math.min(0.9, Math.abs(tgt - s.cy)); s.cy = Math.max(1, Math.min(H - 1 - s.ph, s.cy));
    s.bx += s.vx; s.by += s.vy;
    if (s.by < 1) { s.by = 1; s.vy = Math.abs(s.vy); } if (s.by > H - 2) { s.by = H - 2; s.vy = -Math.abs(s.vy); }
    if (s.bx <= 3 && s.by >= s.py && s.by <= s.py + s.ph) { s.vx = Math.abs(s.vx) + 0.04; s.vy += (s.by - (s.py + s.ph / 2)) * 0.18; s.bx = 3; }
    if (s.bx >= W - 4 && s.by >= s.cy && s.by <= s.cy + s.ph) { s.vx = -Math.abs(s.vx) - 0.04; s.vy += (s.by - (s.cy + s.ph / 2)) * 0.18; s.bx = W - 4; }
    if (s.bx < 0) { s.cs++; reset(s); } if (s.bx > W) { s.ps++; reset(s); }
    setScore(s.ps + " : " + s.cs);
    bump();
  }, 45);
  function reset(s) { s.bx = W / 2; s.by = H / 2; s.vx = (s.bx ? -0.85 : 0.85); s.vy = (Math.random() - 0.5); }
  const s = st.current, b = makeBoard(W, H); b.border("d");
  for (let y = 1; y < H - 1; y += 2) b.set(W / 2 | 0, y, "┊", "d");
  for (let i = 0; i < s.ph; i++) { b.set(2, s.py + i, "█", "p"); b.set(W - 3, s.cy + i, "█", "s"); }
  b.set(s.bx, s.by, "●", "t");
  if (s.ps >= 7 || s.cs >= 7) b.textCenter(H / 2, s.ps > s.cs ? " YOU WIN! space " : " CPU WINS — space ", s.ps > s.cs ? "g" : "e");
  return <CharScreen board={b} />;
}

/* ════════ BREAKOUT ════════ */
function Game_breakout({ setScore }) {
  const W = 46, H = 26;
  const st = _uR(null);
  const [, bump] = _uRed((x) => x + 1, 0);
  if (!st.current) st.current = newBreak(W, H);
  useKeydown((e) => {
    const s = st.current, k = e.key;
    if (k === "ArrowLeft") s.px = Math.max(1, s.px - 2.4);
    else if (k === "ArrowRight") s.px = Math.min(W - 1 - s.pw, s.px + 2.4);
    else if (k === " ") { if (s.stuck) s.stuck = false; if (s.dead || s.won) st.current = newBreak(W, H); }
    else return; e.preventDefault();
  });
  useTick(() => {
    const s = st.current; if (s.dead || s.won) return;
    if (s.stuck) { s.bx = s.px + s.pw / 2; s.by = H - 3; bump(); return; }
    s.bx += s.vx; s.by += s.vy;
    if (s.bx < 1) { s.bx = 1; s.vx = Math.abs(s.vx); } if (s.bx > W - 2) { s.bx = W - 2; s.vx = -Math.abs(s.vx); }
    if (s.by < 1) { s.by = 1; s.vy = Math.abs(s.vy); }
    if (s.by >= H - 3 && s.bx >= s.px && s.bx <= s.px + s.pw) { s.vy = -Math.abs(s.vy); s.vx += (s.bx - (s.px + s.pw / 2)) * 0.16; s.by = H - 3; }
    if (s.by > H - 1) { s.dead = true; }
    const bx = s.bx | 0, by = s.by | 0;
    if (by >= 2 && by < 2 + s.rows && s.bricks[by - 2] && s.bricks[by - 2][bx]) { s.bricks[by - 2][bx] = 0; s.vy = -s.vy; s.score += 5; setScore("" + s.score); s.left--; if (s.left <= 0) s.won = true; }
    bump();
  }, 40);
  const s = st.current, b = makeBoard(W, H); b.border("d");
  const palette = ["e", "t", "p", "s", "g"];
  for (let r = 0; r < s.rows; r++) for (let x = 1; x < W - 1; x++) if (s.bricks[r][x]) b.set(x, 2 + r, "▓", palette[r % palette.length]);
  for (let i = 0; i < s.pw; i++) b.set(s.px + i, H - 2, "▀", "p");
  b.set(s.bx, s.by, "●", "t");
  if (s.dead) b.textCenter(H / 2, " BALL LOST — space ", "e");
  if (s.won) b.textCenter(H / 2, " CLEARED! — space ", "g");
  return <CharScreen board={b} />;
}
function newBreak(W, H) {
  const rows = 5, bricks = []; let left = 0;
  for (let r = 0; r < rows; r++) { const row = new Array(W).fill(0); for (let x = 2; x < W - 2; x++) { row[x] = 1; left++; } bricks.push(row); }
  return { px: W / 2 - 3, pw: 7, bx: W / 2, by: H - 3, vx: 0.6, vy: -0.9, bricks, rows, left, score: 0, stuck: true, dead: false, won: false };
}

/* ════════ FLAPPY ════════ */
function Game_flappy({ setScore }) {
  const W = 46, H = 24;
  const st = _uR(null);
  const [, bump] = _uRed((x) => x + 1, 0);
  if (!st.current) st.current = newFlap(W, H);
  const flap = () => { const s = st.current; if (s.dead) { st.current = newFlap(W, H); setScore("0"); } else s.vy = -1.15; };
  useKeydown((e) => { if (e.key === " " || e.key === "ArrowUp") { flap(); e.preventDefault(); } });
  useTick(() => {
    const s = st.current; if (s.dead) return;
    s.vy += 0.14; s.by += s.vy;
    s.pipes.forEach((p) => p.x -= 0.7);
    if (s.pipes[0] && s.pipes[0].x < -1) { s.pipes.shift(); }
    const last = s.pipes[s.pipes.length - 1];
    if (!last || last.x < W - 16) s.pipes.push({ x: W - 1, gap: 4 + (Math.random() * (H - 12) | 0), scored: false });
    const bx = 8;
    s.pipes.forEach((p) => { if (!p.scored && p.x < bx) { p.scored = true; s.score++; setScore("" + s.score); }
      if (Math.round(p.x) === bx && (s.by < p.gap || s.by > p.gap + 5)) s.dead = true; });
    if (s.by < 1 || s.by > H - 2) s.dead = true;
    bump();
  }, 55);
  const s = st.current, b = makeBoard(W, H); b.border("d");
  s.pipes.forEach((p) => { const px = Math.round(p.x); for (let y = 1; y < H - 1; y++) if (y < p.gap || y > p.gap + 5) b.set(px, y, "█", "g"); });
  b.set(8, s.by, s.dead ? "✖" : "◆", s.dead ? "e" : "t");
  if (s.dead) { b.textCenter(H / 2 - 1, " CRASHED ", "e"); b.textCenter(H / 2 + 1, " space to retry ", "d"); }
  return <CharScreen board={b} />;
}
function newFlap(W, H) { return { by: H / 2, vy: 0, pipes: [{ x: W - 1, gap: 8, scored: false }], dead: false, score: 0 }; }

/* ════════ MAZE ════════ */
function Game_maze({ setScore }) {
  const W = 43, H = 23; // odd
  const st = _uR(null);
  const [, bump] = _uRed((x) => x + 1, 0);
  if (!st.current) st.current = genMaze(W, H);
  useKeydown((e) => {
    const s = st.current, k = e.key; let dx = 0, dy = 0;
    if (k === "ArrowUp") dy = -1; else if (k === "ArrowDown") dy = 1; else if (k === "ArrowLeft") dx = -1; else if (k === "ArrowRight") dx = 1;
    else if (k === " ") { st.current = genMaze(W, H); setScore("0"); bump(); return; }
    else return; e.preventDefault();
    if (s.won) return;
    const nx = s.px + dx, ny = s.py + dy;
    if (s.grid[ny] && s.grid[ny][nx] === 0) { s.px = nx; s.py = ny; s.steps++; setScore("" + s.steps); if (nx === W - 2 && ny === H - 2) s.won = true; }
    bump();
  });
  const s = st.current, b = makeBoard(W, H);
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) if (s.grid[y][x]) b.set(x, y, "█", "d");
  b.set(W - 2, H - 2, "▒", "g");
  b.set(s.px, s.py, "☻", "p");
  if (s.won) b.textCenter(H / 2, " SOLVED! space=new ", "g");
  return <CharScreen board={b} />;
}
function genMaze(W, H) {
  const g = []; for (let y = 0; y < H; y++) g.push(new Array(W).fill(1));
  const stack = [[1, 1]]; g[1][1] = 0;
  const dirs = [[0, -2], [0, 2], [-2, 0], [2, 0]];
  while (stack.length) {
    const [cx, cy] = stack[stack.length - 1];
    const opts = dirs.map(([dx, dy]) => [cx + dx, cy + dy, dx, dy]).filter(([nx, ny]) => nx > 0 && ny > 0 && nx < W - 1 && ny < H - 1 && g[ny][nx] === 1);
    if (!opts.length) { stack.pop(); continue; }
    const [nx, ny, dx, dy] = opts[Math.random() * opts.length | 0];
    g[cy + dy / 2][cx + dx / 2] = 0; g[ny][nx] = 0; stack.push([nx, ny]);
  }
  g[H - 2][W - 2] = 0;
  return { grid: g, px: 1, py: 1, won: false, steps: 0 };
}

/* ════════ INVADERS ════════ */
function Game_invaders({ setScore }) {
  const W = 46, H = 24;
  const st = _uR(null);
  const [, bump] = _uRed((x) => x + 1, 0);
  if (!st.current) st.current = newInv(W, H);
  useKeydown((e) => {
    const s = st.current, k = e.key;
    if (k === "ArrowLeft") s.sx = Math.max(1, s.sx - 2);
    else if (k === "ArrowRight") s.sx = Math.min(W - 2, s.sx + 2);
    else if (k === " ") { if (s.dead || s.won) { st.current = newInv(W, H); setScore("0"); } else if (s.bullets.length < 3) s.bullets.push({ x: s.sx, y: H - 3 }); }
    else return; e.preventDefault();
  });
  useTick(() => {
    const s = st.current; if (s.dead || s.won) return;
    s.t++;
    if (s.t % 6 === 0) {
      let minx = 99, maxx = -1; s.aliens.forEach((a) => { if (a.alive) { minx = Math.min(minx, a.x); maxx = Math.max(maxx, a.x); } });
      if (minx + s.dir < 1 || maxx + s.dir > W - 2) { s.dir *= -1; s.aliens.forEach((a) => a.y++); }
      else s.aliens.forEach((a) => a.x += s.dir);
    }
    s.bullets.forEach((bl) => bl.y -= 1);
    s.bullets = s.bullets.filter((bl) => bl.y > 0);
    s.bullets.forEach((bl) => s.aliens.forEach((a) => { if (a.alive && Math.abs(a.x - bl.x) <= 1 && a.y === bl.y) { a.alive = false; bl.y = -1; s.score += 10; setScore("" + s.score); } }));
    s.bullets = s.bullets.filter((bl) => bl.y > 0);
    if (s.aliens.every((a) => !a.alive)) s.won = true;
    if (s.aliens.some((a) => a.alive && a.y >= H - 3)) s.dead = true;
    bump();
  }, 60);
  const s = st.current, b = makeBoard(W, H); b.border("d");
  s.aliens.forEach((a) => { if (a.alive) b.set(a.x, a.y, "Ѫ", "s"); });
  s.bullets.forEach((bl) => b.set(bl.x, bl.y, "|", "t"));
  b.set(s.sx, H - 2, "▲", "p");
  if (s.dead) b.textCenter(H / 2, " OVERRUN — space ", "e");
  if (s.won) b.textCenter(H / 2, " CLEARED! — space ", "g");
  return <CharScreen board={b} />;
}
function newInv(W, H) {
  const aliens = []; for (let r = 0; r < 4; r++) for (let c = 0; c < 9; c++) aliens.push({ x: 6 + c * 4, y: 2 + r * 2, alive: true });
  return { aliens, sx: W / 2 | 0, bullets: [], dir: 1, t: 0, score: 0, dead: false, won: false };
}

Object.assign(window, { Game_snake, Game_pong, Game_breakout, Game_flappy, Game_maze, Game_invaders });
