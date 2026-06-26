'use strict';

/**
 * icons.js — lightweight Lucide icon helper
 *
 * Usage in innerHTML / template literals:
 *   el.innerHTML = `${icon('trash-2')} Delete`;
 *
 * The MutationObserver at the bottom automatically calls lucide.createIcons()
 * whenever new <i data-lucide="..."> elements appear in the DOM, so callers
 * never need to call createIcons() manually.
 */

/**
 * Returns an <i> tag string that Lucide will replace with an inline SVG.
 * @param {string} name  Lucide icon name in kebab-case (e.g. 'trash-2')
 * @param {Object} [opts]
 * @param {string} [opts.size='1em']   CSS width/height (e.g. '14px', '1em')
 * @param {string} [opts.cls='']       Extra CSS class names
 * @param {string} [opts.style='']     Extra inline CSS appended to the style attr
 */
export function icon(name, opts = {}) {
  const size  = opts.size  || '1em';
  const cls   = opts.cls   ? ` ${opts.cls}` : '';
  const extra = opts.style || '';
  return `<i data-lucide="${name}" class="ui-icon${cls}" style="width:${size};height:${size};display:inline-flex;align-items:center;justify-content:center;vertical-align:middle;flex-shrink:0;${extra}" aria-hidden="true"></i>`;
}

// ── Claude spark mark (NOT a Lucide icon) ───────────────────────────────────────
// The real Claude "spark" logo — an irregular radial sunburst of tapered petals —
// drawn as a raw inline <svg>. Used for the Local Claude Code agent (its create
// tile + agent card/square) so it reads as the genuine brand mark, not a generic
// Lucide sparkle. Painted with `fill="currentColor"`, so whatever colour the
// wrapping element sets (e.g. `.color-claude { color: var(--accent) }`) themes it
// in both dark and light mode. The auto-render observer below ignores it (it has
// no data-lucide attribute), so it is left untouched.
//
// Each spoke is the SAME needle path pointing up — a flame that's full near the
// centre and tapers to a SHARP point at the tip (the quadratics meet at a corner
// at (12,2), not a rounded cap). Per-spoke `rotate()` aims it and a uniform
// `scale(s)` about the centre (12,12) varies its length proportionally. Twelve
// evenly-spaced spokes with only gentle length variation give the full, balanced
// sunburst of the real Claude mark, rather than a sparse off-kilter asterisk.
const _CLAUDE_PETAL = 'M12 12 Q10.8 8.5 12 2 Q13.2 8.5 12 12 Z';
const _CLAUDE_SPOKES = [
  [0, 1.0], [30, 0.9], [60, 0.95], [90, 0.88], [120, 1.0], [150, 0.9],
  [180, 0.97], [210, 0.89], [240, 1.0], [270, 0.9], [300, 0.95], [330, 0.9],
];

/**
 * Returns an inline <svg> string of the Claude spark mark.
 * @param {Object} [opts]
 * @param {string} [opts.size='1em']  CSS width/height (e.g. '24px', '1em')
 * @param {string} [opts.cls='']      Extra CSS class names
 * @param {string} [opts.style='']    Extra inline CSS appended to the style attr
 */
export function claudeMark(opts = {}) {
  const size  = opts.size  || '1em';
  const cls   = opts.cls   ? ` ${opts.cls}` : '';
  const extra = opts.style || '';
  const paths = _CLAUDE_SPOKES.map(([a, s]) =>
    `<path d="${_CLAUDE_PETAL}" transform="rotate(${a} 12 12) translate(12 12) scale(${s}) translate(-12 -12)"/>`
  ).join('');
  return `<svg viewBox="0 0 24 24" class="ui-icon claude-mark${cls}" fill="currentColor" `
    + `style="width:${size};height:${size};display:inline-flex;flex-shrink:0;vertical-align:middle;${extra}" `
    + `aria-hidden="true">${paths}</svg>`;
}

// ── Auto-render ────────────────────────────────────────────────────────────────
// Debounced via requestAnimationFrame so rapid DOM mutations are batched into
// one createIcons() call.

let _rafId = null;

// Convert ONLY unprocessed placeholders. Lucide copies data-lucide onto the
// <svg> it generates AND tags it with class "lucide", so a bare
// createIcons() (which scans every [data-lucide]) re-replaces already-rendered
// icons on every call. Because each replacement is itself a DOM mutation, the
// MutationObserver below would re-fire and we'd spin in a once-per-frame loop —
// constantly tearing icon nodes out from under in-flight clicks (a pointerdown
// on an icon whose node is replaced before mouseup never produces a click).
// Selecting [data-lucide]:not(.lucide) and passing it as `nodes` touches only
// the fresh <i> placeholders, exactly like the rest of the codebase does.
function _renderPending() {
  if (typeof lucide === 'undefined') return;
  const nodes = document.querySelectorAll('[data-lucide]:not(.lucide)');
  if (!nodes.length) return;
  lucide.createIcons({ attrs: { 'stroke-width': 1.5 }, nodes: Array.from(nodes) });
}

function _scheduleRender() {
  if (_rafId) return;
  _rafId = requestAnimationFrame(() => {
    _rafId = null;
    _renderPending();
  });
}

function _setup() {
  // Initial render for static HTML
  _renderPending();
  // Watch for dynamically inserted icons
  new MutationObserver(_scheduleRender).observe(document.body, {
    childList: true,
    subtree: true,
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _setup);
} else {
  _setup();
}
