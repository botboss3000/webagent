'use strict';

/**
 * Agents — mock (create-in-place) agent: shared create state + the create POST.
 *
 * There is ONE "New Agent" tile. Clicking it opens a single create card whose
 * *type* is chosen by a segmented toggle — WebAgent / Claude / Terminal (built in
 * ui/main-panel/agents/js/view.js → _buildMockTypeToggle). This module owns the
 * cross-render create state (the chosen type, the in-progress name, and the
 * per-engine draft settings) so a card re-render (segment switch / tab switch)
 * never loses what you typed, plus `_postNewAgent`, the POST that turns the form
 * into a real agent.
 *
 * The card chrome + the per-type lower area are built in view.js; the Claude /
 * Terminal draft forms live in their sibling modules (claude-agent.js /
 * terminal-chat-agent.js, exported as renderClaudeCreateBody /
 * renderTerminalChatCreateBody). The accept ("+") dispatch is in view.js
 * (_acceptMockCreate), which has all three modules in scope.
 */

import { app } from '../../../shared/js/state.js';
import { MOCK_AGENT_ID, _expandedAgents, _setActive } from './state.js';
import { authHeaders } from '../../../shared/js/left-login.js';

// Random Lucide icon for the WebAgent "create a new agent" card
const _MOCK_ICON_CHOICES = ['bot','cpu','sparkles','zap','rocket','brain-circuit',
  'wand-2','compass','feather','flame','gem','globe','lightbulb','orbit','atom',
  'telescope','puzzle','shapes','terminal','bird','star','blocks','boxes','wrench'];
let _mockIconName = null;

export function _mockRandomIcon() {
  if (!_mockIconName) _mockIconName = _MOCK_ICON_CHOICES[Math.floor(Math.random() * _MOCK_ICON_CHOICES.length)];
  return _mockIconName;
}

function _resetMockIcon() { _mockIconName = null; }

// ── Cross-render create state ──────────────────────────────────────────────────
// Held in module scope so a card re-render (type switch, tab switch) keeps the
// in-progress form. Reset only after a successful create (_resetMockCreateState).
let _createType = 'webagent';                    // 'webagent' | 'claude' | 'terminal'
let _draftName  = '';                            // the typed agent name
let _engineDraft = { claude: {}, terminal: {} }; // per-engine draft settings

export function _mockCreateType() { return _createType; }
export function _setMockCreateType(t) { _createType = t; }
export function _mockDraftName() { return _draftName; }
export function _mockEngineDraft() { return _engineDraft; }

function _resetMockCreateState() {
  _createType = 'webagent';
  _draftName = '';
  _engineDraft = { claude: {}, terminal: {} };
}

// ── Header name field + accept wiring ───────────────────────────────────────────
// The contenteditable name input and the "+" accept button. `onAccept` is supplied
// by view.js and dispatches on the chosen type; the name field seeds from / writes
// to the persisted draft so it survives re-renders.
export function _wireMockCreateField(card, onAccept) {
  const field = card.querySelector('.agent-mock-create-field');
  if (!field) return;
  const input = field.querySelector('.agent-mock-name-input');
  if (!input) return;
  field.addEventListener('click', e => e.stopPropagation());

  if (_draftName && !(input.textContent || '').trim()) input.textContent = _draftName;
  input.addEventListener('input', () => { _draftName = (input.textContent || '').trim(); });

  const accept = (typeof onAccept === 'function') ? onAccept : () => {};
  const createGo = card.querySelector('.agent-mock-create-go');
  if (createGo) createGo.addEventListener('click', (e) => { e.stopPropagation(); accept(); });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); accept(); }
    else if (e.key === 'Escape') { input.blur(); }
  });
  input.addEventListener('paste', (e) => {
    e.preventDefault();
    const text = (e.clipboardData || window.clipboardData).getData('text').replace(/\s+/g, ' ').trim();
    document.execCommand('insertText', false, text);
  });
}

// ── Create POST ─────────────────────────────────────────────────────────────────
// fallow-ignore-next-line unused-export
export async function _postNewAgent({ name, description = '', templateId = 'default', llmConfig = { use_default: true } }) {
  const res = await fetch('/api/v1/agents', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      user_id: app.currentUserId,
      name,
      description,
      template_id: templateId || 'default',
      llm_config: llmConfig,
    }),
  });
  const data = await res.json();
  if (!res.ok) {
    alert(data.detail || 'Failed to create agent');
    return null;
  }
  const newAgent = data.agent;
  if (!newAgent) return null;

  _expandedAgents.delete(MOCK_AGENT_ID);
  _resetMockIcon();
  _resetMockCreateState();

  window.__agentsSharedData = null;

  _setActive(newAgent.id, 'config');

  // Add surgically via the grid module (registered callback pattern)
  if (typeof window.__agentsSurgicalAddSquare === 'function') {
    window.__agentsSurgicalAddSquare(newAgent);
  }
  if (typeof window.__agentsRebuildDetailRegion === 'function') {
    window.__agentsRebuildDetailRegion();
  }

  app.currentAgentId = newAgent.id;
  try { localStorage.setItem('selectedAgentId', newAgent.id); } catch (_) {}
  if (typeof app.populateAgentSelect === 'function') {
    try { await app.populateAgentSelect(app.currentUserId); } catch (_) {}
  }
  return newAgent;
}
