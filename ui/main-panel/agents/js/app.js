'use strict';

/**
 * Agents — application orchestrator.
 *
 * Exports the public API: initAgents, startAgents, stopAgents.
 * Wires the top-level init flow, agent builder bar, and view state restoration.
 */

import { app } from '../../../shared/js/state.js';
import {
  _agents, _expandedAgents, _setActive,
  MOCK_AGENT_ID, _isMockAgent, _setAgentRunning,
} from './state.js';
import { _loadProfile, _loadAgents, _loadAppSettings } from './data.js';
import {
  _renderSkeleton, _renderList,
  _bindBinToolbar, _updateBinToolbar,
  _restoreViewState, _applyAgentRunStatus,
} from './view.js';

// ── Init ──────────────────────────────────────────────────────────────────────

async function initAgents() {
  if (!app.currentUserId) return;

  const tabSelect = document.getElementById('main-tab-select');
  if (tabSelect && tabSelect.value !== 'agents') return;

  if (_agents.length === 0) _renderSkeleton();

  await Promise.all([_loadProfile(), _loadAgents(), _loadAppSettings()]);
  _renderList();
  _bindBinToolbar();
  _updateBinToolbar();
  _restoreViewState();
}

export function startAgents() {
  if (!app.currentUserId) return;
  initAgents();
}

export function stopAgents() {
  // nothing persistent to tear down
}

// ── Global expand handler (called from chat-header dropdown) ──────────────────

window.expandAgent = function expandAgent(agentId) {
  if (!agentId) return;
  _setActive(agentId);
  let attempts = 0;
  const tryRender = () => {
    attempts += 1;
    if (_agents.find(a => a.id === agentId)) {
      _renderList();
      requestAnimationFrame(() => {
        const region = document.getElementById('agents-detail-region');
        if (region && region.scrollIntoView) region.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
      return;
    }
    if (attempts < 40) setTimeout(tryRender, 50);
  };
  tryRender();
};

// ── Refresh handler (called from chat-header after reorder) ───────────────────

app.refreshAgentsOrder = async function refreshAgentsOrder() {
  if (!app.currentUserId) return;
  if (!document.getElementById('agents-grid')) return;
  await _loadAgents();
  _renderList();
};

// ── Live agent lifecycle/status handler (called from WebSocket) ───────────────
// Fired when an agent is created / trashed / permanently deleted / restored in
// ANOTHER tab or device, or starts/stops a run. Keeps the grid in sync without a
// manual refresh. Lifecycle changes are infrequent, so we reload+rerender (the
// same primitive refreshAgentsOrder uses) — correct for every view (main / bin /
// clones / system) without per-view surgical branching. Status changes are
// frequent, so they only repaint the affected card's dot.
app.onAgentLifecycleEvent = async function onAgentLifecycleEvent(event) {
  if (!event || !app.currentUserId) return;

  const gridMounted = !!document.getElementById('agents-grid');

  if (event.type === 'agent_status') {
    // Status dots only matter when the grid is actually on screen.
    if (!gridMounted) return;
    const changed = _setAgentRunning(event.agent_id, event.session_id, event.status === 'running');
    if (changed) _applyAgentRunStatus(event.agent_id);
    return;
  }

  // Lifecycle: created / trashed / deleted / restored. ALWAYS invalidate the
  // cross-page cache so the grid is correct the next time it's opened — even if
  // the Agents page isn't mounted right now (e.g. the user is in chat while an
  // agent with agent-management abilities creates a new agent). Only reload +
  // rerender when the grid is on screen.
  window.__agentsSharedData = null;
  if (!gridMounted) return;
  await _loadAgents();
  _renderList();
  _updateBinToolbar();
};