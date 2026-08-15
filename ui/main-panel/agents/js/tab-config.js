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

  // ── Template badge (custom agent cloned from a template) ──────────────────
  // Shows which template this agent was created from, with separate buttons to
  // push changes back to the DB template row and to the JSON seed file.
  if (isEditable && !isMock && agent.template_id) {
    const g = _group(body, 'layout-template', 'Template');
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
  // in agent.llm_config.multi_providers). Inherited rows are pure references:
  // taking a role with an own model just sinks them in the sort — no per-agent
  // override copy is ever created. When extend is off, none are shown — the
  // agent must add its own. The chat user can switch the model per conversation
  // (resolved server-side in app/admin/settings.py).
  if (isEditable && !isMock && agent.engine !== 'claude_code' && agent.engine !== 'terminal_chat') {
    const llmCfg = agent.llm_config || { use_default: true };

    const g = document.createElement('div');
    body.appendChild(g);
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
      fetchProviderCatalog: async () => {
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
  // session from it (ui/chat/js/chat-ui.js _applyExecutionMode). Once
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

  // ── Chat Display ──────────────────────────────────────────────────────────
  // Per-agent toggles for what the chat stream shows: intermediate mid-turn
  // messages and tool-call accordions.  Stored in metadata.chat_ui (deep-merged
  // server-side).  Default for both is true (visible).
  {
    const g = _group(body, 'eye', 'Chat Display');
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
      if (isEditable && !isMock) {
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
      if (isEditable && !isMock) {
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
  if (isEditable && !isMock) {
    const g = _group(body, 'monitor-smartphone', 'Target device');
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

    if (!isMock) {
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

    // Resume tail messages — stored in metadata, 0 = use app default (32).
    if (isEditable) {
      const { ctrl } = _cfgRow(list, 'Resume Tail Messages', 'How many recent messages to replay when a session resumes. 0 = app default (32).');
      const inputEl = document.createElement('input'); inputEl.type = 'number';
      inputEl.className = 'ac-input ac-input-sm ac-config-num';
      inputEl.min = 0; inputEl.max = 999; inputEl.step = 1;
      inputEl.value = (agent.resume_tail_messages != null && agent.resume_tail_messages > 0) ? agent.resume_tail_messages : '';
      inputEl.placeholder = '0 (default 32)';
      ctrl.appendChild(_wrapNumberStepper(inputEl));
      if (!isMock) {
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
    if (isEditable && !isMock) {
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
    if (isEditable && !isMock) {
      const trig = _cfgRow(list, 'Trigger', 'What starts a run for this agent.');
      const trigSel = document.createElement('select');
      trigSel.className = 'ac-input ac-input-sm ac-config-sel'; trigSel.dataset.field = 'trigger';
      [['chat', 'Chat'], ['webhook', 'Webhook'], ['schedule', 'Schedule'], ['event', 'Event']].forEach(([v, t]) => {
        const o = document.createElement('option'); o.value = v; o.textContent = t;
        if (v === (agent.trigger || 'chat')) o.selected = true; trigSel.appendChild(o);
      });
      trig.ctrl.appendChild(trigSel);

      const keyRowObj = _cfgRow(list, 'Trigger Key', 'Identifier that fires this trigger.');
      const keyInput = document.createElement('input'); keyInput.type = 'text';
      keyInput.className = 'ac-input ac-input-sm'; keyInput.dataset.field = 'trigger_key';
      keyInput.value = agent.trigger_key || ''; keyInput.placeholder = _triggerKeyPlaceholder(agent);
      keyRowObj.ctrl.appendChild(keyInput);

      let confirmedTrig = agent.trigger || 'chat';
      let confirmedKey = agent.trigger_key || '';
      trigSel.addEventListener('change', async () => {
        const selected = trigSel.value;
        if (selected === confirmedTrig) return;
        trigSel.disabled = true;
        const ok = await _saveCfg(agent, { trigger: selected }, trig.ctrl);
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

  // ── Website Embed (chat widget for external sites) ──────────────────────────
  if (isEditable && !isMock && agent.engine !== 'claude_code' && agent.engine !== 'terminal_chat') {
    _renderWebsiteEmbed(body, agent);
  }

  // ── Data (External Data Sources) ────────────────────────────────────────────
  if (isEditable && !isMock && agent.engine !== 'claude_code' && agent.engine !== 'terminal_chat') {
    const g = _group(body, 'database', 'Data');
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
    const g = _group(body, 'settings-2', 'Template options');
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
