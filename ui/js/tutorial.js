'use strict';

/**
 * Tutorial hints — numbered hover popovers attached to features on each page.
 *
 * State lives in localStorage under "tutorialPrefs":
 *   { globalEnabled: boolean, pages: { [pageId]: boolean } }
 *
 * Pages map 1:1 to the main-tab values from index.html (admin-tools,
 * autoagent, agents, stream, flow, loop-visual, account). The chat-panel
 * hints render alongside whichever main tab is active when the chat side is
 * visible.
 *
 * Each hint has a target CSS selector. If the selector doesn't resolve at
 * render time (page not loaded, element hidden, user lacks permission, etc.)
 * the badge is silently skipped. Badges are absolutely positioned in a
 * single fixed overlay layer and repositioned on scroll/resize.
 */

const STORAGE_KEY = 'tutorialPrefs';
const TICK_MS = 500;

const PAGE_LABELS = {
  'admin-tools': 'Admin Tools',
  'autoagent':   'Dashboard',
  'agents':      'Agents',
  'stream':      'Stream',
  'flow':        'Flow',
  'loop-visual': 'Runtime Loop',
  'account':     'Manage Account',
  'chat':        'Chat Panel',
};

const HINTS = {
  'admin-tools': [
    { selector: '.files-strip-view[data-view="explorer"]', title: 'File explorer',
      body: 'Browse the project tree. Click any file to open it in the editor; right-click for rename, delete and other actions.' },
    { selector: '.files-strip-view[data-view="git"]', title: 'Source control',
      body: 'See changed files, commit with a message, push, pull, switch branches and view recent commit history — all in one panel.' },
    { selector: '.files-strip-view[data-view="database"]', title: 'Database viewer',
      body: 'Inspect tables, refresh rows after the agent writes, switch between local databases and reset selected tables.' },
    { selector: '.files-terminal-new', title: 'Open a terminal',
      body: 'Spawns a new terminal tab inside the main panel — each tab is its own independent shell with scrollback and search.' },
    { selector: '.files-settings-toggle-btn', title: 'App settings',
      body: 'Toggle the Settings overlay (App Settings, User Management, default LLM, Agent Abilities, Optimizer, Git, Automation, Events).' },
    { selector: '#files-new-file', title: 'New file',
      body: 'Create a new file in the currently selected folder.' },
    { selector: '#files-new-folder', title: 'New folder',
      body: 'Create a new folder under the currently selected directory.' },
  ],

  stream: [
    { selector: '#stream-filter-btn', title: 'Filter the stream',
      body: 'Hide or show messages by role (user, assistant, tool) and by specific tool name to focus on what matters.' },
    { selector: '#stream-status', title: 'Live / paused indicator',
      body: 'Tells you if the stream is actively receiving events. Switching to another tab pauses it; switching back resumes.' },
    { selector: '#stream-clear', title: 'Clear the stream',
      body: 'Wipes the on-screen log. Useful before kicking off a new run so you only see fresh events.' },
    { selector: '#stream-list', title: 'Live interactions',
      body: 'Each row is a message or tool call from the current session. Click any row to expand its full content.' },
  ],

  flow: [
    { selector: '.loop-filter-btn[data-level="basic"]', title: 'Detail level',
      body: 'Switch between Basic, Detailed and Debug to control how much of the agent pipeline you see.' },
    { selector: '#loop-autoscroll', title: 'Auto-scroll',
      body: 'When on, the view follows the latest step automatically. Turn off to scroll back without being yanked forward.' },
    { selector: '#loop-list', title: 'Pipeline timeline',
      body: 'Step-by-step view of what the agent is doing — model calls, tool invocations, results.' },
  ],

  'loop-visual': [
    { selector: '#loop-visual-pages', title: 'Visualizer pages',
      body: 'Switch between graphs if your agent loop produces multiple visualizations.' },
    { selector: '#loop-visual-legend', title: 'Node state legend',
      body: 'Color key for the graph — idle, active, done, error. Watch nodes change color as the agent runs.' },
    { selector: '#loop-visual-graph-area', title: 'Live graph',
      body: 'Each node lights up as the agent reaches it. Great for understanding loops and conditional branches at a glance.' },
  ],

  autoagent: [
    { selector: '#autoagent-new-page-btn', title: 'New AutoAgent page',
      body: 'Create a new generated page. Each page is built and updated by the agent from your prompts.' },
    { selector: '#autoagent-page-dropdown-trigger', title: 'Switch pages',
      body: 'Hop between your existing generated pages. Each row has a ⋮ menu for rename and delete.' },
    { selector: '#autoagent-prompt-input', title: 'Describe what to build',
      body: 'Type a natural-language instruction — e.g. "add a dashboard with three stat cards" — and the agent will edit the page for you.' },
    { selector: '#autoagent-send-btn', title: 'Send to the agent',
      body: 'Submits your prompt. Watch the iframe below update as the agent applies the change.' },
    { selector: '#autoagent-iframe', title: 'Live preview',
      body: 'The current state of your page renders here. Refreshes automatically after each agent edit.' },
  ],

  agents: [
    { selector: '#btn-new-agent', title: 'Step 1 — Create an agent',
      body: 'Click here to create a new agent. Pick a template to start from a known-good config, or start blank for full control.' },
    { selector: '#agents-grid', title: 'Step 2 — Open an agent card',
      body: 'Each card represents one agent. Click any card to expand it and see its prompts, abilities, model and stats.' },
    { selector: 'body', title: 'Step 3 — Edit the system prompt',
      body: 'Inside an expanded agent you can edit slot-based prompts (persona, instructions, etc.) and save per-user overrides.',
      anchor: { selector: '#agents-grid', position: 'inside-top-right' } },
    { selector: 'body', title: 'Step 4 — Grant abilities',
      body: 'In the Agent Tools / Abilities section you can toggle which tools the agent is allowed to call. Some require confirmation before running.',
      anchor: { selector: '#agents-grid', position: 'inside-bottom-right' } },
  ],

  account: [
    { selector: '#account-save-profile', title: 'Update your profile',
      body: 'Change your email/username or display name, then click Save changes.' },
    { selector: '#account-change-password', title: 'Change password',
      body: 'Type your current password and a new one (twice). Other browsers stay signed in until they expire.' },
    { selector: '#account-delete-trigger', title: 'Delete your account',
      body: 'Permanently removes your account, sessions and data. Requires password confirmation — no undo.' },
  ],

  chat: [
    { selector: '#agent-dropdown-trigger', title: 'Pick the agent',
      body: 'Choose which agent this chat sends to. Switch any time — the conversation stays in the current session.' },
    { selector: '#session-dropdown-trigger', title: 'Sessions',
      body: 'Each session is a separate conversation thread with its own history. Create a new one to start fresh.' },
    { selector: '#chat-attach-btn', title: 'Attach files',
      body: 'Send images, audio, PDFs or text files alongside your prompt. You can also drag-and-drop into the chat.' },
    { selector: '#chat-input', title: 'Talk to the agent',
      body: 'Type your message. Enter sends; Shift+Enter inserts a newline. Click the expand button (top-right) for a full-screen editor.' },
    { selector: '#chat-voice-btn', title: 'Voice input',
      body: 'Record a voice message and the agent will transcribe and respond.' },
  ],
};

// ── State ────────────────────────────────────────────────────────────────

let _currentPage = null;
let _overlay = null;
let _activePopover = null;
let _activeBadge = null;
let _tickTimer = null;
let _badges = []; // { el, hint, target }
let _scrollListener = null;
let _resizeListener = null;

function _loadPrefs() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const p = JSON.parse(raw);
      if (p && typeof p === 'object') {
        return {
          globalEnabled: p.globalEnabled !== false,
          pages: p.pages && typeof p.pages === 'object' ? p.pages : {},
        };
      }
    }
  } catch (_) {}
  return { globalEnabled: true, pages: {} };
}

function _savePrefs(prefs) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs)); } catch (_) {}
  // Notify any open settings UI to re-render
  document.dispatchEvent(new CustomEvent('tutorial-prefs-changed', { detail: prefs }));
}

export function getPrefs() { return _loadPrefs(); }

export function isPageEnabled(pageId) {
  const p = _loadPrefs();
  if (!p.globalEnabled) return false;
  return p.pages[pageId] !== false;
}

export function setPageEnabled(pageId, enabled) {
  const p = _loadPrefs();
  p.pages[pageId] = !!enabled;
  _savePrefs(p);
  if (_currentPage === pageId || pageId === 'chat') {
    refreshTutorial(_currentPage);
  }
}

export function setGlobalEnabled(enabled) {
  const p = _loadPrefs();
  p.globalEnabled = !!enabled;
  _savePrefs(p);
  refreshTutorial(_currentPage);
}

export function getPageList() {
  return Object.keys(HINTS).map(id => ({
    id,
    label: PAGE_LABELS[id] || id,
    count: HINTS[id].length,
  }));
}

// ── Overlay + badge rendering ───────────────────────────────────────────

function _ensureOverlay() {
  if (_overlay && document.body.contains(_overlay)) return _overlay;
  _overlay = document.createElement('div');
  _overlay.id = 'tutorial-overlay';
  document.body.appendChild(_overlay);
  return _overlay;
}

function _clearBadges() {
  _closePopover();
  _badges.forEach(b => { try { b.el.remove(); } catch (_) {} });
  _badges = [];
  if (_tickTimer) { clearInterval(_tickTimer); _tickTimer = null; }
  if (_scrollListener) {
    window.removeEventListener('scroll', _scrollListener, true);
    _scrollListener = null;
  }
  if (_resizeListener) {
    window.removeEventListener('resize', _resizeListener);
    _resizeListener = null;
  }
}

function _resolveAnchor(hint) {
  // Hints can target a normal selector, or use an anchor block to position
  // relative to another element (used when the feature isn't a single DOM
  // node — e.g. "step 3" pointing inside an expanded card).
  if (hint.anchor && hint.anchor.selector) {
    const el = document.querySelector(hint.anchor.selector);
    if (!el) return null;
    return { el, position: hint.anchor.position || 'top-left' };
  }
  const el = document.querySelector(hint.selector);
  if (!el) return null;
  return { el, position: 'top-left' };
}

function _positionBadge(badge, target, position) {
  const rect = target.el.getBoundingClientRect();
  // Skip if target is hidden or offscreen
  if (rect.width === 0 && rect.height === 0) {
    badge.style.display = 'none';
    return;
  }
  badge.style.display = '';

  let x, y;
  switch (target.position) {
    case 'inside-top-right':
      x = rect.right - 28;
      y = rect.top + 6;
      break;
    case 'inside-bottom-right':
      x = rect.right - 28;
      y = rect.bottom - 28;
      break;
    case 'inside-top-left':
      x = rect.left + 6;
      y = rect.top + 6;
      break;
    case 'top-left':
    default:
      x = rect.left - 10;
      y = rect.top - 10;
      break;
  }

  // Clamp to viewport so badges never escape the screen
  x = Math.max(4, Math.min(window.innerWidth - 26, x));
  y = Math.max(4, Math.min(window.innerHeight - 26, y));

  badge.style.left = x + 'px';
  badge.style.top = y + 'px';
}

function _repositionAll() {
  _badges.forEach(b => _positionBadge(b.el, b.target, b.target.position));
  if (_activePopover && _activeBadge) {
    _positionPopover(_activePopover, _activeBadge);
  }
}

function _positionPopover(popover, badge) {
  const br = badge.getBoundingClientRect();
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const margin = 8;
  const pad = 8; // min distance from viewport edge

  // ── Mobile / narrow viewport: pin the popover to the viewport width and
  // stack it above or below the badge, whichever side has more room. This
  // guarantees no horizontal clipping regardless of badge position.
  if (vw <= 600) {
    const width = Math.max(220, vw - pad * 2);
    popover.style.width = width + 'px';
    popover.style.maxWidth = 'none';
    popover.style.left = pad + 'px';

    // Need offsetHeight at the new width, so read after setting it
    const ph = popover.offsetHeight || 160;

    const roomBelow = vh - br.bottom - margin - pad;
    const roomAbove = br.top - margin - pad;
    let y, arrow;
    if (roomBelow >= ph || roomBelow >= roomAbove) {
      y = Math.min(vh - ph - pad, br.bottom + margin);
      arrow = 'top';
    } else {
      y = Math.max(pad, br.top - ph - margin);
      arrow = 'bottom';
    }
    popover.style.top = y + 'px';
    popover.setAttribute('data-arrow', arrow);

    // Point the arrow at the badge horizontally
    const badgeCenter = br.left + br.width / 2;
    const popLeft = pad;
    const arrowOffset = Math.max(10, Math.min(width - 20, badgeCenter - popLeft - 5));
    popover.style.setProperty('--tutorial-arrow-pos', arrowOffset + 'px');
    return;
  }

  // ── Desktop: try right of badge, then left, then below, then above.
  // Reset any mobile-only overrides from a previous call.
  popover.style.width = '';
  popover.style.maxWidth = '';
  popover.style.removeProperty('--tutorial-arrow-pos');

  const pw = popover.offsetWidth || 290;
  const ph = popover.offsetHeight || 160;

  let x, y, arrow;

  if (br.right + margin + pw <= vw - pad) {
    // Fits to the right
    x = br.right + margin;
    y = br.top;
    arrow = 'left';
  } else if (br.left - margin - pw >= pad) {
    // Fits to the left
    x = br.left - pw - margin;
    y = br.top;
    arrow = 'right';
  } else if (br.bottom + margin + ph <= vh - pad) {
    // Stack below
    x = Math.max(pad, Math.min(vw - pw - pad, br.left - 8));
    y = br.bottom + margin;
    arrow = 'top';
  } else {
    // Stack above
    x = Math.max(pad, Math.min(vw - pw - pad, br.left - 8));
    y = Math.max(pad, br.top - ph - margin);
    arrow = 'bottom';
  }

  // Final safety clamp — never let any edge escape the viewport
  x = Math.max(pad, Math.min(vw - pw - pad, x));
  y = Math.max(pad, Math.min(vh - ph - pad, y));

  popover.style.left = x + 'px';
  popover.style.top = y + 'px';
  popover.setAttribute('data-arrow', arrow);
}

function _closePopover() {
  if (_activePopover) {
    _activePopover.remove();
    _activePopover = null;
  }
  if (_activeBadge) {
    _activeBadge.classList.remove('active');
    _activeBadge = null;
  }
}

function _openPopover(badge, hint, step, total) {
  _closePopover();
  badge.classList.add('active');
  _activeBadge = badge;

  const pop = document.createElement('div');
  pop.className = 'tutorial-popover';

  const header = document.createElement('div');
  header.className = 'tutorial-popover-header';

  const stepLabel = document.createElement('span');
  stepLabel.className = 'tutorial-popover-step';
  stepLabel.textContent = `Step ${step} / ${total}`;
  header.appendChild(stepLabel);

  const title = document.createElement('div');
  title.className = 'tutorial-popover-title';
  title.textContent = hint.title || '';
  header.appendChild(title);

  const close = document.createElement('button');
  close.className = 'tutorial-popover-close';
  close.type = 'button';
  close.setAttribute('aria-label', 'Close hint');
  close.textContent = '×';
  close.addEventListener('click', (e) => { e.stopPropagation(); _closePopover(); });
  header.appendChild(close);

  pop.appendChild(header);

  const body = document.createElement('div');
  body.className = 'tutorial-popover-body';
  body.textContent = hint.body || '';
  pop.appendChild(body);

  const footer = document.createElement('div');
  footer.className = 'tutorial-popover-footer';

  const cbLabel = document.createElement('label');
  cbLabel.className = 'tutorial-popover-checkbox';
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = false;
  cb.addEventListener('change', () => {
    if (cb.checked) {
      setPageEnabled(_currentPage, false);
    }
  });
  cbLabel.appendChild(cb);
  cbLabel.appendChild(document.createTextNode('Hide hints on this page'));
  footer.appendChild(cbLabel);

  const nav = document.createElement('div');
  nav.className = 'tutorial-popover-nav';
  const prev = document.createElement('button');
  prev.type = 'button';
  prev.textContent = '‹';
  prev.title = 'Previous hint';
  prev.disabled = step <= 1;
  prev.addEventListener('click', (e) => {
    e.stopPropagation();
    const target = _badges[step - 2];
    if (target) _openPopover(target.el, target.hint, step - 1, total);
  });
  const next = document.createElement('button');
  next.type = 'button';
  next.textContent = '›';
  next.title = 'Next hint';
  next.disabled = step >= total;
  next.addEventListener('click', (e) => {
    e.stopPropagation();
    const target = _badges[step];
    if (target) _openPopover(target.el, target.hint, step + 1, total);
  });
  nav.appendChild(prev);
  nav.appendChild(next);
  footer.appendChild(nav);

  pop.appendChild(footer);

  // Append to the overlay (a fixed, body-level container) rather than body
  // directly. A global index.html rule forces `position: relative` on every
  // direct body child except the stargaze canvas/overlay, which would clobber
  // this popover's `position: fixed` and render it after the body's last block
  // (i.e. far below the viewport, inaccessible).
  _ensureOverlay().appendChild(pop);
  _activePopover = pop;
  _positionPopover(pop, badge);
}

function _outsideClickListener(e) {
  if (!_activePopover) return;
  if (_activePopover.contains(e.target)) return;
  if (_activeBadge && _activeBadge.contains(e.target)) return;
  _closePopover();
}

function _keyListener(e) {
  if (e.key === 'Escape') _closePopover();
}

function _renderForPage(pageId) {
  _clearBadges();
  if (!pageId) return;

  // Both the current page hints and the always-visible chat panel hints
  const pageIds = [pageId];
  // Show chat hints alongside on any tab where chat is visible (most desktop
  // tabs except for full-screen pages). Keep it simple — only add when the
  // chat side is actually displayed.
  const chatSide = document.getElementById('chat-side');
  const chatVisible = chatSide && chatSide.style.display !== 'none' &&
    getComputedStyle(chatSide).display !== 'none';
  if (chatVisible && pageId !== 'chat') pageIds.push('chat');

  const overlay = _ensureOverlay();

  for (const pid of pageIds) {
    if (!isPageEnabled(pid)) continue;
    const hints = HINTS[pid] || [];
    const total = hints.length;
    hints.forEach((hint, idx) => {
      const target = _resolveAnchor(hint);
      if (!target) return;

      const badge = document.createElement('div');
      badge.className = 'tutorial-badge';
      // Continue numbering across page+chat hints by using each page's own index
      badge.textContent = String(idx + 1);
      badge.title = hint.title || '';
      badge.setAttribute('role', 'button');
      badge.setAttribute('tabindex', '0');
      badge.setAttribute('aria-label', `Hint ${idx + 1}: ${hint.title || ''}`);

      const open = (e) => {
        e.stopPropagation();
        if (_activeBadge === badge) {
          _closePopover();
        } else {
          _openPopover(badge, hint, idx + 1, total);
        }
      };
      badge.addEventListener('click', open);
      badge.addEventListener('mouseenter', () => {
        // Hover-to-open, but don't override an explicit open from another badge
        if (!_activePopover) _openPopover(badge, hint, idx + 1, total);
      });
      badge.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(e); }
      });

      overlay.appendChild(badge);
      _badges.push({ el: badge, hint, target });
      _positionBadge(badge, target, target.position);
    });
  }

  if (_badges.length === 0) return;

  // Keep badges aligned with their targets as the page scrolls/animates
  _scrollListener = () => _repositionAll();
  window.addEventListener('scroll', _scrollListener, true);
  _resizeListener = () => _repositionAll();
  window.addEventListener('resize', _resizeListener);
  _tickTimer = setInterval(_repositionAll, TICK_MS);
}

// ── Public API ──────────────────────────────────────────────────────────

export function refreshTutorial(pageId) {
  _currentPage = pageId || _currentPage;
  // Defer one frame so newly-activated tab content has laid out
  requestAnimationFrame(() => {
    _renderForPage(_currentPage);
  });
}

export function initTutorial() {
  _ensureOverlay();
  document.addEventListener('click', _outsideClickListener, true);
  document.addEventListener('keydown', _keyListener);

  // Pick up the initial active tab from localStorage so hints render even
  // before any tab switch happens. tabs.js will call refreshTutorial() right
  // after with the same id, but this avoids a flash on first paint.
  const saved = localStorage.getItem('lastActiveTab');
  if (saved) refreshTutorial(saved);
}

// ── User-management settings UI ─────────────────────────────────────────

export function renderTutorialPrefsCard(container) {
  if (!container) return;
  const prefs = _loadPrefs();
  const pages = getPageList();

  container.innerHTML = '';

  const card = document.createElement('div');
  card.className = 'ac-card';
  card.id = 'ac-tutorial-card';

  const label = document.createElement('div');
  label.className = 'ac-card-label';
  label.textContent = 'Tutorial Hints';
  card.appendChild(label);

  const sub = document.createElement('div');
  sub.className = 'ac-card-sublabel';
  sub.textContent =
    'Numbered hover popovers that highlight features on each page. ' +
    'Turn them off globally, or pick which pages should show them.';
  card.appendChild(sub);

  // Global toggle
  const globalRow = document.createElement('div');
  globalRow.className = 'tutorial-global-row';
  const globalLabel = document.createElement('label');
  const globalCb = document.createElement('input');
  globalCb.type = 'checkbox';
  globalCb.id = 'ac-tutorial-global';
  globalCb.checked = prefs.globalEnabled;
  globalCb.addEventListener('change', () => {
    setGlobalEnabled(globalCb.checked);
    renderTutorialPrefsCard(container);
  });
  globalLabel.appendChild(globalCb);
  globalLabel.appendChild(document.createTextNode(' Show tutorial hints across the app'));
  globalRow.appendChild(globalLabel);

  const globalDesc = document.createElement('span');
  globalDesc.className = 'tutorial-global-desc';
  globalDesc.textContent = prefs.globalEnabled ? 'All hints enabled' : 'All hints hidden';
  globalRow.appendChild(globalDesc);
  card.appendChild(globalRow);

  // Per-page list
  const listLabel = document.createElement('div');
  listLabel.style.cssText = 'margin-top:14px;font-size:11px;color:var(--fg-3);text-transform:uppercase;letter-spacing:0.05em;font-weight:600;';
  listLabel.textContent = 'Per-page hints';
  card.appendChild(listLabel);

  const list = document.createElement('div');
  list.className = 'tutorial-page-list';

  pages.forEach(p => {
    const row = document.createElement('div');
    row.className = 'tutorial-page-row';
    const enabled = prefs.pages[p.id] !== false;
    if (!prefs.globalEnabled || !enabled) row.classList.add('disabled');

    const left = document.createElement('span');
    left.className = 'tutorial-page-name';
    left.textContent = p.label;
    const count = document.createElement('span');
    count.className = 'tutorial-page-count';
    count.textContent = `${p.count} hint${p.count === 1 ? '' : 's'}`;
    left.appendChild(count);
    row.appendChild(left);

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = enabled;
    cb.disabled = !prefs.globalEnabled;
    cb.addEventListener('change', () => {
      setPageEnabled(p.id, cb.checked);
      renderTutorialPrefsCard(container);
    });
    row.appendChild(cb);

    list.appendChild(row);
  });

  card.appendChild(list);

  // Actions
  const actions = document.createElement('div');
  actions.className = 'tutorial-actions';
  const enableAll = document.createElement('button');
  enableAll.type = 'button';
  enableAll.textContent = 'Enable all';
  enableAll.addEventListener('click', () => {
    const p = _loadPrefs();
    p.globalEnabled = true;
    p.pages = {};
    pages.forEach(pg => { p.pages[pg.id] = true; });
    _savePrefs(p);
    refreshTutorial(_currentPage);
    renderTutorialPrefsCard(container);
  });
  const disableAll = document.createElement('button');
  disableAll.type = 'button';
  disableAll.textContent = 'Disable all';
  disableAll.addEventListener('click', () => {
    const p = _loadPrefs();
    pages.forEach(pg => { p.pages[pg.id] = false; });
    _savePrefs(p);
    refreshTutorial(_currentPage);
    renderTutorialPrefsCard(container);
  });
  actions.appendChild(enableAll);
  actions.appendChild(disableAll);
  card.appendChild(actions);

  container.appendChild(card);
}
