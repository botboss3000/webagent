'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'
// or el.style.color = 'var(--accent)'. New colour? Add a token to the palette there first.

/**
 * Agents — Config tab.
 *
 * LAYOUT: this tab mirrors the App Settings "standard config-page layout" (see
 * docs/claude/ui-guidance.md → "The standard config-page layout" + "Toggle-list").
 * Every section is a title-only `.ac-category-group` (an accent Lucide icon + a
 * title, no explainer paragraph) over ONE flush `.ac-list` table of rows — option
 * settings (toggle / dropdown / number) sit inline on the right of a row in the
 * `.ac-config-control` slot; a multi-field configurator (External Data Sources)
 * lives in a row that expands in place. The few genuine free-text fields
 * (Name / Description / Chat Messages) stay as fields (NOT rows — per the
 * "when NOT to use rows" rule) under the same titled headings.
 *
 * SAVES: every control confirms with the ONE shared on-top save overlay — a
 * spinner over the touched control → green ✓, or an orange ⚠ that reverts — the
 * same `_markSaving`/`_flashSaveCheck` (imported here ALIASED as `_ovMarkSaving`/
 * `_ovFlashCheck` — see the naming-clash note on the import below) the ability
 * tables + model table use. Saves are SILENT (`_putAgentField {silent:true}`) so a
 * single edit no longer tears down/re-mounts the open card under the user (the
 * model table already did this). Local caches still update inside `_putAgentField`.
 */

import { app } from '../../../shared/js/state.js';
import { icon } from '../../../shared/js/icons.js';
import { authHeaders, isAnonGuest } from '../../../shared/js/left-login.js';
import { advanceDeleteBtn, resetDeleteBtn } from '../../../shared/js/delete-control.js';
import { NODE_PANEL_INFO } from '../agent-loop/js/loop-node-data.js';
import {
  _agents, _isMockAgent, MOCK_AGENT_ID,
  _userIsAdmin, _extendLlmToAgents,
  _binView, _clearExpanded,
} from './state.js';
import {
  _esc, _btn,
  _debounced, _putAgentField, _triggerKeyPlaceholder,
  _renderAgentIcon, _iconColor,
} from './utils.js';
import { _mockRandomIcon, _postNewAgent } from './mock-agent.js';
import { _openIconPicker } from './icon-picker.js';
import { _loadAgents } from './data.js';
import { mountModelTable } from '../../../shared/js/model-table.js';
import { apiPath } from '../../../shared/js/config.js';
// NOTE — naming clash: `_markSaving` exists in BOTH ./utils.js (clears the legacy
// inline `.agents-autosave-check` tick) AND shared/js/dom-utils.js (the on-top
// `.ac-save-overlay`). This tab uses the OVERLAY one, imported aliased so it can't
// collide with the utils version; the legacy inline-tick path is no longer used here.
import { _wrapNumberStepper, _markSaving as _ovMarkSaving, _flashSaveCheck as _ovFlashCheck } from '../../../shared/js/dom-utils.js';
import { mount as mountDataSources } from '../../../shared/js/data-sources.js';
import { renderAgentIdentitySettings } from './identity-settings.js';

// ── Shared layout helpers (App-Settings standard config-page layout) ──────────

/** A title-only section: accent Lucide icon + title over a `.ac-category-body`.
 *  Returns the body element to fill (with an `.ac-list` and/or fields).
 *  When ``info`` is given, an info (help-circle) button appears after the title
 *  that opens a popover explaining the section. */
function _group(body, iconName, title, info) {
  const group = document.createElement('div');
  group.className = 'ac-category-group';
  const summary = document.createElement('div');
  summary.className = 'ac-category-summary';
  summary.style.cursor = 'default';   // these headings aren't collapsible
  summary.innerHTML =
    `<i data-lucide="${iconName}" class="lucide-icon" style="width:16px;height:16px;color:var(--accent);"></i>`;
  // Title + (optional info button) live in one left-anchored cluster so the
  // button hugs the heading text. A bare .ac-category-title has flex:1, which
  // would shove a sibling button to the far right edge of the row.
  const titleCluster = document.createElement('span');
  titleCluster.style.cssText = 'display:inline-flex;align-items:center;gap:6px;flex:1;min-width:0;';
  const titleEl = document.createElement('span');
  titleEl.className = 'ac-category-title';
  titleEl.style.flex = '0 0 auto';
  titleEl.textContent = title;
  titleCluster.appendChild(titleEl);
  summary.appendChild(titleCluster);
  if (info) {
    const infoBtn = document.createElement('button');
    infoBtn.type = 'button';
    infoBtn.setAttribute('aria-label', `About ${title}`);
    infoBtn.title = 'What is this?';
    infoBtn.style.cssText = 'background:none;border:none;cursor:pointer;color:var(--fg-2);padding:2px;display:inline-flex;align-items:center;border-radius:4px;transition:color .15s;';
    infoBtn.innerHTML = icon('help-circle', { size: '14px' });
    infoBtn.addEventListener('mouseenter', () => { infoBtn.style.color = 'var(--accent)'; });
    infoBtn.addEventListener('mouseleave', () => { infoBtn.style.color = 'var(--fg-2)'; });
    infoBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      _acInfoPopover(infoBtn, title, info);
    });
    titleCluster.appendChild(infoBtn);
  }
  const gbody = document.createElement('div');
  gbody.className = 'ac-category-body';
  group.appendChild(summary);
  group.appendChild(gbody);
  body.appendChild(group);
  return gbody;
}

/** Top-level Agent Config topic. These are intentionally broader than `_group`:
 *  a topic owns every related sub-group, so the page reads as a short, ordered
 *  outline instead of a long run of peer headings. The open state is retained
 *  per agent for the life of the browser session. */
function _topic(body, agent, iconName, title, { open = false } = {}) {
  const key = `webagent.agent-config.${agent.id}.${title.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;
  let expanded = open;
  try {
    const saved = sessionStorage.getItem(key);
    if (saved !== null) expanded = saved === '1';
  } catch (_) { /* storage is optional */ }

  const topic = document.createElement('section');
  topic.className = 'agent-config-topic';
  if (expanded) topic.classList.add('is-open');

  const heading = document.createElement('button');
  heading.type = 'button';
  heading.className = 'agent-config-topic-heading';
  heading.setAttribute('aria-expanded', String(expanded));
  heading.innerHTML =
    `<span class="agent-config-topic-chevron" aria-hidden="true">${icon('chevron-right', { size: '16px' })}</span>` +
    `<span class="agent-config-topic-icon" aria-hidden="true">${icon(iconName, { size: '17px' })}</span>` +
    `<span class="agent-config-topic-title"></span>`;
  heading.querySelector('.agent-config-topic-title').textContent = title;

  const content = document.createElement('div');
  content.className = 'agent-config-topic-content';
  content.hidden = !expanded;

  heading.addEventListener('click', () => {
    expanded = !expanded;
    topic.classList.toggle('is-open', expanded);
    heading.setAttribute('aria-expanded', String(expanded));
    content.hidden = !expanded;
    try { sessionStorage.setItem(key, expanded ? '1' : '0'); } catch (_) { /* optional */ }
  });

  topic.append(heading, content);
  body.appendChild(topic);
  return content;
}

/** A flush option-row table inside a group body. */
function _cfgList(gbody) {
  const list = document.createElement('div');
  list.className = 'ac-list ac-config-list';
  gbody.appendChild(list);
  return list;
}

/** One option row: bold name + muted description on the left, an empty
 *  `.ac-config-control` slot on the right for the caller to fill (input / toggle
 *  / select). Returns the row, the control slot, and the description element. */
function _cfgRow(list, name, desc) {
  const row = document.createElement('div'); row.className = 'ac-ability-row';
  const label = document.createElement('span'); label.className = 'ac-ability-label';
  const nameEl = document.createElement('span'); nameEl.className = 'ac-ability-name';
  nameEl.textContent = name; label.appendChild(nameEl);
  let descEl = null;
  if (desc) {
    descEl = document.createElement('span'); descEl.className = 'ac-ability-desc';
    descEl.textContent = desc; label.appendChild(descEl);
  }
  const ctrl = document.createElement('span'); ctrl.className = 'ac-config-control';
  row.appendChild(label); row.appendChild(ctrl);
  list.appendChild(row);
  return { row, ctrl, descEl };
}

/** A genuine free-text field (NOT a row): a `.ac-label` (with an inline save-
 *  indicator slot) over a full-width `.ac-input` input/textarea. Wires debounced
 *  autosave through the on-top overlay (placed over the small indicator slot so it
 *  never covers the text). `opts.onSave(value)` must return a Promise<boolean>. */
function _cfgField(gbody, { label, field, value, multiline, rows, placeholder, hint, readonly, onSave }) {
  const wrap = document.createElement('div'); wrap.className = 'ac-cfg-field';
  const lbl = document.createElement('label'); lbl.className = 'ac-label';
  lbl.textContent = label;
  const ind = document.createElement('span'); ind.className = 'ac-cfg-ind';
  lbl.appendChild(ind);
  wrap.appendChild(lbl);
  let el;
  if (multiline) { el = document.createElement('textarea'); el.rows = rows || 2; }
  else { el = document.createElement('input'); el.type = 'text'; }
  el.className = 'ac-input';
  if (field) el.dataset.field = field;
  el.value = value || '';
  if (placeholder) el.placeholder = placeholder;
  if (readonly) el.readOnly = true;
  wrap.appendChild(el);
  if (hint) { const h = document.createElement('div'); h.className = 'ac-hint'; h.textContent = hint; wrap.appendChild(h); }
  gbody.appendChild(wrap);
  if (onSave && !readonly) {
    const save = _debounced(async () => {
      _ovMarkSaving(ind);
      let ok = false;
      try { ok = await onSave(el.value); } catch (_) { ok = false; }
      _ovFlashCheck(ind, !!ok, ok ? '' : 'Save failed');
    });
    el.addEventListener('input', save);
    el.addEventListener('blur', () => save.flush());
  }
  return { wrap, el, ind };
}

/** A small themed popover anchored to a control. ``body`` is a plain string
 *  (split into paragraphs) or a DOM node (e.g. a <pre>). One at a time;
 *  dismiss on close, outside click, or Escape. Theming uses design-system
 *  tokens only (dark/light safe). */
function _acInfoPopover(anchorEl, title, body) {
  const old = document.getElementById('ac-info-popover');
  if (old) old.remove();

  const pop = document.createElement('div');
  pop.id = 'ac-info-popover';
  pop.style.cssText = [
    'position:fixed', 'z-index:9999',
    'max-width:360px', 'min-width:250px',
    'background:var(--bg-elev-2, var(--bg-1))',
    'border:var(--border-width) solid var(--border-strong)',
    'border-radius:10px',
    'box-shadow:var(--shadow-float)',
    'padding:12px 14px',
    'font-size:12.5px', 'line-height:1.55',
    'color:var(--fg-1)',
  ].join(';');

  const head = document.createElement('div');
  head.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px;';
  const h = document.createElement('div');
  h.style.cssText = 'font-weight:600;font-size:12.5px;color:var(--accent);';
  h.textContent = title;
  const close = document.createElement('button');
  close.type = 'button';
  close.setAttribute('aria-label', 'Close');
  close.title = 'Close';
  close.style.cssText = 'background:none;border:none;cursor:pointer;color:var(--fg-2);padding:2px;display:inline-flex;align-items:center;border-radius:4px;';
  close.innerHTML = icon('x', { size: '14px' });
  head.appendChild(h);
  head.appendChild(close);
  pop.appendChild(head);

  if (typeof body === 'string') {
    body.split(/\n+/).forEach(para => {
      const t = para.trim();
      if (!t) return;
      const p = document.createElement('div');
      p.style.cssText = 'color:var(--fg-2);margin-bottom:8px;white-space:pre-wrap;';
      p.textContent = t;
      pop.appendChild(p);
    });
  } else {
    pop.appendChild(body);
  }

  document.body.appendChild(pop);

  const r = anchorEl.getBoundingClientRect();
  let top = r.bottom + 8;
  let left = r.left;
  const pw = pop.offsetWidth || 300;
  const ph = pop.offsetHeight || 60;
  if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 8);
  left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
  pop.style.top = top + 'px';
  pop.style.left = left + 'px';

  close.addEventListener('click', () => pop.remove());

  const cleanup = () => {
    document.removeEventListener('mousedown', outside, true);
    document.removeEventListener('keydown', onKey, true);
  };
  const outside = (ev) => {
    const cur = document.getElementById('ac-info-popover');
    if (!cur) { cleanup(); return; }
    if (!cur.contains(ev.target) && ev.target !== anchorEl) { cur.remove(); cleanup(); }
  };
  const onKey = (ev) => {
    if (ev.key !== 'Escape') return;
    const cur = document.getElementById('ac-info-popover');
    if (cur) { cur.remove(); cleanup(); }
  };
  setTimeout(() => {
    document.addEventListener('mousedown', outside, true);
    document.addEventListener('keydown', onKey, true);
  }, 0);
}

/** Fetch the GLOBAL closer template (app-prompts.json → app_level_prompts.output_closer,
 *  legacy keys output_summarizer / output_overviewer) and show it in a popover. */
function _showGlobalCloserPrompt(anchorEl) {
  (async () => {
    let tpl = '';
    try {
      const res = await fetch(apiPath('/api/v1/app-prompts'));
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const lp = (data && data.app_level_prompts) || {};
      const pick = (entry) => entry && (entry.template || entry.text);
      tpl = pick(lp.output_closer) || pick(lp.output_summarizer) || pick(lp.output_overviewer) || '';
      if (!tpl) tpl = '(No global closer template is configured.)';
    } catch (e) {
      tpl = 'Could not load the global closer prompt: ' + (e && e.message ? e.message : e);
    }
    const pre = document.createElement('pre');
    pre.style.cssText = 'white-space:pre-wrap;word-break:break-word;font-family:var(--font-mono,monospace);font-size:11.5px;line-height:1.5;color:var(--fg-2);max-height:280px;overflow:auto;margin:0;';
    pre.textContent = tpl;
    _acInfoPopover(anchorEl, 'Global closer prompt', pre);
  })();
}

/** Save a partial agent update with the on-top overlay over `ctrl` (the row's
 *  `.ac-config-control` slot). Silent so the open card isn't rebuilt on each edit;
 *  caches still update inside `_putAgentField`. Returns the ok boolean. */
async function _saveCfg(agent, updates, ctrl) {
  _ovMarkSaving(ctrl);
  const ok = await _putAgentField(agent, updates, null, { silent: true });
  _ovFlashCheck(ctrl, ok, ok ? '' : 'Save failed');
  return ok;
}

// Resting tooltip for the Config tab's danger-zone trash button. Reused by the
// fail-path reset (resetDeleteBtn) so the two references can't drift apart.
const TRASH_REST_TITLE = 'Move to the recycling bin';
// Resting accessible name (aria-label) for the same button — the more
// descriptive companion to TRASH_REST_TITLE. Set at creation in
// _renderConfigTab and passed to resetDeleteBtn on the fail path so the
// resting label and its reset can't drift apart either.
const TRASH_REST_LABEL = 'Move this agent to the recycling bin';

/** Soft-delete this agent (and, server-side, all of its sessions) to the
 *  recycling bin: DELETE the agent, then clear the open card, refresh the grid
 *  via the window callback registered in view.js, and show the bin notice.
 *  Mirrors the Claude card's in-panel delete flow (claude-agent.js). */
async function _trashAgent(btn, agent) {
  const name = agent.name || agent.id;
  // ── UX hardening: prevent double-submit while the delete is in flight ──
  // The two-click confirm already guards the trash button itself (advanceDeleteBtn
  // ignores clicks once BUSY), but the button stays visually enabled and any other
  // danger-zone button on the card could still fire. Disable the whole danger-zone
  // group for the duration of the request; re-enable it if the delete fails (on
  // success the card is torn down by _clearExpanded + __agentsReload anyway).
  const zone = btn && btn.closest('.ac-category-group');
  const setDangerEnabled = (enabled) => {
    if (btn) btn.disabled = !enabled;
    if (zone) zone.querySelectorAll('button').forEach(b => { b.disabled = !enabled; });
  };
  setDangerEnabled(false);
  // ── Failure notice: honest error feedback, card stays open for a retry ──
  // On any delete failure we re-enable the danger zone (above) and surface a
  // visible error toast in danger styling that says the agent could NOT be
  // moved. The card is deliberately left open (no _clearExpanded on this path)
  // so the user can re-arm the trash button and retry.
  const fail = (msg) => {
    if (btn) resetDeleteBtn(btn, { size: '14px', title: TRASH_REST_TITLE, label: TRASH_REST_LABEL });
    setDangerEnabled(true); // re-enable the danger zone so the user can retry
    if (typeof window.__agentsNotice === 'function') {
      window.__agentsNotice(`Agent "${name}" could NOT be moved to the recycling bin — ${msg}`, { kind: 'error' });
    }
  };
  try {
    const res = await fetch(`/api/v1/agents/${agent.id}?user_id=${encodeURIComponent(app.currentUserId)}`,
      { method: 'DELETE', headers: { ...authHeaders() } });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      fail(err.detail || `the server returned an error (HTTP ${res.status})`);
      return;
    }
  } catch (e) {
    fail(`network error — ${e.message}`);
    return;
  }
  try { if (typeof app.populateAgentSelect === 'function') await app.populateAgentSelect(app.currentUserId); } catch (_) { /* non-fatal */ }
  _clearExpanded();
  if (typeof window.__agentsReload === 'function') window.__agentsReload();
  if (typeof window.__agentsNotice === 'function') {
    window.__agentsNotice(`Agent "${name}" moved to the recycling bin — its sessions went with it.`, {
      action: { label: 'Open bin', fn: () => { if (typeof window.__agentsOpenBin === 'function') window.__agentsOpenBin(); } },
    });
  }
}

// ── Main render ───────────────────────────────────────────────────────────────

function _renderManagerLoopConfig(body, agent) {
  const d = {
    enabled:false, model:null, effort:null,
    budgets:{max_checks:9,max_blocks:3,by_trigger:{plan_gate:1,edit_gate:4,watchdog:3,commit_gate:1}},
    starter:{enabled:false,parallel:true,wait_before_write:false,inherit_prior_summary:true,seed_plan:true,seed_checklist:true,prompt:''},
    contracts:{enabled:false,engine:'subagent',failure_policy:'hybrid',scout:{enabled:true,timeout_seconds:60,model:null},edit_review:{enabled:true,policy:'blocking',timeout_seconds:60,max_checks:4,model:null},close_review:{enabled:true,policy:'blocking',timeout_seconds:120,max_rounds:1,model:null}},
    triggers:{plan_gate:{policy:'off',modes:['plan','auto'],prompt:''},edit_gate:{policy:'off',modes:['auto'],prompt:''},commit_gate:{policy:'off',modes:['auto'],prompt:''}},
    watchdog:{enabled:false,every_n_turns:0,on_errors:0,error_window:8,on_stall:true,cooldown_turns:2,action:'advise',modes:['ask','plan','auto'],prompt:''},
    closer:{audit_mode_contract:true,audit_agent_checklist:true,require_plan_document:true,require_manager_clear:false,max_rounds:1,send_back:true}, mode_overrides:{},
  };
  const src = agent.manager_loop && typeof agent.manager_loop === 'object' ? agent.manager_loop : {};
  const cfg = {
    ...structuredClone(d), ...structuredClone(src),
    budgets:{...d.budgets,...(src.budgets||{}),by_trigger:{...d.budgets.by_trigger,...((src.budgets||{}).by_trigger||{})}},
    starter:{...d.starter,...(src.starter||{})},
    contracts:{...d.contracts,...(src.contracts||{}),scout:{...d.contracts.scout,...((src.contracts||{}).scout||{})},edit_review:{...d.contracts.edit_review,...((src.contracts||{}).edit_review||{})},close_review:{...d.contracts.close_review,...((src.contracts||{}).close_review||{})}},
    triggers:Object.fromEntries(Object.keys(d.triggers).map(k=>[k,{...d.triggers[k],...((src.triggers||{})[k]||{})}])),
    watchdog:{...d.watchdog,...(src.watchdog||{})}, closer:{...d.closer,...(src.closer||{})}, mode_overrides:{...(src.mode_overrides||{})},
  };
  const g = _group(body, 'workflow', 'Manager loop',
    'A bounded supervisor around the main run. It can use one-shot checks or contract-bound orchestration workers for Scout, edit review, and independent close audits.');
  g.classList.add('ac-manager-loop'); const list = _cfgList(g);
  const save = _debounced(()=>{agent.manager_loop=structuredClone(cfg);_saveCfg(agent,{manager_loop:cfg},g);});
  const check=(host,label,value,set)=>{const w=document.createElement('label');w.className='ac-manager-check';const e=document.createElement('input');e.type='checkbox';e.checked=!!value;const s=document.createElement('span');s.textContent=label;w.append(e,s);host.appendChild(w);e.addEventListener('change',()=>{set(e.checked);save();});return e;};
  const number=(host,label,value,min,max,set)=>{const w=document.createElement('label');w.className='ac-manager-field ac-manager-field-number';const s=document.createElement('span');s.textContent=label;const e=document.createElement('input');e.type='number';e.className='ac-input';e.min=min;e.max=max;e.value=value;e.addEventListener('input',()=>{set(Math.max(min,Math.min(max,parseInt(e.value,10)||0)));save();});w.append(s,e);host.appendChild(w);};
  const text=(host,label,value,set,placeholder='')=>{const w=document.createElement('label');w.className='ac-manager-field';const s=document.createElement('span');s.textContent=label;const e=document.createElement('textarea');e.className='ac-input';e.rows=3;e.value=value||'';e.placeholder=placeholder;e.addEventListener('input',()=>{set(e.value);save();});w.append(s,e);host.appendChild(w);};
  const select=(host,label,value,opts,set)=>{const w=document.createElement('label');w.className='ac-manager-field ac-manager-field-number';const s=document.createElement('span');s.textContent=label;const e=document.createElement('select');e.className='ac-input';opts.forEach(([v,t])=>{const o=document.createElement('option');o.value=v;o.textContent=t;o.selected=v===value;e.appendChild(o);});e.addEventListener('change',()=>{set(e.value);save();});w.append(s,e);host.appendChild(w);};
  const stage=(name,hint)=>{const el=document.createElement('details');el.className='ac-manager-stage';const h=document.createElement('summary');const n=document.createElement('span');n.textContent=name;const i=document.createElement('span');i.textContent=hint;h.append(n,i);el.appendChild(h);const host=document.createElement('div');host.className='ac-manager-stage-body';el.appendChild(host);g.appendChild(el);return host;};
  const modes=(host,target)=>{const box=document.createElement('div');box.className='ac-manager-modes';const cap=document.createElement('span');cap.textContent='Applies in';box.appendChild(cap);const all=Array.isArray(agent.execution_modes)&&agent.execution_modes.length?agent.execution_modes:[{id:'ask',label:'Ask'},{id:'plan',label:'Plan'},{id:'auto',label:'Auto'}];all.forEach(m=>check(box,m.label||m.id,(target.modes||[]).includes(m.id),on=>{const set=new Set(target.modes||[]);on?set.add(m.id):set.delete(m.id);target.modes=[...set];}));host.appendChild(box);};

  const enabledRow=_cfgRow(list,'Enable Manager loop','Use quick one-shot second opinions around this agent\'s main run.');check(enabledRow.ctrl,'',cfg.enabled,v=>{cfg.enabled=v;});
  const presetRow=_cfgRow(list,'Managed build preset','Scout asynchronously, block valid edit objections, and require two independent close reviews. Infrastructure failures remain visible and fail open.');const preset=document.createElement('button');preset.type='button';preset.className='ac-btn ac-btn-secondary';preset.textContent='Apply managed build';preset.addEventListener('click',async()=>{if(preset.disabled)return;cfg.enabled=true;cfg.starter.enabled=true;cfg.starter.parallel=true;cfg.contracts.enabled=true;cfg.contracts.engine='subagent';cfg.contracts.failure_policy='hybrid';cfg.contracts.scout.enabled=true;cfg.contracts.edit_review.enabled=true;cfg.contracts.edit_review.policy='blocking';cfg.contracts.close_review.enabled=true;cfg.contracts.close_review.policy='blocking';cfg.contracts.close_review.max_rounds=1;agent.manager_loop=structuredClone(cfg);preset.disabled=true;preset.textContent='Applying…';const ok=await _saveCfg(agent,{manager_loop:cfg},g);const confirmed=ok&&!!(agent.manager_loop&&agent.manager_loop.contracts&&agent.manager_loop.contracts.enabled);preset.disabled=false;preset.textContent=confirmed?'Managed build applied':'Apply managed build';if(confirmed&&typeof window.__agentsNotice==='function')window.__agentsNotice('Managed build is enabled on the live agent.');});presetRow.ctrl.appendChild(preset);
  const preflightRow=_cfgRow(list,'Managed build preflight','All three runtime prerequisites must be available.');const preflight=document.createElement('div');preflight.className='ac-manager-preflight';preflight.textContent='Checking prerequisites…';preflightRow.ctrl.appendChild(preflight);(async()=>{let current=agent;try{const res=await fetch(`/api/v1/agents/${agent.id}?user_id=${encodeURIComponent(app.currentUserId)}`,{headers:authHeaders(),cache:'no-store'});if(res.ok){const data=await res.json();current=data.agent||agent;Object.assign(agent,current);}}catch(_){/* rendered below */}const p=current.manager_contract_preflight||{};const items=[['run_manager app function',p.run_manager],['manager_chk loop node',p.manager_chk],['Agent Orchestration ability',p.agent_orchestration]];preflight.replaceChildren();items.forEach(([label,ready])=>{const line=document.createElement('div');line.textContent=`${ready?'✓':'!'} ${label}`;line.className=ready?'ac-preflight-ok':'ac-preflight-missing';preflight.appendChild(line);});if(!p.ready){const link=document.createElement('a');link.href='/#instances/settings';link.textContent='Open admin settings';link.className='ac-link';preflight.appendChild(link);}if(agent.id==='shared_default'){const divergence=document.createElement('div');divergence.className='ac-manager-seed-state';divergence.textContent=current.shared_default_seed_diverged===false?'Live configuration matches the JSON seed.':current.shared_default_seed_diverged===true?'Live configuration differs from the JSON seed. Use “Push to JSON file” to update the seed.':'Seed comparison unavailable.';preflight.appendChild(divergence);}})();
  const modelRow=_cfgRow(list,'Manager model','Optional low-cost model override; blank uses the agent model.');const model=document.createElement('input');model.className='ac-input ac-input-sm';model.value=cfg.model||'';model.placeholder='Use agent model';model.addEventListener('input',()=>{cfg.model=model.value.trim()||null;save();});modelRow.ctrl.appendChild(model);
  const effortRow=_cfgRow(list,'Reasoning effort','Keep the second opinion small and fast.');const effort=document.createElement('select');effort.className='ac-input ac-input-sm ac-config-sel';[['','Default'],['low','Low'],['medium','Medium'],['high','High']].forEach(([v,t])=>{const o=document.createElement('option');o.value=v;o.textContent=t;o.selected=(cfg.effort||'')===v;effort.appendChild(o);});effort.addEventListener('change',()=>{cfg.effort=effort.value||null;save();});effortRow.ctrl.appendChild(effort);
  let h=stage('Budgets','Hard bounds keep the parallel judgments cheap');number(h,'Total checks',cfg.budgets.max_checks,0,50,v=>{cfg.budgets.max_checks=v;});number(h,'Blocking verdicts',cfg.budgets.max_blocks,0,20,v=>{cfg.budgets.max_blocks=v;});
  h=stage('Starter / Run Scout','Parallel intake, prior-run linking, and plan seeding');check(h,'Enable Starter',cfg.starter.enabled,v=>{cfg.starter.enabled=v;});check(h,'Run in parallel',cfg.starter.parallel,v=>{cfg.starter.parallel=v;});check(h,'Wait before first write',cfg.starter.wait_before_write,v=>{cfg.starter.wait_before_write=v;});check(h,'Use prior-run handoff summary',cfg.starter.inherit_prior_summary,v=>{cfg.starter.inherit_prior_summary=v;});check(h,'Seed persistent plan',cfg.starter.seed_plan,v=>{cfg.starter.seed_plan=v;});check(h,'Seed accumulated checklist',cfg.starter.seed_checklist,v=>{cfg.starter.seed_checklist=v;});text(h,'Prompt override',cfg.starter.prompt,v=>{cfg.starter.prompt=v;},'Blank uses the global Starter prompt.');
  h=stage('Subagent contracts','Persistent edit reviewer plus fresh, independent close auditors');check(h,'Enable contract workers',cfg.contracts.enabled,v=>{cfg.contracts.enabled=v;});select(h,'Infrastructure failure',cfg.contracts.failure_policy,[['hybrid','Hybrid — fail open visibly'],['fail_closed','Fail closed'],['advisory','Advisory']],v=>{cfg.contracts.failure_policy=v;});check(h,'Tool-free Scout worker',cfg.contracts.scout.enabled,v=>{cfg.contracts.scout.enabled=v;});number(h,'Scout timeout (seconds)',cfg.contracts.scout.timeout_seconds,1,600,v=>{cfg.contracts.scout.timeout_seconds=v;});check(h,'Persistent read-only edit reviewer',cfg.contracts.edit_review.enabled,v=>{cfg.contracts.edit_review.enabled=v;});select(h,'Edit policy',cfg.contracts.edit_review.policy,[['blocking','Blocking'],['async','Advisory'],['off','Off']],v=>{cfg.contracts.edit_review.policy=v;});number(h,'Edit checks per turn',cfg.contracts.edit_review.max_checks,1,100,v=>{cfg.contracts.edit_review.max_checks=v;});number(h,'Edit timeout (seconds)',cfg.contracts.edit_review.timeout_seconds,1,600,v=>{cfg.contracts.edit_review.timeout_seconds=v;});check(h,'Fresh alignment and evidence auditors',cfg.contracts.close_review.enabled,v=>{cfg.contracts.close_review.enabled=v;});select(h,'Close policy',cfg.contracts.close_review.policy,[['blocking','Blocking'],['async','Advisory'],['off','Off']],v=>{cfg.contracts.close_review.policy=v;});number(h,'Close correction rounds',cfg.contracts.close_review.max_rounds,0,5,v=>{cfg.contracts.close_review.max_rounds=v;});number(h,'Close timeout (seconds)',cfg.contracts.close_review.timeout_seconds,1,600,v=>{cfg.contracts.close_review.timeout_seconds=v;});
  const labels={plan_gate:'Plan gate',edit_gate:'Edit gate',commit_gate:'Commit gate'};
  Object.keys(labels).forEach(k=>{const t=cfg.triggers[k];const host=stage(labels[k],'Policy, applicable modes, budget, and prompt');select(host,'Behavior',t.policy,[['off','Off'],['async','Advisory / parallel'],['blocking','Blocking gate']],v=>{t.policy=v;});modes(host,t);number(host,'Maximum checks',cfg.budgets.by_trigger[k],0,20,v=>{cfg.budgets.by_trigger[k]=v;});text(host,'Prompt override',t.prompt,v=>{t.prompt=v;},'Blank uses the global trigger prompt.');});
  h=stage('Watchdog','Periodic, error-cluster, and stalled-progress reactions');check(h,'Enable Manager watchdog',cfg.watchdog.enabled,v=>{cfg.watchdog.enabled=v;});check(h,'React to stalls and loops',cfg.watchdog.on_stall,v=>{cfg.watchdog.on_stall=v;});number(h,'Every N turns (0 = off)',cfg.watchdog.every_n_turns,0,100,v=>{cfg.watchdog.every_n_turns=v;});number(h,'Error threshold (0 = off)',cfg.watchdog.on_errors,0,20,v=>{cfg.watchdog.on_errors=v;});number(h,'Error window',cfg.watchdog.error_window,1,100,v=>{cfg.watchdog.error_window=v;});number(h,'Cooldown turns',cfg.watchdog.cooldown_turns,0,100,v=>{cfg.watchdog.cooldown_turns=v;});number(h,'Maximum checks',cfg.budgets.by_trigger.watchdog,0,20,v=>{cfg.budgets.by_trigger.watchdog=v;});select(h,'Reaction',cfg.watchdog.action,[['advise','Advise'],['replan','Require replan'],['verify','Require verification'],['pause','Pause and ask'],['terminate','Terminate']],v=>{cfg.watchdog.action=v;});modes(h,cfg.watchdog);text(h,'Prompt override',cfg.watchdog.prompt,v=>{cfg.watchdog.prompt=v;},'Blank uses the global Watchdog prompt.');
  h=stage('Closer integration','Final handoff, contract audit, and bounded send-back');check(h,'Audit accumulated mode contracts',cfg.closer.audit_mode_contract,v=>{cfg.closer.audit_mode_contract=v;});check(h,'Audit the agent checklist below',cfg.closer.audit_agent_checklist,v=>{cfg.closer.audit_agent_checklist=v;});check(h,'Require persistent Plan document',cfg.closer.require_plan_document,v=>{cfg.closer.require_plan_document=v;});check(h,'Require blocking Manager feedback cleared',cfg.closer.require_manager_clear,v=>{cfg.closer.require_manager_clear=v;});check(h,'Send incomplete work back under the same mode',cfg.closer.send_back,v=>{cfg.closer.send_back=v;});number(h,'Maximum fix rounds',cfg.closer.max_rounds,0,5,v=>{cfg.closer.max_rounds=v;});
}

export function _renderConfigTab(body, agent, panelEl, _renderList) {
  const isEditable = (
    agent.source === 'custom' && agent.id !== 'shared_default'
  ) || (agent.id === 'shared_default' && _userIsAdmin);
  const isMock = _isMockAgent(agent);
  const isAnonymousDraft = isMock && isAnonGuest();

  // Broad parent topics turn the page into a short, ordered outline. Every
  // narrower configurator below mounts into one of these semantic buckets.
  body.classList.add('agent-config-topics');
  const identityTopic = _topic(body, agent, 'badge', 'Identity', { open: true });
  const modelTopic = _topic(body, agent, 'cpu', 'Model');
  const deployTopic = _topic(body, agent, 'share-2', 'Deploy & Share');
  const modesTopic = _topic(body, agent, 'messages-square', 'Modes');
  const managerTopic = _topic(body, agent, 'workflow', 'Manager Loop');
  const runtimeTopic = _topic(body, agent, 'sliders-horizontal', 'Runtime');
  const dataTopic = _topic(body, agent, 'database', 'Data & Memory');

  // New-agent drafts and persisted agents mount the same Identity renderer.
  // Keep the canonical renderer call identical for both paths; the topic body
  // identifies itself and lets the shared renderer omit its duplicate heading.
  {
    const body = identityTopic;
    renderAgentIdentitySettings(body, agent);
  }

  // ── Danger zone: delete this agent (soft-delete to the recycling bin) ──────
  // Only persisted custom agents can be trashed — system agents (template rows)
  // are protected server-side, and the mock/new-agent draft isn't persisted.
  // Hidden in the bin view (a trashed agent's card still opens when expanded).
  if (isEditable && agent.id !== 'shared_default' && !isMock && !_binView) {
    const g = _group(identityTopic, 'trash-2', 'Danger zone');
    // _group returns the .ac-category-body; the .ac-category-summary (with its
    // icon) is a SIBLING of it, so reach the icon via the parent group element.
    const gIcon = g.parentElement && g.parentElement.querySelector('.ac-category-summary i');
    if (gIcon) gIcon.style.color = 'var(--danger)';
    const list = _cfgList(g);
    const { ctrl } = _cfgRow(list, 'Delete agent',
      'Move this agent and all of its sessions to the recycling bin. Nothing is erased — you can restore them from the bin.');
    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'agents-config-delete-btn';
    delBtn.title = TRASH_REST_TITLE;
    delBtn.setAttribute('aria-label', TRASH_REST_LABEL);
    delBtn.innerHTML = icon('trash-2', { size: '14px' });
    delBtn.addEventListener('click', () => {
      advanceDeleteBtn(delBtn, {
        size: '14px', spinSize: '14px',
        armTitle: 'Click again to move to the recycling bin',
        armLabel: 'Click again to move this agent to the recycling bin',
        busyTitle: 'Moving to the recycling bin\u2026',
        busyLabel: 'Moving this agent to the recycling bin\u2026',
        onConfirm: () => _trashAgent(delBtn, agent),
      });
    });
    ctrl.appendChild(delBtn);
  }

  // Manager Loop is a bounded collection of cheap, one-shot second opinions.
  // It shares one normalized contract with Starter, trigger gates, Watchdog,
  // modes, and Closer; raw tool output is deliberately not included.
  if (isEditable) {
    _renderManagerLoopConfig(managerTopic, agent);
  }

  // Stored inside the existing per-agent chat_ui override so the chat shell
  // resolves these through the same global + agent profile merge as its other
  // chrome. An empty array is intentional: this agent shows no starters.
  if (isEditable) {
    const starters = agent.chat_ui?.chat_common?.suggestion_prompts;
    const g = _group(modesTopic, 'sparkles', 'Starter prompts',
      'Optional prompts shown above the composer when this agent opens a brand-new, empty session. Put one prompt on each line. Leave the field empty to show none.');
    _cfgField(g, {
      label: 'Suggested first messages',
      field: 'suggestion_prompts',
      value: Array.isArray(starters) ? starters.join('\n') : '',
      multiline: true,
      rows: 4,
      placeholder: 'Summarize what I should focus on today\nHelp me plan a new project',
      hint: 'One prompt per line. These appear only before the first real message.',
      onSave: async (value) => {
        const suggestionPrompts = value.split('\n').map(line => line.trim()).filter(Boolean);
        const ok = await _putAgentField(agent, {
          chat_ui: { chat_common: { suggestion_prompts: suggestionPrompts } },
        }, null, { silent: true });
        if (ok && app.currentAgentId === agent.id && typeof app.refreshSuggestions === 'function') {
          try { app.refreshSuggestions(); } catch (_) { /* chat may not be mounted */ }
        }
        return ok;
      },
    });
  }

  // ── Closer prompt & checklist (per-agent closer config) ────────────────────
  // metadata['closer_prompt']    — per-agent closer template (blank = global)
  // metadata['audit_checklist']  — {checklist, max_rounds, send_back}
  // The closer summarizes the run into the user-facing 'Closer' bubble AND
  // audits the work against the checklist; on a miss it sends the agent back
  // (bounded by max_rounds) unless send-back is off.
  if (isEditable) {
    // ── Closer prompt ──
    const g1 = _group(managerTopic, 'message-square-text', 'Closer prompt',
      'This is the Closer\'s voice — the message that appears in your chat as the "Closer" bubble after an agent finishes a run.\n\n'
      + 'It takes everything the agent said and rewrites it into ONE concise, polished summary for you, the user. The closer also quietly audits the work against the checklist and reports the result.\n\n'
      + 'Leave this blank to use the app\'s global closer template. Placeholders: {user_request}, {assistant_messages}, {run_transcript}, {audit_results}.');

    _cfgField(g1, {
      label: 'Closer prompt',
      field: 'closer_prompt',
      value: agent.closer_prompt || '',
      multiline: true, rows: 4,
      placeholder: 'Leave blank to use the global closer template.',
      hint: 'Per-agent closer voice. Placeholders: {user_request}, {assistant_messages}, {run_transcript}, {audit_results}. Blank = global template.',
      onSave: (val) => _putAgentField(agent, { closer_prompt: val }, null, { silent: true }),
    });

    // Link to peek at the global prompt (opens a popover with the template).
    const globalLink = document.createElement('button');
    globalLink.type = 'button';
    globalLink.className = 'ac-closer-global-link';
    globalLink.style.cssText = 'background:none;border:none;color:var(--accent);cursor:pointer;font-size:12px;padding:4px 0 2px;text-decoration:underline;';
    globalLink.textContent = 'View global prompt';
    globalLink.addEventListener('click', () => _showGlobalCloserPrompt(globalLink));
    g1.appendChild(globalLink);

    // ── Checklist (contract questions) ──
    const g2 = _group(managerTopic, 'clipboard-check', 'Closer checklist',
      'The checklist is the quality bar the closer holds the agent\'s finished work to.\n\n'
      + 'After a run completes, the closer reads the conversation (your messages and the agent\'s responses — not the raw tool logs) and checks each item one by one. Every item must show evidence it was actually done.\n\n'
      + 'All items pass → the closer writes a clean summary. Any item missing → the closer sends the agent back with feedback to finish it. "Max fix rounds" caps how many times that can happen (0 = never send back). When the rounds run out, the summary honestly flags what is still missing.\n\n'
      + 'Tip: write items that can be judged from what the agent says it did, e.g. "The change was verified — tests run or the edited file re-read."');

    const ac = agent.audit_checklist;
    const managerCloser = agent.manager_loop && agent.manager_loop.closer && typeof agent.manager_loop.closer === 'object'
      ? agent.manager_loop.closer : null;
    const startItems = [];
    let roundsVal = 1;
    let sbVal = true;
    if (typeof ac === 'string') startItems.push(...ac.split('\n').map(s => s.trim()).filter(Boolean));
    else if (Array.isArray(ac)) startItems.push(...ac.map(s => String(s).trim()).filter(Boolean));
    else if (ac && typeof ac === 'object') {
      const cl = ac.checklist;
      if (Array.isArray(cl)) startItems.push(...cl.map(s => String(s).trim()).filter(Boolean));
      else if (typeof cl === 'string') startItems.push(...cl.split('\n').map(s => s.trim()).filter(Boolean));
      if (typeof ac.max_rounds === 'number') roundsVal = ac.max_rounds;
      if (typeof ac.send_back === 'boolean') sbVal = ac.send_back;
    }
    if (managerCloser && typeof managerCloser.max_rounds === 'number') roundsVal = managerCloser.max_rounds;
    if (managerCloser && typeof managerCloser.send_back === 'boolean') sbVal = managerCloser.send_back;

    const chkWrap = document.createElement('div');
    chkWrap.style.cssText = 'display:flex;flex-direction:column;gap:6px;';
    g2.appendChild(chkWrap);

    const collectItems = () => Array.from(chkWrap.querySelectorAll('input.ac-closer-item-input'))
      .map(i => i.value.trim()).filter(Boolean);

    const saveChecklist = () => {
      const vals = collectItems();
      const maxRounds = parseInt(roundsEl.value, 10) || 0;
      const nextManager = structuredClone(agent.manager_loop || {});
      nextManager.closer = { ...(nextManager.closer || {}), max_rounds: maxRounds, send_back: cb.checked };
      agent.manager_loop = nextManager;
      _putAgentField(agent, {
        audit_checklist: vals.length ? { checklist: vals, max_rounds: maxRounds, send_back: cb.checked } : '',
        manager_loop: nextManager,
      }, null, { silent: true });
    };
    const saveChecklistDeb = _debounced(saveChecklist);

    // The one blank "add" row that always lives at the bottom.
    const blankRow = document.createElement('div');
    blankRow.style.cssText = 'display:flex;gap:6px;align-items:center;';
    const blankInp = document.createElement('input');
    blankInp.type = 'text';
    blankInp.className = 'ac-input ac-closer-item-input';
    blankInp.placeholder = 'Add a checklist item';
    const addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.setAttribute('aria-label', 'Add checklist item');
    addBtn.title = 'Add item';
    addBtn.style.cssText = 'flex:none;display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:6px;background:none;border:var(--border-width) solid var(--border);color:var(--fg-2);cursor:pointer;';
    addBtn.innerHTML = icon('plus', { size: '14px' });
    blankRow.appendChild(blankInp);
    blankRow.appendChild(addBtn);
    chkWrap.appendChild(blankRow);

    // One row per existing item: input + minus (delete) button.
    const addItemRow = (value) => {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;gap:6px;align-items:center;';
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.className = 'ac-input ac-closer-item-input';
      inp.value = value;
      inp.addEventListener('input', saveChecklistDeb);
      const del = document.createElement('button');
      del.type = 'button';
      del.setAttribute('aria-label', 'Delete checklist item');
      del.title = 'Delete item';
      del.style.cssText = 'flex:none;display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:6px;background:none;border:var(--border-width) solid var(--border);color:var(--fg-2);cursor:pointer;';
      del.innerHTML = icon('minus', { size: '14px' });
      del.addEventListener('click', () => { row.remove(); saveChecklist(); });
      row.appendChild(inp);
      row.appendChild(del);
      chkWrap.insertBefore(row, blankRow);
      return row;
    };
    startItems.forEach(v => addItemRow(v));

    const commitBlank = () => {
      const v = blankInp.value.trim();
      if (!v) return;
      addItemRow(v);
      blankInp.value = '';
      blankInp.focus();
      saveChecklist();
    };
    addBtn.addEventListener('click', commitBlank);
    blankInp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); commitBlank(); } });

    // Max fix rounds + send-back on failure.
    const list2 = _cfgList(g2);
    const roundsRow = _cfgRow(list2, 'Max fix rounds',
      'How many times a failed audit may send the agent back before the summary flags the missing items. 0 = never send back.');
    const roundsEl = document.createElement('input'); roundsEl.type = 'number';
    roundsEl.className = 'ac-input ac-input-sm ac-config-num';
    roundsEl.min = 0; roundsEl.max = 5; roundsEl.step = 1; roundsEl.value = roundsVal;
    roundsRow.ctrl.appendChild(_wrapNumberStepper(roundsEl));
    roundsEl.addEventListener('input', saveChecklist);

    const sbRow = _cfgRow(list2, 'Send back on failure',
      'When a checklist item is missing, re-run the agent with the feedback so it can fix it.');
    const cb = document.createElement('input'); cb.type = 'checkbox'; cb.className = 'conn-toggle';
    cb.checked = sbVal;
    sbRow.ctrl.appendChild(cb);
    cb.addEventListener('change', saveChecklist);
  }

  // Suggested replies config for user-impersonator
  if (agent.id === 'user-impersonator') {
    _renderSuggestionModeControl(modesTopic);
  }

  // ── Template badge (custom agent cloned from a template) ──────────────────
  // Shows which template this agent was created from, with separate buttons to
  // push changes back to the DB template row and to the JSON seed file.
  if (isEditable && !isMock && agent.template_id) {
    const g = _group(identityTopic, 'layout-template', 'Template');
    const list = _cfgList(g);

    // Badge row
    const badgeRow = document.createElement('div');
    badgeRow.className = 'ac-ability-row';
    badgeRow.style.cssText = 'align-items:center;gap:8px;padding:8px 12px;background:rgba(var(--brand-rgb,99,102,241),0.06);border-radius:6px;margin-bottom:8px;';
    const badgeLabel = document.createElement('span');
    badgeLabel.className = 'ac-ability-name';
    badgeLabel.textContent = '🧬 ' + (agent.template_origin || agent.template_id);
    const badgeTag = document.createElement('span');
    badgeTag.style.cssText = 'font-size:11px;padding:2px 8px;border-radius:4px;background:var(--accent);color:#fff;margin-left:8px;';
    badgeTag.textContent = 'Template-based';
    badgeLabel.appendChild(badgeTag);
    // Small pill showing whether the template has a JSON file
    const srcPill = document.createElement('span');
    srcPill.style.cssText = 'font-size:10px;padding:1px 6px;border-radius:3px;background:var(--muted,#e5e7eb);color:var(--muted-fg,#6b7280);margin-left:6px;';
    srcPill.textContent = agent.template_has_json ? '📄 JSON seed' : '🗄️ DB only';
    badgeLabel.appendChild(srcPill);
    badgeRow.appendChild(badgeLabel);
    list.appendChild(badgeRow);

    // Only admins can push
    if (_userIsAdmin) {
      // Button 1: Push to DB template (always available)
      const { ctrl } = _cfgRow(list, 'Push to database template',
        'Overwrite the "' + (agent.template_origin || agent.template_id) + '" template row with this agent\'s current config + prompts.');
      const btn = document.createElement('button');
      btn.className = 'ac-btn ac-btn-sm';
      btn.textContent = 'Push to DB template';
      btn.addEventListener('click', async () => {
        if (!confirm('Push this agent\'s config to the "' + (agent.template_origin || agent.template_id) + '" template?\n\nThis updates the DB row immediately.')) return;
        btn.disabled = true; btn.textContent = 'Pushing…';
        try {
          const res = await fetch(`/api/v1/agents/${agent.id}/push-to-template`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...authHeaders() },
            body: JSON.stringify({ user_id: app.currentUserId }),
          });
          if (res.ok) { btn.textContent = '✓ Pushed'; btn.style.background = 'var(--success,#22c55e)'; }
          else {
            const err = await res.json().catch(() => ({}));
            btn.textContent = '⚠ Failed'; alert('Push failed: ' + (err.detail || 'Unknown error'));
            btn.disabled = false;
            setTimeout(() => { btn.textContent = 'Push to DB template'; btn.style.background = ''; }, 2000);
          }
        } catch (e) {
          btn.textContent = '⚠ Error'; alert('Push error: ' + e.message);
          btn.disabled = false;
          setTimeout(() => { btn.textContent = 'Push to DB template'; btn.style.background = ''; }, 2000);
        }
      });
      ctrl.appendChild(btn);

      // Button 2: Push to JSON file (only if template has a seed file)
      if (agent.template_has_json) {
        const { ctrl: expCtrl } = _cfgRow(list, 'Push to JSON seed file',
          'Write the template back to app/defaults/agents/' + (agent.template_origin || agent.template_id) + '.json (git-tracked).');
        const expBtn = document.createElement('button');
        expBtn.className = 'ac-btn ac-btn-sm';
        expBtn.textContent = 'Push to JSON file';
        expBtn.addEventListener('click', async () => {
          if (!confirm('Export "' + (agent.template_origin || agent.template_id) + '" to app/defaults/agents/?')) return;
          expBtn.disabled = true; expBtn.textContent = 'Exporting…';
          try {
            const res = await fetch(`/admin/db/templates/${agent.template_id}/export-to-file`, {
              method: 'POST', headers: { ...authHeaders() },
            });
            if (res.ok) { expBtn.textContent = '✓ Exported'; expBtn.style.background = 'var(--success,#22c55e)'; }
            else {
              const err = await res.json().catch(() => ({}));
              expBtn.textContent = '⚠ Failed'; alert('Export failed: ' + (err.detail || 'Unknown error'));
              expBtn.disabled = false;
              setTimeout(() => { expBtn.textContent = 'Push to JSON file'; expBtn.style.background = ''; }, 2000);
            }
          } catch (e) {
            expBtn.textContent = '⚠ Error'; alert('Export error: ' + e.message);
            expBtn.disabled = false;
            setTimeout(() => { expBtn.textContent = 'Push to JSON file'; expBtn.style.background = ''; }, 2000);
          }
        });
        expCtrl.appendChild(expBtn);
      }
    }
  }

  // Local Claude Code agents never reach this tab — they get their own distinct,
  // tab-less card (renderClaudeSettings in ui/main-panel/agents/js/claude-agent.js,
  // diverted from _renderAgentCard in ui/main-panel/agents/js/view.js).

  // ── Template (mock / new-agent only) ──────────────────────────────────────
  if (isMock) {
    const g = _group(identityTopic, 'layout-template', 'Template');
    const list = _cfgList(g);
    const { ctrl } = _cfgRow(list, 'Template', 'Start this agent from a saved template.');
    const tplSelect = document.createElement('select');
    tplSelect.className = 'ac-input ac-config-sel';
    tplSelect.dataset.field = 'template';      // read by view.js _acceptWebAgentCreate
    tplSelect.innerHTML = '<option value="">— No template —</option>';
    ctrl.appendChild(tplSelect);

    (async () => {
      try {
        const url = `/api/v1/agents/templates?user_id=${encodeURIComponent(app.currentUserId)}&discoverable_only=true`;
        const res = await fetch(url);
        if (res.ok) {
          const data = await res.json();
          // The Claude / Terminal engines have their own segments in the create
          // card's type chooser — keep their templates out of the WebAgent dropdown.
          (data.templates || []).filter(t => t.engine !== 'claude_code' && t.engine !== 'terminal_chat').forEach(t => {
            const opt = document.createElement('option');
            opt.value = t.id;
            opt.textContent = t.name || t.id;
            if (t.id === 'default') opt.selected = true;
            tplSelect.appendChild(opt);
          });
        }
      } catch (e) { console.warn('agents: failed to load templates for mock', e); }
    })();
  }

  // ── Model (per-agent LLM — shared model-table component) ──────────────────
  // SISTER-PANEL: MODEL-TABLE — the SAME component as Admin → Agent Settings →
  // Models (ui/shared/js/model-table.js); keep the two mirrored. The table shows
  // the admin's app-default model(s) as read-only "Inherited" rows (when "Extend
  // default LLM to agents" is on) PLUS this agent's own models (editable, stored
  // in agent.llm_config.multi_providers). Inherited rows are pure references:
  // taking a role with an own model just sinks them in the sort — no per-agent
  // override copy is ever created. When extend is off, none are shown — the
  // agent must add its own. The chat user can switch the model per conversation
  // (resolved server-side in app/admin/settings.py).
  if (isEditable && agent.engine !== 'claude_code' && agent.engine !== 'terminal_chat') {
    const llmCfg = agent.llm_config || { use_default: true };

    const g = document.createElement('div');
    modelTopic.appendChild(g);
    const host = document.createElement('div');
    host.style.marginTop = '8px';
    g.appendChild(host);

    // Local mirror of the agent's llm_config blob; persisted whole via the agent
    // PUT (the backend reads metadata['llm_config'] = {use_default, provider,
    // base_url, api_key, model, multi_providers}).
    const agentCfg = { ...llmCfg };
    function persistLlm() {
      // silent: the model table renders every change in place, so a card rebuild
      // here would tear the panel down and re-mount it under the user (losing
      // scroll / expanded rows). Caches still update inside _putAgentField.
      return _putAgentField(agent, { llm_config: { ...agentCfg } }, null, { silent: true })
        .then(ok => { if (!ok) throw new Error('Save failed'); });
    }
    // The admin's app-wide default model id (the brain the app runs by default).
    // Captured by fetchInherited and used to pre-select the right Default radio when
    // this agent hasn't pinned its own default — so the table reflects the model the
    // agent actually runs, instead of showing no selection.
    let _appDefaultModel = '';
    // The admin's app-default models — shown as inherited "Default" rows when
    // "Extend default LLM to agents" is on.
    async function fetchInherited() {
      if (!_extendLlmToAgents) return [];
      try {
        // Send the admin's bearer token so the backend resolves the app-wide
        // (admin) roster — NOT the anonymous user's. Without it these calls
        // resolve to __anonymous__, so the inherited list shows the wrong models
        // (e.g. app-default models silently dropped). Mirrors the admin Agent
        // Settings panel, which fetches the same endpoints with the token.
        const headers = { ...authHeaders() };
        // ONE combined read (single server-side vault+DB resolve); fall back to the
        // legacy parallel pair if the bundle endpoint isn't available yet.
        let prov = {}, multi = {};
        try {
          const rb = await fetch('/admin/settings/provider-bundle', { headers });
          if (rb.ok) { const b = await rb.json(); prov = b.provider || {}; multi = { providers: b.roster || [] }; }
          else throw new Error('no-bundle');
        } catch (_) {
          const [p, m] = await Promise.all([
            fetch('/admin/settings/provider', { headers }).then(r => r.ok ? r.json() : {}).catch(() => ({})),
            fetch('/admin/settings/multi-providers', { headers }).then(r => r.ok ? r.json() : {}).catch(() => ({})),
          ]);
          prov = p || {}; multi = m || {};
        }
        let rows = Array.isArray(multi.providers) ? multi.providers.filter(x => x.model) : [];
        if (!rows.length && prov.model) {
          rows = [{ provider: prov.provider, base_url: prov.base_url, model: prov.model, enabled: true,
            text_capable: prov.text_capable !== false, image_capable: !!prov.image_capable, image_out_capable: !!prov.image_out_capable,
            high_effort_capable: !!prov.high_effort_capable }];
        }
        // The app's chosen default brain (top-level provider model), used to
        // pre-select the Default radio for an agent that inherits it.
        _appDefaultModel = (prov && prov.model) || '';
        // Inherited app-default models shown as read-only — admin settings
        // pass through verbatim.
        return rows.map(r => {
          const key = `${r.provider || ''}|${r.base_url || ''}|${r.model || ''}`;
          return {
            ...r,
            _ovrKey: key,
          };
        });
      } catch (_) { return []; }
    }
    // THIS user's per-model usage with THIS agent (cost + tokens) — scoped to the
    // (viewing user, agent) pair, NOT the agent's all-users total. Fetched once and
    // shared by every model row's USAGE column. Each call was priced at the model it
    // ran on (usage_events.cost_usd), so this is accurate across model switches.
    // See GET /admin/settings/agent-usage.
    let _agentUsagePromise = null;
    function _fetchAgentUsage() {
      if (!_agentUsagePromise) {
        const headers = { ...authHeaders() };
        _agentUsagePromise = fetch(`/admin/settings/agent-usage?agent_id=${encodeURIComponent(agent.id || '')}`, { headers })
          .then(r => r.ok ? r.json() : null).catch(() => null);
      }
      return _agentUsagePromise;
    }
    const adapter = {
      // Feed the model table's USAGE column. Both inherited (app-default) rows and
      // the agent's own pinned rows report the SAME scope: what THIS user has spent
      // running THIS agent on that model — pulled from the single per-(user, agent)
      // agent-usage breakdown. So every USAGE cell is the viewer's own spend with
      // this agent, never the agent's all-users total, the user's all-agents total,
      // or the app-wide total. (inherited is accepted but no longer changes scope.)
      fetchModelUsage: async ({ model }) => {
        const d = await _fetchAgentUsage();
        const u = d && d.by_model && d.by_model[model];
        if (!u) return { total_input_tokens: 0, total_output_tokens: 0, total_cost_usd: 0 };
        return {
          total_input_tokens: u.input || 0,
          total_output_tokens: u.output || 0,
          total_cost_usd: u.cost_usd || 0,
        };
      },
      loadConfig: async () => {
        // Migrated single-model agents store their model in the top-level
        // llm_config, not multi_providers — synthesize a one-row roster from it
        // so the existing model shows as an editable row.
        let roster = agentCfg.multi_providers || [];
        // Handle legacy format where multi_providers was stored as { providers: [...] }
        // instead of a plain array (pre-existing bug in saveRoster).
        if (!Array.isArray(roster) && roster && Array.isArray(roster.providers)) {
          roster = roster.providers;
        }
        if (!roster.length && agentCfg.model) {
          roster = [{
            provider: agentCfg.provider || 'custom', base_url: agentCfg.base_url || '',
            api_key: agentCfg.api_key || '', model: agentCfg.model, enabled: true,
            text_capable: agentCfg.text_capable !== false, image_capable: !!agentCfg.image_capable,
            use_for_image: !!agentCfg.image_capable, image_out_capable: !!agentCfg.image_out_capable,
            use_for_image_out: !!agentCfg.image_out_capable, high_effort_capable: !!agentCfg.high_effort_capable,
          }];
        }
        // Resolve inherited first (this also captures the app's default model).
        const inherited = await fetchInherited();
        // Which model should show as the selected Default? If this agent pinned its
        // own (agentCfg.model) use that; otherwise it's running the app default, so
        // reflect that id (display only — not persisted unless the admin clicks a
        // radio) so one of the inherited Default radios shows selected.
        const displayModel = agentCfg.model || _appDefaultModel || '';
        return {
          provider: agentCfg.provider, base_url: agentCfg.base_url, api_key: agentCfg.api_key, model: displayModel,
          text_capable: agentCfg.text_capable, image_capable: agentCfg.image_capable, image_out_capable: agentCfg.image_out_capable,
          providerConfigs: agentCfg.providerConfigs || {},
          roster,
          inherited,
        };
      },
      saveSingle: async (s) => {
        Object.assign(agentCfg, {
          use_default: false,
          provider: s.provider, base_url: s.base_url, api_key: s.api_key, model: s.model,
          text_capable: s.text_capable, image_capable: s.image_capable, image_out_capable: s.image_out_capable,
        });
        return persistLlm();
      },
      saveRoster: async ({ providers }) => {
        const oldProviders = agentCfg.multi_providers || [];
        agentCfg.multi_providers = providers;
        // A non-empty roster is an explicit per-agent override (its own models,
        // or role overrides materialized from inherited rows). Flip use_default
        // off so the backend honours it — an agent that was pure-inherit (no
        // roster, use_default: true) would otherwise silently ignore the first
        // override row it saves. An EMPTY roster keeps the previous use_default
        // (the stale-pin guard below already resets to inherit when a pinned
        // model is removed).
        if (providers.length && agentCfg.use_default !== false) {
          agentCfg.use_default = false;
        }
        // The Standard role (enabled + text_capable) IS the default brain model.
        // Mirror the admin panel (set_multi_providers in app/admin/settings.py):
        // when this roster save designates a Standard model, promote it to the
        // top-level provider/model slots so new sessions start with it. The
        // Standard row may be a materialized override copy (blank api_key) —
        // the backend union keeps the inherited credential. If no model holds
        // Standard, leave the existing default in place (same as the admin).
        // Runs BEFORE the stale-pin guard so a freshly promoted standard is not
        // mistaken for a stale pin.
        const standard = providers.find(p => p.enabled && p.text_capable !== false);
        if (standard) {
          if (standard.provider) agentCfg.provider = standard.provider;
          if (standard.base_url) agentCfg.base_url = standard.base_url;
          if (standard.api_key) agentCfg.api_key = standard.api_key;
          if (standard.model) agentCfg.model = standard.model;
        }
        // If the agent's pinned default model was in the agent's OWN roster
        // but was just removed, clear the stale pin so the agent falls back
        // to the app default instead of running on a removed model. Only
        // fires when the removed model was actually the agent's own (not
        // inherited) — inherited pins are left alone.
        if (agentCfg.model && agentCfg.use_default === false) {
          const wasInOwn = Array.isArray(oldProviders) && oldProviders.some(p => p.model === agentCfg.model);
          const stillInOwn = providers.some(p => p.model === agentCfg.model);
          if (wasInOwn && !stillInOwn) {
            agentCfg.model = '';
            agentCfg.provider = '';
            agentCfg.base_url = '';
            agentCfg.api_key = '';
            agentCfg.use_default = true;
          }
        }
        return persistLlm();
      },
      saveDefaultModel: async (modelId) => {
        agentCfg.model = modelId || '';
        return persistLlm();
      },
      // Per-agent roster-role prompt injections (grep ROLE-DIRECTIVE-INJECT):
      // stored in THIS agent's llm_config.model_directives and saved through
      // the same agent PUT as the roster (persistLlm) — which the backend
      // gates to the agents administrator (_is_agent_admin). The UI opens the
      // editor read-only for non-admins via directivesEditable.
      loadDirectives: async () => (agentCfg.model_directives || {}),
      saveDirectives: async (directives) => {
        agentCfg.model_directives = directives;
        return persistLlm();
      },
      fetchProviderCatalog: async () => {
        try {
          const r = await fetch(apiPath('/admin/settings/providers'));
          if (r.ok) { const d = await r.json(); if (d && Object.keys(d).length) return d; }
        } catch (_) {}
        return {
          openrouter:  { name: 'OpenRouter',      base_url: 'https://openrouter.ai/api/v1' },
          openai:      { name: 'OpenAI',           base_url: 'https://api.openai.com/v1' },
          gemini:      { name: 'Google Gemini',    base_url: 'https://generativelanguage.googleapis.com/v1beta/openai' },
          groq:        { name: 'Groq',             base_url: 'https://api.groq.com/openai/v1' },
          together:    { name: 'Together AI',       base_url: 'https://api.together.xyz/v1' },
          deepseek:    { name: 'DeepSeek',          base_url: 'https://api.deepseek.com/v1' },
          mistral:     { name: 'Mistral AI',        base_url: 'https://api.mistral.ai/v1' },
          fireworks:   { name: 'Fireworks AI',      base_url: 'https://api.fireworks.ai/inference/v1' },
          xai:         { name: 'xAI (Grok)',        base_url: 'https://api.x.ai/v1' },
          perplexity:  { name: 'Perplexity',        base_url: 'https://api.perplexity.ai' },
          ollama:      { name: 'Ollama (local)',    base_url: 'http://localhost:11434/v1' },
          deepinfra:   { name: 'DeepInfra',         base_url: 'https://api.deepinfra.com/v1/openai' },
          lmstudio:    { name: 'LM Studio (local)', base_url: 'http://localhost:1234/v1' },
        };
      },
    };

    mountModelTable(host, {
      adapter,
      directivesEditable: _userIsAdmin,
      advancedExtra: (body) => {
        const wrap = document.createElement('div'); wrap.className = 'ac-row';
        const head = document.createElement('div'); head.className = 'ac-ability-row';
        const label = document.createElement('span'); label.className = 'ac-ability-label';
        label.innerHTML = '<span class="ac-ability-name">Reset Usage Counters</span>'
          + '<span class="ac-ability-desc">Clear your usage metrics with this agent (the tokens and cost shown in the model table above — your own, not other users\').</span>';
        const chev = document.createElement('span'); chev.className = 'ac-row-chevron';
        chev.innerHTML = icon('chevron-right', { size: 16 });
        head.appendChild(label); head.appendChild(chev);

        const inner = document.createElement('div'); inner.className = 'ac-ability-body';
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;gap:10px;flex-wrap:wrap;';
        const sel = document.createElement('select'); sel.className = 'ac-input ac-config-sel';
        ['all','input','output','cost'].forEach(v => {
          const o = document.createElement('option'); o.value = v;
          o.textContent = {all:'All (input + output + cost)', input:'Input Tokens only', output:'Output Tokens only', cost:'Cost only'}[v];
          sel.appendChild(o);
        });
        const btn = document.createElement('button'); btn.className = 'ac-btn ac-btn-ghost';
        btn.textContent = 'Reset'; btn.style.color = 'var(--danger)';
        const msg = document.createElement('span'); msg.style.cssText = 'font-size:11px;color:var(--fg-3);';
        row.appendChild(sel); row.appendChild(btn); row.appendChild(msg);
        inner.appendChild(row);

        wrap.appendChild(head); wrap.appendChild(inner);
        head.addEventListener('click', () => wrap.classList.toggle('expanded'));
        body.appendChild(wrap);

        btn.addEventListener('click', async () => {
          btn.disabled = true; msg.textContent = 'Resetting…';
          try {
            const headers = {'Content-Type':'application/json', ...authHeaders()};
            const res = await fetch(`/admin/settings/agent-usage/reset?agent_id=${encodeURIComponent(agent.id)}&scope=${sel.value}`, { method:'POST', headers });
            const data = await res.json();
            if (data.ok) { msg.textContent = '✓ Reset complete'; setTimeout(() => msg.textContent = '', 3000); }
            else { msg.textContent = `✗ ${data.error || 'Failed'}`; }
          } catch (e) { msg.textContent = `✗ ${e.message}`; }
          btn.disabled = false;
        });
      },
    });
  }

  // ── Chat modes ─────────────────────────────────────────────────────────────
  // Each mode is a permission policy plus a system-prompt injection and a
  // close-out checklist. Ask/Plan/Auto are permanent defaults; admins can tune
  // them and add mode-on-demand variants such as Research or Planner.
  if (agent.engine !== 'claude_code' && agent.engine !== 'terminal_chat' && agent.engine !== 'codex') {
    const g = _group(modesTopic, 'sliders-horizontal', 'Chat modes',
      'Modes appear in the chat footer selector. Read-only modes can research and produce proposals or plans, but cannot execute mutating tools. Write-capable modes can make changes. Each mode can also inject instructions and require a completion checklist.');
    const list = _cfgList(g);
    const fallbackModes = [
      { id:'ask', label:'Ask', description:'Research freely and deliver a proposal without executing it.', permission_policy:'read_only', prompt:'', contract:{require_plan_document:false,carry_forward:true,checklist:[],max_rounds:0,send_back:false}, builtin:true },
      { id:'plan', label:'Plan', description:'Investigate deeply and deliver an execution-ready plan.', permission_policy:'read_only', prompt:'', contract:{require_plan_document:true,carry_forward:true,checklist:[],max_rounds:1,send_back:true}, builtin:true },
      { id:'auto', label:'Auto', description:'Work autonomously with write-capable permissions.', permission_policy:'write', prompt:'', contract:{require_plan_document:false,carry_forward:true,checklist:[],max_rounds:1,send_back:true}, builtin:true },
    ];
    let modes = structuredClone(Array.isArray(agent.execution_modes) && agent.execution_modes.length ? agent.execution_modes : fallbackModes);
    let cur = modes.some(m => m.id === agent.default_execution_mode) ? agent.default_execution_mode : 'ask';
    const { ctrl, descEl } = _cfgRow(list, 'Default mode', (modes.find(m => m.id === cur) || {}).description || '');
    const sel = document.createElement('select');
    sel.className = 'ac-input ac-input-sm ac-config-sel'; sel.dataset.field = 'default_execution_mode';
    const fillDefaultSelect = () => {
      sel.innerHTML = '';
      modes.forEach(mode => {
        const o = document.createElement('option'); o.value = mode.id; o.textContent = mode.label || mode.id;
        if (mode.id === cur) o.selected = true; sel.appendChild(o);
      });
    };
    fillDefaultSelect();
    if (!isEditable) sel.disabled = true;
    ctrl.appendChild(sel);
    if (isEditable) {
      sel.addEventListener('change', async () => {
        const selected = sel.value;
        if (descEl) descEl.textContent = (modes.find(m => m.id === selected) || {}).description || '';
        if (selected === cur) return;
        const previous = cur;
        sel.disabled = true;
        const ok = await _saveCfg(agent, { default_execution_mode: selected }, ctrl);
        sel.disabled = false;
        if (ok) cur = selected;
        else { sel.value = previous; if (descEl) descEl.textContent = (modes.find(m => m.id === previous) || {}).description || ''; }
      });
    }

    const editor = document.createElement('div');
    editor.style.cssText = 'display:flex;flex-direction:column;gap:8px;padding:10px 0 2px;';
    g.appendChild(editor);
    const saveModes = _debounced(() => {
      agent.execution_modes = modes;
      _saveCfg(agent, { execution_modes: modes }, editor);
    });

    const renderModes = () => {
      editor.innerHTML = '';
      modes.forEach((mode, index) => {
        const card = document.createElement('details');
        card.style.cssText = 'border:var(--border-width) solid var(--border);border-radius:8px;padding:8px 10px;background:var(--bg-1);';
        const summary = document.createElement('summary');
        summary.style.cssText = 'cursor:pointer;display:flex;align-items:center;gap:8px;font-size:12px;font-weight:650;';
        const summaryName = document.createElement('span'); summaryName.textContent = mode.label || mode.id;
        const badge = document.createElement('span');
        badge.style.cssText = 'font-size:10px;color:var(--fg-3);font-weight:500;';
        badge.textContent = mode.permission_policy === 'write' ? 'Write-capable' : 'Read-only';
        summary.appendChild(summaryName); summary.appendChild(badge); card.appendChild(summary);

        const fields = document.createElement('div');
        fields.style.cssText = 'display:grid;grid-template-columns:minmax(120px,1fr) minmax(140px,1fr);gap:8px;margin-top:10px;';
        const field = (label, input, wide=false) => {
          const wrap = document.createElement('label');
          wrap.style.cssText = `display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--fg-3);${wide ? 'grid-column:1/-1;' : ''}`;
          const text = document.createElement('span'); text.textContent = label;
          wrap.appendChild(text); wrap.appendChild(input); fields.appendChild(wrap); return input;
        };
        const name = document.createElement('input'); name.className = 'ac-input'; name.value = mode.label || '';
        field('Name', name);
        const policy = document.createElement('select'); policy.className = 'ac-input';
        [['read_only','Read-only'],['write','Write-capable']].forEach(([v,t]) => { const o=document.createElement('option'); o.value=v; o.textContent=t; o.selected=mode.permission_policy===v; policy.appendChild(o); });
        field('Permission policy', policy);
        const description = document.createElement('input'); description.className = 'ac-input'; description.value = mode.description || '';
        field('Footer description', description, true);
        const prompt = document.createElement('textarea'); prompt.className = 'ac-input'; prompt.rows = 6; prompt.value = mode.prompt || '';
        field('Prompt injection', prompt, true);
        const contract = mode.contract && typeof mode.contract === 'object' ? mode.contract : {
          require_plan_document:false, carry_forward:true, checklist:[], max_rounds:0, send_back:false,
        };
        mode.contract = contract;
        const checklistBox = document.createElement('div');
        checklistBox.style.cssText = 'display:flex;flex-direction:column;gap:6px;';
        const checklistRows = document.createElement('div'); checklistRows.style.cssText = 'display:flex;flex-direction:column;gap:6px;';
        const addRequirement = document.createElement('button'); addRequirement.type = 'button'; addRequirement.className = 'ac-btn ac-btn-ghost';
        addRequirement.innerHTML = `${icon('plus', { size:'13px' })} Add requirement`;
        checklistBox.appendChild(checklistRows); checklistBox.appendChild(addRequirement);
        field('Completion contract — each row is a separate JSON object', checklistBox, true);
        const renderRequirements = () => {
          checklistRows.innerHTML = '';
          (contract.checklist || []).forEach((item, itemIndex) => {
            const row = document.createElement('div'); row.style.cssText = 'display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px;align-items:center;';
            const inputWrap = document.createElement('div'); inputWrap.style.cssText = 'display:flex;flex-direction:column;gap:2px;';
            const inp = document.createElement('input'); inp.className = 'ac-input'; inp.value = item.label || '';
            const key = document.createElement('code'); key.style.cssText = 'font-size:9px;color:var(--fg-3);'; key.textContent = `id: ${item.id}`;
            inputWrap.appendChild(inp); inputWrap.appendChild(key); row.appendChild(inputWrap);
            const del = document.createElement('button'); del.type = 'button'; del.className = 'ac-btn ac-btn-ghost'; del.title = 'Remove requirement'; del.innerHTML = icon('minus', { size:'13px' });
            del.addEventListener('click', () => { contract.checklist.splice(itemIndex, 1); renderRequirements(); sync(); });
            inp.addEventListener('input', () => { item.label = inp.value; sync(); });
            row.appendChild(del); checklistRows.appendChild(row);
          });
        };
        addRequirement.addEventListener('click', () => {
          let n = (contract.checklist || []).length + 1;
          let id = `${mode.id}-requirement-${n}`;
          while ((contract.checklist || []).some(item => item.id === id)) { n++; id = `${mode.id}-requirement-${n}`; }
          contract.checklist = contract.checklist || [];
          contract.checklist.push({ id, label:'' }); renderRequirements(); sync();
          checklistRows.lastElementChild?.querySelector('input')?.focus();
        });
        renderRequirements();
        const planDocWrap = document.createElement('label'); planDocWrap.style.cssText = 'display:flex;align-items:center;gap:7px;font-size:11px;color:var(--fg-2);';
        const requirePlanDoc = document.createElement('input'); requirePlanDoc.type = 'checkbox'; requirePlanDoc.checked = !!contract.require_plan_document;
        planDocWrap.appendChild(requirePlanDoc); planDocWrap.append('Require persistent Plan document + checklist'); fields.appendChild(planDocWrap);
        const carryWrap = document.createElement('label'); carryWrap.style.cssText = 'display:flex;align-items:center;gap:7px;font-size:11px;color:var(--fg-2);';
        const carryForward = document.createElement('input'); carryForward.type = 'checkbox'; carryForward.checked = contract.carry_forward !== false;
        carryWrap.appendChild(carryForward); carryWrap.append('Carry this contract into later modes'); fields.appendChild(carryWrap);
        const rounds = document.createElement('input'); rounds.className = 'ac-input'; rounds.type = 'number'; rounds.min = '0'; rounds.max = '5'; rounds.value = contract.max_rounds || 0;
        field('Max fix rounds', rounds);
        const sendWrap = document.createElement('label'); sendWrap.style.cssText = 'display:flex;align-items:center;gap:7px;font-size:11px;color:var(--fg-2);';
        const sendBack = document.createElement('input'); sendBack.type = 'checkbox'; sendBack.checked = !!contract.send_back;
        sendWrap.appendChild(sendBack); sendWrap.append('Send incomplete work back'); fields.appendChild(sendWrap);
        if (!mode.builtin) {
          const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'ac-btn ac-btn-ghost'; remove.textContent = 'Remove mode'; remove.style.color = 'var(--danger)';
          remove.addEventListener('click', () => {
            modes.splice(index, 1);
            if (cur === mode.id) { cur = 'ask'; _saveCfg(agent, { default_execution_mode: cur }, ctrl); }
            fillDefaultSelect(); renderModes(); saveModes();
          });
          fields.appendChild(remove);
        }
        const sync = () => {
          mode.label = name.value.trim() || mode.id;
          mode.description = description.value.trim(); mode.permission_policy = policy.value;
          mode.prompt = prompt.value;
          contract.require_plan_document = requirePlanDoc.checked; contract.carry_forward = carryForward.checked;
          contract.checklist = (contract.checklist || []).filter(item => item && item.label !== undefined);
          contract.max_rounds = Math.max(0, Math.min(5, parseInt(rounds.value, 10) || 0)); contract.send_back = sendBack.checked;
          summaryName.textContent = mode.label; badge.textContent = mode.permission_policy === 'write' ? 'Write-capable' : 'Read-only';
          if (mode.id === cur && descEl) descEl.textContent = mode.description;
          fillDefaultSelect(); saveModes();
        };
        [name, description, prompt, rounds].forEach(el => el.addEventListener('input', sync));
        [policy, sendBack, requirePlanDoc, carryForward].forEach(el => el.addEventListener('change', sync));
        if (!isEditable) fields.querySelectorAll('input,textarea,select,button').forEach(el => { el.disabled = true; });
        card.appendChild(fields); editor.appendChild(card);
      });
      if (isEditable) {
        const add = document.createElement('button'); add.type = 'button'; add.className = 'ac-btn ac-btn-ghost'; add.innerHTML = `${icon('plus', { size:'14px' })} Add mode`;
        add.addEventListener('click', () => {
          let n = 1; while (modes.some(m => m.id === `custom-${n}`)) n++;
          modes.push({ id:`custom-${n}`, label:'New mode', description:'', permission_policy:'read_only', prompt:'', contract:{require_plan_document:false,carry_forward:true,checklist:[],max_rounds:1,send_back:true}, builtin:false });
          fillDefaultSelect(); renderModes(); saveModes();
        });
        editor.appendChild(add);
      }
    };
    renderModes();
  }

  // ── Chat Display ──────────────────────────────────────────────────────────
  // Per-agent toggles for what the chat stream shows: intermediate mid-turn
  // messages and tool-call accordions.  Stored in metadata.chat_ui (deep-merged
  // server-side).  Default for both is true (visible).
  {
    const g = _group(modesTopic, 'eye', 'Chat Display');
    const list = _cfgList(g);
    const cu = agent.chat_ui || {};

    // show_mid_turn_messages toggle
    {
      const cur = cu.show_mid_turn_messages !== false;  // default true
      const { ctrl } = _cfgRow(list, 'Show mid-turn messages',
        'When ON the agent\'s intermediate reasoning steps appear in chat as they happen. Turn OFF to only see the final response.');
      const wrap = document.createElement('label'); wrap.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
      const cb = document.createElement('input'); cb.type = 'checkbox'; cb.className = 'conn-toggle';
      cb.checked = cur;
      const track = document.createElement('span'); track.className = 'conn-toggle-track';
      wrap.appendChild(cb); wrap.appendChild(track); ctrl.appendChild(wrap);
      if (isEditable) {
        cb.addEventListener('change', async () => {
          cb.disabled = true;
          const ok = await _saveCfg(agent, { chat_ui: { show_mid_turn_messages: cb.checked } }, ctrl);
          cb.disabled = false;
          if (!ok) cb.checked = !cb.checked;
        });
      } else if (!isEditable) {
        cb.disabled = true;
      }
    }

    // show_tool_calls toggle
    {
      const cur = cu.show_tool_calls !== false;  // default true
      const { ctrl } = _cfgRow(list, 'Show tool calls',
        'When ON, tool calls are shown in a collapsible panel under each agent message. Turn OFF to hide the technical detail.');
      const wrap = document.createElement('label'); wrap.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
      const cb = document.createElement('input'); cb.type = 'checkbox'; cb.className = 'conn-toggle';
      cb.checked = cur;
      const track = document.createElement('span'); track.className = 'conn-toggle-track';
      wrap.appendChild(cb); wrap.appendChild(track); ctrl.appendChild(wrap);
      if (isEditable) {
        cb.addEventListener('change', async () => {
          cb.disabled = true;
          const ok = await _saveCfg(agent, { chat_ui: { show_tool_calls: cb.checked } }, ctrl);
          cb.disabled = false;
          if (!ok) cb.checked = !cb.checked;
        });
      } else if (!isEditable) {
        cb.disabled = true;
      }
    }
  }

  // ── Target device (Remote Control default) ────────────────────────────────
  // Which device in the shared fleet runs this agent's chats by default.
  //   • "None" (the DEFAULT, stored as blank '') = no pin — each chat runs on the
  //     device that's messaging the agent (it roams to wherever you are).
  //   • "This device" pins the current machine's own concrete fleet id, so the
  //     choice reads as *this* device from any other one in the fleet (not a relative
  //     '' that every device saw as itself), and dispatches back here from elsewhere.
  //   • any other fleet device pins that device by its concrete id.
  // A fresh session pre-selects a pinned device in the chat Remote Control pill; if
  // it's offline the chat falls back to the messaging device, and the user can still
  // pick a different device per session. A legacy blank '' is treated as "None".
  // Read by chat-ui.js on session open. The device list comes from
  // window.DevicePicker (shared with the chat pill), loaded async — we paint "None"
  // (+ the saved value) first, then fill the fleet.
  if (isEditable && !isAnonymousDraft) {
    const g = _group(deployTopic, 'monitor-smartphone', 'Target device');
    const list = _cfgList(g);
    const { ctrl, descEl } = _cfgRow(list, 'Default device',
      'By default a chat runs on whichever device is messaging the agent. Pick a device to always run there.');
    const sel = document.createElement('select');
    sel.className = 'ac-input ac-input-sm ac-config-sel'; sel.dataset.field = 'default_target_device';
    const cur = (typeof agent.default_target_device === 'string') ? agent.default_target_device : '';
    let confirmed = cur;
    ctrl.appendChild(sel);

    // Rebuild the option list from the live fleet. Keeps a saved-but-offline device
    // as an option so the admin still sees their choice. `null` devices = pre-load
    // paint (None + saved value only).
    const _rebuild = (devices) => {
      const all = Array.isArray(devices) ? devices : [];
      const selfDev = all.find(d => d && d.is_self);
      const selfId = (selfDev && selfDev.instance_id)
        || ((window.DevicePicker && window.DevicePicker.selfId && window.DevicePicker.selfId()) || '');
      const others = all.filter(d => d && !d.is_self);
      const val = confirmed;
      const isNone = !val;                         // blank/absent = roam to messenger
      const isSelf = !!selfId && val === selfId;   // pinned to THIS machine
      sel.innerHTML = '';
      const mk = (value, text, selected) => {
        const o = document.createElement('option'); o.value = value; o.textContent = text;
        if (selected) o.selected = true; sel.appendChild(o);
      };
      // Default: no pin — the agent runs on whichever device is messaging it.
      mk('', 'None — runs on the messaging device', isNone);
      // "This device" pins the current machine's concrete fleet id.
      if (selfId) {
        const selfName = selfDev ? String(selfDev.label || '').trim() : '';
        const base = selfName ? ('This device (' + selfName + ')') : 'This device';
        mk(selfId, base, isSelf);
      }
      // Other fleet devices.
      others.forEach(d => {
        const id = (d && d.instance_id) || '';
        if (!id) return;
        mk(id, (d.label || id) + (d.online ? '' : ' (offline)'), val === id);
      });
      // If the saved value isn't in the fleet (offline / removed), keep it as a
      // disabled option so the admin still sees their choice.
      if (val && !isNone && !isSelf && !others.some(d => (d && d.instance_id) === val)) {
        const o = document.createElement('option'); o.value = val; o.textContent = val + ' (offline)';
        o.selected = true; o.disabled = true; sel.appendChild(o);
      }
    };

    // Paint "None" + saved value immediately; the fleet fills in async.
    _rebuild(null);

    // Listen for the fleet list (shared DevicePicker, loaded by the chat pill).
    if (window.DevicePicker && window.DevicePicker.load) {
      window.DevicePicker.load().then(_rebuild).catch(() => {});
    }

    sel.addEventListener('change', async () => {
      const selected = sel.value;
      if (selected === confirmed) return;
      sel.disabled = true;
      const ok = await _saveCfg(agent, { default_target_device: selected }, ctrl);
      sel.disabled = false;
      if (ok) { confirmed = selected; }
      else { sel.value = confirmed; }
    });
  }

  // ── Limits ────────────────────────────────────────────────────────────────
  // Always present (read-only number fields for non-editable templates); save
  // wiring only for editable, non-mock agents. Max Concurrent Tools folds in here.
  {
    const g = _group(runtimeTopic, 'gauge', 'Limits');
    const list = _cfgList(g);
    const L = [
      { field: 'max_turn_count', label: 'Max Turn Count', value: agent.max_turn_count != null ? agent.max_turn_count : 9999, placeholder: '9999', min: 0, max: 99999, hint: 'Caps the LLM → tool → LLM cycles per response.' },
      { field: 'max_wall_seconds', label: 'Wall Clock (seconds)', value: agent.max_wall_seconds != null ? agent.max_wall_seconds : '', placeholder: '0 (off)', min: 0, max: 86400, step: 1, hint: 'Limits total real time for one response.' },
      { field: 'max_tokens', label: 'Max Tokens', value: agent.max_tokens != null ? agent.max_tokens : 8000, placeholder: '8000', min: 1, step: 1, hint: 'Maximum output tokens per LLM response.' },
      { field: 'max_identical_tool_calls', label: 'Identical Tool Calls', value: agent.max_identical_tool_calls != null ? agent.max_identical_tool_calls : '', placeholder: '0 (off)', min: 0, max: 9999, step: 1, hint: 'Limits how many times the agent can call the same tool with same args.' },
      { field: 'max_stall_strikes', label: 'Stall Strikes', value: agent.max_stall_strikes != null ? agent.max_stall_strikes : '', placeholder: '0 (off)', min: 0, max: 99, step: 1, hint: 'After this many stall guard detections, the agent stops and asks for clarification.' },
    ];

    L.forEach(cfg => {
      const { ctrl } = _cfgRow(list, cfg.label, cfg.hint);
      const inputEl = document.createElement('input'); inputEl.type = 'number';
      inputEl.className = 'ac-input ac-input-sm ac-config-num';
      inputEl.dataset.field = cfg.field; inputEl.value = cfg.value; inputEl.placeholder = cfg.placeholder;
      inputEl.min = cfg.min; inputEl.max = cfg.max; if (cfg.step) inputEl.step = cfg.step;
      if (!isEditable) inputEl.readOnly = true;
      ctrl.appendChild(_wrapNumberStepper(inputEl));
      if (isEditable) {
        const saveLimit = _debounced(() => {
          const raw = inputEl.value;
          const val = cfg.field === 'max_wall_seconds'
            ? (raw === '' ? null : (isNaN(parseFloat(raw)) ? null : parseFloat(raw)))
            : (raw === '' ? 0 : (parseInt(raw, 10) || 0));
          _saveCfg(agent, { [cfg.field]: val }, ctrl);
        });
        inputEl.addEventListener('input', saveLimit); inputEl.addEventListener('blur', () => saveLimit.flush());
      }
    });

    // Resume tail messages — stored in metadata, 0 = use app default (32).
    if (isEditable) {
      const { ctrl } = _cfgRow(list, 'Resume Tail Messages', 'How many recent messages to replay when a session resumes. 0 = app default (32).');
      const inputEl = document.createElement('input'); inputEl.type = 'number';
      inputEl.className = 'ac-input ac-input-sm ac-config-num';
      inputEl.min = 0; inputEl.max = 999; inputEl.step = 1;
      inputEl.value = (agent.resume_tail_messages != null && agent.resume_tail_messages > 0) ? agent.resume_tail_messages : '';
      inputEl.placeholder = '0 (default 32)';
      ctrl.appendChild(_wrapNumberStepper(inputEl));
      if (isEditable) {
        const saveResume = _debounced(() => {
          const raw = inputEl.value;
          const val = (raw === '' || parseInt(raw, 10) === 0) ? null : (parseInt(raw, 10) || 0);
          _saveCfg(agent, { resume_tail_messages: val }, ctrl);
        });
        inputEl.addEventListener('input', saveResume); inputEl.addEventListener('blur', () => saveResume.flush());
      }
    }

    // Max concurrent tools — safety_policy.max_concurrent_tools (0/blank = off).
    // (Relocated from the old Tools tab; the two per-agent confirmation toggles
    // that used to sit beside it were removed as redundant with the chat "Auto"
    // execution mode. The `guardrails` loop node is still managed from the Agent
    // Loop tab; safety_policy.auto_confirm keeps its stored default.)
    if (isEditable) {
      const { ctrl } = _cfgRow(list, 'Max Concurrent Tools', 'Cap how many tools run in parallel. 0 = unlimited.');
      const sp0 = (agent.safety_policy && typeof agent.safety_policy === 'object') ? agent.safety_policy : {};
      const inputEl = document.createElement('input'); inputEl.type = 'number';
      inputEl.className = 'ac-input ac-input-sm ac-config-num';
      inputEl.min = 0; inputEl.max = 20; inputEl.step = 1;
      inputEl.value = sp0.max_concurrent_tools != null ? sp0.max_concurrent_tools : '';
      inputEl.placeholder = 'unlimited';
      ctrl.appendChild(_wrapNumberStepper(inputEl));
      if (isEditable) {
        const saveMax = _debounced(() => {
          const sp = (agent.safety_policy && typeof agent.safety_policy === 'object') ? agent.safety_policy : {};
          const newSp = { ...sp };
          const mv = parseInt(inputEl.value, 10);
          if (mv > 0) newSp.max_concurrent_tools = mv; else delete newSp.max_concurrent_tools;
          _saveCfg(agent, { safety_policy: newSp }, ctrl);
        });
        inputEl.addEventListener('input', saveMax); inputEl.addEventListener('blur', () => saveMax.flush());
      }
    }
  }

  // ── Access & triggering ─────────────────────────────────────────────────────
  // User Mode is shown for everyone (disabled on read-only templates); Trigger +
  // Trigger Key are editable-only.
  {
    const g = _group(runtimeTopic, 'zap', 'Access & triggering');
    const list = _cfgList(g);

    // User mode
    {
      const umMode = agent.user_mode || 'anonymous';
      const umHint = (mode) => mode === 'register'
        ? 'Agent guides new users to register and links accounts across channels.'
        : 'Users get auto-generated anonymous IDs. No registration required.';
      const { ctrl, descEl } = _cfgRow(list, 'User Mode', umHint(umMode));
      const sel = document.createElement('select');
      sel.className = 'ac-input ac-input-sm ac-config-sel'; sel.dataset.field = 'user_mode';
      [['anonymous', 'Anonymous'], ['register', 'Register']].forEach(([v, t]) => {
        const o = document.createElement('option'); o.value = v; o.textContent = t;
        if (v === umMode) o.selected = true; sel.appendChild(o);
      });
      if (!isEditable) sel.disabled = true;
      ctrl.appendChild(sel);
      if (isEditable) {
        let confirmed = umMode;
        sel.addEventListener('change', async () => {
          const selected = sel.value;
          if (descEl) descEl.textContent = umHint(selected);
          if (selected === confirmed) return;
          sel.disabled = true;
          const ok = await _saveCfg(agent, { user_mode: selected }, ctrl);
          sel.disabled = false;
          if (ok) { confirmed = selected; }
          else { sel.value = confirmed; if (descEl) descEl.textContent = umHint(confirmed); }
        });
      }
    }

    // Public link — full-app shared chat page at /{agent_id}
    if (isEditable) {
      const { ctrl } = _cfgRow(list, 'Public link', 'Allow a public shared link (/agent-id) where visitors chat with this agent without signing up.');
      const wrap = document.createElement('label'); wrap.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
      const cb = document.createElement('input'); cb.type = 'checkbox'; cb.className = 'conn-toggle';
      cb.checked = !!agent.public_link; cb.dataset.field = 'public_link';
      const track = document.createElement('span'); track.className = 'conn-toggle-track';
      wrap.appendChild(cb); wrap.appendChild(track); ctrl.appendChild(wrap);
      cb.addEventListener('change', async () => {
        cb.disabled = true;
        const ok = await _saveCfg(agent, { public_link: cb.checked }, ctrl);
        cb.disabled = false;
        if (!ok) cb.checked = !cb.checked;
      });
    }

    // Trigger + Trigger Key (editable only)
    if (isEditable) {
      const trig = _cfgRow(list, 'Trigger', 'What starts a run for this agent.');
      const trigSel = document.createElement('select');
      trigSel.className = 'ac-input ac-input-sm ac-config-sel'; trigSel.dataset.field = 'trigger_type';
      [['user_input', 'Chat'], ['webhook', 'Webhook'], ['schedule', 'Schedule'], ['event', 'Event']].forEach(([v, t]) => {
        const o = document.createElement('option'); o.value = v; o.textContent = t;
        if (v === (agent.trigger_type || 'user_input')) o.selected = true; trigSel.appendChild(o);
      });
      trig.ctrl.appendChild(trigSel);

      const keyRowObj = _cfgRow(list, 'Trigger Key', 'Identifier that fires this trigger.');
      const keyInput = document.createElement('input'); keyInput.type = 'text';
      keyInput.className = 'ac-input ac-input-sm'; keyInput.dataset.field = 'trigger_key';
      keyInput.value = agent.trigger_key || ''; keyInput.placeholder = _triggerKeyPlaceholder(agent);
      keyRowObj.ctrl.appendChild(keyInput);

      let confirmedTrig = agent.trigger_type || 'user_input';
      let confirmedKey = agent.trigger_key || '';
      trigSel.addEventListener('change', async () => {
        const selected = trigSel.value;
        if (selected === confirmedTrig) return;
        trigSel.disabled = true;
        const ok = await _saveCfg(agent, { trigger_type: selected }, trig.ctrl);
        trigSel.disabled = false;
        if (ok) { confirmedTrig = selected; }
        else { trigSel.value = confirmedTrig; }
      });
      const saveKey = _debounced(async () => {
        const val = keyInput.value;
        if (val === confirmedKey) return;
        const ok = await _saveCfg(agent, { trigger_key: val || null }, keyRowObj.ctrl);
        if (ok) confirmedKey = val;
      });
      keyInput.addEventListener('input', saveKey);
      keyInput.addEventListener('blur', () => saveKey.flush());
    }
  }

  // ── Public anonymous policy ────────────────────────────────────────────────
  if (isEditable && agent.id !== 'shared_default') {
    _renderPublicAccessPolicy(deployTopic, agent);
  }

  // ── Website Embed (chat widget for external sites) ──────────────────────────
  if (isEditable && agent.engine !== 'claude_code' && agent.engine !== 'terminal_chat') {
    _renderWebsiteEmbed(deployTopic, agent);
  }

  // ── Data (External Data Sources) ────────────────────────────────────────────
  if (isEditable && !isMock && agent.engine !== 'claude_code' && agent.engine !== 'terminal_chat') {
    const g = _group(dataTopic, 'database', 'Data');
    const list = _cfgList(g);
    const row = document.createElement('div'); row.className = 'ac-ability-row';
    const label = document.createElement('span'); label.className = 'ac-ability-label';
    label.innerHTML = '<span class="ac-ability-name">External Data Sources</span>'
      + '<span class="ac-ability-desc">Connect databases, APIs, and file stores the agent can query.</span>';
    const ctrl = document.createElement('span'); ctrl.className = 'ac-config-control';
    row.appendChild(label); row.appendChild(ctrl);
    list.appendChild(row);
    mountDataSources(ctrl, { agentId: agent.id, authHeaders });
  }

  // ── Template options (discoverable toggle) ──────────────────────────────────
  if (agent.source === 'template') {
    const g = _group(identityTopic, 'settings-2', 'Template options');
    const list = _cfgList(g);
    const { ctrl } = _cfgRow(list, 'Discoverable', 'Show this template in the "New Agent" creation dropdown.');
    const wrap = document.createElement('label'); wrap.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
    const cb = document.createElement('input'); cb.type = 'checkbox'; cb.className = 'conn-toggle';
    cb.checked = !!agent.discoverable; cb.dataset.field = 'discoverable';
    const track = document.createElement('span'); track.className = 'conn-toggle-track';
    wrap.appendChild(cb); wrap.appendChild(track); ctrl.appendChild(wrap);
    if (!isMock) {
      cb.addEventListener('change', async () => {
        cb.disabled = true;
        const ok = await _saveCfg(agent, { discoverable: cb.checked }, ctrl);
        cb.disabled = false;
        if (!ok) cb.checked = !cb.checked;
      });
    }

    // Export to JSON file (templates with json_seed source, admin only)
    if (_userIsAdmin && agent.template_source === 'json_seed') {
      const { ctrl: expCtrl } = _cfgRow(list, 'Push to JSON seed file',
        'Write the current template back to app/defaults/agents/' + (agent.id || agent.template_id) + '.json (git-tracked).');
      const expBtn = document.createElement('button');
      expBtn.className = 'ac-btn ac-btn-sm';
      expBtn.textContent = 'Push to JSON file';
      expBtn.addEventListener('click', async () => {
        if (!confirm('Export "' + (agent.id || agent.template_id) + '" to app/defaults/agents/?')) return;
        expBtn.disabled = true;
        expBtn.textContent = 'Exporting…';
        try {
          const res = await fetch(`/admin/db/templates/${agent.id}/export-to-file`, {
            method: 'POST',
            headers: { ...authHeaders() },
          });
          if (res.ok) {
            expBtn.textContent = '✓ Exported';
            expBtn.style.background = 'var(--success,#22c55e)';
          } else {
            const err = await res.json().catch(() => ({}));
            expBtn.textContent = '⚠ Failed';
            alert('Export failed: ' + (err.detail || 'Unknown error'));
            expBtn.disabled = false;
            setTimeout(() => { expBtn.textContent = 'Export to file'; expBtn.style.background = ''; }, 2000);
          }
        } catch (e) {
          expBtn.textContent = '⚠ Error';
          alert('Export error: ' + e.message);
          expBtn.disabled = false;
          setTimeout(() => { expBtn.textContent = 'Export to file'; expBtn.style.background = ''; }, 2000);
        }
      });
      expCtrl.appendChild(expBtn);
    }
  }

  // A protected/system agent may not expose every topic. Remove only truly
  // empty parents so the outline never contains a chevron that opens to nothing.
  body.querySelectorAll(':scope > .agent-config-topic').forEach((topic) => {
    const content = topic.querySelector(':scope > .agent-config-topic-content');
    if (content && !content.children.length) topic.remove();
  });

  // ── Suggested replies (user-impersonator only) ──────────────────────────────
  function _renderSuggestionModeControl(body) {
    const g = _group(body, 'sparkles', 'Suggested replies');
    const intro = document.createElement('div'); intro.className = 'ac-hint';
    intro.textContent = 'When the user-impersonator agent responds, it can suggest reply chips the real user might tap.';
    g.appendChild(intro);
    const list = _cfgList(g);

    const modeRow = _cfgRow(list, 'When to suggest', 'Choose when the suggestion chips appear.');
    const modeSel = document.createElement('select');
    modeSel.className = 'ac-input ac-input-sm ac-config-sel';
    [['off', 'Off'], ['always', 'Always'], ['after_response', 'After each response']].forEach(([v, t]) => {
      const o = document.createElement('option'); o.value = v; o.textContent = t;
      if (v === (agent.suggestion_mode || 'off')) o.selected = true; modeSel.appendChild(o);
    });
    modeRow.ctrl.appendChild(modeSel);

    const countRow = _cfgRow(list, 'How many suggestions', 'Number of chips to show.');
    const countInput = document.createElement('input'); countInput.type = 'number';
    countInput.className = 'ac-input ac-input-sm ac-config-num';
    countInput.min = 1; countInput.max = 8; countInput.step = 1;
    countInput.value = agent.suggestion_count || 3;
    countRow.ctrl.appendChild(_wrapNumberStepper(countInput));

    if (!isMock) {
      let confirmedMode = agent.suggestion_mode || 'off';
      let confirmedCount = agent.suggestion_count || 3;
      modeSel.addEventListener('change', async () => {
        const selected = modeSel.value;
        if (selected === confirmedMode) return;
        modeSel.disabled = true;
        const ok = await _saveCfg(agent, { suggestion_mode: selected }, modeRow.ctrl);
        modeSel.disabled = false;
        if (ok) { confirmedMode = selected; }
        else { modeSel.value = confirmedMode; }
      });
      const saveCount = _debounced(async () => {
        const val = parseInt(countInput.value, 10) || 3;
        if (val === confirmedCount) return;
        const ok = await _saveCfg(agent, { suggestion_count: val }, countRow.ctrl);
        if (ok) confirmedCount = val;
      });
      countInput.addEventListener('input', saveCount);
      countInput.addEventListener('blur', () => saveCount.flush());
    }
  }
}

function _renderPublicAccessPolicy(body, agent) {
  const g = _group(body, 'shield', 'Public visitor policy');
  const intro = document.createElement('div'); intro.className = 'ac-hint';
  intro.textContent = 'Public agents must use the owner wallet or an agent-owned model key. Limits apply per agent; visitors keep a durable identity while saved chats expire separately.';
  g.appendChild(intro);
  const list = _cfgList(g);
  const policy = JSON.parse(JSON.stringify(agent.public_access || {}));
  policy.funding = policy.funding || {};
  policy.data = policy.data || {};
  policy.usage = policy.usage || {};
  policy.capabilities = policy.capabilities || {};
  policy.chat_ui = policy.chat_ui || {};

  const save = async (ctrl) => {
    policy.enabled = true;
    const ok = await _saveCfg(agent, { public_access: policy }, ctrl);
    if (ok) agent.public_access = JSON.parse(JSON.stringify(policy));
    return ok;
  };
  const addText = (label, hint, value, onValue, { type = 'text', placeholder = '' } = {}) => {
    const { ctrl } = _cfgRow(list, label, hint);
    const input = document.createElement('input'); input.type = type;
    input.className = 'ac-input ac-input-sm'; input.value = value ?? ''; input.placeholder = placeholder;
    ctrl.appendChild(input);
    input.addEventListener('change', async () => { onValue(input.value); await save(ctrl); });
    return input;
  };

  {
    const { ctrl } = _cfgRow(list, 'Token funding', 'Public requests fail closed if this funding source is unavailable. Dedicated key uses the key configured in this agent’s model settings.');
    const sel = document.createElement('select'); sel.className = 'ac-input ac-input-sm ac-config-sel';
    [['owner_wallet', 'Owner wallet'], ['dedicated_key', 'Agent-owned model key']].forEach(([v, t]) => {
      const o = document.createElement('option'); o.value = v; o.textContent = t;
      if ((policy.funding.mode || '') === v) o.selected = true;
      sel.appendChild(o);
    });
    ctrl.appendChild(sel);
    sel.addEventListener('change', async () => { policy.funding.mode = sel.value; await save(ctrl); });
  }

  addText('Turns / day', 'Maximum public turns across this agent each day.', policy.usage.turns_per_agent_per_day || 5000,
    v => { policy.usage.turns_per_agent_per_day = Math.max(1, Number(v) || 1); }, { type: 'number' });
  addText('Concurrent runs', 'Maximum simultaneous public runs for this agent.', policy.usage.concurrent_runs || 10,
    v => { policy.usage.concurrent_runs = Math.max(1, Number(v) || 1); }, { type: 'number' });
  addText('Guest tokens / day', 'Estimated admission tokens allowed per visitor per day.', policy.usage.tokens_per_guest_per_day || 100000,
    v => { policy.usage.tokens_per_guest_per_day = Math.max(1, Number(v) || 1); }, { type: 'number' });
  addText('Agent tokens / month', 'Estimated admission tokens sponsored across all visitors each month.', policy.usage.tokens_per_agent_per_month || 5000000,
    v => { policy.usage.tokens_per_agent_per_month = Math.max(1, Number(v) || 1); }, { type: 'number' });
  addText('Agent spend cap / month', 'Maximum estimated sponsored model cost in cents each month.', policy.usage.cost_cents_per_agent_per_month || 5000,
    v => { policy.usage.cost_cents_per_agent_per_month = Math.max(1, Number(v) || 1); }, { type: 'number' });
  addText('Chat retention (days)', 'Anonymous identity remains durable; only saved conversation data expires.', policy.data.session_retention_days || 14,
    v => { policy.data.session_retention_days = Math.max(1, Number(v) || 1); }, { type: 'number' });
  addText('Sessions / visitor', 'Maximum saved sessions for one anonymous visitor.', policy.data.max_sessions_per_guest || 5,
    v => { policy.data.max_sessions_per_guest = Math.max(1, Number(v) || 1); }, { type: 'number' });
  addText('Transcript MiB / visitor', 'Maximum saved transcript content for one visitor.', Math.round((policy.data.max_transcript_bytes_per_guest || 1048576) / 1048576),
    v => { policy.data.max_transcript_bytes_per_guest = Math.max(1, Number(v) || 1) * 1048576; }, { type: 'number' });
  addText('Total transcript MiB', 'Maximum anonymous transcript content stored for this agent.', Math.round((policy.data.max_total_storage_bytes || 1073741824) / 1048576),
    v => { policy.data.max_total_storage_bytes = Math.max(1, Number(v) || 1) * 1048576; }, { type: 'number' });
  addText('Public abilities', 'Comma-separated ability IDs. Wildcards and platform-sensitive groups are rejected.', (policy.capabilities.abilities || []).join(', '),
    v => { policy.capabilities.abilities = v.split(',').map(x => x.trim()).filter(Boolean); }, { placeholder: 'web_search, company_lookup' });
  addText('Public tools', 'Comma-separated tool names exposed to anonymous visitors.', (policy.capabilities.tools || []).join(', '),
    v => { policy.capabilities.tools = v.split(',').map(x => x.trim()).filter(Boolean); }, { placeholder: 'lookup_order, search_docs' });
  addText('UI feature grants', 'Comma-separated feature gates, such as model_picker. Features do not grant tools or abilities.', (policy.capabilities.features || []).join(', '),
    v => { policy.capabilities.features = v.split(',').map(x => x.trim()).filter(Boolean); }, { placeholder: 'model_picker' });

  const uiControls = [
    ['File attachment button', 'attach', ['active_footer', 'idle_footer']],
    ['Model picker button', 'model_changer', ['active_footer']],
    ['Abilities button', 'abilities', ['active_footer']],
  ];
  uiControls.forEach(([label, control, surfaces]) => {
    let enabled = true;
    const common = policy.chat_ui.chat_common || {};
    for (const surface of surfaces) {
      const configured = common[surface]?.chat_pill?.controls?.[control]?.enabled
        ?? common[surface]?.below_pill?.controls?.[control]?.enabled;
      if (configured === false) enabled = false;
    }
    const { ctrl } = _cfgRow(list, label, 'Show this control to public visitors.');
    const wrap = document.createElement('label'); wrap.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
    const cb = document.createElement('input'); cb.type = 'checkbox'; cb.className = 'conn-toggle'; cb.checked = enabled;
    const track = document.createElement('span'); track.className = 'conn-toggle-track';
    wrap.appendChild(cb); wrap.appendChild(track); ctrl.appendChild(wrap);
    cb.addEventListener('change', async () => {
      policy.chat_ui.chat_common = policy.chat_ui.chat_common || {};
      surfaces.forEach(surface => {
        const parent = control === 'attach' ? 'chat_pill' : 'below_pill';
        policy.chat_ui.chat_common[surface] = policy.chat_ui.chat_common[surface] || {};
        policy.chat_ui.chat_common[surface][parent] = policy.chat_ui.chat_common[surface][parent] || {};
        const slot = policy.chat_ui.chat_common[surface][parent];
        slot.controls = slot.controls || {};
        slot.controls[control] = { ...(slot.controls[control] || {}), enabled: cb.checked };
      });
      await save(ctrl);
    });
  });
}

// ── Website Embed section ───────────────────────────────────────────────────────
function _renderWebsiteEmbed(body, agent) {
  const g = _group(body, 'globe', 'Website Embed');
  const emb = (agent.embed && typeof agent.embed === 'object') ? agent.embed : {};

  const intro = document.createElement('div'); intro.className = 'ac-hint';
  intro.innerHTML = 'Embed this agent as a chat widget on any website. Visitors chat without signing up — '
    + 'this requires <strong>User&nbsp;Mode&nbsp;=&nbsp;Anonymous</strong> (set it under Access&nbsp;&amp;&nbsp;triggering). '
    + 'The widget\'s appearance (accent, messages, launcher style) is set in the agent\'s chat UI config.';
  g.appendChild(intro);

  const list = _cfgList(g);

  // Enable toggle
  const anonWarn = document.createElement('div');
  anonWarn.className = 'ac-hint';
  anonWarn.style.color = 'var(--warn, #d97706)';
  const refreshAnonWarn = () => {
    const on = !!(agent.embed && agent.embed.enabled);
    const anon = (agent.user_mode || 'anonymous') === 'anonymous';
    anonWarn.hidden = !(on && !anon);
    anonWarn.textContent = 'The widget is enabled but User Mode isn\'t Anonymous, so visitors will be turned away. '
      + 'Set User Mode to Anonymous above.';
  };
  {
    const { ctrl } = _cfgRow(list, 'Enable widget', 'Turn the embeddable website widget on for this agent.');
    const wrap = document.createElement('label'); wrap.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
    const cb = document.createElement('input'); cb.type = 'checkbox'; cb.className = 'conn-toggle';
    cb.checked = !!emb.enabled;
    const track = document.createElement('span'); track.className = 'conn-toggle-track';
    wrap.appendChild(cb); wrap.appendChild(track); ctrl.appendChild(wrap);
    cb.addEventListener('change', async () => {
      cb.disabled = true;
      const ok = await _saveCfg(agent, { embed: { enabled: cb.checked } }, ctrl);
      cb.disabled = false;
      if (!ok) cb.checked = !cb.checked;
      refreshAnonWarn();
      refreshSnippet();
    });
  }
  g.appendChild(anonWarn);

  // Launcher position
  {
    const { ctrl } = _cfgRow(list, 'Launcher position', 'Which corner the chat bubble sits in.');
    const sel = document.createElement('select'); sel.className = 'ac-input ac-input-sm ac-config-sel';
    [['right', 'Bottom right'], ['left', 'Bottom left']].forEach(([v, t]) => {
      const o = document.createElement('option'); o.value = v; o.textContent = t;
      if ((emb.launcher_position || 'right') === v) o.selected = true; sel.appendChild(o);
    });
    ctrl.appendChild(sel);
    sel.addEventListener('change', () => _saveCfg(agent, { embed: { launcher_position: sel.value } }, ctrl));
  }

  // Allowed domains
  _cfgField(g, {
    label: 'Allowed domains',
    field: 'embed_allowed_domains',
    value: Array.isArray(emb.allowed_domains) ? emb.allowed_domains.join(', ') : '',
    multiline: true, rows: 2,
    placeholder: 'example.com, app.example.com  (blank = allow any site)',
    hint: 'Restrict which sites may embed this widget. Leave blank to allow embedding anywhere.',
    onSave: (val) => _putAgentField(agent, { embed: { allowed_domains: val } }, null, { silent: true }),
  });

  // Snippet + preview
  const snipWrap = document.createElement('div'); snipWrap.className = 'ac-cfg-field';
  const snipLbl = document.createElement('label'); snipLbl.className = 'ac-label'; snipLbl.textContent = 'Embed snippet';
  snipWrap.appendChild(snipLbl);
  const snip = document.createElement('textarea');
  snip.className = 'ac-input'; snip.rows = 2; snip.readOnly = true;
  snip.style.fontFamily = 'var(--font-mono, monospace)'; snip.style.fontSize = '12.5px';
  snipWrap.appendChild(snip);
  const snipHint = document.createElement('div'); snipHint.className = 'ac-hint';
  snipHint.textContent = 'Paste this before </body> on your site.';
  snipWrap.appendChild(snipHint);
  const actions = document.createElement('div'); actions.style.display = 'flex'; actions.style.gap = '8px'; actions.style.marginTop = '8px';
  const copyBtn = _btn('Copy snippet', 'agents-btn'); actions.appendChild(copyBtn);
  const previewBtn = _btn('Preview widget', 'agents-btn'); actions.appendChild(previewBtn);
  snipWrap.appendChild(actions);
  g.appendChild(snipWrap);

  copyBtn.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(snip.value); copyBtn.textContent = 'Copied ✓'; setTimeout(() => copyBtn.textContent = 'Copy snippet', 1500); }
    catch (_) { snip.select(); document.execCommand && document.execCommand('copy'); }
  });
  previewBtn.addEventListener('click', () => window.open(`/embed/${agent.id}`, '_blank', 'noopener'));

  function refreshSnippet() {
    fetch(`/api/v1/agents/${agent.id}/embed`).then(r => r.ok ? r.json() : null).then(info => {
      if (!info) return;
      snip.value = info.snippet || '';
      const off = !info.embeddable;
      snipWrap.style.opacity = off ? '0.55' : '1';
    }).catch(() => {});
  }
  refreshAnonWarn();
  refreshSnippet();
}
