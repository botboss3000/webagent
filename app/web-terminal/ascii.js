/* WEBAGENT TUI — ASCII stage engine.
   A tiny character framebuffer used by the animated logo (anims.js) and any
   other code that wants to paint glyphs into a <pre>. Single colour comes from
   CSS (.stage-pre { color: var(--primary) }); brightness is conveyed purely by
   the glyph chosen, so every animation is just "set this char at this cell". */

/* Stage size in characters. Height matches .logo-wrap min-height (20 * 10.5px ≈
   210px); width is generous and simply clips (overflow:hidden) on narrow panels. */
window.STAGE_W = 100;
window.STAGE_H = 20;

function makeBuf(w, h) {
  return {
    w, h,
    ch: new Array(w * h).fill(" "),
    z: new Float32Array(w * h),          // depth buffer for the 3D animations

    inb(x, y) { return x >= 0 && x < this.w && y >= 0 && y < this.h; },

    /* plain write — last writer wins */
    set(x, y, c) {
      x |= 0; y |= 0;
      if (x < 0 || x >= this.w || y < 0 || y >= this.h) return;
      this.ch[y * this.w + x] = c;
    },
    get(x, y) {
      x |= 0; y |= 0;
      if (x < 0 || x >= this.w || y < 0 || y >= this.h) return " ";
      return this.ch[y * this.w + x];
    },
    /* depth-tested write — only paints if nearer (larger z = closer to camera) */
    plotz(x, y, zv, c) {
      x |= 0; y |= 0;
      if (x < 0 || x >= this.w || y < 0 || y >= this.h) return;
      const i = y * this.w + x;
      if (zv > this.z[i]) { this.z[i] = zv; this.ch[i] = c; }
    },
    /* Bresenham line */
    line(x0, y0, x1, y1, c) {
      x0 |= 0; y0 |= 0; x1 |= 0; y1 |= 0;
      const dx = Math.abs(x1 - x0), dy = -Math.abs(y1 - y0);
      const sx = x0 < x1 ? 1 : -1, sy = y0 < y1 ? 1 : -1;
      let err = dx + dy;
      for (;;) {
        this.set(x0, y0, c);
        if (x0 === x1 && y0 === y1) break;
        const e2 = 2 * err;
        if (e2 >= dy) { err += dy; x0 += sx; }
        if (e2 <= dx) { err += dx; y0 += sy; }
      }
    },
    text(x, y, str) { for (let i = 0; i < str.length; i++) this.set(x + i, y, str[i]); },
    center(y, str) { this.text(((this.w - str.length) / 2) | 0, y, str); },
  };
}

function clearBuf(buf) {
  buf.ch.fill(" ");
  buf.z.fill(-Infinity);
}

function bufStr(buf) {
  const w = buf.w, h = buf.h, ch = buf.ch;
  const rows = new Array(h);
  for (let y = 0; y < h; y++) rows[y] = ch.slice(y * w, y * w + w).join("");
  return rows.join("\n");
}

Object.assign(window, { makeBuf, clearBuf, bufStr });
