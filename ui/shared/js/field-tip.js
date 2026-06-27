'use strict';

// ── Shared field-help "?" badge + floating bubble ────────────────────────────
//
// A small circled "?" beside a label. CLICK / TAP opens a real help bubble; a
// mouse hover also previews it. One shared floating bubble is appended to <body>
// so it can never be clipped by a panel's scroll box. This is the same
// affordance the App Settings → Deploy form grew per-field (where to find a
// value / how to choose); factored out here so any config form can reuse it.
//
// The bubble's look lives in ui/shared/css/app3.css (.ac-field-tip + .ac-tip-pop)
// — the single source of truth, themed for dark + light. Don't restyle here.
//
// Usage:
//   import { tipBadge } from './field-tip.js';
//   label.appendChild(tipBadge('Plain-text help.'));
//   label.appendChild(tipBadge({ html: '<b>Steps…</b>', wide: true }));   // rich
//
// `tip` is either a plain string or an object { html, wide }:
//   • html  — trusted markup (numbered <ol>, <b>, <code>…). The bubble is
//             pointer-events:none, so links inside are NOT clickable — describe
//             the navigation in words instead.
//   • wide  — widen the bubble for multi-step setup guides (else the 260px CSS
//             default, fine for one-liners).

let _pop = null;        // the shared floating bubble element
let _anchor = null;     // badge it currently points at
let _pinned = false;    // true when opened by click/tap (stays until dismissed)

function _ensurePop() {
  if (_pop) return _pop;
  _pop = document.createElement('div');
  _pop.className = 'ac-tip-pop';
  _pop.setAttribute('role', 'tooltip');
  _pop.hidden = true;
  document.body.appendChild(_pop);
  // Dismiss a pinned bubble on outside click, Escape, scroll or resize.
  document.addEventListener('click', (e) => {
    if (!_pinned) return;
    if (e.target === _anchor) return;
    _hide();
  }, true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') _hide(); });
  window.addEventListener('scroll', () => { if (!_pop.hidden) _hide(); }, true);
  window.addEventListener('resize', () => { if (!_pop.hidden) _hide(); });
  return _pop;
}

function _hide() {
  if (_anchor) _anchor.classList.remove('is-open');
  _pinned = false;
  _anchor = null;
  if (_pop) { _pop.hidden = true; _pop.classList.remove('show'); _pop.style.maxWidth = ''; }
}

function _show(anchor, pinned) {
  const tip = anchor._tip;
  if (!tip) return;
  const pop = _ensurePop();
  if (typeof tip === 'object') {
    pop.innerHTML = tip.html || '';
    pop.style.maxWidth = tip.wide ? '340px' : '';
  } else {
    pop.textContent = tip;
    pop.style.maxWidth = '';
  }
  pop.hidden = false;                       // make it measurable before placing
  _anchor = anchor;
  _pinned = pinned || _pinned;
  // Light up the badge when pinned by a click, so the click visibly "lands".
  document.querySelectorAll('.ac-field-tip.is-open').forEach(el => { if (el !== anchor) el.classList.remove('is-open'); });
  anchor.classList.toggle('is-open', _pinned);
  const margin = 8;
  const r = anchor.getBoundingClientRect();
  const pr = pop.getBoundingClientRect();
  let left = r.left + r.width / 2 - pr.width / 2;
  let top = r.bottom + 8;
  if (left < margin) left = margin;
  if (left + pr.width > window.innerWidth - margin) left = window.innerWidth - margin - pr.width;
  if (top + pr.height > window.innerHeight - margin) top = r.top - pr.height - 8;   // flip up
  pop.style.left = Math.round(Math.max(margin, left)) + 'px';
  pop.style.top = Math.round(Math.max(margin, top)) + 'px';
  pop.classList.add('show');
}

function _toggle(b) {
  if (_pinned && _anchor === b) _hide();
  else _show(b, true);
}

// Build a circled "?" help badge for `tip` (string or {html,wide}). Returns null
// when tip is empty so callers can `const t = tipBadge(x); if (t) lab.append(t)`.
export function tipBadge(tip) {
  if (!tip || (typeof tip === 'object' && !tip.html)) return null;
  const b = document.createElement('span');
  b.className = 'ac-field-tip';
  b.textContent = '?';
  b._tip = tip;
  b.tabIndex = 0;
  b.setAttribute('role', 'button');
  const aria = typeof tip === 'object' ? 'Setup help' : ('Help: ' + tip);
  b.setAttribute('aria-label', aria);
  // preventDefault + stopPropagation so a badge inside a <label> doesn't toggle
  // the control, and so the outside-click dismiss doesn't fight us.
  b.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); _toggle(b); });
  b.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _toggle(b); }
  });
  b.addEventListener('mouseenter', () => { if (!_pinned) _show(b, false); });
  b.addEventListener('mouseleave', () => { if (!_pinned) _hide(); });
  b.addEventListener('blur', () => { if (!_pinned) _hide(); });
  return b;
}
