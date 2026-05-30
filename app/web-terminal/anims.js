/* WEBAGENT TUI — animated ASCII logo bank.
   window.ANIMS is an array of { id, name, desc, fn(buf, t, state) }.
   - buf   : the char framebuffer from ascii.js (buf.set / buf.plotz / buf.line)
   - t     : elapsed seconds (real wall time; identical across the rAF + interval
             drivers in components.jsx, so motion is cadence-independent)
   - state : a per-animation scratch object, reset when the animation changes.
   Colour is uniform (CSS), so brightness is expressed by glyph choice. */

(function () {
  const RAMP  = " .:-=+*#%@";   // 10 levels, [0] = empty
  const SHADE = ".,-~:;=!*#$@"; // 12 levels, classic luminance ramp (never empty)
  const lvl = (s, b) => s[Math.max(0, Math.min(s.length - 1, (b * s.length) | 0))];
  const ramp  = (b) => lvl(RAMP, b);
  const shade = (b) => lvl(SHADE, b);
  const dt = (s, t) => { const d = s.last == null ? 0.016 : Math.min(0.05, t - s.last); s.last = t; return d; };

  /* 5×5 block font for the wordmark */
  const FONT = {
    W: ["#   #", "#   #", "# # #", "## ##", "#   #"],
    E: ["#####", "#    ", "###  ", "#    ", "#####"],
    B: ["#### ", "#   #", "#### ", "#   #", "#### "],
    A: [" ### ", "#   #", "#####", "#   #", "#   #"],
    G: [" ####", "#    ", "#  ##", "#   #", " ### "],
    N: ["#   #", "##  #", "# # #", "#  ##", "#   #"],
    T: ["#####", "  #  ", "  #  ", "  #  ", "  #  "],
  };
  const WORD = "WEBAGENT";

  const ANIMS = [
    /* 1 ─ spinning torus (the donut) */
    { id: "donut", name: "donut", desc: "a spinning ASCII torus",
      fn(buf, t) {
        const W = buf.w, H = buf.h;
        const A = t * 1.1, B = t * 0.7;
        const cA = Math.cos(A), sA = Math.sin(A), cB = Math.cos(B), sB = Math.sin(B);
        const R1 = 1, R2 = 2, K2 = 5, K1 = H * K2 / (R1 + R2);
        for (let th = 0; th < 6.283; th += 0.07) {
          const ct = Math.cos(th), st = Math.sin(th);
          for (let ph = 0; ph < 6.283; ph += 0.02) {
            const cp = Math.cos(ph), sp = Math.sin(ph);
            const cx = R2 + R1 * ct, cy = R1 * st;
            const x = cx * (cB * cp + sA * sB * sp) - cy * cA * sB;
            const y = cx * (sB * cp - sA * cB * sp) + cy * cA * cB;
            const ooz = 1 / (K2 + cA * cx * sp + cy * sA);
            const xp = (W / 2 + K1 * ooz * x) | 0;
            const yp = (H / 2 - K1 * ooz * y * 0.5) | 0;
            const L = cp * ct * sB - cA * ct * sp - sA * st + cB * (cA * st - ct * sA * sp);
            if (L > 0) buf.plotz(xp, yp, ooz, shade(L * 0.9));
          }
        }
      } },

    /* 2 ─ rotating wireframe cube */
    { id: "cube", name: "cube", desc: "a tumbling wireframe cube",
      fn(buf, t) {
        const W = buf.w, H = buf.h, ax = t * 0.8, ay = t * 1.1;
        const cx = Math.cos(ax), sx = Math.sin(ax), cy = Math.cos(ay), sy = Math.sin(ay);
        const V = [[-1,-1,-1],[1,-1,-1],[1,1,-1],[-1,1,-1],[-1,-1,1],[1,-1,1],[1,1,1],[-1,1,1]];
        const f = H * 1.1;
        const P = V.map(([x, y, z]) => {
          const X = x * cy - z * sy, Z1 = x * sy + z * cy;
          const Y = y * cx - Z1 * sx, Z = y * sx + Z1 * cx;
          const s = f / (Z + 4);
          return [W / 2 + X * s, H / 2 - Y * s * 0.5];
        });
        [[0,1],[1,2],[2,3],[3,0],[4,5],[5,6],[6,7],[7,4],[0,4],[1,5],[2,6],[3,7]]
          .forEach(([a, b]) => buf.line(P[a][0], P[a][1], P[b][0], P[b][1], "#"));
        P.forEach((p) => buf.set(p[0], p[1], "@"));
      } },

    /* 3 ─ shaded rotating globe */
    { id: "globe", name: "globe", desc: "a lit, rotating sphere",
      fn(buf, t) {
        const W = buf.w, H = buf.h, rot = t * 0.6, R = Math.min(H / 2 - 1, 9);
        const lxn = 0.5, lyn = -0.45, lzn = 0.74;
        for (let i = 0; i <= 24; i++) {
          const lat = -Math.PI / 2 + Math.PI * i / 24;
          for (let j = 0; j < 48; j++) {
            const lon = 2 * Math.PI * j / 48 + rot;
            const x = Math.cos(lat) * Math.cos(lon), y = Math.sin(lat), z = Math.cos(lat) * Math.sin(lon);
            if (z < 0) continue;
            const L = x * lxn + y * lyn + z * lzn;
            buf.plotz(W / 2 + x * R * 2, H / 2 - y * R, z, shade(Math.max(0, L * 0.7 + 0.25)));
          }
        }
      } },

    /* 4 ─ warp starfield */
    { id: "starfield", name: "starfield", desc: "stars streaking past at warp",
      fn(buf, t, s) {
        const W = buf.w, H = buf.h, d = dt(s, t);
        if (!s.st) { s.st = []; for (let i = 0; i < 110; i++) s.st.push({ x: Math.random() * 2 - 1, y: Math.random() * 2 - 1, z: Math.random() }); }
        const C = ".+*#@";
        s.st.forEach((p) => {
          p.z -= d * 0.55;
          if (p.z <= 0.02) { p.x = Math.random() * 2 - 1; p.y = Math.random() * 2 - 1; p.z = 1; }
          const k = 0.5 / p.z;
          buf.set(W / 2 + p.x * k * W, H / 2 - p.y * k * H, C[Math.min(C.length - 1, ((1 - p.z) * C.length) | 0)]);
        });
      } },

    /* 5 ─ checkerboard tunnel */
    { id: "tunnel", name: "tunnel", desc: "a checkered tunnel rushing in",
      fn(buf, t) {
        const W = buf.w, H = buf.h, cx = W / 2, cy = H / 2;
        for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
          const dx = (x - cx) / (W * 0.5), dy = (y - cy) / (H * 0.5) * 2;
          const dist = Math.hypot(dx, dy) + 1e-4;
          const u = Math.floor(0.4 / dist + t * 1.5), v = Math.floor(Math.atan2(dy, dx) / Math.PI * 5 + t * 0.3);
          const lit = Math.min(1, dist * 1.2) * (((u + v) & 1) ? 1 : 0.35);
          const c = ramp(lit); if (c !== " ") buf.set(x, y, c);
        }
      } },

    /* 6 ─ rising fire */
    { id: "fire", name: "fire", desc: "a flickering wall of flame",
      fn(buf, t, s) {
        const W = buf.w, H = buf.h;
        if (!s.g) s.g = new Float32Array(W * H);
        s.acc = (s.acc || 0) + dt(s, t);
        const C = " .:^*#%@";
        let n = 0;
        while (s.acc > 0.045 && n < 3) {
          s.acc -= 0.045; n++;
          for (let x = 0; x < W; x++) s.g[(H - 1) * W + x] = Math.random() < 0.75 ? 1 : 0.25;
          for (let y = 0; y < H - 1; y++) for (let x = 0; x < W; x++) {
            const b = s.g[(y + 1) * W + x] + s.g[(y + 1) * W + ((x - 1 + W) % W)] +
                      s.g[(y + 1) * W + ((x + 1) % W)] + s.g[Math.min(H - 1, y + 2) * W + x];
            s.g[y * W + x] = Math.max(0, b / 4 - 0.045 - Math.random() * 0.02);
          }
        }
        for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
          const b = s.g[y * W + x]; if (b > 0.05) buf.set(x, y, C[Math.min(C.length - 1, (b * C.length) | 0)]);
        }
      } },

    /* 7 ─ plasma field */
    { id: "plasma", name: "plasma", desc: "an oozing plasma field",
      fn(buf, t) {
        const W = buf.w, H = buf.h;
        for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
          const v = Math.sin(x * 0.18 + t * 1.3) + Math.sin(y * 0.32 + t * 1.1) +
                    Math.sin((x + y) * 0.14 + t) +
                    Math.sin(Math.hypot(x - W / 2, (y - H / 2) * 2) * 0.18 - t * 1.4);
          buf.set(x, y, ramp((v + 4) / 8));
        }
      } },

    /* 8 ─ Conway's Game of Life */
    { id: "life", name: "life", desc: "Conway's Game of Life",
      fn(buf, t, s) {
        const W = buf.w, H = buf.h;
        if (!s.g) { s.g = new Uint8Array(W * H); for (let i = 0; i < W * H; i++) s.g[i] = Math.random() < 0.28 ? 1 : 0; s.acc = 0; }
        s.acc = (s.acc || 0) + dt(s, t);
        if (s.acc > 0.12) {
          s.acc = 0;
          const nx = new Uint8Array(W * H); let alive = 0;
          for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
            let c = 0;
            for (let dy = -1; dy <= 1; dy++) for (let dxi = -1; dxi <= 1; dxi++) {
              if (!dxi && !dy) continue; c += s.g[((y + dy + H) % H) * W + ((x + dxi + W) % W)];
            }
            const a = s.g[y * W + x], v = (a && (c === 2 || c === 3)) || (!a && c === 3) ? 1 : 0;
            nx[y * W + x] = v; alive += v;
          }
          if (alive < W * H * 0.03) for (let i = 0; i < W * H; i++) if (Math.random() < 0.18) nx[i] = 1;
          s.g = nx;
        }
        for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) if (s.g[y * W + x]) buf.set(x, y, "#");
      } },

    /* 9 ─ matrix rain */
    { id: "matrix", name: "matrix", desc: "falling green code (sans the green)",
      fn(buf, t, s) {
        const W = buf.w, H = buf.h, d = dt(s, t);
        if (!s.d) { s.d = []; for (let x = 0; x < W; x++) s.d[x] = { y: Math.random() * H, sp: 6 + Math.random() * 14 }; }
        const TAIL = "@#%*+=:-. ";
        for (let x = 0; x < W; x++) {
          const col = s.d[x]; col.y += col.sp * d; const head = Math.floor(col.y);
          for (let k = 0; k < TAIL.length; k++) { const yy = head - k; if (yy >= 0 && yy < H) buf.set(x, yy, TAIL[k]); }
          if (head - TAIL.length > H) { col.y = -Math.random() * H; col.sp = 6 + Math.random() * 14; }
        }
      } },

    /* 10 ─ interfering waves */
    { id: "wave", name: "wave", desc: "two interfering sine ribbons",
      fn(buf, t) {
        const W = buf.w, H = buf.h;
        for (let x = 0; x < W; x++) {
          const y1 = H / 2 + Math.sin(x * 0.2 + t * 2) * 4 + Math.sin(x * 0.07 - t * 1.3) * 3;
          const y2 = H / 2 + Math.sin(x * 0.15 - t * 1.7) * 5;
          for (let y = 0; y < H; y++) {
            const d1 = Math.abs(y - y1), d2 = Math.abs(y - y2);
            if (d1 < 3.2) buf.set(x, y, ramp(1 - d1 / 3.2));
            else if (d2 < 2.4) buf.set(x, y, lvl("·:+*", 1 - d2 / 2.4));
          }
        }
      } },

    /* 11 ─ the WEBAGENT wordmark with a travelling gleam */
    { id: "banner", name: "banner", desc: "the WEBAGENT wordmark, shimmering",
      fn(buf, t) {
        const W = buf.w, H = buf.h;
        const span = WORD.length * 6 - 1;
        const gx0 = ((W - span) / 2) | 0, gy0 = ((H - 5) / 2 | 0) + Math.round(Math.sin(t * 1.5));
        for (let li = 0; li < WORD.length; li++) {
          const g = FONT[WORD[li]], lx = gx0 + li * 6;
          for (let r = 0; r < 5; r++) for (let c = 0; c < 5; c++) {
            if (g[r][c] === "#") { const gx = lx + c; buf.set(gx, gy0 + r, Math.sin(gx * 0.35 - t * 5) > 0.55 ? "@" : "#"); }
          }
        }
        buf.center(gy0 + 6, "a g e n t   h a r n e s s");
      } },

    /* 12 ─ rotating spiral arms */
    { id: "spiral", name: "spiral", desc: "four sweeping spiral arms",
      fn(buf, t) {
        const W = buf.w, H = buf.h, cx = W / 2, cy = H / 2, rmax = Math.min(W / 2 / 1.7, H);
        for (let r = 0; r < rmax; r += 0.5) for (let a = 0; a < 4; a++) {
          const ang = r * 0.35 + t * 1.2 + a * Math.PI / 2;
          buf.set(cx + Math.cos(ang) * r * 1.7, cy + Math.sin(ang) * r, ramp(1 - r / rmax));
        }
      } },

    /* 13 ─ rain with splashes */
    { id: "rain", name: "rain", desc: "rainfall with little splashes",
      fn(buf, t, s) {
        const W = buf.w, H = buf.h, d = dt(s, t);
        if (!s.r) { s.r = []; s.sp = []; for (let i = 0; i < 50; i++) s.r.push({ x: Math.random() * W, y: Math.random() * H, sp: 18 + Math.random() * 20 }); }
        s.r.forEach((p) => {
          p.y += p.sp * d;
          if (p.y >= H - 1) { s.sp.push({ x: p.x, y: H - 1, life: 0.35 }); p.y = 0; p.x = Math.random() * W; p.sp = 18 + Math.random() * 20; }
          buf.set(p.x, p.y, "|"); buf.set(p.x, p.y - 1, ".");
        });
        s.sp = s.sp.filter((p) => { p.life -= d; if (p.life <= 0) return false; buf.set(p.x - 1, p.y, "\\"); buf.set(p.x + 1, p.y, "/"); buf.set(p.x, p.y - 1, "^"); return true; });
      } },

    /* 14 ─ rotating DNA helix */
    { id: "dna", name: "dna", desc: "a turning double helix",
      fn(buf, t) {
        const W = buf.w, H = buf.h, cy = H / 2;
        for (let x = 0; x < W; x++) {
          const ph = x * 0.25 - t * 2.2, y1 = cy + Math.sin(ph) * 6, y2 = cy - Math.sin(ph) * 6;
          buf.set(x, y1, "O"); buf.set(x, y2, "O");
          if (x % 4 === 0) { const a = Math.min(y1, y2) | 0, b = Math.max(y1, y2) | 0; for (let y = a + 1; y < b; y++) buf.set(x, y, Math.cos(ph) > 0 ? "-" : "."); }
        }
      } },

    /* 15 ─ animated Julia set */
    { id: "julia", name: "julia", desc: "a morphing Julia fractal",
      fn(buf, t) {
        const W = buf.w, H = buf.h, cr = 0.7885 * Math.cos(t * 0.5), ci = 0.7885 * Math.sin(t * 0.5), M = 26;
        for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
          let zr = (x / W * 2 - 1) * 1.6, zi = (y / H * 2 - 1) * 1.1, i = 0;
          for (; i < M; i++) { const r2 = zr * zr, i2 = zi * zi; if (r2 + i2 > 4) break; const tt = 2 * zr * zi; zr = r2 - i2 + cr; zi = tt + ci; }
          if (i < M) buf.set(x, y, ramp(i / M));
        }
      } },

    /* 16 ─ bouncing logo (DVD-style) */
    { id: "bounce", name: "bounce", desc: "a logo bouncing off the walls",
      fn(buf, t, s) {
        const W = buf.w, H = buf.h, d = dt(s, t), lw = 8, lh = 3;
        if (!s.b) s.b = { x: 5, y: 3, vx: 17, vy: 9 };
        const b = s.b; b.x += b.vx * d; b.y += b.vy * d;
        if (b.x <= 0) { b.x = 0; b.vx = Math.abs(b.vx); } if (b.x >= W - lw) { b.x = W - lw; b.vx = -Math.abs(b.vx); }
        if (b.y <= 0) { b.y = 0; b.vy = Math.abs(b.vy); } if (b.y >= H - lh) { b.y = H - lh; b.vy = -Math.abs(b.vy); }
        const bx = b.x | 0, by = b.y | 0;
        buf.text(bx, by, "┌──────┐"); buf.text(bx, by + 1, "│ WA ◆ │"); buf.text(bx, by + 2, "└──────┘");
      } },

    /* 17 ─ pond ripples */
    { id: "ripple", name: "ripple", desc: "overlapping water ripples",
      fn(buf, t) {
        const W = buf.w, H = buf.h;
        const src = [[W * 0.5, H * 0.5], [W * 0.3, H * 0.6], [W * 0.72, H * 0.4]];
        for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
          let v = 0; for (const [sx, sy] of src) v += Math.sin(Math.hypot(x - sx, (y - sy) * 2) * 0.5 - t * 4);
          const c = ramp((v / src.length + 1) / 2); if (c !== " ") buf.set(x, y, c);
        }
      } },

    /* 18 ─ swarm chasing an attractor */
    { id: "swarm", name: "swarm", desc: "a swarm chasing a moving lure",
      fn(buf, t, s) {
        const W = buf.w, H = buf.h, d = dt(s, t);
        if (!s.p) { s.p = []; for (let i = 0; i < 44; i++) s.p.push({ x: Math.random() * W, y: Math.random() * H, a: Math.random() * 6.28 }); }
        const ax = W / 2 + Math.cos(t * 0.7) * W * 0.32, ay = H / 2 + Math.sin(t * 0.9) * H * 0.32;
        s.p.forEach((p) => {
          let da = Math.atan2(ay - p.y, ax - p.x) - p.a;
          while (da > Math.PI) da -= 6.283; while (da < -Math.PI) da += 6.283;
          p.a += da * Math.min(1, d * 3);
          p.x = (p.x + Math.cos(p.a) * 22 * d + W) % W; p.y = (p.y + Math.sin(p.a) * 22 * d * 0.6 + H) % H;
          buf.set(p.x, p.y, "·");
        });
        buf.set(ax, ay, "✦");
      } },

    /* 19 ─ orbiting bodies with trails */
    { id: "orbits", name: "orbits", desc: "little worlds orbiting a star",
      fn(buf, t, s) {
        const W = buf.w, H = buf.h, cx = W / 2, cy = H / 2;
        if (!s.o) { s.o = []; for (let i = 0; i < 6; i++) s.o.push({ r: 3 + i * 1.6, sp: 1.2 / (1 + i * 0.5), ph: Math.random() * 6.28, ch: "@#*+o.".charAt(i) }); }
        buf.set(cx, cy, "☀");
        s.o.forEach((o) => {
          const ang = o.ph + t * o.sp;
          for (let k = 1; k <= 6; k++) buf.set(cx + Math.cos(ang - k * 0.12) * o.r * 1.7, cy + Math.sin(ang - k * 0.12) * o.r, ".");
          buf.set(cx + Math.cos(ang) * o.r * 1.7, cy + Math.sin(ang) * o.r, o.ch);
        });
      } },

    /* 20 ─ oscilloscope Lissajous */
    { id: "scope", name: "scope", desc: "a Lissajous oscilloscope trace",
      fn(buf, t) {
        const W = buf.w, H = buf.h, cx = W / 2, cy = H / 2, A = W * 0.32, B = H * 0.4, ph = t * 0.6;
        for (let i = 0; i < 280; i++) { const u = i / 280 * 6.283; buf.set(cx + Math.sin(3 * u + ph) * A, cy + Math.sin(2 * u) * B, i % 20 < 2 ? "O" : "*"); }
      } },
  ];

  window.ANIMS = ANIMS;
})();
