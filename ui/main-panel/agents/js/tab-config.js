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
import { authHeaders } from '../../../shared/js/left-login.js';
import { NODE_PANEL_INFO } from '../agent-loop/js/loop-node-data.js';
import {
  _agents, _isMockAgent, MOCK_AGENT_ID,
  _userIsAdmin, _extendLlmToAgents,
  _memoryStateFromAgent, _memoryUpdatesFor,
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
import { chatMsg } from '../../../shared/js/app-prompts.js';

// ── Shared layout helpers (App-Settings standard config-page layout) ──────────

/** A title-only section: accent Lucide icon + title over a `.ac-category-body`.
 *  Returns the body element to fill (with an `.ac-list` and/or fields). */
function _group(body, iconName, title) {
  const group = document.createElement('div');
  group.className = 'ac-category-group';
  const summary = document.createElement('div');
  summary.className = 'ac-category-summary';
  summary.style.cursor = 'default';   // these headings aren't collapsible
  summary.innerHTML =
    `<i data-lucide="${iconName}" class="lucide-icon" style="width:16px;height:16px;color:var(--accent);"></i>`
    + `<span class="ac-category-title">${_esc(title)}</span>`;
  const gbody = document.createElement('div');
  gbody.className = 'ac-category-body';
  group.appendChild(summary);
  group.appendChild(gbody);
  body.appendChild(group);
  return gbody;
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

/** Save a partial agent update with the on-top overlay over `ctrl` (the row's
 *  `.ac-config-control` slot). Silent so the open card isn't rebuilt on each edit;
 *  caches still update inside `_putAgentField`. Returns the ok boolean. */
async function _saveCfg(agent, updates, ctrl) {
  _ovMarkSaving(ctrl);
  const ok = await _putAgentField(agent, updates, null, { silent: true });
  _ovFlashCheck(ctrl, ok, ok ? '' : 'Save failed');
  return ok;
}

// ── Main render ───────────────────────────────────────────────────────────────

export function _renderConfigTab(body, agent, panelEl, _renderList) {
  const isEditable = agent.source === 'custom';
  const isMock = _isMockAgent(agent);

  // Suggested replies config for user-impersonator
  if (agent.id === 'user-impersonator') {
    _renderSuggestionModeControl(body);
  }

  // ── Identity (Name + Description) ─────────────────────────────────────────
  // Genuine free-text fields → stay fields (not rows). For the mock create card the
  // NAME lives in the card header (the contenteditable create field), so this tab
  // only shows Description — read on create via [data-field="desc"] (view.js
  // `_acceptWebAgentCreate`); it does not persist while the mock is open.
  if (isEditable) {
    const g = _group(body, 'user', 'Identity');
    if (!isMock) _cfgField(g, {
      label: 'Name', field: 'name', value: agent.name || '', placeholder: 'Name this agent…',
      onSave: (val) => _putAgentField(agent, { name: val.trim() }, null, { silent: true }),
    });
    _cfgField(g, {
      label: 'Description', field: 'desc', multiline: true, rows: 2,
      value: agent.description || '', placeholder: 'Add a description…',
      onSave: isMock ? null : (val) => _putAgentField(agent, { description: val }, null, { silent: true }),
    });
  }

  // Local Claude Code agents never reach this tab — they get their own distinct,
  // tab-less card (renderClaudeSettings in ui/main-panel/agents/js/claude-agent.js,
  // diverted from _renderAgentCard in ui/main-panel/agents/js/view.js).

  // ── Template (mock / new-agent only) ──────────────────────────────────────
  if (isMock) {
    const g = _group(body, 'layout-template', 'Template');
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
  // in agent.llm_config.multi_providers). When extend is off, none are shown — the
  // agent must add its own. The "Default" radio picks which model the agent runs
  // by default; the chat user can switch it per conversation (resolved server-side
  // in app/admin/settings.py).
  if (isEditable && !isMock && agent.engine !== 'claude_code' && agent.engine !== 'terminal_chat') {
    const llmCfg = agent.llm_config || { use_default: true };

    const g = _group(body, 'cpu', 'Model');
    const hint = document.createElement('div');
    hint.className = 'ac-hint';
    hint.textContent = _extendLlmToAgents
      ? 'Runs on the app default model(s) shown below (managed by an admin), plus any you add here.'
      : 'This agent uses only its own model(s) — add at least one below.';
    g.appendChild(hint);

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
        const [prov, multi] = await Promise.all([
          fetch('/admin/settings/provider', { headers }).then(r => r.ok ? r.json() : {}).catch(() => ({})),
          fetch('/admin/settings/multi-providers', { headers }).then(r => r.ok ? r.json() : {}).catch(() => ({})),
        ]);
        let rows = Array.isArray(multi.providers) ? multi.providers.filter(x => x.model) : [];
        if (!rows.length && prov.model) {
          rows = [{ provider: prov.provider, base_url: prov.base_url, model: prov.model, enabled: true,
            text_capable: prov.text_capable !== false, image_capable: !!prov.image_capable, image_out_capable: !!prov.image_out_capable,
            high_effort_capable: !!prov.high_effort_capable }];
        }
        // The app's chosen default brain (top-level provider model), used to
        // pre-select the Default radio for an agent that inherits it.
        _appDefaultModel = (prov && prov.model) || '';
        // Layer this agent's per-model opt-outs over the app-wide ceiling. The
        // admin's checked boxes are the MAXIMUM each model may be used for; the
        // agent may use any subset of that. For each inherited model we compute:
        //   • _ceiling — which capability boxes the admin enabled (gated by what
        //     the model can actually do). These are the only editable columns here.
        //   • the effective checkbox state = ceiling AND (this agent's stored choice
        //     if any, else ON) — so an untouched agent inherits everything (opt-out
        //     default), matching today's behaviour, and a narrowed agent shows fewer.
        const ovr = (agentCfg.inherited_overrides && typeof agentCfg.inherited_overrides === 'object') ? agentCfg.inherited_overrides : {};
        return rows.map(r => {
          const textCap = r.text_capable !== false, inCap = !!r.image_capable, outCap = !!r.image_out_capable;
          const ceiling = {
            text:     textCap && (r.enabled !== false),
            image_in: inCap   && !!r.use_for_image,
            image_out: outCap && !!r.use_for_image_out,
            effort:   textCap && !!r.high_effort_capable,
          };
          const key = `${r.provider || ''}|${r.base_url || ''}|${r.model || ''}`;
          const o = ovr[key];
          const pick = (ceilOn, flag) => ceilOn && (o ? !!o[flag] : true);
          return {
            ...r,
            enabled:            pick(ceiling.text, 'enabled'),
            use_for_image:      pick(ceiling.image_in, 'use_for_image'),
            use_for_image_out:  pick(ceiling.image_out, 'use_for_image_out'),
            high_effort_capable: pick(ceiling.effort, 'high_effort_capable'),
            _ceiling: ceiling,
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
          use_default: false, provider: s.provider, base_url: s.base_url, api_key: s.api_key, model: s.model,
          text_capable: s.text_capable, image_capable: s.image_capable, image_out_capable: s.image_out_capable,
          providerConfigs: s.providers,
        });
        await persistLlm();
      },
      // Persist this agent's opt-in/out for ONE inherited app-default model. Stored
      // as a map keyed by provider|base_url|model under llm_config.inherited_overrides;
      // each entry is the agent's chosen capability subset. Absent key = inherit
      // everything (opt-out default). The backend clamps each flag to the live admin
      // ceiling, so a stored choice can never exceed what the app allows. A model with
      // every flag off is dropped from this agent's runtime model list entirely.
      saveInheritedOverride: async (p) => {
        const map = (agentCfg.inherited_overrides && typeof agentCfg.inherited_overrides === 'object')
          ? { ...agentCfg.inherited_overrides } : {};
        const key = p._ovrKey || `${p.provider || ''}|${p.base_url || ''}|${p.model || ''}`;
        map[key] = {
          enabled: p.enabled !== false,
          use_for_image: !!p.use_for_image,
          use_for_image_out: !!p.use_for_image_out,
          high_effort_capable: !!p.high_effort_capable,
        };
        agentCfg.inherited_overrides = map;
        agentCfg.use_default = false;
        await persistLlm();
      },
      saveRoster: async ({ providers }) => {
        agentCfg.use_default = false;
        agentCfg.multi_providers = providers.map(p => ({
          provider: p.provider === '_custom' ? 'custom' : p.provider,
          base_url: p.base_url, api_key: p.api_key, model: p.model,
          enabled: p.enabled,
          text_capable: p.text_capable !== false, image_capable: !!p.image_capable, use_for_image: !!p.use_for_image,
          image_out_capable: !!p.image_out_capable, use_for_image_out: !!p.use_for_image_out,
          high_effort_capable: !!p.high_effort_capable,
        }));
        await persistLlm();
      },
      // Provider presets — try the server first, fall back to hardcoded list
      // mirroring PROVIDER_PRESETS in app/admin/settings.py so the dropdown
      // always shows every known provider.
      loadPresets: async () => {
        try {
          const r = await fetch(apiPath('/admin/settings/providers'));
          if (r.ok) { const d = await r.json(); if (d && Object.keys(d).length) return d; }
        } catch (_) {}
        return {
          openrouter:  { name: 'OpenRouter',      base_url: 'https://openrouter.ai/api/v1' },
          openai:      { name: 'OpenAI',           base_url: 'https://api.openai.com/v1' },
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

  // ── Chat mode (default execution mode for new sessions) ────────────────────
  // New chat sessions with this agent START in this mode (Ask / Plan / Auto).
  // Stored in metadata.default_execution_mode; the chat pill seeds a fresh
  // session from it (ui/chat-side-panel/js/chat-ui.js _applyExecutionMode). Once
  // a session has its own saved mode it keeps it. Shown for everyone (disabled on
  // read-only templates); the Claude Code engine has its own permission model so
  // it's hidden there. Terminal Chat has no LLM config either.
  if (agent.engine !== 'claude_code' && agent.engine !== 'terminal_chat') {
    const g = _group(body, 'sliders-horizontal', 'Chat mode');
    const list = _cfgList(g);
    const MODE_HINT = {
      ask:  'Asks before any write or destructive tool. Research runs freely.',
      plan: 'Researches and proposes a plan first; any change waits for approval.',
      auto: 'Runs all tools without pausing for confirmation.',
    };
    const cur = ['ask', 'plan', 'auto'].includes(agent.default_execution_mode) ? agent.default_execution_mode : 'ask';
    const { ctrl, descEl } = _cfgRow(list, 'Default chat mode', MODE_HINT[cur]);
    const sel = document.createElement('select');
    sel.className = 'ac-input ac-input-sm ac-config-sel'; sel.dataset.field = 'default_execution_mode';
    [['ask', 'Ask'], ['plan', 'Plan'], ['auto', 'Auto']].forEach(([v, t]) => {
      const o = document.createElement('option'); o.value = v; o.textContent = t;
      if (v === cur) o.selected = true; sel.appendChild(o);
    });
    if (!isEditable) sel.disabled = true;
    ctrl.appendChild(sel);
    if (isEditable && !isMock) {
      let confirmed = cur;
      sel.addEventListener('change', async () => {
        const selected = sel.value;
        if (descEl) descEl.textContent = MODE_HINT[selected] || '';
        if (selected === confirmed) return;
        sel.disabled = true;
        const ok = await _saveCfg(agent, { default_execution_mode: selected }, ctrl);
        sel.disabled = false;
        if (ok) { confirmed = selected; }
        else { sel.value = confirmed; if (descEl) descEl.textContent = MODE_HINT[confirmed] || ''; }
      });
    } else if (isEditable) {
      sel.addEventListener('change', () => { if (descEl) descEl.textContent = MODE_HINT[sel.value] || ''; });
    }
  }

  // ── Target device (Remote Control default) ────────────────────────────────
  // Which device in the shared fleet runs this agent's chats by default. A fresh
  // session pre-selects it in the chat Remote Control pill; if that device is
  // offline the chat falls back to "this device", and the user can still pick a
  // different device per session. Stored in metadata.default_target_device (an
  // instance-id; '' = this device) and read by chat-ui.js on session open. The
  // device list comes from window.DevicePicker (shared with the chat pill), loaded
  // async — we paint "This device" (+ the saved value) first, then fill the fleet.
  if (isEditable && !isMock) {
    const g = _group(body, 'monitor-smartphone', 'Target device');
    const list = _cfgList(g);
    const { ctrl, descEl } = _cfgRow(list, 'Default device',
      'New chats run here by default. If it’s offline they fall back to this device; you can still pick another per chat.');
    const sel = document.createElement('select');
    sel.className = 'ac-input ac-input-sm ac-config-sel'; sel.dataset.field = 'default_target_device';
    const cur = (typeof agent.default_target_device === 'string') ? agent.default_target_device : '';
    let confirmed = cur;
    ctrl.appendChild(sel);

    // Rebuild the option list from the live fleet. Keeps a saved-but-offline device
    // as an option so the admin still sees their choice. `null` devices = pre-load
    // paint (This device + saved value only).
    const _rebuild = (devices) => {
      const others = (Array.isArray(devices) ? devices : []).filter(d => d && !d.is_self);
      const val = confirmed;
      sel.innerHTML = '';
      const mk = (value, text, selected) => {
        const o = document.createElement('option'); o.value = value; o.textContent = text;
        if (selected) o.selected = true; sel.appendChild(o);
      };
      mk('', 'This device', !val);
      const seen = new Set(['']);
      others.forEach(d => {
        mk(d.instance_id, (d.label || d.instance_id) + (d.online ? '' : ' (offline)'), d.instance_id === val);
        seen.add(d.instance_id);
      });
      if (val && !seen.has(val)) {
        const lbl = (window.DevicePicker && window.DevicePicker.labelFor) ? window.DevicePicker.labelFor(val) : val;
        mk(val, (lbl && lbl !== val ? lbl : val) + ' (offline)', true);
      }
      if (descEl) {
        descEl.textContent = (!others.length && !val)
          ? 'No other devices in your fleet yet — chats run on this device.'
          : 'New chats run here by default. If it’s offline they fall back to this device; you can still pick another per chat.';
      }
    };
    _rebuild(null);
    try {
      if (window.DevicePicker && window.DevicePicker.load) {
        window.DevicePicker.load().then(_rebuild).catch(() => {});
      }
    } catch (_) { /* fleet list is best-effort; the saved value still shows */ }

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

  // ── Chat Messages (per-agent UI copy) ─────────────────────────────────────
  // Genuine free-text fields → stay fields. Each field's placeholder shows the
  // current app-wide default (from app/defaults/app-prompts.json); leaving a field
  // blank uses that default. Stored in metadata.chat_ui via the agent PUT;
  // consumed in chat by ui/shared/js/app-prompts.js (agentChatMsg).
  if (isEditable && !isMock) {
    const g = _group(body, 'message-square', 'Chat Messages');
    const intro = document.createElement('div'); intro.className = 'ac-hint';
    intro.textContent = 'Customize this agent’s chat greeting and message-box hints. Leave a field blank to use the app-wide default shown as its placeholder.';
    g.appendChild(intro);

    const cu = (agent.chat_ui && typeof agent.chat_ui === 'object') ? agent.chat_ui : {};
    const CHAT_MSG_FIELDS = [
      ['welcome_bubble',         'Welcome message',          true,  2],
      ['new_session_bubble',     'New-session message',      false, 1],
      ['switched_agent_bubble',  'Switched-to message',      false, 1],
      ['pill_placeholder',       'Message-box hint',         false, 1],
      ['pill_locked_placeholder','Locked message-box hint',  false, 1],
    ];
    CHAT_MSG_FIELDS.forEach(([key, label, multiline, rows]) => {
      _cfgField(g, {
        label, field: 'chatui_' + key, value: cu[key] || '',
        multiline, rows, placeholder: chatMsg(key),
        onSave: (val) => _putAgentField(agent, { chat_ui: { [key]: val } }, null, { silent: true }),
      });
    });
  }

  // ── Limits ────────────────────────────────────────────────────────────────
  // Always present (read-only number fields for non-editable templates); save
  // wiring only for editable, non-mock agents. Max Concurrent Tools folds in here.
  {
    const g = _group(body, 'gauge', 'Limits');
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
      if (isEditable && !isMock) {
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
      if (!isMock) {
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

  // ── Memory ──────────────────────────────────────────────────────────────────
  if (isEditable && agent.engine !== 'claude_code' && agent.engine !== 'terminal_chat') {
    const g = _group(body, 'brain', 'Memory');
    const memList = _cfgList(g);
    const mem = _memoryStateFromAgent(agent);
    const memItems = [
      { field: 'memory_recall', label: 'Recall Past Info', checked: mem.recall, hint: 'Search memory before answering.' },
      { field: 'memory_save', label: 'Remember Conversations', checked: mem.save, hint: 'Automatically save a short note after each exchange.' },
    ];
    memItems.forEach(item => {
      const { ctrl } = _cfgRow(memList, item.label, item.hint);
      const wrap = document.createElement('label'); wrap.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
      const cb = document.createElement('input'); cb.type = 'checkbox'; cb.className = 'conn-toggle';
      cb.checked = item.checked; cb.dataset.field = item.field;
      const track = document.createElement('span'); track.className = 'conn-toggle-track';
      wrap.appendChild(cb); wrap.appendChild(track); ctrl.appendChild(wrap);
      if (!isMock) {
        cb.addEventListener('change', async () => {
          cb.disabled = true;
          const otherField = item.field === 'memory_recall' ? 'memory_save' : 'memory_recall';
          const otherCb = memList.querySelector(`[data-field="${otherField}"]`);
          const otherOn = otherCb ? otherCb.checked : false;
          const recall = item.field === 'memory_recall' ? cb.checked : otherOn;
          const save = item.field === 'memory_save' ? cb.checked : otherOn;
          const ok = await _saveCfg(agent, _memoryUpdatesFor(agent, recall, save), ctrl);
          cb.disabled = false;
          if (!ok) cb.checked = !cb.checked;
        });
      }
    });
  }

  // ── Access & triggering ─────────────────────────────────────────────────────
  // User Mode is shown for everyone (disabled on read-only templates); Trigger +
  // Trigger Key are editable-only.
  {
    const g = _group(body, 'zap', 'Access & triggering');
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
      if (isEditable && !isMock) {
        let umConfirmed = umMode;
        sel.addEventListener('change', async () => {
          const selected = sel.value;
          if (descEl) descEl.textContent = umHint(selected);
          if (selected === umConfirmed) return;
          sel.disabled = true;
          const ok = await _saveCfg(agent, { user_mode: selected }, ctrl);
          sel.disabled = false;
          if (ok) { umConfirmed = selected; }
          else { sel.value = umConfirmed; if (descEl) descEl.textContent = umHint(umConfirmed); }
        });
      } else if (isEditable) {
        sel.addEventListener('change', () => { if (descEl) descEl.textContent = umHint(sel.value); });
      }
    }

    // Trigger + Trigger Key
    if (isEditable) {
      const trig = _cfgRow(list, 'Trigger', 'What starts a run for this agent.');
      const triggerSel = document.createElement('select');
      triggerSel.className = 'ac-input ac-input-sm ac-config-sel'; triggerSel.dataset.field = 'trigger_type';
      for (const [val, text] of [['user_input','User Input'],['slash_command','Slash Command'],['tool_call','Tool Call'],['schedule','Schedule'],['webhook','Webhook'],['background','Background']]) {
        const opt = document.createElement('option'); opt.value = val; opt.textContent = text;
        if ((agent.trigger_type || 'user_input') === val) opt.selected = true;
        triggerSel.appendChild(opt);
      }
      trig.ctrl.appendChild(triggerSel);

      const keyRowObj = _cfgRow(list, 'Trigger Key', 'Identifier that fires this trigger.');
      keyRowObj.row.style.display = (agent.trigger_type && agent.trigger_type !== 'user_input') ? '' : 'none';
      const keyInput = document.createElement('input'); keyInput.type = 'text';
      keyInput.className = 'ac-input ac-input-sm ac-config-key';
      keyInput.dataset.field = 'trigger_key'; keyInput.value = agent.trigger_key || '';
      keyInput.placeholder = _triggerKeyPlaceholder(agent.trigger_type || 'user_input');
      keyRowObj.ctrl.appendChild(keyInput);

      triggerSel.addEventListener('change', () => {
        keyRowObj.row.style.display = triggerSel.value !== 'user_input' ? '' : 'none';
        keyInput.placeholder = _triggerKeyPlaceholder(triggerSel.value);
        if (!isMock) _saveCfg(agent, { trigger_type: triggerSel.value }, trig.ctrl);
      });
      if (!isMock) {
        const saveKey = _debounced(() => _saveCfg(agent, { trigger_key: keyInput.value.trim() || null }, keyRowObj.ctrl));
        keyInput.addEventListener('input', saveKey); keyInput.addEventListener('blur', () => saveKey.flush());
      }
    }
  }

  // ── Data (External Data Sources — expandable configurator row) ──────────────
  if (isEditable && !isMock && agent.engine !== 'claude_code' && agent.engine !== 'terminal_chat') {
    const g = _group(body, 'database', 'Data');
    const list = _cfgList(g);
    const rowWrap = document.createElement('div'); rowWrap.className = 'ac-row';
    const head = document.createElement('div'); head.className = 'ac-ability-row';
    const label = document.createElement('span'); label.className = 'ac-ability-label';
    label.innerHTML = `<span class="ac-ability-name">External Data Sources</span>`
      + `<span class="ac-ability-desc">Attach databases, document folders, or domain-restricted web search as agent tools.</span>`;
    const status = document.createElement('span'); status.className = 'ac-ability-status'; status.textContent = 'None';
    const chev = document.createElement('span'); chev.className = 'ac-row-chevron';
    chev.innerHTML = icon('chevron-right', { size: 16 });
    head.appendChild(label); head.appendChild(status); head.appendChild(chev);
    const dsBody = document.createElement('div'); dsBody.className = 'ac-ability-body';
    rowWrap.appendChild(head); rowWrap.appendChild(dsBody);
    head.addEventListener('click', () => rowWrap.classList.toggle('expanded'));
    list.appendChild(rowWrap);
    try {
      mountDataSources(dsBody, agent, app.currentUserId, {
        bare: true,
        onCount: (n) => { status.textContent = n ? `${n} attached` : 'None'; status.classList.toggle('tone-ok', !!n); },
      });
    } catch (e) {
      dsBody.innerHTML = `<div class="ac-hint">Error mounting data sources: ${_esc(e.message)}</div>`;
    }
  }

  // ── Template options (Discoverable — admin-only, for templates) ─────────────
  if (!isEditable && _userIsAdmin && agent.source === 'template') {
    const g = _group(body, 'settings-2', 'Template options');
    const list = _cfgList(g);
    const { ctrl } = _cfgRow(list, 'Discoverable', 'Show this template in the "New Agent" creation dropdown.');
    const wrap = document.createElement('label'); wrap.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
    const cb = document.createElement('input'); cb.type = 'checkbox'; cb.className = 'conn-toggle';
    cb.checked = !!agent.discoverable; cb.dataset.field = 'discoverable';
    const track = document.createElement('span'); track.className = 'conn-toggle-track';
    wrap.appendChild(cb); wrap.appendChild(track); ctrl.appendChild(wrap);
    cb.addEventListener('change', async () => {
      const target = cb.checked; cb.disabled = true;
      _ovMarkSaving(ctrl);
      let ok = false, errMsg = '';
      try {
        const res = await fetch(`/api/v1/agent-templates/config`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ user_id: app.currentUserId, template_id: agent.id, discoverable: target }) });
        const data = await res.json();
        if (res.ok) { agent.discoverable = data.template?.discoverable; const idx = _agents.findIndex(a => a.id === agent.id); if (idx !== -1) _agents[idx].discoverable = agent.discoverable; ok = true; }
        else errMsg = data.detail || 'Save failed';
      } catch (e) { errMsg = e.message; }
      cb.disabled = false;
      _ovFlashCheck(ctrl, ok, ok ? '' : errMsg);
      if (!ok) cb.checked = !target;
    });
  }

  // ── Save as Template (admin) ──────────────────────────────────────────────
  if (isEditable && !isMock && _userIsAdmin) {
    const content = panelEl.querySelector('.agent-detail-content');
    const bar = document.createElement('div'); bar.className = 'agents-save-bar';
    const msg = document.createElement('span'); msg.className = 'agents-save-msg';
    const tplBtn = _btn('Save as Template', 'agents-btn');
    tplBtn.title = 'Save this agent\'s config and prompts as a reusable template (admin only).';
    tplBtn.addEventListener('click', () => _openSaveAsTemplateModal(agent, bar));
    bar.appendChild(tplBtn); bar.appendChild(msg);
    if (content) content.appendChild(bar);
  }
}

// ── Claude Code engine card — MOVED ─────────────────────────────────────────────
// Local Claude Code agents now render their own distinct, tab-less card instead of
// a section of this Config tab — they never reach _renderConfigTab. The settings
// renderer is `renderClaudeSettings` in ui/main-panel/agents/js/claude-agent.js,
// diverted from `_renderAgentCard` in ui/main-panel/agents/js/view.js.

// ── Suggestion mode control (user-impersonator) ─────────────────────────────────

function _renderSuggestionModeControl(body) {
  const g = _group(body, 'sparkles', 'Suggested replies');
  const hint = document.createElement('div'); hint.className = 'ac-hint';
  hint.textContent = 'Grey suggestion chips above the chat box, written in your voice.';
  g.appendChild(hint);

  const list = _cfgList(g);

  const modeRow = _cfgRow(list, 'When to suggest', 'Choose when the suggestion chips appear.');
  const modeSel = document.createElement('select'); modeSel.className = 'ac-input ac-config-sel';
  [['on', 'On — after each reply'], ['scheduler', 'On + refresh while idle'], ['off', 'Off']].forEach(([v, t]) => {
    const o = document.createElement('option'); o.value = v; o.textContent = t; modeSel.appendChild(o);
  });
  modeRow.ctrl.appendChild(modeSel);

  const countRow = _cfgRow(list, 'How many suggestions', 'Number of chips to show.');
  const countSel = document.createElement('select'); countSel.className = 'ac-input ac-config-sel';
  [1, 2, 3, 4, 5].forEach(n => { const o = document.createElement('option'); o.value = String(n); o.textContent = String(n); countSel.appendChild(o); });
  countRow.ctrl.appendChild(countSel);

  fetch('/api/v1/chat/suggestions/config').then(r => r.ok ? r.json() : null).then(cfg => {
    if (!cfg) return;
    if (cfg.mode) modeSel.value = cfg.mode;
    if (cfg.count) countSel.value = String(cfg.count);
  }).catch(() => {});

  async function _save(ctrl) {
    _ovMarkSaving(ctrl);
    let ok = false;
    try {
      const res = await fetch('/api/v1/chat/suggestions/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: modeSel.value, count: parseInt(countSel.value, 10) }) });
      ok = res.ok;
    } catch (_) { ok = false; }
    _ovFlashCheck(ctrl, ok, ok ? '' : 'Save failed');
  }
  modeSel.addEventListener('change', () => _save(modeRow.ctrl));
  countSel.addEventListener('change', () => _save(countRow.ctrl));
}

// ── Save-as-template modal ────────────────────────────────────────────────────

function _openSaveAsTemplateModal(agent, hostBar) {
  const existing = document.getElementById('agents-save-as-template-modal');
  if (existing) existing.remove();

  const backdrop = document.createElement('div');
  backdrop.id = 'agents-save-as-template-modal';
  backdrop.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:9000;display:flex;align-items:center;justify-content:center;';

  const defaultSlug = (agent.name || agent.id || 'template').toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64) || 'template';

  const panel = document.createElement('div');
  panel.style.cssText = 'background:var(--bg-elev);color:var(--fg-1);border:1px solid var(--border);border-radius:8px;padding:18px 20px;width:420px;max-width:92vw;box-shadow:0 8px 24px rgba(var(--shadow-rgb),0.4);';
  panel.innerHTML = `
    <div style="font-size:15px;font-weight:600;margin-bottom:4px;">Save as Template</div>
    <div style="font-size:12px;color:var(--fg-3);margin-bottom:14px;">Snapshots this agent's config and admin-base prompts into a reusable template.</div>
    <label style="display:block;font-size:12px;color:var(--fg-2);margin-bottom:2px;">Template ID (slug)</label>
    <input id="sat-tpl-id" type="text" class="agents-input" style="width:100%;margin-bottom:10px;" value="${_esc(defaultSlug)}" placeholder="my_template">
    <label style="display:block;font-size:12px;color:var(--fg-2);margin-bottom:2px;">Name</label>
    <input id="sat-tpl-name" type="text" class="agents-input" style="width:100%;margin-bottom:10px;" value="${_esc(agent.name || '')}" placeholder="Display name">
    <label style="display:block;font-size:12px;color:var(--fg-2);margin-bottom:2px;">Description</label>
    <textarea id="sat-tpl-desc" class="agents-textarea" rows="2" style="width:100%;margin-bottom:10px;" placeholder="Short description">${_esc(agent.description || '')}</textarea>
    <label style="display:block;font-size:12px;color:var(--fg-2);margin-bottom:2px;">Icon (emoji, optional)</label>
    <input id="sat-tpl-icon" type="text" class="agents-input" style="width:100px;margin-bottom:12px;" value="${_esc(agent.icon || '')}" placeholder="🤖" maxlength="4">
    <label style="display:flex;align-items:center;gap:8px;font-size:13px;color:var(--fg-2);margin-bottom:6px;cursor:pointer;">
      <input id="sat-tpl-discoverable" type="checkbox"> Discoverable in the "New Agent" menu
    </label>
    <div id="sat-tpl-msg" style="font-size:12px;color:var(--danger);min-height:16px;margin-top:8px;"></div>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;">
      <button id="sat-tpl-cancel" class="agents-btn">Cancel</button>
      <button id="sat-tpl-save" class="agents-btn primary">Save Template</button>
    </div>`;
  backdrop.appendChild(panel);
  document.body.appendChild(backdrop);

  const slugIn = panel.querySelector('#sat-tpl-id');
  const nameIn = panel.querySelector('#sat-tpl-name');
  const descIn = panel.querySelector('#sat-tpl-desc');
  const iconIn = panel.querySelector('#sat-tpl-icon');
  const discCb = panel.querySelector('#sat-tpl-discoverable');
  const msgEl  = panel.querySelector('#sat-tpl-msg');
  const saveBtn = panel.querySelector('#sat-tpl-save');
  const cancelBtn = panel.querySelector('#sat-tpl-cancel');

  function _close() { backdrop.remove(); }
  cancelBtn.addEventListener('click', _close);
  backdrop.addEventListener('click', e => { if (e.target === backdrop) _close(); });

  saveBtn.addEventListener('click', async () => {
    msgEl.textContent = ''; msgEl.style.color = 'var(--danger)';
    const slug = (slugIn.value || '').trim().toLowerCase();
    const name = (nameIn.value || '').trim();
    if (!slug || !/^[a-z0-9][a-z0-9_-]{1,63}$/.test(slug)) { msgEl.textContent = 'Template ID must be 2-64 chars: lowercase letters, digits, "_" or "-".'; return; }
    if (!name) { msgEl.textContent = 'Template name is required.'; return; }
    saveBtn.disabled = true; saveBtn.textContent = 'Saving…';
    try {
      const res = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/save-as-template`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ user_id: app.currentUserId, template_id: slug, name, description: (descIn.value || '').trim(), icon: (iconIn.value || '').trim(), discoverable: !!discCb.checked, access_level: 'all' }) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { msgEl.textContent = data.detail || `Save failed (HTTP ${res.status}).`; saveBtn.disabled = false; saveBtn.textContent = 'Save Template'; return; }
      msgEl.style.color = 'var(--success)'; msgEl.textContent = '✓ Template saved.';
      if (hostBar) { const sm = hostBar.querySelector('.agents-save-msg'); if (sm) { sm.textContent = `✓ Saved as template "${slug}"`; sm.className = 'agents-save-msg'; } }
      setTimeout(() => { _close(); _loadAgents().then(agents => { if (_renderList) _renderList(agents); }).catch(() => {}); }, 700);
    } catch (e) { msgEl.textContent = `Error: ${e.message}`; saveBtn.disabled = false; saveBtn.textContent = 'Save Template'; }
  });
  slugIn.focus(); slugIn.select();
}
