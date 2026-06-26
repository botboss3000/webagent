'use strict';

/**
 * Agents — data loading (fetch agents, profile, app settings).
 */

import { app } from '../../../shared/js/state.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import {
  _setAgents, _agents,
  _showSystem, _binView, _clonesView,
  _setUserIsAdmin, _setExtendLlmToAgents,
  _expandedAgents, MOCK_AGENT_ID,
} from './state.js';

// ── Data loading ──────────────────────────────────────────────────────────────

export async function _loadProfile() {
  const cached = window.__agentsProfileData;
  if (cached) {
    _setUserIsAdmin(!!cached.is_admin);
    return;
  }
  try {
    const res = await fetch(`/api/v1/user/profile?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() });
    if (res.ok) {
      const data = await res.json();
      _setUserIsAdmin(!!data.is_admin);
    }
  } catch (e) {
    console.warn('agents: could not load profile', e);
  }
}

export async function _loadAgents() {
  try {
    if (!_showSystem && !_binView && !_clonesView) {
      const shared = window.__agentsSharedData;
      if (shared && shared.agents) {
        _setAgents(shared.agents);
        return;
      }
    }
    const params = new URLSearchParams({ user_id: app.currentUserId });
    if (_showSystem) params.set('include_system', 'true');
    if (_binView) params.set('view', 'bin');
    else if (_clonesView) params.set('view', 'clones');
    const res = await fetch(`/api/v1/agents?${params.toString()}`, { headers: authHeaders() });
    if (res.ok) {
      const data = await res.json();
      const agentList = data.agents || [];
      _setAgents(agentList);
      if (!_showSystem && !_binView && !_clonesView) window.__agentsSharedData = data;
    }
  } catch (e) {
    console.warn('agents: could not load agent list', e);
  }
}

export async function _loadAppSettings() {
  const cached = window.__agentsAppSettingsData;
  if (cached) {
    _setExtendLlmToAgents(cached.extend_llm_to_agents !== false);
    return;
  }
  try {
    const res = await fetch('/admin/settings/app', { headers: authHeaders() });
    if (res.ok) {
      const data = await res.json();
      _setExtendLlmToAgents(data.extend_llm_to_agents !== false);
    }
  } catch (e) {
    // non-fatal — keep default true
  }
}

// The WebAgent template dropdown is fetched directly in tab-config.js (the Config
// tab's Template section); the old header-menu helper `_fetchMockTemplates` that
// lived here was removed with the header template menu.

// ── Fetch connections for abilities/tools counting ────────────────────────────

export async function _fetchAbilitiesAndTools(agent) {
  const { _ensureAbilityCatalog, ABILITY_TO_TOOLS, _toolsForAgent } = await import('./state.js');
  await _ensureAbilityCatalog();
  // The Abilities-tab counter must match what the Abilities tab actually shows:
  // the agent's ENABLED abilities, which the tab renders from /connections filtered
  // to `section === 'ability'` (see tab-abilities.js loadConnections). So count the
  // very same set here. The old code instead counted the OAuth ability *registry*
  // (/api/v1/agents/{id}/abilities) — that endpoint only knows the OAuth integration
  // abilities and omits the plugin abilities (git_control, codebase_admin, …) that
  // most agents enable, so the badge read 0 for any agent whose abilities aren't
  // OAuth integrations.
  let connEnabled = new Set();
  try {
    const connRes = await fetch(`/api/v1/agents/${agent.id}/connections?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() });
    if (connRes.ok) {
      const connData = await connRes.json();
      for (const c of (connData.connections || [])) {
        if (c.enabled && c.section === 'ability') connEnabled.add(c.connection_type);
      }
    }
  } catch (e) { /* non-fatal */ }

  const tools = _toolsForAgent(agent, connEnabled);
  return { toolCount: tools.length, abilitiesCount: connEnabled.size };
}

export async function _fetchMembersCount(agent) {
  const res = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/members?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() });
  if (!res.ok) return 0;
  const data = await res.json();
  const admins = data.admins || [];
  const members = data.members || [];
  return admins.length + members.length;
}