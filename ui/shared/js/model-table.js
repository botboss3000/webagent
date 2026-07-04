'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'.

/**
 * Shared LLM model-table component — the "Models" configurator + saved-models
 * list (Text / In / Out capability columns + token usage) used in TWO places:
 *
 *   • Admin → Agent Settings → Models   (ui/admin-tools/app-config/agent-settings/agent-settings.js)
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
 *     saveSingle(single)        → persist the DEFAULT model (provider/url/key/model + caps);
 *                                 also fired when a "Default" radio in the saved list is picked
 *     saveRoster({providers})   → persist the saved-models list
 *     saveInheritedOverride?(p) → (per-agent table only) persist this agent's
 *                                 chosen subset of an INHERITED app-default model.
 *                                 `p` carries the row's current capability flags
 *                                 (enabled / use_for_image / use_for_image_out /
 *                                 high_effort_capable) + `_ovrKey`. Inherited rows
 *                                 carry `_ceiling` ({text,image_in,image_out,effort})
 *                                 = the admin's app-wide allowance; a box the admin
 *                                 left off stays greyed (can't exceed it), a box the
 *                                 admin enabled is unticked to opt this agent out.
 *     loadPresets?()            → { key: {name, base_url} }     (default: /admin/settings/providers)
 *     fetchModels?({provider, api_key, base_url}) → { models:[...], error? }
 *     fetchModelInfo?(model)    → { info }                       (default: /admin/settings/model-info)
 *     fetchModelUsage?({model, provider}) → usage                (default: /admin/settings/model-usage)
 *     clearSingle?()            → optional server wipe of the single default
 *   opts.fetchFn   — fetch-like (default window.fetch); admin passes its _fetch
 *   opts.apiPath   — path mapper (default identity); admin passes apiPath
 *   opts.advancedExtra(bodyEl) — optional: append extra controls to the advanced body
 *   opts.onModelChange()       — optional: fired after a model add/clear (e.g. refresh footer)
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

let _mtInstanceSeq = 0;

export function mountModelTable(host, opts = {}) {
  const adapter = opts.adapter || {};
  // Unique radio group name for THIS table instance's "Default" column, so two
  // mounts on the same page never share a radio group.
  const defaultRadioName = 'mt-default-' + (++_mtInstanceSeq);
  const fetchFn = opts.fetchFn || ((...a) => window.fetch(...a));
  const apiPath = opts.apiPath || ((p) => p);
  const features = opts.features || {};
  const onModelChange = opts.onModelChange || (() => {});
  // 'user' (default) shows only the current user's usage for each model; 'global'
  // (admin Agent Settings) sums every agent, user, and background task app-wide.
  const usageScope = opts.usageScope || 'user';

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
    uid: 0,
    defaultUid: null,           // _uid of the row currently picked as the default
  };
  let fetchDebounce = null, autosaveDebounce = null;
  let modelsBusy = 0;   // >0 while the provider's available-model list is loading (drives the + button spinner)
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
      ['Text', m.text_capable !== false],
      ['Image-in', !!m.image_capable],
      ['Image-out', !!m.image_out_capable],
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
        + (badges ? `<div class="ac-model-item-badges">${badges}</div>` : '');
      item.addEventListener('click', () => {
        S.selectedModel = m.id; modelSearch.value = m.id; modelDd.style.display = 'none';
        S.selCaps = { text: m.text_capable !== false, image: !!m.image_capable, imageOut: !!m.image_out_capable };
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
        if (sel) { S.selCaps = { text: sel.text_capable !== false, image: !!sel.image_capable, imageOut: !!sel.image_out_capable }; renderDetectedCaps(sel); }
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
  // Inherited (app-default) capability cell with a CEILING. The admin's global
  // checkboxes are the maximum: a capability the admin left OFF stays greyed +
  // disabled here (this agent can't exceed the app-wide allowance — managed in
  // Admin → Agent Settings), while a capability the admin turned ON is a live
  // checkbox the per-agent admin can untick to OPT THIS AGENT OUT of it. Default
  // (untouched) is ON, so an agent keeps every globally-available capability until
  // someone narrows it. Unticking every box = this agent won't use the model at all.
  function capCellInherited(ceilingOn, checked, title, onChange) {
    const cell = document.createElement('span');
    cell.className = 'ac-saved-cap';
    cell.addEventListener('click', e => e.stopPropagation());
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.style.cssText = 'width:15px;height:15px;accent-color:var(--accent);cursor:pointer;';
    if (!ceilingOn) {
      cb.disabled = true; cb.checked = false; cb.style.cursor = 'default'; cb.style.opacity = '0.4';
      cb.title = title + ' (not enabled app-wide — managed in Admin → Agent Settings)';
    } else {
      cb.checked = !!checked;
      cb.title = title + ' (inherited app default — untick to opt this agent out)';
      cb.addEventListener('change', () => onChange(cb.checked, cb));
    }
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
      + `<span class="ac-cap-badge ${p.high_effort_capable ? 'on' : 'off'}">Text +</span></div>`;
    const desc = meta.description ? `<div class="ac-model-meta-desc" style="margin-top:8px;">${esc(meta.description)}</div>` : '';
    // Provider now lives here (lifted off the one-line row).
    const provName = S.presets[p.provider]?.name || p.provider || 'custom';
    const provHtml = `<div class="ac-model-meta-prov" style="margin-top:2px;font-size:12px;color:var(--fg-3);">Provider: <strong style="color:var(--fg-2);">${esc(provName)}</strong></div>`;
    body.innerHTML = provHtml + (chips ? `<div class="ac-model-meta-chips" style="margin-top:8px;">${chips}</div>` : '') + usageHtml + caps + desc
      + (chips ? '' : '<div class="ac-hint" style="margin:8px 0 0;">No token or cost data available for this model.</div>');
    if (readOnly) {
      const note = document.createElement('div'); note.className = 'ac-hint'; note.style.margin = '8px 0 0';
      note.textContent = 'Inherited from the app default — managed in Admin → Agent Settings.';
      body.appendChild(note);
      return;
    }
    const delWrap = document.createElement('div'); delWrap.className = 'ac-saved-del-row';
    const delBtn = document.createElement('button'); delBtn.className = 'ac-btn ac-btn-ghost ac-saved-del-btn';
    delBtn.textContent = 'Remove model'; delBtn.style.color = 'var(--danger)';
    delBtn.addEventListener('click', e => {
      e.stopPropagation();
      S.providers = S.providers.filter(x => x._uid !== p._uid);
      Promise.resolve(adapter.saveRoster({ providers: S.providers }));
      // Remove ONLY this row's node — no list rebuild, so scroll stays put.
      const node = table.querySelector(`.ac-saved-injected[data-mt-uid="${p._uid}"]`);
      if (node) node.remove();
      // If the list is now empty (no own rows and nothing inherited), drop the header.
      if (!S.providers.length && !(S.inherited || []).length) {
        table.querySelectorAll('.ac-saved-head').forEach(n => n.remove());
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

  // The normalized provider key for the configurator's current single config —
  // '_custom' maps to the stored 'custom'. Used to match a roster row against the
  // top-level default so the right "Default" radio shows checked.
  function _curProviderKey() { return S.currentProvider === '_custom' ? 'custom' : S.currentProvider; }

  // Which roster row IS the current default? The default is the top-level single
  // config (S.selectedModel + provider/base_url). Match on model, disambiguating
  // by provider + base_url when several rows share a model id.
  function _defaultRowUid(rows) {
    const cands = rows.filter(r => r.p.model && r.p.model === S.selectedModel);
    if (!cands.length) return null;
    if (cands.length === 1) return cands[0].p._uid;
    const curProv = _curProviderKey(), curUrl = (urlInput.value || '').trim();
    const exact = cands.find(r => (r.p.provider || 'custom') === curProv && (r.p.base_url || '') === curUrl);
    return (exact || cands[0]).p._uid;
  }

  // Whether a row may be picked as the default brain: it must have a model, be
  // Text-capable, AND have its Text column checked on (p.enabled). An In/Out-only
  // worker — or a Text model with Text unticked — can't be the default.
  function _canBeDefault(p) {
    return !!p.model && p.text_capable !== false && p.enabled !== false;
  }

  // One "Default" radio cell. Any Text-checked model (a brain candidate) is
  // selectable — INCLUDING inherited app-default rows, so a per-agent admin can
  // pick which model this agent runs by default among the Text options (mirroring
  // the admin Models table). Non-Text rows show a GREYED, disabled radio (never an
  // empty slot). Picking a row makes it the agent/app default via the saveSingle
  // path; inherited picks reference the app credentials (see setDefaultRow).
  function defaultCell(p, checked, readOnly) {
    const cell = document.createElement('span');
    cell.className = 'ac-saved-cap';
    cell.addEventListener('click', e => e.stopPropagation());
    const rb = document.createElement('input');
    rb.type = 'radio'; rb.name = defaultRadioName;
    rb.className = 'ac-saved-def-input';
    rb.style.cssText = 'width:15px;height:15px;accent-color:var(--accent);cursor:pointer;';
    const eligible = _canBeDefault(p);
    rb.checked = !!checked && eligible;
    if (!eligible) {
      rb.disabled = true; rb.style.cursor = 'default'; rb.style.opacity = '0.4';
      rb.title = 'Tick the Text column to make this model selectable as the default';
    } else {
      rb.title = 'Use as the default model (the user can switch it per chat)';
    }
    // Wire the change handler unconditionally (a disabled radio can't fire it) and
    // gate the action on live eligibility — so a row whose Text gets ticked AFTER
    // render (which un-disables this radio via the Text toggle) becomes pickable
    // too, instead of silently doing nothing. The handler is attached once here;
    // no re-wiring on toggle, so it can't double-fire.
    rb.addEventListener('change', () => {
      if (rb.checked && _canBeDefault(p)) setDefaultRow(p, cell);
    });
    cell.appendChild(rb);
    return cell;
  }

  // Promote a roster row to the default brain: mirror its provider/model/key/caps
  // into the configurator's single-config slots (as reload() does) and persist via
  // saveSingle. No list rebuild — the "Default" radios share one group, so the
  // browser already moved the selection in place; re-rendering here would collapse
  // and re-expand the list and jump the scroll position.
  async function setDefaultRow(p, slotEl) {
    const prevDefaultUid = S.defaultUid;
    // Inherited (app-default) rows reference the APP's credentials — don't copy the
    // admin's api_key into the agent's own config, or a later key rotation would
    // strand the agent. We store provider/base_url/model and leave the key blank so
    // the backend inherits the live app key (_merge_agent_override keeps base's key
    // when the override doesn't set one).
    const isInherited = !!p._ceiling;
    S.selectedModel = p.model || '';
    S.selCaps = { text: p.text_capable !== false, image: !!p.image_capable, imageOut: !!p.image_out_capable };
    S.currentProvider = (p.provider && S.presets[p.provider]) ? p.provider : '_custom';
    provSel.value = S.currentProvider;
    urlInput.value = p.base_url || '';
    if (p.api_key && !isInherited) keyInput.value = p.api_key;
    modelSearch.value = S.selectedModel;
    renderDetectedCaps({ text_capable: S.selCaps.text, image_capable: S.selCaps.image, image_out_capable: S.selCaps.imageOut });
    // Saving feedback rides ON TOP of the clicked Default radio (matching the
    // capability boxes); falls back to the configurator's side tick if no cell.
    if (slotEl) _markSaving(slotEl); else markSaving(modelCheck);
    try {
      await adapter.saveSingle({
        provider: (p.provider === '_custom' || !p.provider) ? 'custom' : p.provider,
        base_url: p.base_url || '', api_key: isInherited ? '' : (p.api_key || ''), model: p.model,
        providers: S.providerConfigs,
        text_capable: p.text_capable !== false, image_capable: !!p.image_capable, image_out_capable: !!p.image_out_capable,
      });
      S.defaultUid = p._uid;
      if (slotEl) _flashSaveCheck(slotEl, true); else flashCheck(modelCheck, true);
      onModelChange();
    } catch (e) {
      if (slotEl) _flashSaveCheck(slotEl, false, e.message); else flashCheck(modelCheck, false, e.message);
      // Save failed — the browser already moved the radio, so snap the selection
      // back to the previously-default row so the column stays truthful.
      table.querySelectorAll(`input[name="${defaultRadioName}"]`).forEach(rb => { rb.checked = false; });
      const prevNode = table.querySelector(`.ac-saved-injected[data-mt-uid="${prevDefaultUid}"] .ac-saved-def-input`);
      if (prevNode) prevNode.checked = true;
    }
  }

  // Column template shared by the saved-list header and every row.
  // Order: Model · Def · Txt · Txt+ · In · Out · Usage — the Default toggle sits
  // immediately left of the capability columns rather than out on the far left.
  // (Txt+ = a premium / more-capable model the agent can upgrade onto; not to be
  // confused with provider reasoning "effort" levels — min/low/med/high.)
  const SAVED_GRID = 'minmax(0, 1fr) 28px 28px 28px 28px 28px 38px';

  // Build + insert ONE saved-model row (just before the configurator anchor) and
  // kick off its usage fetch. Returns the rowWrap. Capability toggles and the
  // default radio persist IN PLACE — no list rebuild — so editing a row never
  // collapses the table or disturbs the scroll position.
  function _buildSavedRow(p, readOnly, checked, cfgAnchor) {
    const rowWrap = document.createElement('div'); rowWrap.className = 'ac-row ac-saved-injected';
    rowWrap.dataset.mtUid = p._uid;
    const row = document.createElement('div'); row.className = 'ac-ability-row ac-saved-row';
    row.style.gridTemplateColumns = SAVED_GRID;
    // Single-line label: just the model name (the provider moved into the expanded
    // detail panel, so the row stays one line tall).
    const label = document.createElement('span'); label.className = 'ac-ability-label ac-saved-name-click';
    label.title = 'Show usage details';
    const badge = readOnly ? ' <span class="ac-cap-badge on" title="Inherited from the app default" style="margin-left:6px;">Inherited</span>' : '';
    label.innerHTML = `<span class="ac-ability-name ac-saved-model-name">${esc(p.model || '—')}${badge}</span>`;
    row.appendChild(label);
    // Default toggle sits just left of the capability columns.
    const defCell = defaultCell(p, checked, readOnly);
    const defInput = defCell.querySelector('.ac-saved-def-input');
    row.appendChild(defCell);
    // Persist a capability change without rebuilding the list — behind the
    // hold-to-cancel guard so an accidental double-click can't flip it back. The
    // box locks the moment it's ticked (hazard triangle on top); the save runs,
    // then settles to a green ✓ (or an orange ⚠ + revert on failure). A deliberate
    // long-press of the triangle reverts the box and saves the reverted value.
    // `afterChange` re-syncs dependent UI (default-radio eligibility) on both the
    // optimistic flip AND any revert. (guardCapToggle reads the flipped state off
    // the checkbox, so `v` is passed only to keep the call sites self-documenting.)
    const persistCap = (key, v, cb, afterChange) => {
      guardCapToggle(cb, {
        writeFlag: (val) => { p[key] = val; },
        save: () => adapter.saveRoster({ providers: S.providers }),
        afterChange,
      });
    };
    // Persist a per-agent opt-out on an INHERITED (app-default) model. Same guard +
    // feedback as own rows, but writes the agent's chosen subset of the app-wide
    // ceiling (saveInheritedOverride) instead of the agent's own roster.
    const persistInherited = (key, v, cb, afterChange) => {
      guardCapToggle(cb, {
        writeFlag: (val) => { p[key] = val; },
        save: () => (adapter.saveInheritedOverride ? adapter.saveInheritedOverride(p) : null),
        afterChange,
      });
    };
    // Toggling the Text column flips this model's eligibility to be the default —
    // enable/disable the default radio live so the two columns stay consistent.
    // Shared by the agent's own rows and the inherited (ceiling) rows: any
    // Text-checked model is a default candidate, including inherited ones.
    const syncDefaultEligibility = () => {
      if (!defInput) return;
      const ok = _canBeDefault(p);
      defInput.disabled = !ok;
      defInput.style.cursor = ok ? 'pointer' : 'default';
      defInput.style.opacity = ok ? '1' : '0.4';
      defInput.title = ok ? 'Use as the default model (the user can switch it per chat)'
                          : 'Tick the Text column to make this model selectable as the default';
      if (!ok) defInput.checked = false;
    };
    // Text toggles pass syncDefaultEligibility as the guard's afterChange, so the
    // default radio's enabled/disabled state stays consistent on the optimistic
    // flip AND on a hold-to-cancel revert.
    const onTextToggle = (v, cb) => persistCap('enabled', v, cb, syncDefaultEligibility);
    const onInhTextToggle = (v, cb) => persistInherited('enabled', v, cb, syncDefaultEligibility);
    // Inherited rows whose admin ceiling is known render INTERACTIVE ceiling cells
    // (opt in/out within the app-wide allowance); all other rows (the agent's own
    // models, or inherited rows on a pre-ceiling backend) use the standard cells.
    const ceil = (readOnly && p._ceiling) ? p._ceiling : null;
    if (ceil) {
      row.appendChild(capCellInherited(ceil.text, p.enabled, 'Available as the chat / brain model', onInhTextToggle));
      row.appendChild(capCellInherited(ceil.effort, !!p.high_effort_capable, 'Text + — a premium / more-capable model the agent can upgrade onto for a hard task', (v, cb) => persistInherited('high_effort_capable', v, cb)));
      row.appendChild(capCellInherited(ceil.image_in, !!p.use_for_image, 'Use to read attached images', (v, cb) => persistInherited('use_for_image', v, cb)));
      row.appendChild(capCellInherited(ceil.image_out, !!p.use_for_image_out, 'Use to generate images', (v, cb) => persistInherited('use_for_image_out', v, cb)));
    } else {
      row.appendChild(capCell(p.text_capable !== false, p.enabled, 'Available as the chat / brain model', onTextToggle, readOnly));
      // Text + tier — only text/brain models can be a Text + target, so the gate
      // mirrors the Text column (an image-only worker shows an inert dot).
      row.appendChild(capCell(p.text_capable !== false, !!p.high_effort_capable, 'Text + — a premium / more-capable model the agent can upgrade onto for a hard task', (v, cb) => persistCap('high_effort_capable', v, cb), readOnly));
      row.appendChild(capCell(!!p.image_capable, !!p.use_for_image, 'Use to read attached images', (v, cb) => persistCap('use_for_image', v, cb), readOnly));
      row.appendChild(capCell(!!p.image_out_capable, !!p.use_for_image_out, 'Use to generate images', (v, cb) => persistCap('use_for_image_out', v, cb), readOnly));
    }
    const usageCell = document.createElement('span'); usageCell.className = 'ac-saved-usage';
    const statIn = usageLine('Total input tokens'), statOut = usageLine('Total output tokens'), statCost = usageLine('Total provider cost');
    usageCell.appendChild(statIn.line); usageCell.appendChild(statOut.line); usageCell.appendChild(statCost.line);
    row.appendChild(usageCell);
    const body = document.createElement('div'); body.className = 'ac-ability-body';
    body.innerHTML = '<div class="ac-hint" style="margin:0;">Loading details…</div>';
    label.addEventListener('click', () => { const open = rowWrap.classList.toggle('expanded'); if (open) loadSavedDetail(p, body, statIn.val, statOut.val, statCost.val, readOnly); });
    rowWrap.appendChild(row); rowWrap.appendChild(body);
    table.insertBefore(rowWrap, cfgAnchor);
    fillUsage(p, statIn.val, statOut.val, statCost.val, readOnly);
    return rowWrap;
  }

  // Build the saved-list header (Model · Def · Txt · Txt+ · In · Out · Usage) once.
  function _buildSavedHead(cfgAnchor) {
    const head = document.createElement('div');
    head.className = 'ac-ability-row ac-saved-head ac-saved-row ac-saved-injected';
    head.style.gridTemplateColumns = SAVED_GRID;
    const headName = document.createElement('span'); headName.className = 'ac-ability-label';
    headName.innerHTML = '<span class="ac-saved-th">Model</span>'; head.appendChild(headName);
    const headDef = document.createElement('span'); headDef.className = 'ac-saved-cap ac-saved-th';
    headDef.textContent = 'Def'; headDef.title = 'Default model — the one the agent runs by default (users can switch it per chat)';
    head.appendChild(headDef);
    colCells(head, [['Txt', 'Available as the chat / brain model'], ['Txt+', 'Text + — a premium / more-capable model the agent can upgrade onto for a hard task'], ['In', 'Use to read attached images (vision)'], ['Out', 'Use to generate images']]);
    const headUsage = document.createElement('span'); headUsage.className = 'ac-saved-th ac-saved-usage-th';
    headUsage.textContent = 'Usage'; headUsage.title = 'Total tokens in / out and provider cost'; head.appendChild(headUsage);
    table.insertBefore(head, cfgAnchor);
  }

  // Full (re)build of the saved list — used on initial load only. In-place edits
  // (toggle / default / add / remove) update just their own row, never call this.
  function renderSaved() {
    table.querySelectorAll('.ac-saved-injected').forEach(n => n.remove());
    // Inherited (app default) rows render first, read-only; the agent's own
    // editable rows follow. On the admin table S.inherited is always empty.
    // A model the agent picked that matches an app default is shown BOTH ways on
    // purpose: once as the read-only "Inherited" app row, and once as the agent's
    // own editable row — so it's clear the agent is reusing an app-wide model.
    const rows = [
      ...(S.inherited || []).map(p => ({ p, readOnly: true })),
      ...S.providers.map(p => ({ p, readOnly: false })),
    ];
    if (!rows.length) return;
    const cfgAnchor = table.querySelector('.ac-model-cfg-row');
    const defUid = _defaultRowUid(rows);
    S.defaultUid = defUid;
    _buildSavedHead(cfgAnchor);
    rows.forEach(({ p, readOnly }) => _buildSavedRow(p, readOnly, p._uid === defUid, cfgAnchor));
    // keep the advanced/+ row pinned last
    table.appendChild(advRow);
  }

  // ── Add / Clear ──
  async function addModel() {
    const provider = S.currentProvider === '_custom' ? 'custom' : S.currentProvider;
    const baseUrl = urlInput.value.trim(), apiKey = keyInput.value.trim();
    if (!baseUrl) return setModelStatus('Please enter a Base URL');
    if (!apiKey) return setModelStatus('Please enter an API Key');
    if (!S.selectedModel) return setModelStatus('Please select a model');
    const capText = S.selCaps.text !== false, capImage = !!S.selCaps.image, capImageOut = !!S.selCaps.imageOut;
    // saveSingle persists the ONE top-level default-brain slot (provider/url/key/
    // model) — it must only be touched when this model is actually meant to become
    // that default. That's true for the very first model ever added (the agent
    // needs SOMETHING to run on) or for any later text-capable add (mirroring the
    // radio-clearing gate below). A later image-in/out-only or non-text worker add
    // must NOT overwrite the default — previously it did unconditionally, which is
    // why adding a second (purpose-specific) model silently clobbered the first.
    const becomesDefault = capText || !S.providers.length;
    if (becomesDefault) {
      await adapter.saveSingle({ provider, base_url: baseUrl, api_key: apiKey, model: S.selectedModel, providers: S.providerConfigs, text_capable: capText, image_capable: capImage, image_out_capable: capImageOut });
    }
    const dupe = S.providers.find(p => p.provider === provider && p.model === S.selectedModel && p.base_url === baseUrl);
    if (!dupe) {
      const newP = { provider, base_url: baseUrl, api_key: apiKey, model: S.selectedModel, enabled: true, text_capable: capText, image_capable: capImage, use_for_image: capImage, image_out_capable: capImageOut, use_for_image_out: capImageOut, high_effort_capable: false, _uid: ++S.uid };
      S.providers.push(newP);
      await adapter.saveRoster({ providers: S.providers });
      // Append ONLY the new row — no full rebuild/reload, so the list (and scroll)
      // stays put. If this add became the default (becomesDefault above), its
      // radio is checked below; clear the others to keep the single-selection
      // invariant. First model ever? There's no header yet — do a one-time full render.
      const cfgAnchor = table.querySelector('.ac-model-cfg-row');
      if (table.querySelector('.ac-saved-head')) {
        // Only a text/brain model can become the default; clear the other radios
        // just in that case so a new image-only worker can't blank the default.
        if (capText) { table.querySelectorAll(`input[name="${defaultRadioName}"]`).forEach(rb => { rb.checked = false; }); S.defaultUid = newP._uid; }
        _buildSavedRow(newP, false, !!capText, cfgAnchor);
        table.appendChild(advRow);            // keep the advanced/+ row pinned last
      } else {
        renderSaved();
      }
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
    try {
      const [presets, cfg] = await Promise.all([
        Promise.resolve(fetchPresets()).catch(() => ({})),
        Promise.resolve(adapter.loadConfig()).catch((e) => { console.warn('model-table: load failed', e); return {}; }),
      ]);
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
      S.selCaps = { text: cfg.text_capable !== false, image: !!cfg.image_capable, imageOut: !!cfg.image_out_capable };
      renderDetectedCaps(S.selectedModel ? { text_capable: S.selCaps.text, image_capable: S.selCaps.image, image_out_capable: S.selCaps.imageOut } : null);
      setModelStatus(S.selectedModel ? `Selected: ${S.selectedModel}` : '', true);

      const norm = (p) => ({
        provider: p.provider || 'openrouter', base_url: p.base_url || '', api_key: p.api_key || '', model: p.model || '',
        enabled: p.enabled !== false,
        text_capable: p.text_capable !== false, image_capable: !!p.image_capable, use_for_image: !!p.use_for_image,
        image_out_capable: !!p.image_out_capable, use_for_image_out: !!p.use_for_image_out,
        high_effort_capable: !!p.high_effort_capable, _uid: ++S.uid,
        // Inherited-row extras (undefined on the agent's own rows): the admin
        // ceiling that gates which capability boxes are editable, and the stable
        // key the per-agent opt-out is stored under. Preserved verbatim.
        _ceiling: p._ceiling, _ovrKey: p._ovrKey,
      });
      S.inherited = (Array.isArray(cfg.inherited) ? cfg.inherited : []).map(norm);
      S.providers = (Array.isArray(cfg.roster) ? cfg.roster : []).map(norm);
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
    docListeners.forEach(([t, fn]) => document.removeEventListener(t, fn));
    host.innerHTML = '';
  }

  reload();
  return { reload, destroy, getState: () => ({ ...S }) };
}
