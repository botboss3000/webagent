'use strict';

/**
 * Data Management tab — storage provider configs + display settings.
 */

import { registerSectionHook } from '../nav.js';
import { initStickyNav } from '../sticky-nav.js';

export function init() {
  // Expandable ability-table rows. The eight storage/maintenance configs are
  // grouped into four `.ac-list`s of `.ac-row`s (same component as the
  // Automation Engine in Agent Settings). Each row collapses to one line and
  // expands in place to reveal its configurator — wired here, mirroring
  // agent-settings.js `_wireAutomationRows`. The control drivers
  // (shared/js/storage.js, shared/js/data-management.js) bind by element id and
  // are unaffected; their loads still run on section-show, so each row's
  // status badge populates while collapsed.
  _wireRows();
  // Sticky section navigator (shared — see ../sticky-nav.js). The four group
  // headings are always-open `.ac-category-group`s (no chevron) — same as
  // App Settings / Agent Settings; on this opted-in page their headings pin/stack
  // as you scroll and clicking a heading jumps to it. Re-measure each time the
  // page is shown (hidden at init() → everything measures 0).
  initStickyNav('database');
  registerSectionHook('database', () => initStickyNav('database'));
}

// Make every `.ac-row` in the section expand/collapse on a header click. A
// click on a control inside the row body never reaches this handler (it's bound
// to the `.ac-ability-row` head, a sibling of the body), but the guard is kept
// for parity with the other ability-table pages.
function _wireRows() {
  const sec = document.getElementById('ac-section-database');
  if (!sec) return;
  for (const row of sec.querySelectorAll('.ac-list > .ac-row')) {
    const head = row.querySelector(':scope > .ac-ability-row');
    if (!head || head.dataset.rowWired) continue;
    head.dataset.rowWired = '1';
    head.addEventListener('click', (e) => {
      if (e.target.closest('select, input, button, a, label, pre, textarea, summary')) return;
      row.classList.toggle('expanded');
    });
  }
}

export function load() {
  try { if (typeof window.__refreshStorageSection === 'function') window.__refreshStorageSection(); } catch {}
}