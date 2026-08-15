'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'
// or el.style.color = 'var(--accent)'. New colour? Add a token to the palette there first.

/**
 * Agents — view rendering: grid, squares carousel, agent card, tab bar, detail panel.
 *
 * Combined into one module to avoid circular dependencies between grid rendering
 * and agent card rendering (which call each other via _selectAgent ↔ _renderAgentCard).
 */

import { app } from '../../../shared/js/state.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import { icon, claudeMark, codexMark } from '../../../shared/js/icons.js';
import { fetchAllToolMeta } from '../agent-loop/js/loop-logic.js';
import { NODE_PANEL_INFO } from '../agent-loop/js/loop-node-data.js';
import { LOOP_W, LOOP_NODES, TOGGLEABLE_NODES, renderLoopDiagram } from '../agent-loop/js/loop-diagram.js';
import { advanceDeleteBtn, resetDeleteBtn } from '../../../shared/js/delete-control.js';
import { renderAgentMonetization } from '../billing/agent-billing.js';
import { build as buildAgentAbilityTable } from '../../../shared/js/agent-ability-table.js';
import { sortAgentsForDisplay } from '../../../shared/js/ordering.js';
import {
  _agents, _expandedAgents, _activeId, _setActive,
  _isMockAgent, _createMockAgent, MOCK_AGENT_ID,
  _showSystem, _setShowSystem, _binView, _clonesView, _selectedIds,
  _clearExpanded, _setAgents,
  _isAgentRunning,
  _ensureAbilityCatalog,
  TIER_2_CATEGORIES,
  _memoryStateFromAgent, _memoryUpdatesFor,
  _localLoopLogicObjs, _loopNodeEnabledPersisted,
} from './state.js';
import { _loadProfile, _loadAgents, _loadAppSettings, _fetchAbilitiesAndTools, _fetchMembersCount } from './data.js';
import {
  _esc, _btn, _timeAgo, _displayName, _iconColor,
  _renderAgentIcon, _makeAutosaveCheck, _flashSaved, _markSaving,
  _debounced, _putAgentField, _toggleSave, _triggerKeyPlaceholder,
} from './utils.js';
import {
  _mockRandomIcon, _wireMockCreateField, _postNewAgent,
  _mockCreateType, _setMockCreateType, _mockDraftName, _mockEngineDraft,
} from './mock-agent.js';
import { _openIconPicker } from './icon-picker.js';
// Local Claude Code is its OWN engine: the "Claude" segment of the unified create
// card builds renderClaudeCreateBody, and a created claude_code agent opens into a
// stripped, tab-less card. See ui/main-panel/agents/js/claude-agent.js.
import { _isClaudeAgent, mountClaudeCardTabs, renderClaudeCreateBody, _defaultClaudeName } from './claude-agent.js';
import { _isCodexAgent, renderCodexCreateBody, renderCodexSettings, _defaultCodexName, mountCodexCardTabs } from './codex-agent.js';
// Terminal Chat is its OWN engine: the "Terminal" segment of the create card builds
// renderTerminalChatCreateBody. See ui/main-panel/agents/js/terminal-chat-agent.js.
import { _isTerminalChatAgent, mountTerminalChatCardTabs, renderTerminalChatCreateBody, _defaultTerminalChatName } from './terminal-chat-agent.js';

// ── Regster window-level callbacks for surgical updates ───────────────────────
// These let utility/mock-agent modules trigger re-renders without circular imports.
window.__agentsSurgicalUpdateSquare = _surgicalUpdateSquare;
window.__agentsSurgicalAddSquare = _surgicalAddSquare;
window.__agentsRebuildDetailRegion = _rebuildDetailRegion;
// Full reload+repaint of the grid — used by the Claude card's in-panel delete
// (claude-agent.js) to return to the overview after a soft-delete.
window.__agentsReload = async () => {
  window.__agentsSharedData = null;
  await _loadAgents();
  _renderList();
  _updateBinToolbar();
};

// ── Skeleton ──────────────────────────────────────────────────────────────────

export function _renderSkeleton(count = 6) {
  const grid = document.getElementById('agents-grid');
  if (!grid) return;
  let html = '';
  for (let i = 0; i < count; i++) {
    html += `
      <div class="agent-row">
        <div class="agent-skeleton" aria-hidden="true">
          <div class="sk-icon sk-shimmer"></div>
          <div class="sk-lines">
            <div class="sk-line long sk-shimmer"></div>
            <div class="sk-line short sk-shimmer"></div>
          </div>
        </div>
      </div>`;
  }
  grid.classList.remove('carousel');
  // Preserve the toolbar — _renderSkeleton is called before _renderList, and
  // _renderList saves+re-inserts the toolbar, but if we wipe it here with
  // innerHTML the toolbar is gone for good. Same pattern as _renderList.
  const toolbar = document.getElementById('agents-toolbar');
  grid.innerHTML = `<div class="agents-squares">${html}</div>`;
  if (toolbar) grid.insertBefore(toolbar, grid.firstChild);
}

// ── Main render ───────────────────────────────────────────────────────────────

export function _renderList() {
  const grid = document.getElementById('agents-grid');
  if (!grid) return;

  const activeId = _activeId();
  grid.classList.toggle('carousel', !!activeId);

  // Preserve scroll position across the wholesale rebuild below. #agents-grid is
  // the vertical scroller in carousel mode (its own scrollTop zeroes when its
  // innerHTML is cleared), and .agents-squares is REBUILT as a brand-new node each
  // render (so its scrollTop in grid mode / scrollLeft in carousel mode start at
  // 0). Capture both from the outgoing nodes now and restore onto the fresh ones
  // after — otherwise a background WS refresh or opening a tile snaps the page to
  // the top / the tile row back to the left.
  const _prevGridTop = grid.scrollTop;
  const _oldSquares = grid.querySelector('.agents-squares');
  const _prevSqTop = _oldSquares ? _oldSquares.scrollTop : 0;
  const _prevSqLeft = _oldSquares ? _oldSquares.scrollLeft : 0;

  // Save the toolbar so it can be re-inserted inside the grid after rebuild
  const toolbar = document.getElementById('agents-toolbar');

  grid.innerHTML = '';

  // Re-insert toolbar as the first child of the grid
  if (toolbar) grid.appendChild(toolbar);

  // Squares strip
  const wrap = document.createElement('div');
  wrap.className = 'agents-squares-wrap';
  wrap.innerHTML = `
    <button type="button" class="agents-carousel-chev left" aria-label="Scroll agents left" tabindex="-1">&#10094;</button>
    <button type="button" class="agents-carousel-chev right" aria-label="Scroll agents right" tabindex="-1">&#10095;</button>`;
  const squares = document.createElement('div');
  squares.className = 'agents-squares';
  wrap.appendChild(squares);
  grid.appendChild(wrap);

  // New Agent tile (hidden in bin/clones)
  const mockAgent = _createMockAgent();
  let activeAgent = (!_binView && !_clonesView && activeId === MOCK_AGENT_ID) ? mockAgent : null;

  if (!_binView && !_clonesView) {
    // ONE "New Agent" tile. Its create card carries a WebAgent / Claude / Terminal
    // type chooser (admin only for the two engine types — they're admin-gated at
    // runtime); the old separate "New Claude" / "New Terminal" tiles are gone.
    _renderSquare(squares, mockAgent, activeId === MOCK_AGENT_ID);
    _renderClonesTile(squares);
  }

  if (_agents.length) {
    if (_clonesView) {
      _renderClonesList(squares);
    } else {
      const ordered = sortAgentsForDisplay(_agents, app.currentUserId);
      for (const agent of ordered) {
        const isActive = activeId === agent.id;
        _renderSquare(squares, agent, isActive);
        if (isActive) activeAgent = agent;
      }
    }
  } else if (_binView) {
    const empty = document.createElement('div');
    empty.className = 'agents-bin-empty';
    empty.textContent = 'The recycling bin is empty.';
    squares.appendChild(empty);
  } else if (_clonesView) {
    const empty = document.createElement('div');
    empty.className = 'agents-bin-empty';
    empty.textContent = 'No clones.';
    squares.appendChild(empty);
  }

  // Detail region
  const region = document.createElement('div');
  region.id = 'agents-detail-region';
  grid.appendChild(region);
  if (activeAgent) {
    region.classList.add('open');
    _renderAgentCard(region, activeAgent);
  }

  // Restore the pre-rebuild scroll offsets onto the freshly-created nodes (see the
  // capture near the top of _renderList). `squares` is the new .agents-squares.
  if (_prevGridTop) grid.scrollTop = _prevGridTop;
  if (squares) {
    if (_prevSqTop) squares.scrollTop = _prevSqTop;
    if (_prevSqLeft) squares.scrollLeft = _prevSqLeft;
  }

  _wireSquaresCarousel(wrap);
  _updateBinToolbar();
}

// ── Square rendering ──────────────────────────────────────────────────────────

function _renderSquare(container, agent, isActive) {
  const isMock = _isMockAgent(agent);
  const colorClass = isMock ? 'color-blue' : _iconColor(agent);
  const iconHtml = isMock ? icon('plus', { size: '36px' }) : _renderAgentIcon(agent, '24px');
  const name = isMock ? 'New Agent' : _displayName(agent);

  const card = document.createElement('div');
  card.className = 'agent-card agent-square' + (isActive ? ' activated' : '');
  card.innerHTML = `
    <div class="agent-card-top">
      <div class="agent-card-icon-wrap ${colorClass}">${iconHtml}</div>
      <div class="agent-card-meta">
        <div class="agent-card-name-row">
          <span class="agent-card-name">${_esc(name)}</span>
          ${isMock ? '' : `<span class="agent-status-dot${_isAgentRunning(agent.id) ? ' running' : ''}"></span>`}
        </div>
      </div>
    </div>`;
  card.addEventListener('click', () => _selectAgent(agent));

  if (!isMock) {
    const sqIconWrap = card.querySelector('.agent-card-icon-wrap');
    if (sqIconWrap) {
      let _lpTimer = null;
      sqIconWrap.addEventListener('pointerdown', e => {
        _lpTimer = setTimeout(() => {
          _lpTimer = null;
          e.stopPropagation();
          _openIconPicker(sqIconWrap, agent, null);
        }, 500);
      });
      sqIconWrap.addEventListener('pointerup', () => { if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; } });
      sqIconWrap.addEventListener('pointerleave', () => { if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; } });
    }
  }

  const row = document.createElement('div');
  row.className = 'agent-row';
  row.dataset.agentId = agent.id;

  if (!isMock && agent.source === 'custom') {
    const on = _selectedIds.has(agent.id);
    if (on) row.classList.add('selected');
    const box = document.createElement('div');
    box.className = 'agent-select-box';
    box.title = 'Select';
    box.innerHTML = on ? icon('check', { size: '13px' }) : '';
    box.addEventListener('click', e => {
      e.stopPropagation();
      _toggleSelect(agent.id);
    });
    card.appendChild(box);
  }

  // Chat-message button (bottom-right) — starts a new chat with this agent.
  // Skipped for the mock "New Agent" tile and in the recycling bin.
  if (!isMock && !_binView) {
    const chatBtn = document.createElement('button');
    chatBtn.type = 'button';
    chatBtn.className = 'agent-square-chat-btn';
    chatBtn.title = 'New chat with this agent';
    chatBtn.setAttribute('aria-label', 'New chat with this agent');
    chatBtn.innerHTML = icon('message-square', { size: '13px' });
    chatBtn.addEventListener('click', e => {
      e.stopPropagation();
      _startChatWithAgent(agent);
    });
    card.appendChild(chatBtn);
  }

  row.appendChild(card);
  container.appendChild(row);
}

function _renderClonesTile(container) {
  const card = document.createElement('div');
  card.className = 'agent-card agent-square';
  card.innerHTML = `
    <div class="agent-card-top">
      <div class="agent-card-icon-wrap color-teal">${icon('copy', { size: '24px' })}</div>
      <div class="agent-card-meta">
        <div class="agent-card-name-row">
          <span class="agent-card-name">Clones</span>
        </div>
      </div>
    </div>`;
  card.addEventListener('click', () => _enterClones());
  const row = document.createElement('div');
  row.className = 'agent-row';
  row.appendChild(card);
  container.appendChild(row);
}

function _renderClonesList(container) {
  const groups = {};
  for (const agent of _agents) {
    let orchId = agent.orchestrator_agent_id;
    if (!orchId) {
      try {
        const meta = typeof agent.metadata === 'string' ? JSON.parse(agent.metadata) : (agent.metadata || {});
        orchId = meta.clone_of || null;
      } catch (_) { orchId = null; }
    }
    if (!groups[orchId]) groups[orchId] = { orchId, agents: [] };
    groups[orchId].agents.push(agent);
  }

  for (const [orchId, group] of Object.entries(groups)) {
    let orchName = null;
    const first = group.agents[0];
    if (first.orchestrator_name) {
      orchName = first.orchestrator_name;
    } else {
      try {
        const meta = typeof first.metadata === 'string' ? JSON.parse(first.metadata) : (first.metadata || {});
        orchName = meta.clone_of_name || null;
      } catch (_) {}
    }
    const heading = document.createElement('div');
    heading.className = 'agents-clones-group-heading';
    heading.textContent = orchName ? `From: ${orchName}` : 'Unknown orchestrator';
    container.appendChild(heading);

    for (const agent of group.agents) {
      const status = agent.spawn_status || '—';
      const summary = agent.result_summary || '';
      const colorClass = _iconColor(agent);
      const iconHtml = _renderAgentIcon(agent, '24px');
      const name = _displayName(agent);

      const row = document.createElement('div');
      row.className = 'agent-row';
      row.dataset.agentId = agent.id;

      const card = document.createElement('div');
      card.className = 'agent-card agent-square';
      card.innerHTML = `
        <div class="agent-card-top">
          <div class="agent-card-icon-wrap ${colorClass}">${iconHtml}</div>
          <div class="agent-card-meta">
            <div class="agent-card-name-row">
              <span class="agent-card-name">${_esc(name)}</span>
              <span class="agent-clones-status">${_esc(status)}</span>
            </div>
            ${summary ? `<div class="agent-card-desc">${_esc(summary.length > 120 ? summary.slice(0, 120) + '…' : summary)}</div>` : ''}
          </div>
        </div>`;
      row.appendChild(card);
      container.appendChild(row);
    }
  }
}

// ── Squares carousel wiring ───────────────────────────────────────────────────

// ╔═╗ CAROUSEL-WIRING PATTERN (1st of 4 copies)  ═══════════════════════════════════╗
// ║ Native scroll + chevron affordance. Sisters: _wireTabCarousel below,         ║
// ║ _wirePromptCarousel (tab-prompts.js), _wireSkillsCarousel (claude-skills.js). ║
// ║ If you fix scroll or affordance logic, update ALL FOUR.                      ║
// ╚════════════════════════════════════════════════════════════════════════════════╝
function _wireSquaresCarousel(wrap) {
  const scroller = wrap.querySelector('.agents-squares');
  if (!scroller) return;
  const chevLeft  = wrap.querySelector('.agents-carousel-chev.left');
  const chevRight = wrap.querySelector('.agents-carousel-chev.right');

  const updateAffordances = () => {
    const maxScroll = scroller.scrollWidth - scroller.clientWidth;
    const atStart = scroller.scrollLeft <= 1;
    const atEnd   = scroller.scrollLeft >= maxScroll - 1;
    const overflowing = maxScroll > 1;
    wrap.classList.toggle('can-scroll-left',  overflowing && !atStart);
    wrap.classList.toggle('can-scroll-right', overflowing && !atEnd);
  };

  scroller.addEventListener('scroll', updateAffordances, { passive: true });
    applyRubberBand(scroller);
  requestAnimationFrame(updateAffordances);
  setTimeout(updateAffordances, 120);
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(updateAffordances).observe(scroller);
  }

  const page = () => Math.max(scroller.clientWidth * 0.7, 132);
  if (chevLeft)  chevLeft.addEventListener('click',  e => { e.stopPropagation(); scroller.scrollBy({ left: -page(), behavior: 'smooth' }); });
  if (chevRight) chevRight.addEventListener('click', e => { e.stopPropagation(); scroller.scrollBy({ left:  page(), behavior: 'smooth' }); });
}

// ── Tab carousel wiring ──────────────────────────────────────────────────────

// ╔═╗ CAROUSEL-WIRING PATTERN (2nd of 4 copies — sister of _wireSquaresCarousel) ═╗
// ║ Native scroll + chevron affordance. Differences: CSS selectors only.          ║
// ║ Mirror fixes across all four copies (see the 1st-copy banner above).          ║
// ╚════════════════════════════════════════════════════════════════════════════════╝
function _wireTabCarousel(wrap) {
  if (wrap.dataset.carouselWired === '1') return;
  wrap.dataset.carouselWired = '1';
  const scroller = wrap.querySelector('.agent-card-tabs');
  if (!scroller) return;
  const chevLeft  = wrap.querySelector('.agent-card-tabs-chev.left');
  const chevRight = wrap.querySelector('.agent-card-tabs-chev.right');

  const updateAffordances = () => {
    const maxScroll = scroller.scrollWidth - scroller.clientWidth;
    const atStart = scroller.scrollLeft <= 1;
    const atEnd   = scroller.scrollLeft >= maxScroll - 1;
    const overflowing = maxScroll > 1;
    wrap.classList.toggle('can-scroll-left',  overflowing && !atStart);
    wrap.classList.toggle('can-scroll-right', overflowing && !atEnd);
  };

  scroller.addEventListener('scroll', updateAffordances, { passive: true });
    applyRubberBand(scroller);
  requestAnimationFrame(updateAffordances);
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(updateAffordances).observe(scroller);
  }

  const page = () => Math.max(scroller.clientWidth * 0.7, 120);
  if (chevLeft)  chevLeft.addEventListener('click', e => { e.stopPropagation(); scroller.scrollBy({ left: -page(), behavior: 'smooth' }); });
  if (chevRight) chevRight.addEventListener('click', e => { e.stopPropagation(); scroller.scrollBy({ left: page(), behavior: 'smooth' }); });
}

// ── Selection / toggle ────────────────────────────────────────────────────────

function _selectAgent(agent) {
  if (_expandedAgents.has(agent.id)) {
    _expandedAgents.delete(agent.id);
  } else {
    _setActive(agent.id);
  }
  _renderList();
  _saveViewState();
}

// Start a brand-new chat with this agent in the chat side-panel. Reveals the chat
// panel if it's hidden, then hands off to the chat module's `switchToAgent`
// (set in ui/chat/js/session-init.js) with forceNewSession so the user
// lands in a fresh session bound to this agent. Wired to the chat-message button
// on both the grid square and the expanded agent card.
function _startChatWithAgent(agent) {
  if (!agent || _isMockAgent(agent)) return;
  try {
    if (typeof window.__getChatVisible === 'function' && !window.__getChatVisible()
        && typeof window.__applyChatVisible === 'function') {
      window.__applyChatVisible(true);
    }
  } catch (_) { /* visibility helpers live in index.html — best-effort */ }
  try {
    if (typeof app.switchToAgent === 'function') {
      app.switchToAgent(agent.id, { forceNewSession: true });
    }
  } catch (e) {
    console.warn('agents: start chat with agent failed', e);
  }
}

// ── Surgical square updates ───────────────────────────────────────────────────

function _squaresContainer() {
  return document.querySelector('.agents-squares');
}

function _squareRow(agentId) {
  return document.querySelector(`.agents-squares > .agent-row[data-agent-id="${agentId}"]`);
}

// Repaint an agent's status dot(s) from the live running state — used by the
// `agent_status` WS handler so a run start/stop lights or clears the dot without
// re-rendering the whole grid. Covers both the grid square and, when this agent
// is the open one, its expanded detail card.
export function _applyAgentRunStatus(agentId) {
  if (!agentId) return;
  const running = _isAgentRunning(agentId);
  const dots = [];
  const row = _squareRow(agentId);
  if (row) dots.push(...row.querySelectorAll('.agent-status-dot'));
  if (_activeId() === agentId) {
    const region = document.getElementById('agents-detail-region');
    if (region) dots.push(...region.querySelectorAll('.agent-status-dot'));
  }
  for (const dot of dots) dot.classList.toggle('running', running);
}

function _surgicalAddSquare(agent, opts = {}) {
  const container = _squaresContainer();
  if (!container) return;
  if (_squareRow(agent.id)) return;

  _setAgents([..._agents, agent]);

  _renderSquare(container, agent, _activeId() === agent.id);
  const newRow = container.lastElementChild;
  if (newRow) {
    if (opts.insertBefore && opts.insertBefore !== newRow) {
      container.insertBefore(newRow, opts.insertBefore);
    }
    newRow.classList.add('agent-enter');
    newRow.addEventListener('animationend', () => {
      newRow.classList.remove('agent-enter');
    }, { once: true });
  }
  _syncCarouselState();
}

function _surgicalUpdateSquare(agent) {
  const idx = _agents.findIndex(a => a.id === agent.id);
  if (idx !== -1) _setAgents(_agents.map((a, i) => i === idx ? agent : a));

  const row = _squareRow(agent.id);
  if (!row) return;

  const fragment = document.createElement('div');
  _renderSquare(fragment, agent, _activeId() === agent.id);
  const newRow = fragment.firstElementChild;
  const newCard = newRow ? newRow.querySelector('.agent-card') : null;
  if (!newCard) return;

  const oldCard = row.querySelector('.agent-card');
  if (oldCard) oldCard.replaceWith(newCard);
  else row.prepend(newCard);

  row.classList.remove('agent-pulse');
  void row.offsetWidth;
  row.classList.add('agent-pulse');
  row.addEventListener('animationend', () => { row.classList.remove('agent-pulse'); }, { once: true });

  if (_activeId() === agent.id) _rebuildAgentCard(agent);
}

function _rebuildDetailRegion() {
  const region = document.getElementById('agents-detail-region');
  if (!region) return;
  region.innerHTML = '';
  const activeId = _activeId();
  if (!activeId) { region.classList.remove('open'); _syncSquareActivation(); return; }
  region.classList.add('open');
  if (activeId === MOCK_AGENT_ID) _renderAgentCard(region, _createMockAgent());
  else { const agent = _agents.find(a => a.id === activeId); if (agent) _renderAgentCard(region, agent); }
  _syncSquareActivation();
}

function _syncSquareActivation() {
  const activeId = _activeId();
  document.querySelectorAll('.agents-squares .agent-row').forEach(row => {
    const card = row.querySelector('.agent-card');
    if (!card) return;
    const aid = row.dataset.agentId;
    card.classList.toggle('activated', aid === activeId);
  });
}

function _rebuildAgentCard(agent) {
  const region = document.getElementById('agents-detail-region');
  if (!region) return;
  region.innerHTML = '';
  region.classList.add('open');
  _renderAgentCard(region, agent);
  _syncSquareActivation();
}

function _syncCarouselState() {
  const grid = document.getElementById('agents-grid');
  if (!grid) return;
  const activeId = _activeId();
  grid.classList.toggle('carousel', !!activeId);
  const wrap = grid.querySelector('.agents-squares-wrap');
  if (wrap) _wireSquaresCarousel(wrap);
}

// ── Agent card (expanded) ────────────────────────────────────────────────────

function _renderAgentCard(grid, agent) {
  const isMock = _isMockAgent(agent);

  // Local Claude Code agents get an entirely separate, tab-less card.
  if (!isMock && _isClaudeAgent(agent)) { _renderClaudeAgentCard(grid, agent); return; }
  if (!isMock && _isCodexAgent(agent)) { _renderCodexAgentCard(grid, agent); return; }

  // Terminal Chat agents get an entirely separate, stripped card.
  if (!isMock && _isTerminalChatAgent(agent)) { _renderTerminalChatAgentCard(grid, agent); return; }

  // For the mock create card, the chosen type (WebAgent / Claude / Terminal) drives
  // the header icon + hint, and — for the two engine types — replaces the tabbed
  // lower area with an inline engine settings form (built below).
  const mockType = isMock ? _mockCreateType() : 'webagent';
  const engineMock = isMock && mockType !== 'webagent';

  const card = document.createElement('div');
  card.className = 'agent-card active' + (isMock ? ' agent-card-mock' : '');
  const iconSize = '20px';

  const mockIconColor = mockType === 'claude' ? 'color-claude'
    : mockType === 'codex' ? 'color-codex'
    : mockType === 'terminal' ? 'color-terminal' : 'color-blue';
  const mockIconHtml = mockType === 'claude' ? claudeMark({ size: '30px' })
    : mockType === 'codex' ? codexMark({ size: '30px' })
    : mockType === 'terminal' ? icon('terminal', { size: '30px' })
    : icon(_mockRandomIcon(), { size: '30px' });
  const mockPlaceholder = mockType === 'claude' ? 'Name this Claude agent…'
    : mockType === 'terminal' ? 'Name this Terminal agent…'
    : 'Create a new agent…';
  const mockHint = mockType === 'claude' ? 'Sign in to Claude and set a working folder, then hit +'
    : mockType === 'terminal' ? 'Set the command to run, then hit +'
    : 'Type a name (pick a template below), then hit +';

  const nameCell = isMock
    ? `<div class="agent-mock-create-field">
         <div class="agent-mock-name-input" contenteditable="true" role="textbox"
              aria-label="New agent name" data-placeholder="${_esc(mockPlaceholder)}"
              spellcheck="false"></div>
       </div>`
    : `<span class="agent-card-name">${_esc(_displayName(agent))}</span>`;

  // Engine create forms own their full lower area (no tab strip).
  const tabsWrapHtml = `
    <div class="agent-card-tabs-wrap">
      <button type="button" class="agent-card-tabs-chev left" aria-label="Scroll tabs left" tabindex="-1">&#10094;</button>
      <div class="agent-card-tabs" role="tablist"></div>
      <button type="button" class="agent-card-tabs-chev right" aria-label="Scroll tabs right" tabindex="-1">&#10095;</button>
    </div>`;

  card.innerHTML = `
    <div class="agent-card-top">
      <div class="agent-card-icon-wrap ${isMock ? mockIconColor : _iconColor(agent)}">
        ${isMock ? mockIconHtml : _renderAgentIcon(agent, iconSize)}
      </div>
      <div class="agent-card-meta">
        <div class="agent-card-name-row">
          ${nameCell}
          ${isMock ? '' : `<span class="agent-status-dot${_isAgentRunning(agent.id) ? ' running' : ''}"></span>`}
          ${_userIsAdmin && !isMock && agent.source === 'custom' ? `<span class="agent-edit-hint">long-press to edit</span>` : ''}
        </div>
        ${isMock ? '' : (agent.source === 'custom'
          ? `<div class="agent-card-desc"></div>${_userIsAdmin ? `<span class="agent-edit-hint">long-press to edit</span>` : ''}`
          : (agent.description ? `<div class="agent-card-desc">${_esc(agent.description)}</div>` : ''))}
        ${isMock ? `<div class="agent-card-desc agent-card-mock-hint">${_esc(mockHint)}</div>` : ''}
      </div>
      <div class="agent-card-badge-wrap">
        ${isMock
          ? `<button type="button" class="agent-mock-create-go" title="Create agent">${icon('plus', { size: '22px' })}</button>`
          : `<button type="button" class="agent-card-close-btn" title="Close" aria-label="Close agent card">${icon('x', { size: '16px' })}</button>${_binView ? '' : `<button type="button" class="agent-card-chat-btn" title="New chat with this agent" aria-label="New chat with this agent">${icon('message-square', { size: '16px' })}</button>`}`}
      </div>
    </div>
    ${engineMock ? '' : tabsWrapHtml}
  `;

  // X button (top of the badge column) closes the card and returns to the agent
  // grid. Deletion now lives only on the squares' selection checkboxes + toolbar.
  const closeBtn = card.querySelector('.agent-card-close-btn');
  if (closeBtn) {
    closeBtn.addEventListener('click', e => {
      e.stopPropagation();
      _clearExpanded();
      _renderList();
      _saveViewState();
    });
  }

  const cardChatBtn = card.querySelector('.agent-card-chat-btn');
  if (cardChatBtn) {
    cardChatBtn.addEventListener('click', e => {
      e.stopPropagation();
      _startChatWithAgent(agent);
    });
  }

  const iconWrap = card.querySelector('.agent-card-icon-wrap');
  if (iconWrap && !isMock) {
    iconWrap.dataset.hasLongPress = 'true';
    iconWrap.classList.add('agent-icon-editable');
    iconWrap.title = 'Click to change icon';
    // Single click opens the icon picker so a freshly-created agent's random icon
    // can be fine-tuned without the old double-click.
    iconWrap.addEventListener('click', e => {
      e.stopPropagation();
      _openIconPicker(iconWrap, agent, null);
    });
  }

  // Name + description are inline-editable (double-click / long-press) on custom
  // agents, so they can be renamed without opening the Config tab.
  _wireInlineEdit(card.querySelector('.agent-card-name'), agent, 'name', {
    placeholder: 'Name this agent…',
  });
  _wireInlineEdit(card.querySelector('.agent-card-desc'), agent, 'description', {
    placeholder: 'Add a description…',
  });

  if (isMock) {
    // The WebAgent / Claude / Terminal type chooser sits between the header and the
    // lower area. The two engine types are admin-only (the engines are admin-gated
    // at runtime); a non-admin only ever sees the WebAgent form, so the chooser is
    // hidden for them (nothing to choose).
    if (_userIsAdmin) {
      const top = card.querySelector('.agent-card-top');
      if (top) top.after(_buildMockTypeToggle(mockType));
    }
    _wireMockCreateField(card, () => _acceptMockCreate(card));
  }

  const row = document.createElement('div');
  row.className = 'agent-row expanded';
  row.dataset.agentId = agent.id;
  row.appendChild(card);

  if (engineMock) {
    // Claude / Terminal: the lower area is the inline engine create form (no tabs).
    row.appendChild(_buildEngineCreatePanel(agent, mockType));
  } else {
    const state = _expandedAgents.get(agent.id);
    let panel = null;
    if (state) {
      panel = _buildDetailPanel(agent);
      row.appendChild(panel);
    }

    const cardTabBar = card.querySelector('.agent-card-tabs');
    if (cardTabBar) _populateAgentTabBar(cardTabBar, agent, panel);
    const cardTabWrap = card.querySelector('.agent-card-tabs-wrap');
    if (cardTabWrap) _wireTabCarousel(cardTabWrap);
  }

  grid.appendChild(row);
}

// ── Mock create card: type chooser, engine forms, accept dispatch ──────────────

// The segmented WebAgent / Claude / Terminal toggle at the top of the create card.
// Clicking a segment records the chosen type and re-renders the detail region in
// place (the typed name persists via the mock draft state in mock-agent.js).
function _buildMockTypeToggle(activeType) {
  const seg = document.createElement('div');
  seg.className = 'agent-mock-type-seg';
  seg.addEventListener('click', e => e.stopPropagation());
  const opts = [
    ['webagent', 'WebAgent', icon('sparkles', { size: '14px' })],
    ['claude', 'Claude', claudeMark({ size: '14px' })],
    ['codex', 'Codex', codexMark({ size: '14px' })],
    ['terminal', 'Terminal', icon('terminal', { size: '14px' })],
  ];
  for (const [key, label, ic] of opts) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'agent-mock-type-seg-btn' + (key === activeType ? ' active' : '');
    b.dataset.type = key;
    b.innerHTML = `${ic}<span>${label}</span>`;
    b.addEventListener('click', e => {
      e.stopPropagation();
      if (_mockCreateType() === key) return;
      _setMockCreateType(key);
      _rebuildDetailRegion();
    });
    seg.appendChild(b);
  }
  return seg;
}

// The inline engine (Claude / Terminal) create form panel. Reuses the Claude card's
// panel skin; stashes the form's `collect()` on the panel so the accept handler can
// read the chosen settings.
function _buildEngineCreatePanel(agent, type) {
  const panel = document.createElement('div');
  panel.className = 'agent-detail-panel agent-detail-panel-claude agent-detail-panel-mock-engine';
  panel.dataset.agentId = agent.id;
  const content = document.createElement('div'); content.className = 'agent-detail-content';
  const body = document.createElement('div'); body.className = 'agent-detail-body';
  content.appendChild(body); panel.appendChild(content);
  const drafts = _mockEngineDraft();
  panel._collect = type === 'claude' ? renderClaudeCreateBody(body, drafts.claude)
    : type === 'codex' ? renderCodexCreateBody(body, drafts.codex)
    : renderTerminalChatCreateBody(body, drafts.terminal);
  return panel;
}

// The "+" accept button finalises the create form, dispatching on the chosen type.
function _acceptMockCreate(card) {
  const type = _mockCreateType();
  if (type === 'claude' || type === 'codex' || type === 'terminal') return _acceptEngineCreate(card, type);
  return _acceptWebAgentCreate(card);
}

async function _acceptWebAgentCreate(card) {
  const input = card.querySelector('.agent-mock-name-input');
  const name = (_mockDraftName() || (input ? input.textContent : '') || '').trim();
  if (!name) { if (input) input.focus(); return; }
  // Template + description come from the Config tab (the WebAgent lower area); the
  // mock has no per-agent model UI, so it always inherits the app default model.
  const row = card.closest('.agent-row');
  const panel = row ? row.querySelector('.agent-detail-panel') : null;
  const tplSel = panel ? panel.querySelector('[data-field="template"]') : null;
  const descEl = panel ? panel.querySelector('[data-field="desc"]') : null;
  const goBtn = card.querySelector('.agent-mock-create-go');
  if (goBtn) goBtn.disabled = true;
  try {
    await _postNewAgent({
      name,
      description: descEl ? descEl.value.trim() : '',
      templateId: tplSel ? tplSel.value : 'default',
      llmConfig: { use_default: true },
    });
  } catch (e) {
    console.warn('agents: create agent failed', e);
    alert('Error creating agent: ' + e.message);
    if (goBtn) goBtn.disabled = false;
  }
}

// Create a Claude / Terminal engine agent: clone its template, then apply the
// inline form's draft settings in one silent PUT and re-render the (now distinct)
// engine card. The name falls back to a non-colliding default when left blank.
async function _acceptEngineCreate(card, type) {
  const row = card.closest('.agent-row');
  const panel = row ? row.querySelector('.agent-detail-panel') : null;
  const collect = (panel && typeof panel._collect === 'function') ? panel._collect : null;
  const settings = collect ? collect() : null;
  const input = card.querySelector('.agent-mock-name-input');
  const typed = (_mockDraftName() || (input ? input.textContent : '') || '').trim();
  const name = typed || (type === 'claude' ? _defaultClaudeName() : type === 'codex' ? _defaultCodexName() : _defaultTerminalChatName());
  const templateId = type === 'claude' ? 'local-claude' : type === 'codex' ? 'local-codex' : 'terminal-chat';
  const goBtn = card.querySelector('.agent-mock-create-go');
  if (goBtn) { goBtn.disabled = true; goBtn.innerHTML = icon('loader-2', { size: '22px' }); }
  try {
    const newAgent = await _postNewAgent({ name, templateId });
    if (newAgent && settings) {
      const updates = type === 'claude'
        ? { claude_code: settings.claude_code, default_execution_mode: settings.default_execution_mode }
        : type === 'codex' ? { codex_code: settings.codex_code } : { terminal_chat: settings.terminal_chat };
      await _putAgentField(newAgent, updates, null, { silent: true });
      if (typeof window.__agentsRebuildDetailRegion === 'function') window.__agentsRebuildDetailRegion();
    }
  } catch (e) {
    console.warn('agents: create engine agent failed', e);
    alert('Error creating agent: ' + e.message);
    if (goBtn) { goBtn.disabled = false; goBtn.innerHTML = icon('plus', { size: '22px' }); }
  }
}

// ── Local Claude Code agent card (distinct — no tabs) ──────────────────────────
// A claude_code agent opens into its OWN card: the same chrome (icon, name,
// close/chat) as a normal agent so it sits naturally in the grid, but instead of
// the Config/Prompts/Agent Loop/Abilities tab stack it shows only its Claude
// settings (rendered by renderClaudeSettings in ./claude-agent.js). Created from
// the "Claude" segment of the create card's type chooser, never the WebAgent form.
function _renderClaudeAgentCard(grid, agent) {
  const card = document.createElement('div');
  card.className = 'agent-card active agent-card-claude';
  const iconSize = '20px';
  card.innerHTML = `
    <div class="agent-card-top">
      <div class="agent-card-icon-wrap ${_iconColor(agent)}">${_renderAgentIcon(agent, iconSize)}</div>
      <div class="agent-card-meta">
        <div class="agent-card-name-row">
          <span class="agent-card-name">${_esc(_displayName(agent))}</span>
          <span class="agent-status-dot${_isAgentRunning(agent.id) ? ' running' : ''}"></span>
          <span class="agent-card-claude-tag">Claude Code</span>
        </div>
      </div>
      <div class="agent-card-badge-wrap">
        <button type="button" class="agent-card-close-btn" title="Close" aria-label="Close agent card">${icon('x', { size: '16px' })}</button>
        <button type="button" class="agent-card-chat-btn" title="New chat with this agent" aria-label="New chat with this agent">${icon('message-square', { size: '16px' })}</button>
      </div>
    </div>
    <div class="agent-card-tabs-wrap">
      <div class="agent-card-tabs" role="tablist"></div>
    </div>`;

  const closeBtn = card.querySelector('.agent-card-close-btn');
  if (closeBtn) closeBtn.addEventListener('click', e => {
    e.stopPropagation();
    _clearExpanded();
    _renderList();
    _saveViewState();
  });
  const chatBtn = card.querySelector('.agent-card-chat-btn');
  if (chatBtn) chatBtn.addEventListener('click', e => { e.stopPropagation(); _startChatWithAgent(agent); });

  const iconWrap = card.querySelector('.agent-card-icon-wrap');
  if (iconWrap) {
    iconWrap.classList.add('agent-icon-editable');
    iconWrap.title = 'Click to change icon';
    iconWrap.addEventListener('click', e => { e.stopPropagation(); _openIconPicker(iconWrap, agent, null); });
  }

  // Rename in place — the only normal-card behaviour the Claude card keeps.
  _wireInlineEdit(card.querySelector('.agent-card-name'), agent, 'name', { placeholder: 'Name this agent…' });

  const row = document.createElement('div');
  row.className = 'agent-row expanded';
  row.dataset.agentId = agent.id;
  row.appendChild(card);

  const panel = document.createElement('div');
  panel.className = 'agent-detail-panel agent-detail-panel-claude';
  panel.dataset.agentId = agent.id;
  const content = document.createElement('div');
  content.className = 'agent-detail-content';
  const body = document.createElement('div');
  body.className = 'agent-detail-body';
  content.appendChild(body);
  panel.appendChild(content);
  row.appendChild(panel);

  // Slim two-tab bar (Settings | Skills) lives inside the card; it swaps the panel
  // body between the Claude settings and the Skills tab (claude-skills.js).
  const tabBar = card.querySelector('.agent-card-tabs');
  mountClaudeCardTabs(tabBar, body, agent);

  grid.appendChild(row);
}

// Codex is an alternate headless CLI engine too, but it deliberately keeps a
// smaller card than Claude: credentials are managed by Codex itself, so the only
// per-agent controls are its working folder, model, and optional CLI flags.
function _renderCodexAgentCard(grid, agent) {
  const card = document.createElement('div');
  card.className = 'agent-card active agent-card-claude';
  card.innerHTML = `
    <div class="agent-card-top">
      <div class="agent-card-icon-wrap ${_iconColor(agent)}">${_renderAgentIcon(agent, '20px')}</div>
      <div class="agent-card-meta"><div class="agent-card-name-row"><span class="agent-card-name">${_esc(_displayName(agent))}</span><span class="agent-status-dot${_isAgentRunning(agent.id) ? ' running' : ''}"></span><span class="agent-card-claude-tag">Codex</span></div></div>
      	      <div class="agent-card-badge-wrap"><button type="button" class="agent-card-close-btn" title="Close">${icon('x', { size: '16px' })}</button><button type="button" class="agent-card-chat-btn" title="New chat with this agent">${icon('message-square', { size: '16px' })}</button></div>
	    </div>
	    <div class="agent-card-tabs-wrap">
	      <div class="agent-card-tabs" role="tablist"></div>
	    </div>`;
  card.querySelector('.agent-card-close-btn').addEventListener('click', e => { e.stopPropagation(); _clearExpanded(); _renderList(); _saveViewState(); });
  card.querySelector('.agent-card-chat-btn').addEventListener('click', e => { e.stopPropagation(); _startChatWithAgent(agent); });
  const iconWrap = card.querySelector('.agent-card-icon-wrap');
  iconWrap.classList.add('agent-icon-editable'); iconWrap.title = 'Click to change icon';
  iconWrap.addEventListener('click', e => { e.stopPropagation(); _openIconPicker(iconWrap, agent, null); });
  _wireInlineEdit(card.querySelector('.agent-card-name'), agent, 'name', { placeholder: 'Name this agent…' });
  const row = document.createElement('div'); row.className = 'agent-row expanded'; row.dataset.agentId = agent.id; row.appendChild(card);
  const panel = document.createElement('div'); panel.className = 'agent-detail-panel agent-detail-panel-claude';
  const content = document.createElement('div'); content.className = 'agent-detail-content';
  const body = document.createElement('div'); body.className = 'agent-detail-body'; content.appendChild(body); panel.appendChild(content); row.appendChild(panel);
  const tabBar = card.querySelector('.agent-card-tabs');
  mountCodexCardTabs(tabBar, body, agent);
  grid.appendChild(row);
}

// A terminal_chat agent opens into its OWN card: the same chrome (icon, name,
// close/chat) as a normal agent so it sits naturally in the grid, but instead of
// the Config/Prompts/Agent Loop/Abilities tab stack it shows only its Terminal
// Chat settings (rendered by renderTerminalChatSettings in ./terminal-chat-agent.js).
// Created from the "Terminal" segment of the create card's type chooser, never the
// WebAgent form.
function _renderTerminalChatAgentCard(grid, agent) {
  const card = document.createElement('div');
  card.className = 'agent-card active agent-card-claude';
  const iconSize = '20px';
  card.innerHTML = `
    <div class="agent-card-top">
      <div class="agent-card-icon-wrap ${_iconColor(agent)}">${_renderAgentIcon(agent, iconSize)}</div>
      <div class="agent-card-meta">
        <div class="agent-card-name-row">
          <span class="agent-card-name">${_esc(_displayName(agent))}</span>
          <span class="agent-status-dot${_isAgentRunning(agent.id) ? ' running' : ''}"></span>
          <span class="agent-card-claude-tag">Terminal Chat</span>
        </div>
      </div>
      <div class="agent-card-badge-wrap">
        <button type="button" class="agent-card-close-btn" title="Close" aria-label="Close agent card">${icon('x', { size: '16px' })}</button>
        <button type="button" class="agent-card-chat-btn" title="New chat with this agent" aria-label="New chat with this agent">${icon('message-square', { size: '16px' })}</button>
      </div>
    </div>
    <div class="agent-card-tabs-wrap">
      <div class="agent-card-tabs" role="tablist"></div>
    </div>`;

  const closeBtn = card.querySelector('.agent-card-close-btn');
  if (closeBtn) closeBtn.addEventListener('click', e => {
    e.stopPropagation();
    _clearExpanded();
    _renderList();
    _saveViewState();
  });
  const chatBtn = card.querySelector('.agent-card-chat-btn');
  if (chatBtn) chatBtn.addEventListener('click', e => { e.stopPropagation(); _startChatWithAgent(agent); });

  const iconWrap = card.querySelector('.agent-card-icon-wrap');
  if (iconWrap) {
    iconWrap.classList.add('agent-icon-editable');
    iconWrap.title = 'Click to change icon';
    iconWrap.addEventListener('click', e => { e.stopPropagation(); _openIconPicker(iconWrap, agent, null); });
  }

  // Rename in place.
  _wireInlineEdit(card.querySelector('.agent-card-name'), agent, 'name', { placeholder: 'Name this agent…' });

  const row = document.createElement('div');
  row.className = 'agent-row expanded';
  row.dataset.agentId = agent.id;
  row.appendChild(card);

  const panel = document.createElement('div');
  panel.className = 'agent-detail-panel agent-detail-panel-claude';
  panel.dataset.agentId = agent.id;
  const content = document.createElement('div');
  content.className = 'agent-detail-content';
  const body = document.createElement('div');
  body.className = 'agent-detail-body';
  content.appendChild(body);
  panel.appendChild(content);
  row.appendChild(panel);

  // Slim tab bar (Settings) lives inside the card; it swaps the panel body.
  const tabBar = card.querySelector('.agent-card-tabs');
  mountTerminalChatCardTabs(tabBar, body, agent);

  grid.appendChild(row);
}

// ── Inline name/description editing ──────────────────────────────────────────────
// Turns the card's name span / description div into an in-place editor on
// double-click or long-press (custom agents only). Saves via the same partial-PUT
// path the Config tab uses, so the square + cache stay in sync. Mirrors the
// icon-picker gesture so all three card fields are tweakable without leaving the card.
function _wireInlineEdit(el, agent, field, { placeholder = '' } = {}) {
  if (!el || _isMockAgent(agent) || agent.source !== 'custom') {
    // Non-editable: leave any pre-rendered text as-is.
    return;
  }
  el.dataset.hasLongPress = 'true';   // keep the row-level long-press off this zone
  el.classList.add('agent-inline-editable');
  el.title = 'Double-click or long-press to edit';

  const current = () => (field === 'name' ? _displayName(agent) : (agent.description || ''));
  const raw = () => (field === 'name' ? (agent.name || '') : (agent.description || ''));

  const paint = () => {
    const val = current();
    if (field === 'description' && !raw().trim()) {
      el.textContent = placeholder;
      el.classList.add('agent-inline-placeholder');
    } else {
      el.textContent = val;
      el.classList.remove('agent-inline-placeholder');
    }
  };
  paint();

  let editing = false;
  const begin = () => {
    if (editing) return;
    editing = true;
    const before = raw();
    el.classList.remove('agent-inline-placeholder');
    el.classList.add('editing');
    el.textContent = before;
    el.setAttribute('contenteditable', 'true');
    el.focus();
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);

    const finish = async (commit) => {
      if (!editing) return;
      editing = false;
      el.removeEventListener('keydown', onKey);
      el.removeEventListener('blur', onBlur);
      el.removeAttribute('contenteditable');
      el.classList.remove('editing');
      const val = (el.textContent || '').trim();
      const changed = val !== before.trim();
      // Names can't be blanked; descriptions may be cleared.
      if (commit && changed && (field !== 'name' || val)) {
        await _putAgentField(agent, { [field]: val });
      }
      paint();
    };
    const onKey = (e) => {
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    };
    const onBlur = () => finish(true);
    el.addEventListener('keydown', onKey);
    el.addEventListener('blur', onBlur);
  };

  el.addEventListener('dblclick', e => { e.stopPropagation(); begin(); });
  let lp = null;
  const clearLp = () => { if (lp) { clearTimeout(lp); lp = null; } };
  el.addEventListener('pointerdown', e => {
    lp = setTimeout(() => { lp = null; begin(); }, 500);
  });
  el.addEventListener('pointerup', clearLp);
  el.addEventListener('pointerleave', clearLp);
}

// ── Tab bar ────────────────────────────────────────────────────────────────────

function _populateAgentTabBar(tabBar, agent, panel) {
  const state = _expandedAgents.get(agent.id);
  const isMock = _isMockAgent(agent);
  const activeTab = state ? state.tab : null;
  tabBar.innerHTML = '';
  const tabs = [['config','Config'],['prompts','Prompts'],['test','Agent Loop'],['connections','Abilities']];
  if (!isMock) {
    if (_userIsAdmin) tabs.push(['members','Members']);
    tabs.push(['monetization','Monetization']);
  }

  for (const [key, label] of tabs) {
    const btn = document.createElement('button');
    btn.className = 'agents-detail-tab' + (activeTab === key ? ' active' : '');
    btn.dataset.tab = key;
    if (key === 'connections') {
      btn.innerHTML = `${label} <span class="tab-count-badge tab-count-badge-pending">…</span>`;
    } else if (key === 'members') {
      btn.innerHTML = `${label} <span class="tab-count-badge tab-count-badge-pending">…</span>`;
    } else {
      btn.textContent = label;
    }
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      _setActive(agent.id, key);
      _renderList();
      _saveViewState();
    });
    tabBar.appendChild(btn);
  }

  if (isMock) return;

  _fetchAbilitiesAndTools(agent).then(({ abilitiesCount }) => {
    const connBtn = tabBar.querySelector('.agents-detail-tab[data-tab="connections"]');
    if (connBtn) connBtn.innerHTML = `Abilities <span class="tab-count-badge">${abilitiesCount}</span>`;
  }).catch(() => {
    const connBtn = tabBar.querySelector('.agents-detail-tab[data-tab="connections"]');
    if (connBtn) connBtn.innerHTML = `Abilities <span class="tab-count-badge">0</span>`;
  });

  _fetchMembersCount(agent).then(count => {
    const memBtn = tabBar.querySelector('.agents-detail-tab[data-tab="members"]');
    if (memBtn) memBtn.innerHTML = `Members <span class="tab-count-badge">${count}</span>`;
  }).catch(() => {
    const memBtn = tabBar.querySelector('.agents-detail-tab[data-tab="members"]');
    if (memBtn) memBtn.innerHTML = `Members <span class="tab-count-badge">0</span>`;
  });
}

// ── Detail panel ──────────────────────────────────────────────────────────────

function _buildDetailPanel(agent) {
  const panel = document.createElement('div');
  panel.className = 'agent-detail-panel';
  panel.dataset.agentId = agent.id;

  const content = document.createElement('div');
  content.className = 'agent-detail-content';
  panel.appendChild(content);

  // The mock create card finalises via the header "+" accept button
  // (view.js _acceptMockCreate), so the panel carries no separate "Create" bar.

  const body = document.createElement('div');
  body.className = 'agent-detail-body';
  content.appendChild(body);

  _renderPanelBody(agent, panel);

  return panel;
}

// ── Panel body (tab content) ──────────────────────────────────────────────────

function _renderPanelBody(agent, panelEl) {
  const state = _expandedAgents.get(agent.id);
  let tab = state?.tab || 'config';

  const row = panelEl.closest('.agent-row');
  if (row) {
    row.querySelectorAll('.agent-card-tabs .agents-detail-tab').forEach(t => {
      t.classList.toggle('active', t.dataset.tab === tab);
    });
  }

  const body = panelEl.querySelector('.agent-detail-body');
  if (!body) return;
  body.innerHTML = '';

  const content = panelEl.querySelector('.agent-detail-content');
  const oldSaveBar = content ? content.querySelector(':scope > .agents-save-bar') : null;
  if (oldSaveBar) oldSaveBar.remove();

  if (tab === 'config')            _renderConfigTab(body, agent, panelEl);
  else if (tab === 'prompts')      _renderPromptsTab(body, agent, panelEl);
  else if (tab === 'test')         _renderTestTab(body, agent);
  else if (tab === 'connections')  _renderConnectionsTab(body, agent);
  else if (tab === 'members')      _renderMembersTab(body, agent);
  else if (tab === 'monetization') {
    renderAgentMonetization(body, agent.id);
  }
}

// ── Selection / bin / clones toolbar ──────────────────────────────────────────

function _visibleSelectableIds() {
  if (_clonesView) return [];
  return _agents.filter(a => a.source === 'custom' && !_isMockAgent(a)).map(a => a.id);
}

function _syncSelectionVisuals() {
  document.querySelectorAll('.agents-squares .agent-row').forEach(row => {
    const id = row.dataset.agentId;
    const on = _selectedIds.has(id);
    row.classList.toggle('selected', on);
    const box = row.querySelector('.agent-select-box');
    if (box) box.innerHTML = on ? icon('check', { size: '13px' }) : '';
  });
}

function _toggleSelect(agentId) {
  if (_selectedIds.has(agentId)) _selectedIds.delete(agentId);
  else _selectedIds.add(agentId);
  _syncSelectionVisuals();
  const trashBtn = document.getElementById('agents-trash-btn');
  if (trashBtn) resetDeleteBtn(trashBtn, { size: '16px', title: trashBtn.title });
  _updateBinToolbar();
}

export function _updateBinToolbar() {
  const selectAll  = document.getElementById('agents-select-all');
  const deselectAll = document.getElementById('agents-deselect-all');
  const restoreBtn = document.getElementById('agents-restore-btn');
  const trashBtn   = document.getElementById('agents-trash-btn');
  const backBtn    = document.getElementById('agents-back-btn');
  const title      = document.getElementById('agents-toolbar-title');
  const sub        = document.getElementById('agents-toolbar-sub');

  const n = _selectedIds.size;
  const selectable = _visibleSelectableIds().length;

  const showBack = _binView || _clonesView;
  if (backBtn) backBtn.style.display = showBack ? 'inline-flex' : 'none';
  if (title) {
    title.textContent = showBack ? 'Back to Agents' : 'Agents';
    title.style.cursor = showBack ? 'pointer' : '';
  }
  if (sub) {
    if (_binView) sub.textContent = ' · Recycling bin';
    else if (_clonesView) sub.textContent = ' · Clones';
    else sub.textContent = '';
  }
  if (restoreBtn) { restoreBtn.style.display = _binView ? 'inline-flex' : 'none'; restoreBtn.disabled = (n === 0); }
  if (selectAll)   selectAll.disabled = (selectable === 0 || n >= selectable);
  if (deselectAll) deselectAll.disabled = (n === 0);

  if (trashBtn) {
    if (_binView) {
      trashBtn.title = n ? 'Permanently delete selected' : 'Select agents to delete';
      trashBtn.disabled = (n === 0);
    } else {
      trashBtn.title = n ? 'Move selected to the recycling bin' : 'Open recycling bin';
      trashBtn.disabled = false;
    }
    // Show recycle icon (green) when nothing is selected, trash icon (red as-is) when items are selected
    trashBtn.innerHTML = icon(n > 0 ? 'trash-2' : 'recycle', { size: '16px', style: n > 0 ? '' : 'color:var(--success)' });
  }

  // Show/hide-system-agents eye — only in the normal Agents view (not bin/clones).
  const sysBtn = document.getElementById('agents-system-btn');
  if (sysBtn) {
    sysBtn.style.display = showBack ? 'none' : 'inline-flex';
    sysBtn.classList.toggle('active', _showSystem);
    sysBtn.title = _showSystem ? 'Hide system agents' : 'Show system agents';
    sysBtn.innerHTML = icon(_showSystem ? 'eye' : 'eye-off', { size: '15px' });
  }
}

export function _bindBinToolbar() {
  const actions = document.getElementById('agents-toolbar-actions');
  if (!actions || actions._bound) return;
  actions._bound = true;

  const selectAll   = document.getElementById('agents-select-all');
  const deselectAll = document.getElementById('agents-deselect-all');
  const restoreBtn  = document.getElementById('agents-restore-btn');
  const trashBtn    = document.getElementById('agents-trash-btn');
  const backBtn     = document.getElementById('agents-back-btn');
  const title       = document.getElementById('agents-toolbar-title');

  const resetTrash = () => { if (trashBtn) resetDeleteBtn(trashBtn, { size: '16px', title: trashBtn.title }); };

  if (selectAll) selectAll.addEventListener('click', () => {
    _visibleSelectableIds().forEach(id => _selectedIds.add(id));
    _syncSelectionVisuals(); resetTrash(); _updateBinToolbar();
  });
  if (deselectAll) deselectAll.addEventListener('click', () => {
    _selectedIds.clear();
    _syncSelectionVisuals(); resetTrash(); _updateBinToolbar();
  });
  if (restoreBtn) restoreBtn.addEventListener('click', () => {
    if (_selectedIds.size > 0) _restoreSelected();
  });
  if (backBtn) backBtn.addEventListener('click', () => { if (_binView) _exitBin(); else if (_clonesView) _exitClones(); });
  if (title) title.addEventListener('click', () => { if (_binView) _exitBin(); else if (_clonesView) _exitClones(); });

  // Show/hide system-level agents (agents-only; bound here alongside the toolbar
  // actions). Click on the BUTTON, never the icon node (Lucide swaps it).
  const sysBtn = document.getElementById('agents-system-btn');
  if (sysBtn) sysBtn.addEventListener('click', async () => {
    _setShowSystem(!_showSystem);
    _updateBinToolbar();
    _renderSkeleton();
    await _loadAgents();
    _renderList();
  });

  if (trashBtn) trashBtn.addEventListener('click', () => {
    if (_clonesView) return;
    if (!_binView && _selectedIds.size === 0) { _enterBin(); return; }
    if (_selectedIds.size === 0) return;
    advanceDeleteBtn(trashBtn, {
      size: '16px', spinSize: '16px',
      armTitle: _binView ? 'Click again to permanently delete' : 'Click again to move to the bin',
      onConfirm: () => (_binView ? _permanentDeleteSelected(trashBtn) : _trashSelected(trashBtn)),
    });
  });
}


async function _enterBin() {
  if (_binView) return;
  _setBinView(true);
  _selectedIds.clear();
  _clearExpanded();
  _renderSkeleton();
  await _loadAgents();
  _renderList();
  _updateBinToolbar();
}

async function _exitBin() {
  if (!_binView) return;
  _setBinView(false);
  _selectedIds.clear();
  _clearExpanded();
  _renderSkeleton();
  await _loadAgents();
  _renderList();
  _updateBinToolbar();
}

async function _enterClones() {
  if (_clonesView) return;
  _setClonesView(true);
  _selectedIds.clear();
  _clearExpanded();
  _renderSkeleton();
  await _loadAgents();
  _renderList();
  _updateBinToolbar();
}

async function _exitClones() {
  if (!_clonesView) return;
  _setClonesView(false);
  _selectedIds.clear();
  _clearExpanded();
  _renderSkeleton();
  await _loadAgents();
  _renderList();
  _updateBinToolbar();
}

async function _trashSelected(btn) {
  const ids = [..._selectedIds];
  await Promise.all(ids.map(id =>
    fetch(`/api/v1/agents/${id}?user_id=${encodeURIComponent(app.currentUserId)}`, { method: 'DELETE', headers: { ...authHeaders() } }).catch(() => {})
  ));
  _selectedIds.clear();
  // Remove trashed agents from local state immediately so the grid updates
  // without waiting on the re-fetch (avoids stale-cache / race-condition
  // issues where _loadAgents might return old data).
  _setAgents(_agents.filter(a => !ids.includes(a.id)));
  window.__agentsSharedData = null;
  _renderList();
  if (btn) resetDeleteBtn(btn, { size: '16px', title: 'Open recycling bin' });
  _updateBinToolbar();
  // Background re-fetch to sync with server state.
  _loadAgents().then(() => { _renderList(); _updateBinToolbar(); }).catch(() => {});
  if (typeof app.populateAgentSelect === 'function') { try { await app.populateAgentSelect(app.currentUserId); } catch (_) {} }
}

async function _restoreSelected() {
  const ids = [..._selectedIds];
  await Promise.all(ids.map(id =>
    fetch(`/api/v1/agents/${id}/restore?user_id=${encodeURIComponent(app.currentUserId)}`, { method: 'POST', headers: { ...authHeaders() } }).catch(() => {})
  ));
  _selectedIds.clear();
  window.__agentsSharedData = null;
  await _loadAgents();
  _renderList();
  _updateBinToolbar();
  if (typeof app.populateAgentSelect === 'function') { try { await app.populateAgentSelect(app.currentUserId); } catch (_) {} }
}

async function _permanentDeleteSelected(btn) {
  const ids = [..._selectedIds];
  await Promise.all(ids.map(id =>
    fetch(`/api/v1/agents/${id}?user_id=${encodeURIComponent(app.currentUserId)}&permanent=true`, { method: 'DELETE', headers: { ...authHeaders() } }).catch(() => {})
  ));
  _selectedIds.clear();
  // Remove deleted agents from local state immediately so the bin grid updates
  // without depending on the re-fetch.
  _setAgents(_agents.filter(a => !ids.includes(a.id)));
  window.__agentsSharedData = null;
  _renderList();
  if (btn) resetDeleteBtn(btn, { size: '16px', title: 'Select agents to delete' });
  _updateBinToolbar();
  // Background re-fetch to sync with server state.
  _loadAgents().then(() => { _renderList(); _updateBinToolbar(); }).catch(() => {});
}


// ── View state persistence ────────────────────────────────────────────────────

const _STORAGE_KEY = 'agents_view_state';

function _saveViewState() {
  try {
    const expanded = {};
    for (const [agentId, state] of _expandedAgents) {
      if (agentId === MOCK_AGENT_ID) continue;
      expanded[agentId] = { tab: state.tab };
    }
    localStorage.setItem(_STORAGE_KEY, JSON.stringify({ expanded }));
  } catch (_) {}
}

export function _restoreViewState() {
  try {
    const raw = localStorage.getItem(_STORAGE_KEY);
    if (!raw) return;
    const { expanded } = JSON.parse(raw);
    if (!expanded || typeof expanded !== 'object') return;
    let changed = false;
    for (const [agentId, state] of Object.entries(expanded)) {
      if (_agents.find(a => a.id === agentId)) {
        // 'tools' tab was removed — fall back to config for any stale saved state.
        const tab = (state.tab && state.tab !== 'tools') ? state.tab : 'config';
        _setActive(agentId, tab);
        changed = true;
        break;
      }
    }
    if (changed) _renderList();
  } catch (_) {}
}

// ── Lazy imports for tab modules (imported on first render) ───────────────────
// These are dynamic imports so the view module doesn't statically import every
// tab — tabs are loaded only when first visited.

async function _renderConfigTab(body, agent, panelEl) {
  const mod = await import('./tab-config.js');
  mod._renderConfigTab(body, agent, panelEl, _renderList);
}

async function _renderPromptsTab(body, agent, panelEl) {
  const mod = await import('./tab-prompts.js');
  mod._renderPromptsTab(body, agent, panelEl);
}

async function _renderTestTab(body, agent) {
  const mod = await import('./tab-agent-loop.js');
  mod._renderTestTab(body, agent);
}

async function _renderConnectionsTab(body, agent) {
  const mod = await import('./tab-abilities.js');
  mod._renderConnectionsTab(body, agent);
}

async function _renderMembersTab(body, agent) {
  const mod = await import('./tab-members.js');
  mod._renderMembersTab(body, agent);
}

// ── Re-export for external consumers ──────────────────────────────────────────
// These functions need to be available to other modules that import from agents.js



import { _userIsAdmin, _setBinView, _setClonesView } from './state.js';
import { applyRubberBand } from '../../../shared/js/rubber-band.js';
