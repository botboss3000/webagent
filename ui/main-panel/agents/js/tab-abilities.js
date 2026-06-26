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
} from './state.js';
import { _esc } from './utils.js';
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
  const loadConnections = () => {
    if (!_connPromise) {
      _connPromise = (async () => {
        const res = await fetch(`/api/v1/agents/${agent.id}/connections?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        const connections = (data.connections || []).filter(c => c.section === 'ability');
        const canEdit = (data.user_role || 'member') === 'admin';
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
      await _saveConnection(agent, null, null, enabled, { connection_type: abilityId, section: 'ability', enabled, ...(config || {}) });
      if (_lastEnabled[abilityId] !== !!enabled) {
        _lastEnabled[abilityId] = !!enabled;
        _onAbilityToggle(agent, null, null, abilityId, enabled, 'ability');
      }
    },
  });
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