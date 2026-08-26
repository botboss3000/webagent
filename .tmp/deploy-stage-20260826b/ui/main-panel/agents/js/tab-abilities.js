'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'
// or el.style.color = 'var(--accent)'. New colour? Add a token to the palette there first.

/**
 * Agents — Abilities/Connections tab.
 * Renders the agent ability table.
 */

import { app } from '../../../shared/js/state.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import {
  _isMockAgent,
  _ensureAbilityCatalog,
  _memoryStateFromAgent,
  _memoryUpdatesFor,
} from './state.js';
import { _esc, _putAgentField } from './utils.js';
import { build as buildAgentAbilityTable } from '../../../shared/js/agent-ability-table.js';

export async function _renderConnectionsTab(body, agent) {
  if (_isMockAgent(agent)) {
    body.innerHTML = '<div style="padding:20px;color:var(--fg-3);font-size:13px;text-align:center;">Save this agent first to configure abilities.</div>';
    return;
  }

  // Build the static skeleton SYNCHRONOUSLY (no awaits first) so the panel never
  // shows blank: the ability table's own spinner appears on the very next line,
  // and the catalog/per-agent fetches run underneath it. The ability table loads
  // progressively — groups first, then toggle statuses, then a group's contents on
  // expand, then an ability's details on its own expand (see agent-ability-table.js).
  body.innerHTML = '';
  const noticeSlot = document.createElement('div'); noticeSlot.className = 'conn-notice-slot';
  body.appendChild(noticeSlot);
  const abilitySection = document.createElement('div');
  abilitySection.className = 'conn-section';
  body.appendChild(abilitySection);
  const abilityContainer = document.createElement('div');
  abilitySection.appendChild(abilityContainer);

  // Warm the catalog (ability icons / tool map) — the table also awaits it, but
  // kicking it off now lets it overlap the table's own setup. Cached after first.
  _ensureAbilityCatalog();

  // Track each ability's last enabled state so a CONFIG-only save (which reuses
  // the same onSave with the unchanged enabled flag) doesn't pop a misleading
  // "<ability> turned ON" notice — that notice is only for real toggle changes.
  const _lastEnabled = {};

  // Lazy, cached per-agent connections loader. The ability table calls this in
  // PARALLEL with rendering the group heads (so the first paint never waits on it),
  // then fills in each group's toggle status once it lands. Returns the ability
  // connections + whether this viewer can edit. The initial render needs only this
  // one light call; the agent's full resolved tool set (/tools) is fetched even
  // later, on first ability-expand, by loadAgentTools below.
  let _connPromise = null;
  let _memoryConnection = null;
  const loadConnections = () => {
    if (!_connPromise) {
      _connPromise = (async () => {
        const res = await fetch(`/api/v1/agents/${agent.id}/connections?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        const connections = (data.connections || []).filter(c => c.section === 'ability');
        const canEdit = (data.user_role || 'member') === 'admin';
        // Memory predates the descriptor-driven ability table. Until this agent
        // has saved the new connection row, seed the ability from its canonical
        // loop/tool state so existing recall/save choices migrate without a
        // database rewrite or a surprising reset to defaults.
        const memory = connections.find(c => c.connection_type === 'memory');
        _memoryConnection = memory || null;
        if (memory && !(memory.config && memory.config.ability_settings)) {
          const state = _memoryStateFromAgent(agent);
          memory.enabled = state.recall || state.save;
          memory.config = {
            ...(memory.config || {}),
            ability_settings: {
              memory_recall: state.recall,
              memory_save: state.save,
            },
          };
        }
        connections.forEach(c => { _lastEnabled[c.connection_type] = !!c.enabled; });
        return { connections, canEdit };
      })();
    }
    return _connPromise;
  };

  // Lazy, cached loader for the agent's resolved tools. The ability table calls
  // this the first time an ability is expanded (and reuses the result for every
  // subsequent expand), so the heavy /tools resolution never blocks the initial
  // list and only runs if the user actually opens an ability.
  let _toolsPromise = null;
  const loadAgentTools = () => {
    if (!_toolsPromise) {
      _toolsPromise = fetch(`/api/v1/agents/${agent.id}/tools?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() })
        .then(r => (r.ok ? r.json() : { tools: [] }))
        .then(d => d.tools || [])
        .catch(() => []);
    }
    return _toolsPromise;
  };

  await buildAgentAbilityTable(abilityContainer, {
    agent,
    connectionsLoader: loadConnections,
    abilitiesByProvider: {},
    userId: app.currentUserId,
    agentToolsLoader: loadAgentTools,
    onSave: async (abilityId, enabled, config) => {
      if (abilityId === 'memory') {
        const savedConfig = (_memoryConnection && _memoryConnection.config) || {};
        const settings = (config && config.ability_settings) || savedConfig.ability_settings || {};
        const isOn = value => value === true || ['1', 'true', 'yes', 'on', 'enabled'].includes(String(value || '').trim().toLowerCase());
        const recallPref = settings.memory_recall == null ? true : isOn(settings.memory_recall);
        const savePref = settings.memory_save == null ? true : isOn(settings.memory_save);
        const recall = !!enabled && recallPref;
        const save = !!enabled && savePref;
        const previous = _memoryStateFromAgent(agent);
        const ok = await _putAgentField(agent, _memoryUpdatesFor(agent, recall, save), null, { silent: true });
        if (!ok) throw new Error('Memory settings could not be saved');
        const persistedConfig = {
          ...savedConfig,
          ...(config || {}),
          ability_settings: {
            memory_recall: recallPref,
            memory_save: savePref,
          },
        };
        try {
          const data = await _saveConnection(agent, _memoryConnection, null, enabled, {
            connection_type: abilityId,
            section: 'ability',
            enabled,
            ...persistedConfig,
          });
          if (_memoryConnection) {
            _memoryConnection.enabled = !!enabled;
            _memoryConnection.config = (data.connection && data.connection.config) || persistedConfig;
          }
        } catch (error) {
          await _putAgentField(agent, _memoryUpdatesFor(agent, previous.recall, previous.save), null, { silent: true });
          throw error;
        }
      } else {
        await _saveConnection(agent, null, null, enabled, { connection_type: abilityId, section: 'ability', enabled, ...(config || {}) });
      }
      if (_lastEnabled[abilityId] !== !!enabled) {
        _lastEnabled[abilityId] = !!enabled;
        _onAbilityToggle(agent, null, null, abilityId, enabled, 'ability');
      }
    },
  });

  const customSection = document.createElement('div');
  customSection.className = 'conn-section soft-abilities-section';
  customSection.style.marginTop = '18px';
  body.appendChild(customSection);
  await _renderSoftAbilities(customSection, agent);
}

async function _renderSoftAbilities(container, agent) {
  container.innerHTML = '<div class="conn-loading" style="padding:12px;color:var(--fg-3);">Loading custom abilities…</div>';
  let abilities = [];
  try {
    const res = await fetch(`/api/v1/agents/${agent.id}/soft-abilities?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    abilities = (await res.json()).abilities || [];
  } catch (err) {
    container.innerHTML = '<div style="padding:12px;color:var(--danger);">Custom abilities could not be loaded.</div>';
    return;
  }

  container.innerHTML = '';
  const header = document.createElement('div');
  header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;padding:10px 2px;';
  header.innerHTML = '<div><strong>Custom abilities</strong><div style="font-size:11px;color:var(--fg-3);margin-top:3px;">Per-agent skills and workflows built from existing tools.</div></div>';
  const add = document.createElement('button');
  add.type = 'button'; add.className = 'btn btn-sm'; add.textContent = '+ New ability';
  header.appendChild(add);
  container.appendChild(header);

  const list = document.createElement('div');
  list.className = 'conn-grid ac-list';
  container.appendChild(list);

  const renderRows = () => {
    list.innerHTML = '';
    if (!abilities.length) {
      list.innerHTML = '<div style="padding:14px;color:var(--fg-3);font-size:12px;border:1px dashed var(--border);border-radius:8px;">No custom abilities yet. Create one to add a reusable skill to this agent.</div>';
      return;
    }
    abilities.forEach(item => list.appendChild(_softAbilityRow(item, agent, async (saved, removed) => {
      if (removed) abilities = abilities.filter(a => a.id !== item.id);
      else {
        const idx = abilities.findIndex(a => a.id === saved.id);
        if (idx >= 0) abilities[idx] = saved; else abilities.push(saved);
      }
      renderRows();
    })));
  };
  add.addEventListener('click', () => {
    abilities.unshift({ id: null, slug: '', display_name: '', description: '', icon: 'sparkles', enabled: true, skill_summary: '', skill_body: '', allowed_tools: [], workflow: {}, credential_schema: [], policy: {}, status: 'ready' });
    renderRows();
    const first = list.firstElementChild;
    if (first) first.classList.add('expanded');
  });
  renderRows();
}

function _softAbilityRow(item, agent, onChanged) {
  const row = document.createElement('div');
  row.className = 'ac-row ac-group';
  const head = document.createElement('div');
  head.className = 'ac-group-head';
  head.style.cursor = 'pointer';
  const title = document.createElement('div');
  title.innerHTML = `<strong>${_esc(item.display_name || 'New custom ability')}</strong><div style="font-size:11px;color:var(--fg-3);">${_esc(item.description || item.skill_summary || 'Configure this ability')}</div>`;
  const badge = document.createElement('span');
  badge.style.cssText = 'font-size:10px;color:var(--fg-3);margin-left:auto;margin-right:10px;';
  badge.textContent = item.id ? `Custom · v${item.version || 1}` : 'Unsaved';
  head.append(title, badge);
  row.appendChild(head);

  const body = document.createElement('div');
  body.className = 'ac-group-body';
  body.style.padding = '12px';
  const field = (label, value, multiline = false) => {
    const wrap = document.createElement('label');
    wrap.style.cssText = 'display:block;font-size:11px;color:var(--fg-3);margin-bottom:10px;';
    wrap.append(document.createTextNode(label));
    const input = document.createElement(multiline ? 'textarea' : 'input');
    input.value = value || '';
    input.style.cssText = 'display:block;width:100%;box-sizing:border-box;margin-top:4px;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--fg);font:inherit;';
    if (multiline) input.rows = 6;
    wrap.appendChild(input); body.appendChild(wrap); return input;
  };
  const name = field('Name', item.display_name);
  const slug = field('Slug (lowercase letters, numbers, underscores)', item.slug);
  const desc = field('Description', item.description);
  const summary = field('When to use it', item.skill_summary);
  const skill = field('Skill instructions', item.skill_body, true);
  const tools = field('Allowed existing tools (comma separated)', (item.allowed_tools || []).join(', '));
  const workflow = field('Workflow JSON (optional)', JSON.stringify(item.workflow || {}, null, 2), true);
  workflow.rows = 4;
  const credentials = field('Credential schema JSON (metadata only; secrets remain in the vault)', JSON.stringify(item.credential_schema || [], null, 2), true);
  credentials.rows = 3;
  const enabledWrap = document.createElement('label');
  enabledWrap.style.cssText = 'display:flex;gap:7px;align-items:center;font-size:12px;margin-bottom:12px;';
  const enabled = document.createElement('input'); enabled.type = 'checkbox'; enabled.checked = item.enabled !== false;
  enabledWrap.append(enabled, document.createTextNode('Enabled and available to the agent'));
  body.appendChild(enabledWrap);
  const actions = document.createElement('div'); actions.style.cssText = 'display:flex;gap:8px;';
  const save = document.createElement('button'); save.type = 'button'; save.className = 'btn btn-sm'; save.textContent = 'Save';
  const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'btn btn-sm'; remove.textContent = item.id ? 'Delete' : 'Cancel';
  actions.append(save, remove); body.appendChild(actions); row.appendChild(body);
  head.addEventListener('click', () => row.classList.toggle('expanded'));

  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      const parsedWorkflow = JSON.parse(workflow.value || '{}');
      const parsedCredentials = JSON.parse(credentials.value || '[]');
      if (!parsedCredentials || !Array.isArray(parsedCredentials)) throw new Error('Credential schema must be a JSON array.');
      const payload = { user_id: app.currentUserId, slug: slug.value.trim(), display_name: name.value.trim(), description: desc.value.trim(), icon: item.icon || 'sparkles', enabled: enabled.checked, skill_summary: summary.value.trim(), skill_body: skill.value.trim(), workflow: parsedWorkflow, allowed_tools: tools.value.split(',').map(v => v.trim()).filter(Boolean), credential_schema: parsedCredentials, policy: item.policy || {}, status: 'ready' };
      const url = `/api/v1/agents/${agent.id}/soft-abilities${item.id ? '/' + encodeURIComponent(item.id) : ''}`;
      const res = await fetch(url, { method: item.id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: JSON.stringify(payload) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = data.detail && data.detail.errors ? data.detail.errors.join(' ') : (data.detail || `HTTP ${res.status}`);
        throw new Error(detail);
      }
      _showAbilitiesNotice(agent, `${payload.display_name} saved`);
      await onChanged(data.ability, false);
    } catch (err) { _showAbilitiesNotice(agent, err.message || 'Save failed'); }
    finally { save.disabled = false; }
  });
  remove.addEventListener('click', async () => {
    if (!item.id) return onChanged(null, true);
    if (!window.confirm(`Delete custom ability “${item.display_name}”?`)) return;
    const res = await fetch(`/api/v1/agents/${agent.id}/soft-abilities/${encodeURIComponent(item.id)}?user_id=${encodeURIComponent(app.currentUserId)}`, { method: 'DELETE', headers: authHeaders() });
    if (res.ok) onChanged(null, true); else _showAbilitiesNotice(agent, 'Delete failed');
  });
  return row;
}

function _showAbilitiesNotice(agent, text) {
  const slot = document.querySelector('.conn-notice-slot');
  if (!slot) return;
  slot.textContent = text;
  slot.style.display = 'block';
  setTimeout(() => { slot.style.display = 'none'; }, 4000);
}

async function _onAbilityToggle(agent, conn, cardEl, abilityId, enabled, source) {
  _showAbilitiesNotice(agent, `${abilityId} turned ${enabled ? 'ON' : 'OFF'}`);
}

async function _saveConnection(agent, conn, cardEl, enabled, _extConfig) {
  // Persist a connection / ability toggle to the backend. Two callers share this:
  //   • the OAuth connection cards  → conn + cardEl are real, _extConfig = { enabled }
  //   • the agent ability table     → conn = null, the ability id + its settings
  //                                    arrive via _extConfig.connection_type / config
  // Without this PUT the toggles only flipped in the DOM and reverted on refresh.
  // Throws on a non-OK response so the caller can revert the switch.
  const ext = _extConfig || {};
  const connectionType = (conn && conn.connection_type) || ext.connection_type;
  if (!connectionType) throw new Error('No connection type to save');

  // Build the config payload from _extConfig minus the control keys.
  const config = {};
  for (const [k, v] of Object.entries(ext)) {
    if (k === 'enabled' || k === 'connection_type' || k === 'section') continue;
    config[k] = v;
  }

  const res = await fetch(
    `/api/v1/agents/${agent.id}/connections/${connectionType}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ user_id: app.currentUserId, enabled, config }),
    }
  );
  let data = {};
  try { data = await res.json(); } catch (e) { /* tolerate a non-JSON error body */ }
  if (!res.ok) throw new Error(data.detail || `Save failed (HTTP ${res.status})`);

  // Reflect the saved state back into the in-memory connection + card, so a tab
  // re-render without a full reload stays consistent with what was persisted.
  if (conn) {
    conn.enabled = enabled;
    if (data.connection && data.connection.config) conn.config = data.connection.config;
  }
  if (cardEl) cardEl.classList.toggle('enabled', enabled);
  return data;
}
