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
import { authHeaders, isAnonGuest, showLeftOverlay } from '../../../shared/js/left-login.js';
import { icon, claudeMark, codexMark } from '../../../shared/js/icons.js';
import { advanceDeleteBtn, resetDeleteBtn } from '../../../shared/js/delete-control.js';
import { sortAgentsForDisplay } from '../../../shared/js/ordering.js';
import { kvRead, kvWrite } from '../../../shared/js/kv-ui-state.js';
import { invalidateAgentListCaches } from './agent-cache.js';
import {
  _agents, _expandedAgents, _activeId, _setActive,
  _isMockAgent, _createMockAgent, MOCK_AGENT_ID,
  _showSystem, _setShowSystem, _binView, _clonesView, _selectedIds,
  _clearExpanded, _setAgents,
  _isAgentRunning,
  _ensureAbilityCatalog,
  TIER_2_CATEGORIES,
  _localLoopLogicObjs, _loopNodeEnabledPersisted,
  _mockAgentConfigPayload,
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
import { setSessionsAgentContext } from '../sessions/js/sessions-page.js';

// ── Regster window-level callbacks for surgical updates ───────────────────────
// These let utility/mock-agent modules trigger re-renders without circular imports.
window.__agentsSurgicalUpdateSquare = _surgicalUpdateSquare;
window.__agentsSurgicalAddSquare = _surgicalAddSquare;
window.__agentsRebuildDetailRegion = _rebuildDetailRegion;
// Full reload+repaint of the grid — used by the Claude card's in-panel delete
// (claude-agent.js) to return to the overview after a soft-delete.
window.__agentsReload = async () => {
  window.__agentsSharedData = null;
  invalidateAgentListCaches();
  await _loadAgents();
  _renderList();
  _updateBinToolbar();
};

// ── Recycling-bin notice toast ────────────────────────────────────────────────
// A brief fixed toast (mirrors the Sessions/automations delete toasts) with an
// optional action button. Used by the Config tab's delete row (tab-config.js) to
// tell the user the agent — and all of its sessions — are in the recycling bin.
let _agentsNoticeTimer = null;
function _hideAgentsNotice(toast) {
  if (!toast) toast = document.getElementById('agents-notice-toast');
  if (!toast) return;
  toast.classList.remove('agents-notice-visible');
  clearTimeout(_agentsNoticeTimer);
}
window.__agentsNotice = (msg, opts = {}) => {
  let toast = document.getElementById('agents-notice-toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'agents-notice-toast';
    toast.setAttribute('role', 'status');
    toast.setAttribute('aria-live', 'polite');
    document.body.appendChild(toast);
  }
  toast.innerHTML = '';
  const text = document.createElement('span');
  text.textContent = msg;
  toast.appendChild(text);
  if (opts.action && typeof opts.action.fn === 'function') {
    const act = document.createElement('button');
    act.type = 'button';
    act.className = 'agents-notice-action';
    act.textContent = opts.action.label || 'Open';
    act.addEventListener('click', () => { opts.action.fn(); _hideAgentsNotice(toast); });
    toast.appendChild(act);
  }
  toast.classList.toggle('agents-notice-error', opts.kind === 'error');
  toast.classList.add('agents-notice-visible');
  clearTimeout(_agentsNoticeTimer);
  _agentsNoticeTimer = setTimeout(() => _hideAgentsNotice(toast), 4000);
};
// Lets the toast's "Open bin" action jump into the bin view (module-private here).
window.__agentsOpenBin = () => _enterBin();

// ── Skeleton ──────────────────────────────────────────────────────────────────

export function _renderSkeleton(count) {
  const grid = document.getElementById('agents-grid');
  if (!grid) return;
  // Exactly two skeleton squares (matching the static phantom — see agents.html).
  // Each card's lines still get a fresh random width so every load paints a
  // slightly different skeleton.
  const rand = (min, max) => Math.floor(Math.random() * (max - min + 1)) + min;
  const n = (typeof count === 'number') ? count : 2;
  let html = '';
  for (let i = 0; i < n; i++) {
    const w1 = rand(55, 92), w2 = rand(30, 50);
    html += `
      <div class="agent-row">
        <div class="agent-skeleton" aria-hidden="true">
          <div class="sk-icon sk-shimmer"></div>
          <div class="sk-lines">
            <div class="sk-line sk-shimmer" style="width:${w1}%"></div>
            <div class="sk-line sk-shimmer" style="width:${w2}%"></div>
          </div>
        </div>
      </div>`;
  }
  grid.classList.remove('carousel');
  // Skeleton state marker: while it's set (until _renderList removes it), the
  // real toolbar stays display:none and the shimmer stand-in shows instead —
  // same deal the static phantom gets from .agents-phantom. Needed because
  // _syncMergedLayout strips .agents-phantom below, which would otherwise
  // expose the real buttons mid-skeleton.
  grid.classList.add('agents-skeleton');
  // The stand-in mirrors the toolbar's SHAPE per view so takeover is
  // zero-layout-shift: overview = eye + trash (two 28px icons); bin = back +
  // wide Restore + trash; clones = back + eye + trash (three icons).
  const phantomBtns = _binView
    ? `<div class="agents-tb-phantom sk-shimmer" style="width:26px"></div>
       <div class="agents-tb-phantom agents-tb-phantom-wide sk-shimmer"></div>
       <div class="agents-tb-phantom sk-shimmer"></div>`
    : _clonesView
      ? `<div class="agents-tb-phantom sk-shimmer"></div>
         <div class="agents-tb-phantom sk-shimmer"></div>
         <div class="agents-tb-phantom sk-shimmer"></div>`
      : `<div class="agents-tb-phantom sk-shimmer"></div>
         <div class="agents-tb-phantom sk-shimmer"></div>`;
  // Preserve the toolbar — _renderSkeleton is called before _renderList, and
  // _renderList saves+re-inserts the toolbar, but if we wipe it here with
  // innerHTML the toolbar is gone for good. Same pattern as _renderList.
  const toolbar = document.getElementById('agents-toolbar');
  // Same wrap structure as _renderList so the skeleton carousel sits exactly
  // where the live squares will (the merged layout styles the WRAP, not the
  // grid — no wrap means the row would hug the top). The toolbar (vertical
  // action stack) is re-inserted inside the same top row, right of the strip,
  // beside the shimmer .agents-toolbar-phantom stand-in.
  grid.innerHTML = `
    <div class="agents-top-row">
      <div class="agents-squares-wrap">
        <button type="button" class="agents-carousel-chev left" aria-label="Scroll agents left" tabindex="-1">&#10094;</button>
        <button type="button" class="agents-carousel-chev right" aria-label="Scroll agents right" tabindex="-1">&#10095;</button>
        <div class="agents-squares">${html}</div>
      </div>
      <div class="agents-toolbar-phantom" aria-hidden="true">
        ${phantomBtns}
      </div>
    </div>`;
  const topRow = grid.querySelector('.agents-top-row');
  if (topRow && toolbar) topRow.appendChild(toolbar);
  _syncMergedLayout();
}

// ── Main render ───────────────────────────────────────────────────────────────

export function _renderList() {
  const grid = document.getElementById('agents-grid');
  if (!grid) return;

  // The Sessions tab may currently own #sessions-section. Move it out before
  // rebuilding the grid so the single table instance is never destroyed.
  setSessionsAgentContext(null);

  const activeId = _activeId();
  const allAgentsActive = !_binView && !_clonesView && !activeId;
  grid.classList.toggle('carousel', !!activeId || allAgentsActive);
  // Skeleton state ends here: the real toolbar (hidden by the .agents-skeleton
  // rules) becomes visible again, and the shimmer stand-in is gone with the
  // rebuild below.
  grid.classList.remove('agents-skeleton');

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

  // Save the toolbar so it can be re-inserted after rebuild — it sits to the
  // RIGHT of the squares strip as a vertical action stack (same top row).
  const toolbar = document.getElementById('agents-toolbar');

  grid.innerHTML = '';

  // Top row: squares strip + toolbar. Flex row — the strip flexes to fill,
  // the toolbar is a fixed-width vertical stack on the right.
  const topRow = document.createElement('div');
  topRow.className = 'agents-top-row';

  // Squares strip
  const wrap = document.createElement('div');
  wrap.className = 'agents-squares-wrap';
  wrap.innerHTML = `
    <button type="button" class="agents-carousel-chev left" aria-label="Scroll agents left" tabindex="-1">&#10094;</button>
    <button type="button" class="agents-carousel-chev right" aria-label="Scroll agents right" tabindex="-1">&#10095;</button>`;
  const squares = document.createElement('div');
  squares.className = 'agents-squares';
  wrap.appendChild(squares);
  topRow.appendChild(wrap);

  // Re-insert the toolbar as the last child of the top row (right side)
  if (toolbar) topRow.appendChild(toolbar);

  grid.appendChild(topRow);

  // New Agent tile (hidden in bin/clones)
  const mockAgent = _createMockAgent();
  let activeAgent = (!_binView && !_clonesView && activeId === MOCK_AGENT_ID) ? mockAgent : null;

  if (!_binView && !_clonesView) {
    _renderAllAgentsSquare(squares, allAgentsActive);
    // NOTE — there is no dedicated "New Agent" tile anymore: creation lives on
    // the All Agents square's corner + button (see _renderAllAgentsSquare).
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
  if (allAgentsActive) {
    region.classList.add('open');
    _renderAllAgentsCard(region);
  } else if (activeAgent) {
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
  _syncMergedLayout();
}

// ── Square rendering ──────────────────────────────────────────────────────────

function _renderAllAgentsSquare(container, isActive) {
  const card = document.createElement('div');
  card.className = 'agent-card agent-square agent-square-all' + (isActive ? ' activated' : '');
  card.innerHTML = `
    <div class="agent-card-top">
      <div class="agent-card-icon-wrap color-purple">${icon('layout-grid', { size: '24px' })}</div>
      <div class="agent-card-meta"><div class="agent-card-name-row">
        <span class="agent-card-name">All Agents</span>
      </div></div>
    </div>`;
  card.addEventListener('click', () => {
    _clearExpanded();
    setSessionsAgentContext(null);
    _renderList();
    _saveViewState();
  });
  // "New agent" + button (bottom-right). The dedicated "New Agent" tile was
  // folded into this square, so creation now lives here: this opens the SAME
  // create card the tile did (activeId → MOCK_AGENT_ID → the mock create card
  // renders in the detail region). Mirrors the .agent-square-chat-btn overlay.
  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'agent-square-add-btn';
  addBtn.title = 'Create a new agent';
  addBtn.setAttribute('aria-label', 'Create a new agent');
  addBtn.innerHTML = icon('plus', { size: '14px' });
  addBtn.addEventListener('click', e => {
    e.stopPropagation();
    _selectAgent(_createMockAgent());
  });
  card.appendChild(addBtn);
  const row = document.createElement('div');
  row.className = 'agent-row';
  row.dataset.view = 'all-agents';
  row.appendChild(card);
  container.appendChild(row);
}

// The account-wide card mirrors a normal expanded agent card, but its two tabs
// operate on global data: the shared sessions table and the app-level ability
// policy table. The ability table remains visible to every caller; only app
// admins receive editable controls.
let _allAgentsTab = 'sessions';

function _renderAllAgentsCard(container) {
  const card = document.createElement('div');
  card.className = 'agent-card active agent-card-tabs-only';
  card.innerHTML = `
    <div class="agent-card-tabs-wrap">
      <div class="agent-card-tabs" role="tablist"></div>
    </div>`;

  const panel = document.createElement('div');
  panel.className = 'agent-detail-panel';
  const content = document.createElement('div');
  content.className = 'agent-detail-content';
  const body = document.createElement('div');
  body.className = 'agent-detail-body agent-detail-body-all-agents';
  content.appendChild(body);
  panel.appendChild(content);

  const renderTab = () => {
    setSessionsAgentContext(null);
    body.innerHTML = '';
    card.querySelectorAll('.agents-detail-tab').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.tab === _allAgentsTab);
    });
    if (_allAgentsTab === 'sessions') {
      body.classList.add('all-agents-sessions-body');
      setSessionsAgentContext(null, body);
      return;
    }
    body.classList.remove('all-agents-sessions-body');
    import('../../../shared/js/admin-ability-table.js').then(({ build }) => build(body, {
      abilityStates: {},
      canEdit: _userIsAdmin,
      showSearch: true,
    })).catch((error) => {
      console.error('agents: all-agents abilities tab failed', error);
      body.textContent = 'Unable to load abilities.';
    });
  };

  const tabBar = card.querySelector('.agent-card-tabs');
  [['sessions', 'Sessions'], ['abilities', 'Abilities']].forEach(([key, label]) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'agents-detail-tab';
    btn.dataset.tab = key;
    btn.textContent = label;
    btn.addEventListener('click', (event) => {
      event.stopPropagation();
      if (_allAgentsTab === key) return;
      _allAgentsTab = key;
      renderTab();
    });
    tabBar.appendChild(btn);
  });

  const row = document.createElement('div');
  row.className = 'agent-row expanded agent-row-all-agents';
  row.appendChild(card);
  row.appendChild(panel);
  container.appendChild(row);
  renderTab();
}

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
  const startedAt = typeof performance === 'undefined' ? 0 : performance.now();
  if (_expandedAgents.has(agent.id)) {
    _expandedAgents.delete(agent.id);
  } else {
    _setActive(agent.id, _isMockAgent(agent) ? 'config' : 'sessions');
  }
  _renderList();
  _saveViewState();
  const section = document.getElementById('sessions-section');
  if (section && startedAt) {
    section.dataset.agentFilterDurationMs = (performance.now() - startedAt).toFixed(1);
    section.dataset.agentFilterId = _expandedAgents.has(agent.id) ? agent.id : '';
  }
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
    const isAllAgents = row.dataset.view === 'all-agents';
    card.classList.toggle('activated', isAllAgents ? !activeId : aid === activeId);
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
  const allAgentsActive = !_binView && !_clonesView && !activeId;
  grid.classList.toggle('carousel', !!activeId || allAgentsActive);
  const wrap = grid.querySelector('.agents-squares-wrap');
  if (wrap) _wireSquaresCarousel(wrap);
  _syncMergedLayout();
}

// ── Merged sessions layout ───────────────────────────────────────────────────
// The Agents page embeds the Sessions table below the squares strip (see
// agents.html #sessions-section). The section is visible only in the normal
// overview — not the bin, not clones, and not while an agent card is open. The
// class on #tab-agents lets the CSS switch the two layouts atomically.
function _syncMergedLayout() {
  const tab = document.getElementById('tab-agents');
  if (!tab) return;
  // Phantom takeover: drop the static shell's .agents-phantom markers so the
  // REAL merged-layout CSS (#tab-agents.agents-show-sessions) governs from here
  // on — and, crucially, so the sessions section hides again when a card opens.
  const _grid = document.getElementById('agents-grid');
  if (_grid) _grid.classList.remove('agents-phantom');
  const _section = document.getElementById('sessions-section');
  if (_section) _section.classList.remove('agents-phantom');
  // The account-wide sessions table now lives inside the All Agents Sessions
  // tab, so the old separate merged overview is retired.
  const merged = false;
  tab.classList.toggle('agents-show-sessions', merged);
  if (merged) {
    _wireMergedScroll();      // one-time listeners (idempotent)
    _measureMergedScroll();   // (re)measure the table height + (re)apply the lock
  } else {
    tab.classList.remove('sessions-locked');
  }
}

// ── Merged sessions two-phase scroll ─────────────────────────────────────────
// Overview scroll happens in two phases so the overscroll (rubber-band) stays on
// the data table rather than the whole page:
//   1. The page (#tab-agents) scrolls the agents strip off the top while the
//      sessions toolbar sticks (position:sticky, top:0).
//   2. Once the strip is gone we flip .sessions-locked: the table wrap becomes
//      its own scroller and the wheel is routed to it. Scrolling back up at the
//      table's top hands the wheel back to the page so the strip can return.
let _mergedScrollWired = false;

function _measureMergedScroll() {
  const tab = document.getElementById('tab-agents');
  if (!tab || !tab.classList.contains('agents-show-sessions')) return;
  const toolbar = document.getElementById('sessions-toolbar');
  const wrap = document.getElementById('sessions-table-wrap');
  if (toolbar && wrap && tab.clientHeight > 0) {
    const h = tab.clientHeight - toolbar.offsetHeight;
    if (h > 0) wrap.style.height = h + 'px';
  }
  const grid = document.getElementById('agents-grid');
  if (grid) {
    tab.classList.toggle('sessions-locked', tab.scrollTop >= grid.offsetHeight - 2);
  }
  _syncTableOverscroll();
}

// Touch handoff for the table scroller. In phase 1 (not locked) the table wrap
// has touch-action: pan-x, so a vertical swipe inside it chains to the PAGE and
// pushes the strip away. Once locked, the table owns vertical scrolling; we keep
// overscroll contained at the bottom (rubber-band stays in the table) but relax
// it to `auto` at the table's top so a downward swipe chains back to the page and
// reveals the strip again — mirroring the wheel routing in _wireMergedScroll.
function _syncTableOverscroll() {
  const tab = document.getElementById('tab-agents');
  const wrap = document.getElementById('sessions-table-wrap');
  if (!tab || !wrap) return;
  if (!tab.classList.contains('sessions-locked')) {
    wrap.style.overscrollBehaviorY = '';   // phase 1: CSS default (auto) chains to page
    return;
  }
  wrap.style.overscrollBehaviorY = (wrap.scrollTop <= 1) ? 'auto' : 'contain';
}

function _wireMergedScroll() {
  if (_mergedScrollWired) return;
  const tab = document.getElementById('tab-agents');
  const toolbar = document.getElementById('sessions-toolbar');
  const wrap = document.getElementById('sessions-table-wrap');
  if (!tab || !toolbar || !wrap) return;
  _mergedScrollWired = true;

  tab.addEventListener('scroll', _measureMergedScroll, { passive: true });
  wrap.addEventListener('scroll', _syncTableOverscroll, { passive: true });
  window.addEventListener('resize', _measureMergedScroll);
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(_measureMergedScroll).observe(tab);
    new ResizeObserver(_measureMergedScroll).observe(toolbar);
  }

  // Route the wheel between the page scroller and the table scroller.
  wrap.addEventListener('wheel', (e) => {
    let dx = e.deltaX || 0;
    let dy = e.deltaY || 0;
    if (e.deltaMode === 1) { dx *= 20; dy *= 20; }
    else if (e.deltaMode === 2) { dx *= wrap.clientHeight; dy *= wrap.clientHeight; }
    if (Math.abs(dx) > Math.abs(dy)) return;  // horizontal intent → native

    const locked = tab.classList.contains('sessions-locked');
    if (!locked) {
      // Strip still visible: the PAGE owns vertical wheel — scroll it.
      tab.scrollTop += dy;
      _measureMergedScroll();
      e.preventDefault();
    } else if (wrap.scrollTop <= 0 && dy < 0) {
      // Table pinned at its top, scrolling up: hand back to the page so the
      // strip can return into view.
      tab.scrollTop += dy;
      _measureMergedScroll();
      e.preventDefault();
    }
  }, { passive: false });

  requestAnimationFrame(_measureMergedScroll);
  setTimeout(_measureMergedScroll, 120);
}

// ── Agent card (expanded) ────────────────────────────────────────────────────

function _renderAgentCard(grid, agent) {
  const isMock = _isMockAgent(agent);
  const anonymousCreatePreview = isMock && _isAnonymousCreatePreview();

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
  card.className = 'agent-card active' + (isMock ? ' agent-card-mock' : ' agent-card-tabs-only');

  const mockIconColor = mockType === 'claude' ? 'color-claude'
    : mockType === 'codex' ? 'color-codex'
    : mockType === 'terminal' ? 'color-terminal' : 'color-blue';
  const mockIconHtml = mockType === 'webagent' && agent.icon ? icon(agent.icon, { size: '30px' })
    : mockType === 'claude' ? claudeMark({ size: '30px' })
    : mockType === 'codex' ? codexMark({ size: '30px' })
    : mockType === 'terminal' ? icon('terminal', { size: '30px' })
    : icon(_mockRandomIcon(), { size: '30px' });
  const mockPlaceholder = anonymousCreatePreview ? 'Register an account to name and create an agent…'
    : mockType === 'claude' ? 'Name this Claude agent…'
    : mockType === 'terminal' ? 'Name this Terminal agent…'
    : 'Create a new agent…';
  const mockHint = anonymousCreatePreview
    ? 'Preview the agent configuration below. Register or sign in when you are ready to create it.'
    : mockType === 'claude' ? 'Sign in to Claude and set a working folder, then hit +'
    : mockType === 'terminal' ? 'Set the command to run, then hit +'
    : 'Type a name (pick a template below), then hit +';

  const nameCell = `<div class="agent-mock-create-field">
    <div class="agent-mock-name-input" contenteditable="true" role="textbox"
         aria-label="New agent name" data-placeholder="${_esc(mockPlaceholder)}"
         spellcheck="false"></div>
  </div>`;

  // Engine create forms own their full lower area (no tab strip).
  const tabsWrapHtml = `
    <div class="agent-card-tabs-wrap">
      <button type="button" class="agent-card-tabs-chev left" aria-label="Scroll tabs left" tabindex="-1">&#10094;</button>
      <div class="agent-card-tabs" role="tablist"></div>
      <button type="button" class="agent-card-tabs-chev right" aria-label="Scroll tabs right" tabindex="-1">&#10095;</button>
    </div>`;

  card.innerHTML = isMock ? `
    <div class="agent-card-top">
      <div class="agent-card-icon-wrap ${mockIconColor}">${mockIconHtml}</div>
      <div class="agent-card-meta">
        <div class="agent-card-name-row">
          ${nameCell}
        </div>
        <div class="agent-card-desc agent-card-mock-hint">${_esc(mockHint)}</div>
      </div>
      <div class="agent-card-badge-wrap">
        <button type="button" class="agent-mock-create-go${anonymousCreatePreview ? ' registration-gated' : ''}"
                title="${anonymousCreatePreview ? 'Register or sign in to create an agent' : 'Create agent'}"
                aria-label="${anonymousCreatePreview ? 'Register or sign in to create an agent' : 'Create agent'}"
                ${anonymousCreatePreview ? 'aria-disabled="true" aria-haspopup="dialog"' : ''}>${icon('plus', { size: '22px' })}</button>
      </div>
    </div>
    ${engineMock ? '' : tabsWrapHtml}
  ` : tabsWrapHtml;

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

function _isAnonymousCreatePreview() {
  return isAnonGuest() || String(app.currentUserId || '').startsWith('anon_');
}

function _showMockRegistrationGate(card) {
  document.querySelectorAll('.agent-create-registration-popover').forEach(el => el.remove());
  const button = card.querySelector('.agent-mock-create-go');
  if (!button) return;

  const popover = document.createElement('div');
  popover.className = 'agent-create-registration-popover';
  popover.setAttribute('role', 'dialog');
  popover.setAttribute('aria-label', 'Registration required');
  popover.innerHTML = `
    <strong>Register to create this agent</strong>
    <span>Anonymous visitors can explore the configuration form, but only registered accounts can create and manage agents.</span>
    <div class="agent-create-registration-actions">
      <button type="button" class="agent-create-registration-dismiss">Not now</button>
      <button type="button" class="agent-create-registration-open">Register or sign in</button>
    </div>`;

  const badge = button.closest('.agent-card-badge-wrap') || card;
  badge.appendChild(popover);
  popover.querySelector('.agent-create-registration-dismiss')?.addEventListener('click', e => {
    e.stopPropagation();
    popover.remove();
    button.focus();
  });
  popover.querySelector('.agent-create-registration-open')?.addEventListener('click', e => {
    e.stopPropagation();
    popover.remove();
    showLeftOverlay();
  });
}

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
  if (_isAnonymousCreatePreview()) {
    _showMockRegistrationGate(card);
    return;
  }
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
  const config = _mockAgentConfigPayload();
  const description = descEl ? descEl.value.trim() : (config.description || '');
  delete config.description;
  const goBtn = card.querySelector('.agent-mock-create-go');
  if (goBtn) goBtn.disabled = true;
  try {
    await _postNewAgent({
      name,
      description,
      templateId: tplSel ? tplSel.value : 'default',
      config,
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

// ── Local engine agent tabs ───────────────────────────────────────────────────
// Persisted engine agents use the same tabs-only chrome as WebAgents. Their
// engine-specific Config bodies are mounted by the corresponding module.
function _renderClaudeAgentCard(grid, agent) {
  const card = document.createElement('div');
  card.className = 'agent-card active agent-card-claude agent-card-tabs-only';
  card.innerHTML = `
    <div class="agent-card-tabs-wrap">
      <div class="agent-card-tabs" role="tablist"></div>
    </div>`;

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
  card.className = 'agent-card active agent-card-claude agent-card-tabs-only';
  card.innerHTML = `
    <div class="agent-card-tabs-wrap">
      <div class="agent-card-tabs" role="tablist"></div>
    </div>`;
  const row = document.createElement('div'); row.className = 'agent-row expanded'; row.dataset.agentId = agent.id; row.appendChild(card);
  const panel = document.createElement('div'); panel.className = 'agent-detail-panel agent-detail-panel-claude';
  const content = document.createElement('div'); content.className = 'agent-detail-content';
  const body = document.createElement('div'); body.className = 'agent-detail-body'; content.appendChild(body); panel.appendChild(content); row.appendChild(panel);
  const tabBar = card.querySelector('.agent-card-tabs');
  mountCodexCardTabs(tabBar, body, agent);
  grid.appendChild(row);
}

// Terminal Chat keeps its engine-specific Config body under the shared tabs-only
// chrome used by the other persisted agent types.
function _renderTerminalChatAgentCard(grid, agent) {
  const card = document.createElement('div');
  card.className = 'agent-card active agent-card-claude agent-card-tabs-only';
  card.innerHTML = `
    <div class="agent-card-tabs-wrap">
      <div class="agent-card-tabs" role="tablist"></div>
    </div>`;

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

  // Slim tab bar lives above the panel body.
  const tabBar = card.querySelector('.agent-card-tabs');
  mountTerminalChatCardTabs(tabBar, body, agent);

  grid.appendChild(row);
}

// ── Tab bar ────────────────────────────────────────────────────────────────────

function _populateAgentTabBar(tabBar, agent, panel) {
  const state = _expandedAgents.get(agent.id);
  const isMock = _isMockAgent(agent);
  const activeTab = state ? state.tab : null;
  tabBar.innerHTML = '';
  const tabs = isMock
    ? [['config','Config'],['prompts','Prompts'],['test','Agent Loop'],['connections','Abilities']]
    : [['sessions','Sessions'],['config','Config'],['prompts','Prompts'],['test','Agent Loop'],['connections','Abilities']];
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
  let tab = state?.tab || (_isMockAgent(agent) ? 'config' : 'sessions');

  const row = panelEl.closest('.agent-row');
  if (row) {
    row.querySelectorAll('.agent-card-tabs .agents-detail-tab').forEach(t => {
      t.classList.toggle('active', t.dataset.tab === tab);
    });
  }

  const body = panelEl.querySelector('.agent-detail-body');
  if (!body) return;
  setSessionsAgentContext(null);
  body.innerHTML = '';

  const content = panelEl.querySelector('.agent-detail-content');
  const oldSaveBar = content ? content.querySelector(':scope > .agents-save-bar') : null;
  if (oldSaveBar) oldSaveBar.remove();

  if (tab === 'sessions')          setSessionsAgentContext(agent.id, body);
  else if (tab === 'config')       _renderConfigTab(body, agent, panelEl);
  else if (tab === 'prompts')      _renderPromptsTab(body, agent, panelEl);
  else if (tab === 'test')         _renderTestTab(body, agent);
  else if (tab === 'connections')  _renderConnectionsTab(body, agent);
  else if (tab === 'members')      _renderMembersTab(body, agent);
  else if (tab === 'monetization') {
    import('../billing/agent-billing.js').then(({ renderAgentMonetization }) => {
      if (_expandedAgents.get(agent.id)?.tab === 'monetization') {
        renderAgentMonetization(body, agent.id);
      }
    }).catch((e) => console.error('agents: monetization tab failed', e));
  }
}

// ── Selection / bin / clones toolbar ──────────────────────────────────────────

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
  const restoreBtn = document.getElementById('agents-restore-btn');
  const trashBtn   = document.getElementById('agents-trash-btn');
  const backBtn    = document.getElementById('agents-back-btn');

  const n = _selectedIds.size;

  const showBack = _binView || _clonesView;
  if (backBtn) backBtn.style.display = showBack ? 'inline-flex' : 'none';
  if (restoreBtn) { restoreBtn.style.display = _binView ? 'inline-flex' : 'none'; restoreBtn.disabled = (n === 0); }

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

  const restoreBtn  = document.getElementById('agents-restore-btn');
  const trashBtn    = document.getElementById('agents-trash-btn');
  const backBtn     = document.getElementById('agents-back-btn');

  const resetTrash = () => { if (trashBtn) resetDeleteBtn(trashBtn, { size: '16px', title: trashBtn.title }); };

  if (restoreBtn) restoreBtn.addEventListener('click', () => {
    if (_selectedIds.size > 0) _restoreSelected();
  });
  if (backBtn) backBtn.addEventListener('click', () => { if (_binView) _exitBin(); else if (_clonesView) _exitClones(); });

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
  invalidateAgentListCaches();
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
  invalidateAgentListCaches();
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
  invalidateAgentListCaches();
  _renderList();
  if (btn) resetDeleteBtn(btn, { size: '16px', title: 'Select agents to delete' });
  _updateBinToolbar();
  // Background re-fetch to sync with server state.
  _loadAgents().then(() => { _renderList(); _updateBinToolbar(); }).catch(() => {});
}


// ── View state persistence ────────────────────────────────────────────────────

const _STORAGE_KEY = 'agents_view_state';

function _saveViewState() {
  const expanded = {};
  for (const [agentId, state] of _expandedAgents) {
    if (agentId === MOCK_AGENT_ID) continue;
    expanded[agentId] = { tab: state.tab };
  }
  kvWrite('agents:view-state', _STORAGE_KEY, { expanded });
}

export function _restoreViewState() {
  try {
    const saved = kvRead('agents:view-state', _STORAGE_KEY);
    if (!saved) return;
    const parsed = typeof saved === 'string' ? JSON.parse(saved) : saved;
    const { expanded } = parsed;
    if (!expanded || typeof expanded !== 'object') return;
    let changed = false;
    for (const [agentId, state] of Object.entries(expanded)) {
      if (_agents.find(a => a.id === agentId)) {
        // Removed/blank tabs fall back to the agent's Sessions landing view.
        const tab = (state.tab && state.tab !== 'tools') ? state.tab : 'sessions';
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
