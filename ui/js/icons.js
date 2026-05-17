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

// ── Auto-render ────────────────────────────────────────────────────────────────
// Debounced via requestAnimationFrame so rapid DOM mutations are batched into
// one createIcons() call.

let _rafId = null;

function _scheduleRender() {
  if (_rafId) return;
  _rafId = requestAnimationFrame(() => {
    _rafId = null;
    if (typeof lucide !== 'undefined' && document.querySelector('[data-lucide]')) {
      lucide.createIcons({ attrs: { 'stroke-width': 1.5 } });
    }
  });
}

function _setup() {
  // Initial render for static HTML
  if (typeof lucide !== 'undefined') {
    lucide.createIcons({ attrs: { 'stroke-width': 1.5 } });
  }
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
