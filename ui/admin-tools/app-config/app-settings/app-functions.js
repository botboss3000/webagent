'use strict';

/**
 * App Functions table (App Settings tab).
 *
 * Renders the BACKGROUND app services — Session Namer, Context Control's
 * compaction train, the Render Recorder — that run for the app itself rather
 * than being agent-invoked abilities. They are the ability catalog entries
 * marked `"app_function": true`, split out of the two ability tables by
 * `ui_catalog()` (app/abilities/__init__.py) into the catalog's `app_functions`
 * list. A drop-in ability that sets that flag appears here automatically — no
 * edit to this file or the HTML. See docs/claude/production-editions.md
 * "App functions".
 *
 * Each entry renders as an expandable `.ac-row` (icon + name + description, an
 * on/off toggle, and — for a non-simple function — a settings body). It reuses
 * the SAME app-level config endpoints and shared row/setting helpers as the
 * admin ability table, so an app function's on/off and knobs persist to
 * data/config/agent-abilities.json exactly like an ability's:
 *   • on/off  → POST / DELETE /admin/integrations/abilities/{id}
 *   • config  → GET / PUT     /api/v1/abilities/{id}/config  (+ /config-schema)
 * A `locked_on` function (Context Control) renders its toggle fixed ON.
 */

import { _fetch } from '../utils.js';
import {
  _iconHtml,
  _esc,
  _buildSettingsRowsHtml,
  _markSaving,
  _flashSaveCheck,
} from '../../../shared/js/dom-utils.js';

const _LIST_ID = 'ac-app-functions-list';
const _CHEVRON =
  '<span class="ac-row-chevron"><svg width="13" height="13" viewBox="0 0 24 24" ' +
  'fill="none" stroke="currentColor" stroke-width="2.5">' +
  '<polyline points="9 18 15 12 9 6"/></svg></span>';

// Only re-render once per page open — the catalog is stable for the session.
let _built = false;

export async function initAppFunctions() {
  const host = document.getElementById(_LIST_ID);
  if (!host || _built) return;

  let cat;
  try {
    const res = await fetch('/api/v1/abilities/catalog');
    if (!res.ok) return;
    cat = await res.json();
  } catch (_) {
    return;
  }
  const fns = Array.isArray(cat && cat.app_functions) ? cat.app_functions : [];
  if (!fns.length) return;

  _built = true;
  host.innerHTML = '';
  for (const fn of fns) host.appendChild(_buildRow(fn));

  if (window.lucide) { try { lucide.createIcons(); } catch (_) {} }
}

// Build one expandable app-function row (head + settings body).
function _buildRow(fn) {
  const lockedOn = !!fn.locked_on;
  const enabled = lockedOn ? true : !!fn.enabled;
  const hasBody = fn.simple === false; // non-simple → has a config schema

  const row = document.createElement('div');
  row.className = 'ac-row';
  row.id = 'ac-appfn-' + fn.id;

  const head = document.createElement('div');
  head.className = 'ac-ability-row';
  head.innerHTML =
    `<span class="ac-ability-icon" style="color:${_esc(fn.color || 'var(--brand)')};">` +
      _iconHtml(fn.icon || 'cog', '18px') + '</span>' +
    '<div class="ac-ability-label">' +
      `<div class="ac-ability-name">${_esc(fn.display_name || fn.id)}</div>` +
      `<div class="ac-ability-desc">${_esc(fn.description || '')}</div>` +
    '</div>';

  head.appendChild(_buildToggle(fn, enabled, lockedOn));
  if (hasBody) head.insertAdjacentHTML('beforeend', _CHEVRON);
  row.appendChild(head);

  const body = document.createElement('div');
  body.className = 'ac-ability-body';
  row.appendChild(body);

  // Expand/collapse on head click — but never when the click lands on the
  // toggle (or any control), so flipping the switch doesn't also toggle the row.
  if (hasBody) {
    head.addEventListener('click', (e) => {
      if (e.target.closest('.ac-config-control, .conn-toggle-wrap, input, button, select, a, label')) return;
      row.classList.toggle('expanded');
      if (row.classList.contains('expanded')) _fillBody(fn, body);
    });
  }

  return row;
}

// The right-aligned on/off switch (the same `.conn-toggle` the ability tables
// use). A locked-on function is fixed ON and cannot be changed.
function _buildToggle(fn, enabled, lockedOn) {
  const ctrl = document.createElement('span');
  ctrl.className = 'ac-config-control';
  const title = lockedOn ? 'Always on — cannot be deactivated' : '';
  ctrl.innerHTML =
    `<label class="conn-toggle-wrap ac-ability-toggle-wrap${lockedOn ? ' ac-ability-toggle-locked' : ''}"` +
      (title ? ` title="${_esc(title)}"` : '') + '>' +
      `<input type="checkbox" class="conn-toggle"${enabled ? ' checked' : ''}${lockedOn ? ' disabled' : ''}>` +
      '<span class="conn-toggle-track"></span>' +
    '</label>';

  if (lockedOn) return ctrl;

  const input = ctrl.querySelector('input');
  input.addEventListener('change', async () => {
    const desired = input.checked;
    input.disabled = true;
    _markSaving(ctrl);
    try {
      const res = await _fetch(
        '/admin/integrations/abilities/' + encodeURIComponent(fn.id),
        { method: desired ? 'POST' : 'DELETE' },
      );
      if (!res.ok) throw new Error('save failed');
      _flashSaveCheck(ctrl, true);
    } catch (_) {
      input.checked = !desired; // revert
      _flashSaveCheck(ctrl, false);
    } finally {
      input.disabled = false;
    }
  });
  return ctrl;
}

// Fill the settings body on first expand: the function's config-schema rows
// (pre-filled with saved app-level values), each saving on change. Cheap
// functions with no schema show a short note instead.
async function _fillBody(fn, body) {
  if (body.dataset.filled) return;
  body.dataset.filled = '1';
  body.innerHTML = '<div class="ac-hint">Loading…</div>';

  let schema = null;
  let current = {};
  try {
    const [sRes, cRes] = await Promise.all([
      fetch('/api/v1/abilities/' + encodeURIComponent(fn.id) + '/config-schema'),
      _fetch('/api/v1/abilities/' + encodeURIComponent(fn.id) + '/config'),
    ]);
    schema = sRes.ok ? await sRes.json() : null;
    if (cRes.ok) {
      const cData = await cRes.json();
      if (cData && cData.ability_settings && typeof cData.ability_settings === 'object') {
        current = cData.ability_settings;
      }
    }
  } catch (_) { /* fall through to the empty note */ }

  if (!schema || !Array.isArray(schema.settings) || !schema.settings.length) {
    body.innerHTML = '<div class="ac-hint">No additional settings.</div>';
    return;
  }

  body.innerHTML = _buildSettingsRowsHtml(schema, current, 'ac-appfn-field', 'data-config-key');
  if (schema.note) {
    body.insertAdjacentHTML('afterbegin', `<div class="ac-hint">${_esc(schema.note)}</div>`);
  }

  const fields = [...body.querySelectorAll('.ac-appfn-field')];
  const collect = () => {
    const out = {};
    fields.forEach((f) => { out[f.dataset.configKey] = f.value; });
    return out;
  };
  fields.forEach((f) => {
    const fieldCtrl = f.closest('.ac-config-control');
    let prev = f.value;
    f.addEventListener('change', async () => {
      f.disabled = true;
      _markSaving(fieldCtrl);
      try {
        const res = await _fetch(
          '/api/v1/abilities/' + encodeURIComponent(fn.id) + '/config',
          {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ability_settings: collect() }),
          },
        );
        if (!res.ok) throw new Error('save failed');
        prev = f.value;
        _flashSaveCheck(fieldCtrl, true);
      } catch (_) {
        f.value = prev; // revert to last saved
        _flashSaveCheck(fieldCtrl, false);
      } finally {
        f.disabled = false;
      }
    });
  });

  if (window.lucide) { try { lucide.createIcons(); } catch (_) {} }
}
