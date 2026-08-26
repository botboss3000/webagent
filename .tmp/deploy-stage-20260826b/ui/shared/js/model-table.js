'use strict';

import { makeRowsReorderable } from './ordering.js';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'.

/**
 * Shared LLM model-table component — the "Models" configurator + saved-models
 * list (Standard / Premium / Vision / Image capability columns + token usage) used in TWO places:
 *
 *   • Admin → Agent Settings → Models   (ui/admin-tools/instances/app-config/agent-settings/agent-settings.js)
 *   • Agent card → Config tab → LLM     (ui/main-panel/agents/js/tab-config.js)
 *
 * ╔══════════════════════════════════════════════════════════════════════════╗
 * ║  SISTER-PANEL: MODEL-TABLE — these two mounts MUST stay mirrored in look   ║
 * ║  and behaviour. They are the SAME component (this file). Change the table  ║
 * ║  here and BOTH update together; only the data source differs (the admin    ║
 * ║  default-provider config vs. a per-agent llm_config blob), injected as an   ║
 * ║  `adapter`. Don't fork a second copy. Grep this banner to find both mounts. ║
 * ╚══════════════════════════════════════════════════════════════════════════╝
 *
 * The markup (configurator rows + advanced row) is built here, not in HTML, so
 * the single source of truth is one file. All DOM refs are instance-scoped
 * closures (NO document.getElementById) so two instances never collide on ids.
 * Styling reuses the app-wide `.ac-*` model-table classes in ui/shared/css/app3.css.
 *
 * Contract — mountModelTable(host, opts) → { reload(), destroy(), getState() }
 *   opts.adapter (required):
 *     loadConfig()              → { provider, base_url, api_key, model,
 *                                   text_capable, image_capable, image_out_capable,
 *                                   providers:[...] }   (the saved roster)
 *     saveSingle(single)        → persist the initial model configuration (provider/url/key/model + caps)
 *     saveRoster({providers})   → persist the saved-models list
 *     loadPresets?()            → { key: {name, base_url} }     (default: /admin/settings/providers)
 *     fetchModels?({provider, api_key, base_url}) → { models:[...], error? }
 *     fetchModelInfo?(model)    → { info }                       (default: /admin/settings/model-info)
 *     fetchModelUsage?({model, provider}) → usage                (default: /admin/settings/model-usage)
 *     clearSingle?()            → optional server wipe of the single default
 *   opts.fetchFn   — fetch-like (default window.fetch); admin passes its _fetch
 *   opts.apiPath   — path mapper (default identity); admin passes apiPath
 *   opts.advancedExtra(bodyEl) — optional: append extra controls to the advanced body
 *   opts.onModelChange()       — optional: fired after a model add/clear (e.g. refresh footer)
 *   opts.loadDirectives?()     — optional: resolve the admin's roster-role prompt
 *                                directives → {standard:'', premium:'', ...} (grep
 *                                ROLE-DIRECTIVE-INJECT). Absent (or a rejected
 *                                promise) hides the "Prompt injections" strip.
 *   opts.saveDirectives?(dict) — optional: persist the full directives dict (admin
 *                                only). Absent ⇒ the editor opens read-only.
 *   opts.directivesEditable?   — bool: whether the directive editor may save.
 */

// Unified save-status indicator (spinner → green ✓ / orange ⚠ + popup, centered
// ON TOP of the clicked control). Shared with the ability tree + appearance panel
// so every saved-model box speaks the same save language. SISTER-PANEL note in
// dom-utils.js. (dom-utils.js is the shared utility hub — importing it is the
// sanctioned alternative to cloning these helpers, NOT a cross-page dependency.)
import { _markSaving, _flashSaveCheck } from './dom-utils.js';

// ── tiny local helpers (self-contained — no cross-page imports) ──────────────
// (These lowercase markSaving/flashCheck drive the configurator's side-slot tick
// — the `.agents-autosave-check` span beside Provider / URL / Key / Add. The
// imported `_markSaving`/`_flashSaveCheck` above drive the on-top overlay used by
// the saved-models capability boxes + Default radio.)
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
function makeCheck() {
  const el = document.createElement('span');
  el.className = 'agents-autosave-check';
  el.setAttribute('aria-hidden', 'true');
  return el;
}
function markSaving(el) {
  if (!el) return;
  clearTimeout(el._fadeT);
  el.classList.remove('saved', 'error');
  el.innerHTML = '<span class="agents-spinner"></span>';
}
function flashCheck(el, ok, errMsg = '') {
  if (!el) return;
  clearTimeout(el._fadeT);
  el.classList.remove('saved', 'error');
  el.innerHTML = '';
  if (ok) {
    el.classList.add('saved'); el.textContent = '✓'; el.title = 'Saved';
    el._fadeT = setTimeout(() => { el.classList.remove('saved'); el.textContent = ''; }, 2200);
  } else {
    el.classList.add('error'); el.textContent = '!'; el.title = errMsg || 'Save failed';
    el._fadeT = setTimeout(() => { el.classList.remove('error'); el.textContent = ''; }, 4000);
  }
}
function fmtTokens(n) {
  if (!n) return '—';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}
function fmtContext(ctx) {
  if (!ctx || typeof ctx !== 'number') return '';
  if (ctx >= 1_000_000) return `${(ctx / 1_000_000).toFixed(ctx % 1_000_000 ? 1 : 0)}M ctx`;
  if (ctx >= 1000) return `${Math.round(ctx / 1000)}K ctx`;
  return `${ctx} ctx`;
}
function fmtPrice(inCost, outCost) {
  const f = (v) => (v === 0 ? 'free' : (typeof v === 'number' ? `$${v}` : null));
  const a = f(inCost), b = f(outCost);
  if (a === 'free' && b === 'free') return 'free';
  if (a && b) return `${a}/${b} per 1M`;
  if (a) return `${a} in /1M`;
  if (b) return `${b} out /1M`;
  return '';
}

// ── Hold-to-cancel guard for the saved-model capability checkboxes ───────────
// A capability box (Txt / Txt+ / In / Out) commits its change to the server the
// instant it's ticked. To stop an accidental DOUBLE-CLICK from silently flipping
// it straight back, the box LOCKS the moment it's toggled: the underlying
// checkbox is disabled and a hazard triangle drops on TOP of it (blocking a
// second click) while the save runs. The ONLY way to reverse the change is a
// deliberate LONG-PRESS of that triangle — the same hold-to-confirm affordance
// the delete buttons use (ui/shared/js/admin-ability-table.js) — which reverts
// the box and persists the reverted value. Once the save lands (or the hold
// cancels it) the triangle settles to the shared green ✓ / orange ⚠ save overlay
// (dom-utils `_flashSaveCheck`), so it still speaks the app-wide save language.
const _HAZ_MIN_MS = 900;    // keep the hazard (its cancel window) up at least this long
const _HAZ_HOLD_MS = 600;   // long-press duration that triggers a cancel

const _HAZ_SVG = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';

// Drop the hazard cover onto a cap cell and wire its long-press. `onHoldComplete`
// fires after a full _HAZ_HOLD_MS press; releasing early resets the fill. The
// returned node carries `_holding` (a hold is in progress) and `_teardown()`.
function _buildCapHazard(cell, onHoldComplete) {
  const haz = document.createElement('span');
  haz.className = 'ac-cap-hazard';
  haz.title = 'Hold to undo';
  const fill = document.createElement('span'); fill.className = 'ac-cap-hazard-fill';
  const ico = document.createElement('span'); ico.className = 'ac-cap-hazard-icon'; ico.innerHTML = _HAZ_SVG;
  haz.appendChild(fill); haz.appendChild(ico);
  cell.appendChild(haz);
  requestAnimationFrame(() => haz.classList.add('show'));

  let raf = null, startAt = 0;
  haz._holding = false;
  const _stop = () => { if (raf) { cancelAnimationFrame(raf); raf = null; } };
  const step = () => {
    const pct = Math.min(100, ((Date.now() - startAt) / _HAZ_HOLD_MS) * 100);
    fill.style.height = pct + '%';
    if (pct >= 100) { _stop(); haz._holding = false; onHoldComplete(); return; }
    raf = requestAnimationFrame(step);
  };
  const startHold = (e) => {
    if (e && e.button != null && e.button !== 0) return;   // left button / touch only
    haz._holding = true; startAt = Date.now(); fill.style.height = '0%'; step();
  };
  const cancelHold = () => { if (!haz._holding) return; haz._holding = false; _stop(); fill.style.height = '0%'; };
  haz.addEventListener('pointerdown', startHold);
  haz.addEventListener('pointerup', cancelHold);
  haz.addEventListener('pointerleave', cancelHold);
  haz.addEventListener('pointercancel', cancelHold);
  // Swallow the click so a completed/aborted hold never bubbles to the row's
  // expand toggle or re-triggers the checkbox.
  haz.addEventListener('click', (e) => { e.stopPropagation(); e.preventDefault(); });

  haz._teardown = () => { _stop(); haz.remove(); };
  return haz;
}

// Run a capability-checkbox change behind the hold-to-cancel guard.
//   writeFlag(v)  — set the model's in-memory flag to v (applied, then prev on revert)
//   save()        — returns the persist Promise for the current in-memory state
//   afterChange() — optional: re-sync dependent UI (e.g. default-radio eligibility)
function guardCapToggle(cb, { writeFlag, save, afterChange }) {
  const cell = cb && cb.closest('.ac-saved-cap');
  const applied = !!(cb && cb.checked);
  const prev = !applied;
  writeFlag(applied);
  if (afterChange) afterChange();
  if (!cell) { Promise.resolve(save()); return; }   // no cell to cover — just persist

  cb.disabled = true;                 // block a flip-back click for the whole window
  let cancelled = false, settled = false;
  const unlock = () => { cb.disabled = false; };

  const haz = _buildCapHazard(cell, () => {
    if (settled || cancelled) return;
    cancelled = true;
    haz._teardown();
    writeFlag(prev); cb.checked = prev; if (afterChange) afterChange();
    _markSaving(cell);                // compensating save of the reverted value
    Promise.resolve(save())
      .then(() => _flashSaveCheck(cell, true))
      .catch((e) => _flashSaveCheck(cell, false, (e && e.message) || 'Save failed'))
      .finally(unlock);
  });

  const startAt = Date.now();
  Promise.resolve(save())
    .then(() => {
      if (cancelled) return;
      const settle = () => {
        if (cancelled) return;
        if (haz._holding) { setTimeout(settle, 120); return; }   // don't yank the box mid-hold
        settled = true; haz._teardown(); _flashSaveCheck(cell, true); unlock();
      };
      setTimeout(settle, Math.max(0, _HAZ_MIN_MS - (Date.now() - startAt)));
    })
    .catch((e) => {
      if (cancelled) return;
      settled = true; haz._teardown();
      writeFlag(prev); cb.checked = prev; if (afterChange) afterChange();
      _flashSaveCheck(cell, false, (e && e.message) || 'Save failed');
      unlock();
    });
}

export function mountModelTable(host, opts = {}) {
  const adapter = opts.adapter || {};
  const fetchFn = opts.fetchFn || ((...a) => window.fetch(...a));
  const apiPath = opts.apiPath || ((p) => p);
  const features = opts.features || {};
  const onModelChange = opts.onModelChange || (() => {});
  // 'user' (default) shows only the current user's usage for each model; 'global'
  // (admin Agent Settings) sums every agent, user, and background task app-wide.
  const usageScope = opts.usageScope || 'user';
  // Roster-role prompt directives (grep ROLE-DIRECTIVE-INJECT): editable only
  // when the mount both allows it AND provides a save adapter (admin mounts).
  const directivesEditable = !!(opts.directivesEditable && adapter.saveDirectives);
  // Inherited (app-default) rows are pure references: always visible, read-only.
  // Roles are decided by the app default; an agent's own model can take a role
  // away from an inherited row (single-assignment) and the inherited row sinks —
  // but no per-agent override copy is ever created. The admin Agent Settings
  // panel manages the app defaults themselves.

  // ── instance state ──
  const S = {
    presets: {},
    providerConfigs: {},        // per-provider remembered single config (key/model/base_url)
    currentProvider: '_custom',
    allModels: [],
    selectedModel: '',
    selCaps: { text: false, image: false, imageOut: false },
    providers: [],              // the saved roster (each gets a _uid)
    inherited: [],              // read-only app-default rows (per-agent table only)
    directives: null,           // per-agent roster-role prompt directives (null = unavailable)
    defaultModelId: '',         // the effective default model (drives custom-slot numbering)
    uid: 0,
  };
  let fetchDebounce = null, autosaveDebounce = null;
  let modelsBusy = 0;   // >0 while the provider's available-model list is loading (drives the + button spinner)
  let _directiveModalCleanup = null;  // closes an open directive editor on destroy
  const docListeners = [];

  // ── default endpoint helpers (adapter may override any) ──
  async function fetchPresets() {
    if (adapter.loadPresets) return adapter.loadPresets();
    try {
      const r = await fetchFn(apiPath('/admin/settings/providers'));
      return r.ok ? await r.json() : {};
    } catch { return {}; }
  }
  async function fetchModels(args) {
    if (adapter.fetchModels) return adapter.fetchModels(args);
    const params = new URLSearchParams({ provider: args.provider || '' });
    if (args.api_key && args.base_url) { params.set('api_key', args.api_key); params.set('base_url', args.base_url); }
    try {
      const r = await fetchFn(apiPath(`/admin/settings/models?${params.toString()}`));
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    } catch (e) { return { error: e.message }; }
  }
  async function fetchModelInfo(model) {
    if (adapter.fetchModelInfo) return adapter.fetchModelInfo(model);
    try {
      const r = await fetchFn(apiPath(`/admin/settings/model-info?model=${encodeURIComponent(model || '')}`));
      const d = r.ok ? await r.json() : null;
      return (d && d.info) || {};
    } catch { return {}; }
  }
  async function fetchModelUsage(args) {
    if (adapter.fetchModelUsage) return adapter.fetchModelUsage(args);
    try {
      const params = new URLSearchParams({ model: args.model || '', provider: args.provider || '', scope: usageScope });
      const r = await fetchFn(apiPath(`/admin/settings/model-usage?${params.toString()}`));
      const d = r.ok ? await r.json() : null;
      return (d && !d.error) ? d : null;
    } catch { return null; }
  }

  // ─────────────────────────────────────────────────────────────────────────
  // ── Build the markup (mirrors agent-settings.html's Models block) ──────────
  // ─────────────────────────────────────────────────────────────────────────
  host.innerHTML = '';
  const root = document.createElement('div');
  root.className = 'mt-root';

  const table = document.createElement('div');
  table.className = 'ac-list ac-model-config ac-cfg-collapsed';
  table.style.display = 'none';

  // Provider row
  const provRow = document.createElement('div');
  provRow.className = 'ac-ability-row ac-model-cfg-row ac-row-static';
  provRow.innerHTML = `<span class="ac-ability-label"><span class="ac-ability-name">Provider</span></span>`;
  const provCtrl = document.createElement('span');
  provCtrl.className = 'ac-model-cfg-control';
  const provSel = document.createElement('select');
  provSel.className = 'ac-input ac-input-sm';
  const provCheck = makeCheck();
  provCtrl.appendChild(provSel); provCtrl.appendChild(provCheck);
  provRow.appendChild(provCtrl);
  table.appendChild(provRow);

  // Base URL row
  const urlRow = document.createElement('div');
  urlRow.className = 'ac-ability-row ac-model-cfg-row ac-row-static';
  urlRow.innerHTML = `<span class="ac-ability-label"><span class="ac-ability-name">Base URL</span></span>`;
  const urlCtrl = document.createElement('span');
  urlCtrl.className = 'ac-model-cfg-control';
  const urlInput = document.createElement('input');
  urlInput.type = 'text'; urlInput.className = 'ac-input ac-input-sm';
  urlInput.placeholder = 'https://openrouter.ai/api/v1'; urlInput.autocomplete = 'off';
  const urlCheck = makeCheck();
  urlCtrl.appendChild(urlInput); urlCtrl.appendChild(urlCheck);
  urlRow.appendChild(urlCtrl);
  table.appendChild(urlRow);

  // API Key row
  const keyRow = document.createElement('div');
  keyRow.className = 'ac-ability-row ac-model-cfg-row ac-row-static';
  keyRow.innerHTML = `<span class="ac-ability-label"><span class="ac-ability-name">API Key</span></span>`;
  const keyCtrl = document.createElement('span');
  keyCtrl.className = 'ac-model-cfg-control';
  const keyInput = document.createElement('input');
  keyInput.type = 'text'; keyInput.className = 'ac-input ac-input-sm';
  keyInput.placeholder = 'sk-...'; keyInput.autocomplete = 'off';
  const keyCheck = makeCheck();
  keyCtrl.appendChild(keyInput); keyCtrl.appendChild(keyCheck);
  keyRow.appendChild(keyCtrl);
  table.appendChild(keyRow);

  // Model row (search + caps + Add/Clear)
  const modelRow = document.createElement('div');
  modelRow.className = 'ac-ability-row ac-model-cfg-row ac-model-cfg-model ac-row-static';
  const modelLabel = document.createElement('span');
  modelLabel.className = 'ac-ability-label';
  const searchWrap = document.createElement('span');
  searchWrap.className = 'ac-model-search-wrap';
  const modelSearch = document.createElement('input');
  modelSearch.type = 'text'; modelSearch.className = 'ac-input ac-input-sm';
  modelSearch.placeholder = 'Search models…'; modelSearch.autocomplete = 'off';
  const modelDd = document.createElement('div');
  modelDd.className = 'ac-model-dropdown'; modelDd.style.display = 'none';
  searchWrap.appendChild(modelSearch); searchWrap.appendChild(modelDd);
  const modelBar = document.createElement('div');
  modelBar.className = 'ac-model-cfg-modelbar';
  const capsEl = document.createElement('span');
  capsEl.className = 'ac-model-caps';
  const addControl = document.createElement('span');
  addControl.className = 'ac-model-cfg-control ac-model-add-control';
  const addBtn = document.createElement('button');
  addBtn.className = 'ac-btn ac-btn-primary ac-btn-sm'; addBtn.textContent = 'Add';
  const clearBtn = document.createElement('button');
  clearBtn.className = 'ac-btn ac-btn-ghost ac-btn-sm'; clearBtn.textContent = 'Clear';
  const modelCheck = makeCheck();
  addControl.appendChild(addBtn); addControl.appendChild(clearBtn); addControl.appendChild(modelCheck);
  modelBar.appendChild(capsEl); modelBar.appendChild(addControl);
  modelLabel.appendChild(searchWrap); modelLabel.appendChild(modelBar);
  modelRow.appendChild(modelLabel);
  table.appendChild(modelRow);

  // Advanced / + action row
  const advRow = document.createElement('div');
  advRow.className = 'ac-row'; advRow.dataset.role = 'mt-advanced-row';
  const advHead = document.createElement('div');
  advHead.className = 'ac-ability-row ac-model-advanced-head';
  const advActions = document.createElement('span');
  advActions.className = 'ac-model-row-actions';
  const advToggle = document.createElement('button');
  advToggle.type = 'button'; advToggle.className = 'ac-model-icon-btn';
  advToggle.title = 'Advanced options'; advToggle.setAttribute('aria-label', 'Advanced options');
  advToggle.innerHTML = `<i data-lucide="sliders-horizontal" class="lucide-icon" style="width:16px;height:16px;"></i>`;
  const addToggle = document.createElement('button');
  addToggle.type = 'button'; addToggle.className = 'ac-model-add-toggle';
  addToggle.title = 'Add a model'; addToggle.setAttribute('aria-label', 'Add a model');
  addToggle.innerHTML = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>`;
  // Spinner shown ON the + button while the provider's available-model list loads
  // (the one genuinely slow fetch — it powers the "Search models…" box for adding
  // a model). Hidden by default; CSS swaps the + glyph for it while `.loading`.
  const addSpin = document.createElement('span');
  addSpin.className = 'ac-model-add-spinner'; addSpin.setAttribute('aria-hidden', 'true');
  addToggle.appendChild(addSpin);
  advActions.appendChild(advToggle); advActions.appendChild(addToggle);
  advHead.appendChild(advActions);
  const advBody = document.createElement('div');
  advBody.className = 'ac-ability-body';
  advRow.appendChild(advHead); advRow.appendChild(advBody);

  // Advanced body content: caller extras only (parallel model racing removed —
  // the agent runs a single default model, switchable per-chat by the user).
  if (typeof opts.advancedExtra === 'function') opts.advancedExtra(advBody);

  // ── Skeleton placeholder — shown while saved-models data loads ────────────
  // Mimics the saved-models header + 3 phantom rows with sk-shimmer so the user
  // sees structure (not just the Advanced/+ buttons) during the async fetch.
  const GRID = '20px minmax(0, 1fr) auto 38px 28px';
  const skelRoot = document.createElement('div');
  skelRoot.className = 'ac-model-skeleton';
  skelRoot.style.display = 'none';
  // Header
  const skelHead = document.createElement('div');
  skelHead.className = 'ac-ability-row ac-saved-head ac-saved-row';
  skelHead.style.gridTemplateColumns = GRID;
  skelHead.innerHTML = '<span></span><span class="ac-ability-label"><span class="ac-saved-th">Model</span></span>'
    + '<span class="ac-saved-flags"></span>'
    + '<span class="ac-saved-th ac-saved-usage-th">Usage</span>'
    + '<span class="ac-saved-cap ac-saved-th" style="font-size:10px;font-weight:700;letter-spacing:0.4px;color:var(--fg-3);">More</span>';
  skelRoot.appendChild(skelHead);
  // Phantom rows — variable-width shimmer lines for model names, a short line for
  // the flags, stacked short lines for usage, and a line for the More button.
  const SKEL_NAMES = ['gpt-4o', 'claude-sonnet-4', 'claude-3-haiku'];
  for (let i = 0; i < 3; i++) {
    const row = document.createElement('div');
    row.className = 'ac-ability-row ac-saved-row';
    row.style.gridTemplateColumns = GRID;
    const nmSpan = 50 + Math.round((SKEL_NAMES[i].length / 14) * 40);
    row.innerHTML = '<span></span>'
      + `<span class="ac-ability-label"><span class="sk-shimmer sk-line" style="width:${nmSpan}%"></span></span>`
      + '<span></span>'
      + '<span class="ac-saved-usage" style="display:flex;flex-direction:column;align-items:flex-end;gap:4px">'
      + '<span class="sk-shimmer sk-line" style="width:70%;height:8px"></span>'
      + '<span class="sk-shimmer sk-line" style="width:55%;height:8px"></span>'
      + '<span class="sk-shimmer sk-line" style="width:40%;height:8px"></span></span>'
      + '<span class="ac-saved-cap"><span class="sk-shimmer sk-line" style="width:18px;height:12px"></span></span>';
    skelRoot.appendChild(row);
  }
  table.appendChild(skelRoot);

  table.appendChild(advRow);
  root.appendChild(table);

  // Status line lives INSIDE the table (as a footer row of the .ac-list) so it
  // reads as part of the model table and hides together with it while loading —
  // never floats as orphan text above/below a not-yet-visible table.
  const statusEl = document.createElement('div');
  statusEl.className = 'ac-status ac-model-cfg-status'; statusEl.style.cssText = 'display:none;padding:8px 14px;';
  table.appendChild(statusEl);

  host.appendChild(root);

  if (window.lucide && advRow.querySelector('[data-lucide]:not(.lucide)')) {
    try { window.lucide.createIcons({ nodes: Array.from(advRow.querySelectorAll('[data-lucide]:not(.lucide)')) }); } catch {}
  }

  // ─────────────────────────────────────────────────────────────────────────
  // ── Behaviour ──────────────────────────────────────────────────────────────
  // ─────────────────────────────────────────────────────────────────────────
  function setModelStatus(text, ok) {
    // No dedicated status line in the configurator; reuse the caps area's title.
    modelSearch.title = text || '';
    if (text) { statusEl.textContent = text; statusEl.style.display = 'block'; statusEl.style.color = ok ? 'var(--success)' : 'var(--fg-3)'; }
    else { statusEl.style.display = 'none'; }
  }

  function renderDetectedCaps(m) {
    if (!m) { capsEl.innerHTML = ''; return; }
    const caps = [
      ['Standard', m.text_capable !== false],
      ['Vision', !!m.image_capable],
      ['Image', !!m.image_out_capable],
      ['Voice', !!m.voice_capable],
    ];
    capsEl.innerHTML = caps.map(([label, on]) => `<span class="ac-cap-badge ${on ? 'on' : 'off'}">${label}</span>`).join('');
  }

  function renderModelDropdown(filter) {
    if (!S.allModels.length) { modelDd.style.display = 'none'; return; }
    const filtered = filter
      ? S.allModels.filter(m => m.id.toLowerCase().includes(filter) || (m.name || '').toLowerCase().includes(filter))
      : S.allModels;
    if (!filtered.length) { modelDd.style.display = 'none'; return; }
    modelDd.innerHTML = ''; modelDd.style.display = 'block';
    filtered.slice(0, 200).forEach(m => {
      const item = document.createElement('div');
      item.className = 'ac-model-item';
      if (m.id === S.selectedModel) item.style.background = 'rgba(var(--brand-rgb),0.12)';
      const ctx = fmtContext(m.context);
      const price = fmtPrice(m.cost_input, m.cost_output);
      const badges = [ctx, price].filter(Boolean).map(t => `<span class="ac-model-badge">${esc(t)}</span>`).join('');
      item.innerHTML = `<div class="ac-model-item-row"><span style="font-weight:500;">${esc(m.id)}</span>`
        + `<span style="color:var(--fg-3);font-size:11px;margin-left:6px;">${esc(m.name || '')}</span></div>`
        + (badges ? `<div class="ac-model-item-badges">${badges}</div>` : '')
        + `<div class="ac-model-item-caps">${['Standard','Vision','Image','Voice'].filter(c => m[{'Standard':'text_capable','Vision':'image_capable','Image':'image_out_capable','Voice':'voice_capable'}[c]]).map(c => `<span class="ac-cap-badge on" style="font-size:9px;">${c}</span>`).join('')}</div>`;
      item.addEventListener('click', () => {
        S.selectedModel = m.id; modelSearch.value = m.id; modelDd.style.display = 'none';
        S.selCaps = { text: m.text_capable !== false, image: !!m.image_capable, imageOut: !!m.image_out_capable, voice: !!m.voice_capable };
        renderDetectedCaps(m);
        setModelStatus(`Selected: ${m.id}`, true);
        autosaveSingle(modelCheck);
      });
      modelDd.appendChild(item);
    });
  }

  // Reflect the in-flight provider-model fetch on the + button (counter-based so
  // overlapping debounced calls don't clear the spinner early).
  function setAddBusy(on) {
    modelsBusy = Math.max(0, modelsBusy + (on ? 1 : -1));
    addToggle.classList.toggle('loading', modelsBusy > 0);
  }
  async function loadModels() {
    const provider = S.currentProvider === '_custom' ? '' : S.currentProvider;
    const key = keyInput.value.trim();
    const base = urlInput.value.trim() || S.presets[S.currentProvider]?.base_url || '';
    if (!key) { setModelStatus('Enter an API key to see available models.'); S.allModels = []; modelDd.style.display = 'none'; return; }
    setModelStatus('Loading models…');
    setAddBusy(true);
    try {
      const data = await fetchModels({ provider, api_key: key, base_url: base });
      if (!data || data.error) {
        setModelStatus(data && data.error === 'No API key configured' ? 'Enter an API key to see available models.' : `Error: ${(data && data.error) || 'failed'}`);
        S.allModels = []; return;
      }
      S.allModels = data.models || [];
      setModelStatus(S.allModels.length ? `${S.allModels.length} models available. Type to filter.` : 'No models available.');
      if (S.selectedModel) {
        const sel = S.allModels.find(m => m.id === S.selectedModel);
        if (sel) { S.selCaps = { text: sel.text_capable !== false, image: !!sel.image_capable, imageOut: !!sel.image_out_capable, voice: !!sel.voice_capable }; renderDetectedCaps(sel); }
      }
    } finally {
      setAddBusy(false);
    }
  }
  function scheduleFetch() { clearTimeout(fetchDebounce); fetchDebounce = setTimeout(loadModels, 500); }

  // Remember the configurator's current single config under its provider key.
  function rememberProvider(key) {
    if (!key || key === '_custom') return;
    S.providerConfigs[key] = { api_key: keyInput.value || '', model: S.selectedModel, base_url: urlInput.value.trim() || '' };
  }

  // Auto-save the configurator (provider/url/key/model + detected caps) as the
  // single "default" config. Debounced; flashes `checkEl` on confirm.
  function autosaveSingle(checkEl) {
    rememberProvider(S.currentProvider);
    const single = {
      provider: S.currentProvider === '_custom' ? 'custom' : S.currentProvider,
      base_url: urlInput.value.trim(),
      api_key: keyInput.value.trim(),
      model: S.selectedModel || '',
      providers: S.providerConfigs,
      text_capable: S.selCaps.text, image_capable: S.selCaps.image,
      image_out_capable: S.selCaps.imageOut,
      voice_capable: S.selCaps.voice,
    };
    if (!single.api_key) return;                 // nothing persistable yet
    markSaving(checkEl);
    Promise.resolve(adapter.saveSingle(single))
      .then(() => flashCheck(checkEl, true))
      .catch(e => flashCheck(checkEl, false, e.message));
  }
  function scheduleAutosave(checkEl) { clearTimeout(autosaveDebounce); autosaveDebounce = setTimeout(() => autosaveSingle(checkEl), 700); }

  // ── Saved-models (roster) rendering ──
  // readOnly rows are inherited app-default models (shown for reference on the
  // per-agent table): their capability boxes are disabled and they carry no
  // delete control.
  function capCell(capable, checked, title, onChange, readOnly) {
    const cell = document.createElement('span');
    cell.className = 'ac-saved-cap';
    cell.addEventListener('click', e => e.stopPropagation());
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = capable && checked; cb.title = title;
    cb.style.cssText = 'width:15px;height:15px;accent-color:var(--accent);cursor:pointer;';
    // A model that doesn't support this capability (or an inherited app-default row)
    // shows the box GREYED + disabled rather than an empty slot, so every column
    // reads as a real (just locked) control.
    if (!capable) { cb.disabled = true; cb.checked = false; cb.style.cursor = 'default'; cb.style.opacity = '0.4'; cb.title = title + ' (not supported by this model)'; }
    else if (readOnly) { cb.disabled = true; cb.style.cursor = 'default'; cb.style.opacity = '0.4'; cb.title = title + ' (app default — read-only)'; }
    else cb.addEventListener('change', () => onChange(cb.checked, cb));
    cell.appendChild(cb); return cell;
  }
  function usageLine(title) {
    const line = document.createElement('span'); line.className = 'ac-saved-usage-line'; line.title = title;
    const val = document.createElement('span'); val.className = 'ac-saved-usage-v'; val.textContent = '—';
    line.appendChild(val); return { line, val };
  }
  // Canonical cost string: prefer the published-price total_cost_usd (computed
  // per call at its own model's rate, summed across chat + background), falling
  // back to the provider-billed cents only when no USD figure is present.
  function fmtUsageCost(u) {
    if (u && typeof u.total_cost_usd === 'number' && u.total_cost_usd > 0) {
      return u.total_cost_usd >= 1 ? `$${u.total_cost_usd.toFixed(2)}` : `$${u.total_cost_usd.toFixed(4)}`;
    }
    const c = (u && u.total_cost_cents) || 0;
    return c >= 100 ? `$${(c / 100).toFixed(2)}` : `${c}¢`;
  }
  // Build one role toggle row inside the floating popover: label + capability checkbox.
  // Mirrors the look of the session-dropdown more-menu info rows.
  // `locked` disables the checkbox; `lockHint` explains WHY it's locked (or, for
  // an editable inherited row, what editing it does).
  function _buildRoleToggle(parent, capable, checked, label, tooltip, onChange, locked, lockHint) {
    const row = document.createElement('div'); row.className = 'ac-saved-role-row';
    const lbl = document.createElement('span'); lbl.className = 'ac-saved-role-label';
    lbl.textContent = label;
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = capable && checked; cb.title = tooltip;
    cb.style.cssText = 'width:15px;height:15px;accent-color:var(--accent);cursor:pointer;flex-shrink:0;';
    if (!capable) { cb.disabled = true; cb.checked = false; cb.style.cursor = 'default'; cb.style.opacity = '0.4'; cb.title = tooltip + ' (not supported by this model)'; }
    else if (locked) { cb.disabled = true; cb.style.cursor = 'default'; cb.style.opacity = '0.4'; cb.title = tooltip + (lockHint || ' (app default — read-only)'); }
    else cb.addEventListener('change', () => onChange(cb.checked, cb));
    row.appendChild(lbl); row.appendChild(cb);
    parent.appendChild(row);
  }
  const usageCache = {}, metaCache = {};
  // A model can appear as BOTH an inherited (app-wide) row and the agent's own
  // row. Those are separate usage calculations — the inherited row reports the
  // app-wide total, the own row reports just this agent's spend — so the cache
  // key (and the fetch) are scoped by `inherited` to keep them apart.
  function _usageKey(p, inherited) { return `usage|${inherited ? 'inh' : 'own'}|${p.provider || ''}|${p.model || ''}`; }
  async function fillUsage(p, statIn, statOut, statCost, inherited) {
    const k = _usageKey(p, inherited);
    if (!usageCache[k]) { const d = await fetchModelUsage({ model: p.model, provider: p.provider, inherited: !!inherited }); if (d) usageCache[k] = d; }
    const u = usageCache[k] || {};
    if (u.total_input_tokens != null) {
      if (statIn) statIn.textContent = fmtTokens(u.total_input_tokens);
      if (statOut) statOut.textContent = fmtTokens(u.total_output_tokens);
      if (statCost) statCost.textContent = fmtUsageCost(u);
    }
  }

  // Saved rows are rebuilt when their role-based sort order changes. Keep their
  // stable _uid positions so those rebuilds can use a FLIP-style transition:
  // existing rows slide to their new slots while a newly added row fades in.
  // The advanced action row is included because it moves with the list length.
  const SAVED_MOTION_MS = 220;
  function _savedMotionEnabled() {
    return typeof window !== 'undefined'
      && typeof window.matchMedia === 'function'
      && !window.matchMedia('(prefers-reduced-motion: reduce)').matches
      && typeof advRow.animate === 'function';
  }
  function _savedMotionNodes() {
    return [
      ...table.querySelectorAll('.ac-saved-injected[data-mt-uid]'),
      advRow,
    ];
  }
  function _savedMotionKey(node) {
    return node === advRow ? '__advanced__' : String(node.dataset.mtUid || '');
  }
  function _snapshotSavedLayout() {
    const positions = new Map();
    _savedMotionNodes().forEach(node => positions.set(_savedMotionKey(node), node.getBoundingClientRect()));
    return positions;
  }
  function _animateSavedLayout(before, enteringUids = []) {
    if (!_savedMotionEnabled()) return;
    const entering = new Set(enteringUids.map(String));
    _savedMotionNodes().forEach(node => {
      const key = _savedMotionKey(node);
      if (entering.has(key)) {
        node.animate(
          [{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'translateY(0)' }],
          { duration: SAVED_MOTION_MS, easing: 'ease-out' },
        );
        return;
      }
      const previous = before.get(key);
      if (!previous) return;
      const current = node.getBoundingClientRect();
      const deltaY = previous.top - current.top;
      if (Math.abs(deltaY) < 0.5) return;
      node.animate(
        [{ transform: `translateY(${deltaY}px)` }, { transform: 'translateY(0)' }],
        { duration: SAVED_MOTION_MS, easing: 'cubic-bezier(.2,.8,.2,1)' },
      );
    });
  }
  function _fadeSavedRow(node, fadeIn = false) {
    if (!node || !_savedMotionEnabled()) return Promise.resolve();
    node.getAnimations().forEach(animation => animation.cancel());
    const frames = fadeIn
      ? [{ opacity: 0, transform: 'translateX(-8px)' }, { opacity: 1, transform: 'translateX(0)' }]
      : [{ opacity: 1, transform: 'translateX(0)' }, { opacity: 0, transform: 'translateX(-8px)' }];
    const animation = node.animate(frames, {
      duration: 180,
      easing: 'ease-out',
      fill: fadeIn ? 'none' : 'forwards',
    });
    return animation.finished.catch(() => {});
  }
  async function loadSavedDetail(p, body, statIn, statOut, statCost, readOnly) {
    await fillUsage(p, statIn, statOut, statCost, readOnly);
    const usage = usageCache[_usageKey(p, readOnly)] || {};
    if (body.dataset.loaded === '1') return;
    body.dataset.loaded = '1';
    const ck = `${p.provider || ''}|${p.model || ''}`;
    let meta = metaCache[ck];
    if (!meta) { meta = await fetchModelInfo(p.model); metaCache[ck] = meta; }
    const ctx = fmtContext(meta.context);
    const out = (typeof meta.max_output === 'number') ? `${fmtContext(meta.max_output).replace(' ctx', '')} out` : '';
    const price = fmtPrice(meta.cost_input, meta.cost_output);
    const chips = [ctx, out, price].filter(Boolean).map(t => `<span class="ac-model-badge">${esc(t)}</span>`).join('');
    let usageHtml = '';
    if (usage.total_input_tokens != null) {
      const inT = usage.total_input_tokens || 0, outT = usage.total_output_tokens || 0;
      const costStr = fmtUsageCost(usage);
      const scopeLabel = (usageScope === 'global' || readOnly) ? ' (all agents &amp; users)' : '';
      usageHtml = `<div class="ac-usage-summary" style="margin-top:8px;display:flex;gap:16px;font-size:13px;"><span style="color:var(--fg-2);">Used${scopeLabel}: <strong>${fmtTokens(inT)}</strong> in · <strong>${fmtTokens(outT)}</strong> out · <strong>${costStr}</strong></span></div>`;
    }
    const caps = `<div class="ac-model-caps" style="margin-top:8px;">`
      + `<span class="ac-cap-badge ${p.text_capable !== false ? 'on' : 'off'}">Text</span>`
      + `<span class="ac-cap-badge ${p.image_capable ? 'on' : 'off'}">Image-in</span>`
      + `<span class="ac-cap-badge ${p.image_out_capable ? 'on' : 'off'}">Image-out</span>`
      + `<span class="ac-cap-badge ${p.voice_capable ? 'on' : 'off'}">Voice</span>`
      + `<span class="ac-cap-badge ${p.high_effort_capable ? 'on' : 'off'}">Text +</span></div>`;
    const desc = meta.description ? `<div class="ac-model-meta-desc" style="margin-top:8px;">${esc(meta.description)}</div>` : '';
    // Provider now lives here (lifted off the one-line row).
    const provName = S.presets[p.provider]?.name || p.provider || 'custom';
    const provHtml = `<div class="ac-model-meta-prov" style="margin-top:2px;font-size:12px;color:var(--fg-3);">Provider: <strong style="color:var(--fg-2);">${esc(provName)}</strong></div>`;
    body.innerHTML = provHtml + (chips ? `<div class="ac-model-meta-chips" style="margin-top:8px;">${chips}</div>` : '') + usageHtml + caps + desc
      + (chips ? '' : '<div class="ac-hint" style="margin:8px 0 0;">No token or cost data available for this model.</div>');
    if (readOnly) {
      const note = document.createElement('div'); note.className = 'ac-hint'; note.style.margin = '8px 0 0';
      note.textContent = 'Inherited from the app default — managed in Admin → Agent Settings. A role can be taken by one of this agent\'s own models.';
      body.appendChild(note);
      return;
    }
    const delWrap = document.createElement('div'); delWrap.className = 'ac-saved-del-row';
    const delBtn = document.createElement('button'); delBtn.className = 'ac-btn ac-btn-ghost ac-saved-del-btn';
    delBtn.textContent = 'Remove model'; delBtn.style.color = 'var(--danger)';
    delBtn.addEventListener('click', async e => {
      e.stopPropagation();
      delBtn.disabled = true;
      const removedIndex = S.providers.findIndex(x => x._uid === p._uid);
      const removed = S.providers[removedIndex];
      S.providers = S.providers.filter(x => x._uid !== p._uid);
      const node = table.querySelector(`.ac-saved-injected[data-mt-uid="${p._uid}"]`);
      const fade = _fadeSavedRow(node);
      try {
        await adapter.saveRoster({ providers: S.providers });
        await fade;
        // Rebuild after the faded row leaves so every remaining row animates to
        // its new position, including role-sorted rows above/below the deletion.
        renderSaved({ animate: true });
      } catch (err) {
        await fade;
        // Restore the exact roster position and fade the still-present row back
        // in when persistence fails.
        if (removed) S.providers.splice(Math.max(0, removedIndex), 0, removed);
        await _fadeSavedRow(node, true);
        delBtn.disabled = false;
        _flashSaveCheck(node, false, (err && err.message) || 'Remove failed');
      }
    });
    delWrap.appendChild(delBtn); body.appendChild(delWrap);
  }
  function colCells(parent, cols) {
    for (const [label, title] of cols) {
      const cell = document.createElement('span'); cell.className = 'ac-saved-cap ac-saved-th';
      cell.textContent = label; cell.title = title; parent.appendChild(cell);
    }
  }

  // One "Default" radio cell. Any Text-checked model (a brain candidate) is
  // selectable — INCLUDING inherited app-default rows, so a per-agent admin can
  // pick which model this agent runs by default among the Text options (mirroring
  // the admin Models table). Non-Text rows show a GREYED, disabled radio (never an
  // empty slot). Picking a row makes it the agent/app default via the saveSingle
  // path; inherited picks reference the app credentials (see setDefaultRow).
  // Promote a roster row to the default brain: mirror its provider/model/key/caps
  // into the configurator's single-config slots (as reload() does) and persist via
  // saveSingle. No list rebuild — the "Default" radios share one group, so the
  // browser already moved the selection in place; re-rendering here would collapse
  // and re-expand the list and jump the scroll position.
  // Column template shared by the saved-list header and every row.
  // The "More" column is a ⋮ button that opens a floating popover with
  // role toggles and space for future per-model controls (mirroring the chat
  // header more-menu pattern). Order: Drag · Model · Flags · Usage · More
  const SAVED_GRID = '20px minmax(0, 1fr) auto 38px 28px';

  // ── Role flag definitions ─────────────────────────────────────────────
  // Each role gets a small 10×10 square avatar shown in the header when no
  // model holds it, or on the model's row when assigned. Order controls the
  // left-to-right display.
  const ROLE_FLAGS = [
    { key: 'enabled',              symbol: 'S',     title: 'Standard — primary chat model' },
    { key: 'high_effort_capable',  symbol: 'P',     title: 'Premium — harder tasks' },
    { key: 'use_for_image',        symbol: '👁',   title: 'Vision — read images' },
    { key: 'use_for_image_out',    symbol: '🖼',    title: 'Image — generate images' },
    { key: 'use_for_system',       symbol: '⚙',    title: 'System — app misc. LLM tasks' },
    { key: 'use_for_voice',        symbol: '🎤',   title: 'Voice — LLM voice input' },
  ];

  // ── Roster-role prompt directives (grep ROLE-DIRECTIVE-INJECT) ────────────
  // Per-agent prompt injections, shown/edited from the "Prompt injections"
  // strip under the saved list. Fixed roster roles have their own key; EACH
  // custom roster slot ('custom:<position>') carries its own directive too
  // (see _customDirectiveSlots). Keys mirror MODEL_DIRECTIVE_KEYS in
  // app/admin/settings.py.
  const DIRECTIVE_ROLES = {
    standard:  { symbol: 'S',  label: 'Standard', hint: 'Injected into the system prompt when the session runs on the Standard (default) brain.' },
    premium:   { symbol: 'P',  label: 'Premium',  hint: 'Injected when the session runs on the Premium brain. An agent-authored "Model Upgrade Directive" slot wins over this text when set.' },
    image_in:  { symbol: '👁', label: 'Vision',   hint: 'Injected into the system prompt when the session runs on the Vision brain (reads attached images).' },
    image_out: { symbol: '🖼', label: 'Image',    hint: 'Prepended to every generated-image prompt (Image-out role).' },
    system:    { symbol: '⚙', label: 'System',   hint: 'Prepended to app misc-LLM task prompts (session naming, context management).' },
    voice:     { symbol: '🎤', label: 'Voice',    hint: 'Reserved — LLM voice transcription is audio→text, so there is no prompt-injection point yet.' },
  };

  /** Custom roster slots in footer-picker order, mirroring _assign_slots in
   *  app/admin/settings.py (own rows first, then inherited app-defaults;
   *  role fills removed; standard pinned to the default model). Each custom
   *  slot gets its own directive key 'custom:<position>' — Custom 1, Custom 2,
   *  ... — so the admin can inject per-custom-model prompts. */
  function _customDirectiveSlots() {
    const union = [...(S.providers || []), ...(S.inherited || [])];
    const seen = new Set();
    const rows = [];
    union.forEach(p => {
      const key = `${p.provider || ''}|${p.base_url || ''}|${p.model || ''}`;
      if (seen.has(key)) return;
      seen.add(key);
      rows.push(p);
    });
    const keyOf = (p) => `${p.provider || ''}|${p.base_url || ''}|${p.model || ''}`;
    const isStd = (p) => p.enabled !== false && p.text_capable !== false;
    const pinned = S.defaultModelId ? rows.find(p => p.model === S.defaultModelId && isStd(p)) : null;
    const roles = { standard: pinned, premium: null, image_in: null, image_out: null };
    rows.forEach(p => {
      if (!roles.standard && isStd(p)) roles.standard = p;
      if (!roles.premium && p.high_effort_capable) roles.premium = p;
      if (!roles.image_in && p.image_capable && p.use_for_image) roles.image_in = p;
      if (!roles.image_out && p.image_out_capable && p.use_for_image_out) roles.image_out = p;
    });
    const roleKeys = new Set(Object.values(roles).filter(Boolean).map(keyOf));
    return rows.filter(p => !roleKeys.has(keyOf(p)))
      .map((p, i) => ({ slot_ref: `custom:${i + 1}`, label: `Custom ${i + 1}`, model: p.model || '' }));
  }

  /** Render a single 10×10 flag box. */
  function _flagDom(symbol, title) {
    const box = document.createElement('span');
    box.className = 'ac-role-flag';
    box.textContent = symbol;
    box.title = title;
    return box;
  }

  /** Compute which roles are unassigned (header) vs assigned to each model.
   *  Returns { headerFlags: [...], rowFlags: { uid: [...] } }. */
  function _computeFlags() {
    const allRows = [...S.inherited, ...S.providers];
    const assigned = new Set();  // role keys that SOME model holds
    const rowMap = {};           // uid → role keys this row holds
    allRows.forEach(p => {
      const keys = [];
      ROLE_FLAGS.forEach(rf => {
        if (p[rf.key]) { keys.push(rf.key); assigned.add(rf.key); }
      });
      if (keys.length) rowMap[p._uid] = keys;
    });
    const headerFlags = ROLE_FLAGS.filter(rf => !assigned.has(rf.key));
    return { headerFlags, rowMap };
  }

  // Build + insert ONE saved-model row (just before the configurator anchor) and
  // kick off its usage fetch. Capability toggles and the
  // default radio persist IN PLACE — no list rebuild — so editing a row never
  // collapses the table or disturbs the scroll position.
  function _buildSavedRow(p, readOnly, checked, cfgAnchor, rolesEditable) {
    const isRoleAssigned = !!(p.enabled || p.high_effort_capable || p.use_for_image || p.use_for_image_out || p.use_for_system || p.use_for_voice);
    const rowWrap = document.createElement('div'); rowWrap.className = 'ac-row ac-saved-injected' + (readOnly ? ' ac-saved-inherited' : ' ac-saved-own') + (isRoleAssigned ? ' ac-saved-role' : '');
    rowWrap.dataset.mtUid = p._uid;
    rowWrap.dataset.id = String(p._uid);  // for makeRowsReorderable's rowIdList()
    const row = document.createElement('div'); row.className = 'ac-ability-row ac-saved-row';
    row.style.gridTemplateColumns = SAVED_GRID;
    // Role-assigned rows stay fixed at the top. Only custom, agent-owned rows
    // may be reordered.
    if (!readOnly && !isRoleAssigned) {
      const grip = document.createElement('span');
      grip.className = 'ac-saved-drag-handle';
      grip.title = 'Drag to reorder';
      grip.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="16" y2="6"/><line x1="8" y1="12" x2="16" y2="12"/><line x1="8" y1="18" x2="16" y2="18"/></svg>';
      row.appendChild(grip);
    } else {
      row.appendChild(document.createElement('span')); // fixed-role/read-only slot
    }
    // Single-line label: just the model name (the provider moved into the expanded
    // detail panel, so the row stays one line tall).
    const label = document.createElement('span'); label.className = 'ac-ability-label ac-saved-name-click';
    label.title = 'Show usage details';
    const badge = readOnly
      ? ' <span class="ac-cap-badge on" title="Inherited from the app default — read-only reference" style="margin-left:6px;">Inherited</span>'
      : '';
    label.innerHTML = `<span class="ac-ability-name ac-saved-model-name">${esc(p.model || '—')}${badge}</span>`;
    row.appendChild(label);
    // Flags column — small 10×10 square avatars for roles THIS model holds.
    const flagsCell = document.createElement('span'); flagsCell.className = 'ac-saved-flags';
    const { rowMap } = _computeFlags();
    const myFlags = rowMap[p._uid] || [];
    const flagLookup = {};
    ROLE_FLAGS.forEach(rf => { flagLookup[rf.key] = rf; });
    myFlags.forEach(k => { const rf = flagLookup[k]; if (rf) flagsCell.appendChild(_flagDom(rf.symbol, rf.title)); });
    row.appendChild(flagsCell);
    // Roles more-button — opens a floating popover with capability toggles,
    // mirroring the chat header more-menu style. Replaces the old inline column
    // checkboxes (Def radio + Standard/Premium/Vision/Image).
    const rolesCell = document.createElement('span');
    rolesCell.className = 'ac-saved-cap ac-saved-roles-cell';
    rolesCell.style.position = 'relative';
    // Build a badge string showing assigned roles
    const roleLabels = [];
    if (p.enabled) roleLabels.push('Standard');
    if (p.high_effort_capable) roleLabels.push('Premium');
    if (p.use_for_image) roleLabels.push('Vision');
    if (p.use_for_image_out) roleLabels.push('Image');
    if (p.use_for_system) roleLabels.push('System');
    if (p.use_for_voice) roleLabels.push('Voice');
    const rolesBtn = document.createElement('button');
    rolesBtn.type = 'button';
    rolesBtn.className = 'ac-saved-roles-btn';
    rolesBtn.title = roleLabels.length ? roleLabels.join(' · ') : 'No roles assigned';
    rolesBtn.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="5" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="12" cy="19" r="1.5"/></svg>';
    // Show a small colour-coded dot indicator for assigned roles
    const roleDot = document.createElement('span');
    roleDot.className = 'ac-saved-role-dot' + (roleLabels.length ? ' has-roles' : '');
    roleDot.style.setProperty('--role-count', String(roleLabels.length));
    roleDot.setAttribute('aria-hidden', 'true');
    rolesBtn.appendChild(roleDot);
    rolesCell.appendChild(rolesBtn);
    // Floating popover — section-based like the chat header more-menu,
    // so future per-model controls can be added as their own sections.
    const popover = document.createElement('div');
    popover.className = 'ac-saved-roles-popover';
    popover.hidden = true;
    popover.setAttribute('role', 'menu');
    rolesCell.appendChild(popover);

    // ── Roles section ──
    const rolesSection = document.createElement('div');
    rolesSection.className = 'ac-pm-section';
    const rolesHead = document.createElement('div');
    rolesHead.className = 'ac-pm-section-head';
    const rolesTitle = document.createElement('span');
    rolesTitle.className = 'ac-pm-section-title';
    rolesTitle.textContent = 'Roles';
    rolesHead.appendChild(rolesTitle);
    rolesSection.appendChild(rolesHead);

    // ── Popover content ──
    // Persist a role change on an agent-owned row. Roles stay single-assignment:
    // taking a role clears it on every other row — including inherited ones, which
    // then simply sink in the sort. Inherited rows themselves are read-only, so
    // nothing here ever creates a per-agent copy of an app default.
    const persistRole = async (key, v, cb) => {
      const target = p;
      const provSnapshot = S.providers.slice(), inhSnapshot = S.inherited.slice();
      const allRows = [...S.inherited, ...S.providers];
      const before = allRows.map(item => item[key]);
      if (v) allRows.forEach(item => { if (item !== target) item[key] = false; });
      target[key] = v;
      cb.disabled = true;
      _markSaving(cb.closest('.ac-saved-cap'));
      try {
        const saves = [];
        if (S.providers.length) saves.push(adapter.saveRoster({ providers: S.providers }));
        await Promise.all(saves);
        _flashSaveCheck(cb.closest('.ac-saved-cap'), true);
        renderSaved();
        // Re-open this row's popover — renderSaved destroys the DOM, so we
        // re-find the new row by uid and open its popover.
        const newRow = table.querySelector(`.ac-saved-injected[data-mt-uid="${target._uid}"]`);
        if (newRow) {
          const newBtn = newRow.querySelector('.ac-saved-roles-btn');
          const newPop = newRow.querySelector('.ac-saved-roles-popover');
          if (newBtn && newPop) {
            newPop.hidden = false;
            newBtn.classList.add('open');
          }
        }
      } catch (e) {
        allRows.forEach((item, i) => { item[key] = before[i]; });
        S.providers = provSnapshot; S.inherited = inhSnapshot;
        _flashSaveCheck(cb.closest('.ac-saved-cap'), false, e.message || 'Save failed');
        cb.disabled = false;
      }
    };
    // Inherited rows are read-only references to the app default; their role
    // toggles stay locked (the lock hint explains why).
    const locked = readOnly && !rolesEditable;
    const lockHint = ' (app default — read-only)';
    _buildRoleToggle(rolesSection, p.text_capable !== false, p.enabled, 'Standard', 'The primary chat / brain model',
      (v, cb) => persistRole('enabled', v, cb), locked, lockHint);
    _buildRoleToggle(rolesSection, p.text_capable !== false, !!p.high_effort_capable, 'Premium', 'A premium model for harder tasks',
      (v, cb) => persistRole('high_effort_capable', v, cb), locked, lockHint);
    _buildRoleToggle(rolesSection, !!p.image_capable, !!p.use_for_image, 'Vision', 'Use to read attached images',
      (v, cb) => persistRole('use_for_image', v, cb), locked, lockHint);
    _buildRoleToggle(rolesSection, !!p.image_out_capable, !!p.use_for_image_out, 'Image', 'Use to generate images',
      (v, cb) => persistRole('use_for_image_out', v, cb), locked, lockHint);
    _buildRoleToggle(rolesSection, p.text_capable !== false, !!p.use_for_system, 'System', 'App misc. LLM tasks (session naming, context mgmt)',
      (v, cb) => persistRole('use_for_system', v, cb), locked, lockHint);
    _buildRoleToggle(rolesSection, p.text_capable !== false, !!p.use_for_voice, 'Voice', 'LLM-powered voice input in chat',
      (v, cb) => persistRole('use_for_voice', v, cb), locked, lockHint);
    popover.appendChild(rolesSection);

    // ── Wire popover open/close ──
    rolesBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const isOpen = !popover.hidden;
      // Close all other popovers first
      table.querySelectorAll('.ac-saved-roles-popover:not([hidden])').forEach(m => {
        if (m !== popover) m.hidden = true;
      });
      table.querySelectorAll('.ac-saved-roles-btn.open').forEach(b => b.classList.remove('open'));
      popover.hidden = isOpen;
      rolesBtn.classList.toggle('open', !isOpen);
    });
    // Click-away closes
    const closeRoles = (e) => {
      if (!rolesCell.contains(e.target)) {
        popover.hidden = true;
        rolesBtn.classList.remove('open');
      }
    };
    document.addEventListener('pointerdown', closeRoles);
    docListeners.push(['pointerdown', closeRoles]);
    const usageCell = document.createElement('span'); usageCell.className = 'ac-saved-usage';
    const statIn = usageLine('Total input tokens'), statOut = usageLine('Total output tokens'), statCost = usageLine('Total provider cost');
    usageCell.appendChild(statIn.line); usageCell.appendChild(statOut.line); usageCell.appendChild(statCost.line);
    row.appendChild(usageCell);
    row.appendChild(rolesCell);
    const body = document.createElement('div'); body.className = 'ac-ability-body';
    body.innerHTML = '<div class="ac-hint" style="margin:0;">Loading details…</div>';
    label.addEventListener('click', () => { const open = rowWrap.classList.toggle('expanded'); if (open) loadSavedDetail(p, body, statIn.val, statOut.val, statCost.val, readOnly); });
    rowWrap.appendChild(row); rowWrap.appendChild(body);
    table.insertBefore(rowWrap, cfgAnchor);
    fillUsage(p, statIn.val, statOut.val, statCost.val, readOnly);
    return rowWrap;
  }

  // Build the saved-list header (Model · Flags · Usage · (empty for More)) once.
  function _buildSavedHead(cfgAnchor) {
    const { headerFlags } = _computeFlags();
    const head = document.createElement('div');
    head.className = 'ac-ability-row ac-saved-head ac-saved-row ac-saved-injected';
    head.style.gridTemplateColumns = SAVED_GRID;
    // Empty slot for the drag-handle column
    head.appendChild(document.createElement('span'));
    const headName = document.createElement('span'); headName.className = 'ac-ability-label';
    headName.innerHTML = '<span class="ac-saved-th">Model</span>'; head.appendChild(headName);
    // Flags column — shows flags for unassigned roles
    const headFlags = document.createElement('span'); headFlags.className = 'ac-saved-flags';
    headerFlags.forEach(rf => headFlags.appendChild(_flagDom(rf.symbol, rf.title)));
    head.appendChild(headFlags);
    const headUsage = document.createElement('span'); headUsage.className = 'ac-saved-th ac-saved-usage-th';
    headUsage.textContent = 'Usage'; headUsage.title = 'Total tokens in / out and provider cost'; head.appendChild(headUsage);
    head.appendChild(document.createElement('span')); // empty — More button has no header label
    table.insertBefore(head, cfgAnchor);
  }

  // ── Roster-role prompt directives (grep ROLE-DIRECTIVE-INJECT) ──────────
  // Admin-authored prompt injections per roster role, edited from the "Prompt
  // injections" strip under the saved list. Writes are admin-only (the mount
  // decides via `directivesEditable` + the presence of `adapter.saveDirectives`);
  // non-admin mounts show the strip read-only (or hidden when the read fails).
  function _directivePreview(key) {
    let v = '';
    if (S.directives) {
      v = key.startsWith('custom:')
        ? ((S.directives.custom || {})[key] || '')
        : (S.directives[key] || '');
    }
    return v ? (v.length > 42 ? v.slice(0, 42) + '…' : v) : '—';
  }

  function _buildDirectiveStrip(cfgAnchor) {
    const strip = document.createElement('div');
    strip.className = 'ac-saved-injected ac-directives-strip';
    strip.style.cssText = 'display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding:7px 14px;border-bottom:var(--border-width) solid var(--border);';
    const lab = document.createElement('span');
    lab.className = 'ac-saved-th';
    lab.textContent = 'Prompt injections';
    lab.title = 'Admin-authored prompt text injected per roster role (grep ROLE-DIRECTIVE-INJECT)';
    lab.style.cssText = 'font-size:11px;letter-spacing:0.4px;color:var(--fg-3);margin-right:4px;white-space:nowrap;';
    strip.appendChild(lab);
    for (const [key, role] of Object.entries(DIRECTIVE_ROLES)) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'ac-directive-chip';
      chip.style.cssText = 'display:inline-flex;align-items:center;gap:5px;max-width:230px;padding:2px 9px;border:var(--border-width) solid var(--border);border-radius:999px;background:var(--bg-tint);color:var(--fg-2);font-size:11px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
      chip.title = `${role.label} — click to ${directivesEditable ? 'edit' : 'view'} the prompt injection`;
      chip.innerHTML = `<span style="font-weight:700;">${role.symbol}</span><span style="opacity:.85;">${role.label}</span><span style="opacity:.55;overflow:hidden;text-overflow:ellipsis;">${esc(_directivePreview(key))}</span>`;
      chip.addEventListener('click', () => openDirectiveEditor(key));
      strip.appendChild(chip);
    }
    // One chip per custom roster slot — Custom 1, Custom 2, ... each with its
    // own injection, numbered to match the footer picker's "Custom N" labels.
    _customDirectiveSlots().forEach(slot => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'ac-directive-chip';
      chip.style.cssText = 'display:inline-flex;align-items:center;gap:5px;max-width:230px;padding:2px 9px;border:var(--border-width) solid var(--border);border-radius:999px;background:var(--bg-tint);color:var(--fg-2);font-size:11px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
      chip.title = `${slot.label} (${slot.model}) — click to ${directivesEditable ? 'edit' : 'view'} the prompt injection`;
      chip.innerHTML = `<span style="font-weight:700;">★</span><span style="opacity:.85;">${slot.label}</span><span style="opacity:.55;overflow:hidden;text-overflow:ellipsis;">${esc(_directivePreview(slot.slot_ref))}</span>`;
      chip.addEventListener('click', () => openDirectiveEditor(slot.slot_ref));
      strip.appendChild(chip);
    });
    table.insertBefore(strip, cfgAnchor);
  }

  /** Open the directive editor for one roster slot — a fixed role key
   *  ('standard', 'premium', ...) or a custom slot ref ('custom:1', ...). */
  function openDirectiveEditor(key) {
    const isCustom = key.startsWith('custom:');
    const role = isCustom ? null : DIRECTIVE_ROLES[key];
    if (!role && !isCustom) return;
    const customSlot = isCustom
      ? (_customDirectiveSlots().find(s => s.slot_ref === key) || null)
      : null;
    const symbol = role ? role.symbol : '★';
    const label = role ? role.label : (customSlot ? customSlot.label : key);
    const hint = role
      ? role.hint
      : (customSlot
          ? `Injected into the system prompt when the session runs on ${customSlot.label} (${customSlot.model}).`
          : 'Injected into the system prompt when the session runs on this custom roster model.');
    const editable = directivesEditable;
    const ov = document.createElement('div');
    ov.className = 'ac-directive-overlay';
    ov.style.cssText = 'position:fixed;inset:0;z-index:1200;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;';
    const panel = document.createElement('div');
    panel.className = 'ac-directive-modal';
    panel.style.cssText = 'width:min(660px,92vw);max-height:82vh;display:flex;flex-direction:column;background:var(--bg-elev);border:var(--border-width) solid var(--border);border-radius:12px;box-shadow:0 20px 60px rgba(0,0,0,.45);overflow:hidden;';
    const head = document.createElement('div');
    head.style.cssText = 'display:flex;align-items:center;gap:8px;padding:12px 16px;border-bottom:var(--border-width) solid var(--border);';
    head.innerHTML = `<span style="font-weight:700;">${symbol} ${esc(label)}</span><span style="color:var(--fg-3);font-size:12px;">prompt injection</span>`;
    const closeX = document.createElement('button');
    closeX.type = 'button';
    closeX.textContent = '✕';
    closeX.title = 'Close';
    closeX.style.cssText = 'margin-left:auto;background:none;border:none;color:var(--fg-3);font-size:14px;cursor:pointer;padding:4px;';
    head.appendChild(closeX);
    const body = document.createElement('div');
    body.style.cssText = 'padding:14px 16px;overflow-y:auto;display:flex;flex-direction:column;gap:10px;';
    const hintEl = document.createElement('div');
    hintEl.style.cssText = 'font-size:12px;color:var(--fg-3);line-height:1.45;';
    hintEl.textContent = hint;
    body.appendChild(hintEl);
    const ta = document.createElement('textarea');
    ta.value = isCustom
      ? ((S.directives && S.directives.custom) ? (S.directives.custom[key] || '') : '')
      : ((S.directives && S.directives[key]) || '');
    ta.placeholder = 'No injection set — this role runs with its own prompt.';
    ta.spellcheck = false;
    ta.style.cssText = 'width:100%;min-height:150px;resize:vertical;background:var(--bg-0);color:var(--fg-1);border:var(--border-width) solid var(--border);border-radius:8px;padding:10px;font-family:var(--font-mono, monospace);font-size:12px;line-height:1.5;';
    ta.readOnly = !editable;
    if (!editable) ta.style.opacity = '.85';
    body.appendChild(ta);
    const foot = document.createElement('div');
    foot.style.cssText = 'display:flex;align-items:center;gap:10px;padding:12px 16px;border-top:var(--border-width) solid var(--border);';
    const err = document.createElement('span');
    err.style.cssText = 'font-size:12px;color:var(--danger);flex:1;';
    foot.appendChild(err);
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'ac-btn ac-btn-ghost';
    cancel.textContent = 'Close';
    foot.appendChild(cancel);
    let saveBtn = null;
    if (editable) {
      saveBtn = document.createElement('button');
      saveBtn.type = 'button';
      saveBtn.className = 'ac-btn';
      saveBtn.textContent = 'Save';
      saveBtn.style.cssText = 'background:var(--accent);color:var(--bg-0);border:none;border-radius:8px;padding:6px 16px;font-weight:600;cursor:pointer;';
      foot.insertBefore(saveBtn, cancel);
    }
    panel.appendChild(head); panel.appendChild(body); panel.appendChild(foot);
    ov.appendChild(panel);
    document.body.appendChild(ov);
    if (editable) ta.focus();

    const close = () => {
      document.removeEventListener('keydown', onKey);
      if (_directiveModalCleanup === close) _directiveModalCleanup = null;
      ov.remove();
    };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    _directiveModalCleanup = close;
    closeX.addEventListener('click', close);
    cancel.addEventListener('click', close);
    ov.addEventListener('pointerdown', (e) => { if (e.target === ov) close(); });
    if (saveBtn) {
      saveBtn.addEventListener('click', async () => {
        const next = { ...(S.directives || {}), custom: { ...((S.directives && S.directives.custom) || {}) } };
        if (isCustom) next.custom[key] = ta.value;
        else next[key] = ta.value;
        _markSaving(saveBtn);
        err.textContent = '';
        try {
          await adapter.saveDirectives(next);
          S.directives = next;
          _flashSaveCheck(saveBtn, true);
          renderSaved();
          setTimeout(close, 350);
        } catch (e2) {
          _flashSaveCheck(saveBtn, false, (e2 && e2.message) || 'Save failed');
          err.textContent = (e2 && e2.message) || 'Save failed';
        }
      });
    }
  }

  // Full (re)build of the saved list. Initial loads render immediately; role,
  // add, and remove changes can opt into keyed layout animation.
  function renderSaved({ animate = false, enteringUids = [] } = {}) {
    const before = animate ? _snapshotSavedLayout() : null;
    table.querySelectorAll('.ac-saved-injected').forEach(n => n.remove());
    // Inherited (app default) rows render first; the agent's own editable rows
    // follow. On the admin table S.inherited is always empty. On the per-agent
    // table an inherited row is NOT hidden when the agent also owns the same
    // model — the redundant pair shows side by side (the inherited row is the
    // read-only app-default reference, the own row is the agent's working copy).
    const rows = [
      ...(S.inherited || []).map(p => ({ p, readOnly: true, rolesEditable: false })),
      ...S.providers.map(p => ({ p, readOnly: false, rolesEditable: false })),
    ];
    const slotRank = (p) => {
      if (p.enabled) return 0;
      if (p.high_effort_capable) return 1;
      if (p.use_for_image) return 2;
      if (p.use_for_image_out) return 3;
      if (p.use_for_system) return 4;
      if (p.use_for_voice) return 5;
      return 6;
    };
    rows.sort((a, b) => slotRank(a.p) - slotRank(b.p));
    if (!rows.length) {
      if (before) _animateSavedLayout(before, enteringUids);
      return;
    }
    const cfgAnchor = table.querySelector('.ac-model-cfg-row');
    _buildSavedHead(cfgAnchor);
    if (S.directives) _buildDirectiveStrip(cfgAnchor);
    rows.forEach(({ p, readOnly, rolesEditable }) => _buildSavedRow(p, readOnly, false, cfgAnchor, rolesEditable));
    // keep the advanced/+ row pinned last
    table.appendChild(advRow);
    // Wire drag-to-reorder on the agent's own rows (not inherited/read-only).
    // The handle is `.ac-saved-drag-handle`; rows are identified by `data-mt-uid`.
    makeRowsReorderable(table, {
      rowSelector: '.ac-saved-own[data-mt-uid]:not(.ac-saved-role)',
      handleSelector: '.ac-saved-drag-handle',
      dropBefore: advRow,
      onReorder: (orderedUids) => {
        // Reorder S.providers to match the new DOM order. Inherited rows are
        // read-only and never have a drag handle, so they stay put; only the
        // agent's own rows (which carry _uid matching data-mt-uid) are reordered.
        const uidToIdx = new Map();
        S.providers.forEach((p, i) => uidToIdx.set(String(p._uid), i));
        const ownUids = orderedUids.filter(uid => uidToIdx.has(uid));
        if (!ownUids.length) return;
        // Build the new providers array in the dragged order.
        const reordered = ownUids.map(uid => S.providers[uidToIdx.get(uid)]);
        // Preserve any own-rows that somehow weren't in the DOM (shouldn't
        // happen, but defensive) at the end in their original order.
        const seen = new Set(ownUids);
        S.providers.forEach((p, i) => {
          if (!seen.has(String(p._uid))) reordered.push(p);
        });
        S.providers = reordered;
        adapter.saveRoster({ providers: S.providers });
      },
    });
    if (before) _animateSavedLayout(before, enteringUids);
  }

  // ── Add / Clear ──
  async function addModel() {
    const provider = S.currentProvider === '_custom' ? 'custom' : S.currentProvider;
    const baseUrl = urlInput.value.trim(), apiKey = keyInput.value.trim();
    if (!baseUrl) return setModelStatus('Please enter a Base URL');
    if (!apiKey) return setModelStatus('Please enter an API Key');
    if (!S.selectedModel) return setModelStatus('Please select a model');
    const capText = S.selCaps.text !== false, capImage = !!S.selCaps.image, capImageOut = !!S.selCaps.imageOut, capVoice = !!S.selCaps.voice;
    // saveSingle persists the ONE top-level default-brain slot (provider/url/key/
    // model) — it must only be touched when this model is actually meant to become
    // that default. Only the very first model ever added becomes the default (the
    // agent needs SOMETHING to run on); every later add is a plain custom roster
    // entry that must NOT overwrite the default. Previously any text-capable add
    // became default AND auto-claimed the Standard role, silently clobbering the
    // existing default on a second add.
    const becomesDefault = !S.providers.length;
    if (becomesDefault) {
      await adapter.saveSingle({ provider, base_url: baseUrl, api_key: apiKey, model: S.selectedModel, providers: S.providerConfigs, text_capable: capText, image_capable: capImage, image_out_capable: capImageOut, voice_capable: capVoice });
    }
    const dupe = S.providers.find(p => p.provider === provider && p.model === S.selectedModel && p.base_url === baseUrl);
    if (!dupe) {
      const newP = { provider, base_url: baseUrl, api_key: apiKey, model: S.selectedModel, enabled: becomesDefault, text_capable: capText, image_capable: capImage, use_for_image: false, image_out_capable: capImageOut, use_for_image_out: false, high_effort_capable: false, use_for_system: false, use_for_voice: false, _uid: ++S.uid };
      // Only the first-ever add is role-bearing (enabled → Standard); every later
      // add is role-neutral and never displaces an existing holder. The guards
      // below still cover the first add, where enabling the new model clears the
      // previous Standard holder — an inherited row that loses the role simply
      // sinks in the sort; no copy is created (inherited rows are read-only
      // references to the app default).
      const existingRows = [...S.inherited, ...S.providers];
      if (newP.enabled) existingRows.forEach(p => { p.enabled = false; });
      if (newP.high_effort_capable) existingRows.forEach(p => { p.high_effort_capable = false; });
      if (newP.use_for_image) existingRows.forEach(p => { p.use_for_image = false; });
      if (newP.use_for_image_out) existingRows.forEach(p => { p.use_for_image_out = false; });
      if (newP.use_for_system) existingRows.forEach(p => { p.use_for_system = false; });
      if (newP.use_for_voice) existingRows.forEach(p => { p.use_for_voice = false; });
      S.providers.push(newP);
      const saves = [adapter.saveRoster({ providers: S.providers })];
      await Promise.all(saves);
      // Rebuild into the role-sorted position. Stable row ids let renderSaved
      // slide existing rows around the insertion while the new row fades in.
      renderSaved({ animate: true, enteringUids: [newP._uid] });
    } else {
      if (apiKey && dupe.api_key !== apiKey) { dupe.api_key = apiKey; }
      await adapter.saveRoster({ providers: S.providers });
    }
    setModelStatus('Saved', true);
    onModelChange();
  }
  async function clearModel() {
    if (adapter.clearSingle) { try { await adapter.clearSingle(); } catch {} await reload(); }
    else { keyInput.value = ''; modelSearch.value = ''; S.selectedModel = ''; renderDetectedCaps(null); setModelStatus(''); }
    onModelChange();
  }

  // ── wire configurator events ──
  provSel.addEventListener('change', () => {
    rememberProvider(S.currentProvider);
    S.currentProvider = provSel.value;
    const saved = S.providerConfigs[S.currentProvider];
    urlInput.value = (saved && saved.base_url) || S.presets[S.currentProvider]?.base_url || '';
    keyInput.value = ''; keyInput.placeholder = 'sk-...';
    S.selectedModel = (saved && saved.model) || '';
    modelSearch.value = S.selectedModel;
    setModelStatus(S.selectedModel ? `Selected: ${S.selectedModel}` : '', true);
    renderDetectedCaps(null); S.allModels = []; modelDd.style.display = 'none';
    loadModels();
    autosaveSingle(provCheck);
  });
  keyInput.addEventListener('input', () => { scheduleFetch(); scheduleAutosave(keyCheck); });
  urlInput.addEventListener('input', () => { scheduleFetch(); scheduleAutosave(urlCheck); });
  modelSearch.addEventListener('focus', () => renderModelDropdown(modelSearch.value.toLowerCase()));
  modelSearch.addEventListener('input', () => renderModelDropdown(modelSearch.value.toLowerCase()));
  addBtn.addEventListener('click', addModel);
  clearBtn.addEventListener('click', clearModel);
  advToggle.addEventListener('click', () => advRow.classList.toggle('expanded'));
  addToggle.addEventListener('click', () => table.classList.toggle('ac-cfg-collapsed'));

  const ddAway = (e) => { if (!searchWrap.contains(e.target)) modelDd.style.display = 'none'; };
  document.addEventListener('click', ddAway);
  docListeners.push(['click', ddAway]);

  // ── load / reload ──
  // Progressive load: show the table FRAME immediately (kept collapsed, so only
  // the slim +/advanced action row is visible) with a lightweight inline spinner
  // over the saved-models area — instead of hiding the whole panel behind one
  // centered spinner until the per-user config read returns.
  //
  // The provider list (fetchPresets — has an INSTANT local fallback) and the saved
  // config (the slow per-user vault+DB read) are INDEPENDENT, so they're fetched
  // together rather than one after the other; the config read, in turn, is a
  // single combined bundle (see the admin adapter) instead of two round-trips.
  async function reload() {
    table.style.display = '';
    table.classList.add('ac-cfg-collapsed');   // keep the add-a-model form hidden while loading
    // Show skeleton immediately so the user sees structure (header + phantom rows)
    // instead of just the Advanced/+ buttons during the async fetch.
    if (skelRoot) skelRoot.style.display = '';
    try {
      const [presets, cfg, directives] = await Promise.all([
        Promise.resolve(fetchPresets()).catch(() => ({})),
        Promise.resolve(adapter.loadConfig()).catch((e) => { console.warn('model-table: load failed', e); return {}; }),
        Promise.resolve(adapter.loadDirectives ? adapter.loadDirectives() : null).catch(() => null),
      ]);
      // Per-agent roster-role prompt directives — null hides the strip entirely
      // (mounts without a directives read).
      S.defaultModelId = (cfg && cfg.model) || '';
      const d = (directives && typeof directives === 'object') ? directives : null;
      if (d) d.custom = (d.custom && typeof d.custom === 'object') ? d.custom : {};
      S.directives = d;
      // Hide skeleton before rendering real data so there's no overlap flash
      if (skelRoot) skelRoot.style.display = 'none';
      S.presets = presets || {};
      provSel.innerHTML = '';
      for (const [k, preset] of Object.entries(S.presets)) {
        const o = document.createElement('option'); o.value = k; o.textContent = preset.name; provSel.appendChild(o);
      }
      const custom = document.createElement('option'); custom.value = '_custom'; custom.textContent = 'Custom'; provSel.appendChild(custom);

      S.providerConfigs = cfg.providerConfigs || {};
      S.currentProvider = (cfg.provider && S.presets[cfg.provider]) ? cfg.provider : '_custom';
      provSel.value = S.currentProvider;
      urlInput.value = cfg.base_url || '';
      keyInput.value = cfg.api_key || '';
      S.selectedModel = cfg.model || '';
      modelSearch.value = S.selectedModel;
      S.selCaps = { text: cfg.text_capable !== false, image: !!cfg.image_capable, imageOut: !!cfg.image_out_capable, voice: !!cfg.voice_capable };
      renderDetectedCaps(S.selectedModel ? { text_capable: S.selCaps.text, image_capable: S.selCaps.image, image_out_capable: S.selCaps.imageOut, voice_capable: S.selCaps.voice } : null);
      setModelStatus(S.selectedModel ? `Selected: ${S.selectedModel}` : '', true);

      const norm = (p) => ({
        provider: p.provider || 'openrouter', base_url: p.base_url || '', api_key: p.api_key || '', model: p.model || '',
        enabled: p.enabled !== false,
        text_capable: p.text_capable !== false, image_capable: !!p.image_capable, use_for_image: !!p.use_for_image,
        image_out_capable: !!p.image_out_capable, use_for_image_out: !!p.use_for_image_out,
        high_effort_capable: !!p.high_effort_capable,
        use_for_system: !!p.use_for_system,
        use_for_voice: !!p.use_for_voice, _uid: ++S.uid,
        // Stable key for inherited rows. Preserved verbatim.
        _ovrKey: p._ovrKey,
      });
      S.inherited = (Array.isArray(cfg.inherited) ? cfg.inherited : []).map(norm);
      S.providers = (Array.isArray(cfg.roster) ? cfg.roster : []).map(norm);
      // Per-agent table: inherited (app-default) rows ALWAYS stay visible, even
      // when the agent also owns a row for the same (provider|base_url|model) —
      // the redundant pair is intentional, so the agent admin can see the app
      // default and their own copy side by side. The backend union keeps the
      // agent's own row authoritative for that model at runtime; editing roles
      // on the inherited row still materializes a per-agent override.
      renderSaved();
      // First load with no saved models: open the configurator so the user sees
      // the add-a-model form up front. Once at least one model is saved, default
      // back to the collapsed (form-hidden) state. The + toggle still flips it.
      const hasSaved = S.providers.length + (S.inherited ? S.inherited.length : 0) > 0;
      table.classList.toggle('ac-cfg-collapsed', hasSaved);
      if (keyInput.value) loadModels();
    } catch (e) {
      console.warn('model-table: render failed', e);
    } finally {
      table.style.display = '';   // frame was shown up front; nothing to un-hide
    }
  }

  function destroy() {
    clearTimeout(fetchDebounce); clearTimeout(autosaveDebounce);
    if (_directiveModalCleanup) { try { _directiveModalCleanup(); } catch (_) {} _directiveModalCleanup = null; }
    docListeners.forEach(([t, fn]) => document.removeEventListener(t, fn));
    host.innerHTML = '';
  }

  reload();
  return { reload, destroy, getState: () => ({ ...S }) };
}
