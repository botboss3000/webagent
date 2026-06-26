'use strict';

import { app } from '../../shared/js/state.js';
import { authHeaders as _authHeaders } from '../../shared/js/left-login.js';

/**
 * Tutorial - a single linear walkthrough of the app's main features.
 *
 * One ordered TOUR array of 12 steps. Each step has a tab (where it lives),
 * a CSS selector for its target element, and the popover title/body. Badges
 * for the steps that target the active tab (plus the always-visible header
 * steps and the chat-panel steps when the chat side is showing) are
 * rendered as numbered circles using each step's **global** index. Past
 * steps (below the persisted `currentStep`) drop their badges so only the
 * upcoming ones glow; going back via Prev brings them back. The popover's
 * Prev/Next buttons walk the entire TOUR - auto-switching tabs on boundary
 * crossings and skipping past steps whose targets don't currently resolve.
 * Advancing past the final step auto-disables the tour. Each popover also
 * exposes a "Hide all hints" checkbox that turns the tour off immediately.
 *
 * State lives in two places: localStorage["tutorialPrefs"] is the
 * synchronous local cache; user_profiles.tutorial_prefs in the database
 * is the cross-device source of truth. On boot we hydrate from the server
 * (overwriting local), and every change writes through to both. Older
 * local shapes ({ globalEnabled, pages }) are migrated on read.
 */

const STORAGE_KEY = 'tutorialPrefs';
const TICK_MS = 500;
// App-wide master switch (admin App Settings → Startup & Boot → Show tooltips,
// persisted server-side as hints_enabled). Cached for a synchronous render gate
// and reconciled from /api/v1/auth/ui-config — mirrors the splash plugin's
// webagent.splashEnabled.v1 cache. Off here = no tour for anyone, regardless of
// each user's own "show app tour" preference.
const ADMIN_ENABLED_KEY = 'webagent.hintsEnabled.v1';

// Single ordered tour. `tab` values:
//   '*'        - always visible (header / overlay targets)
//   'chat'     - chat-panel target; rendered when the chat side is visible
//   <tabId>    - main-tab id; rendered when that tab is active
const TOUR = [
  { tab: '*', selector: '#chat-toggle-btn',
    title: 'Show or hide the chat',
    body: 'This icon in the top bar shows or hides the chat panel - it\'s where you talk to your agents. An agent is already set up for you, so click it to slide the panel open and start chatting.' },

  { tab: 'chat', selector: '#chat-input',
    title: 'Say hi to your agent',
    body: 'Type a message and press Enter; Shift+Enter adds a newline. The ↗ button in the input opens a bigger editor when you need it.' },

  { tab: 'chat', selector: '#session-dropdown-trigger',
    title: 'Sessions, attachments and voice',
    body: 'Each session is its own conversation thread with its own history - switch or start fresh from this dropdown. The paperclip attaches files (drag-and-drop also works) and the mic records a voice message the agent transcribes.' },

  { tab: 'agents', selector: '#agents-grid',
    title: 'Browse and customize agents',
    body: 'Each card is one agent. Click to expand it - tabs across the top let you edit the system prompt, toggle which tools the agent can call, pick its model, and connect channels like Telegram.' },

  { tab: 'agents', selector: '.agent-card-mock',
    title: 'Create your own',
    body: 'The first card is a blank agent. Click it, give it a name, pick a template, then click "Create Agent" to save it.' },

  { tab: 'canvas', selector: '#canvas-new-page-btn',
    title: 'AI-built canvases',
    body: 'Canvases are custom UIs the AI builds for you. Hit + to create one — give it a name like "My Dashboard".' },

  { tab: 'canvas', selector: '#canvas-prompt-input',
    title: 'Tell the Visualizer what to build',
    body: 'Describe what you want - e.g. "Add three stat cards showing my goals" - and the Visualizer agent generates it. The preview above refreshes after each change.' },

  { tab: 'canvas', selector: '#canvas-page-dropdown-trigger',
    title: 'Switch and manage canvases',
    body: 'Jump between your canvases here. Each row\'s ⋮ menu has rename and delete.' },

  { tab: 'admin-tools', selector: '.files-sidebar-strip',
    title: 'Sidebar views',
    body: 'Behind-the-scenes tools: file browser, source control, database, terminal - plus live Interactions and Runtime Loop views that show what your agents are doing right now.' },

  { tab: 'admin-tools', selector: '.files-settings-toggle-btn',
    title: 'App configuration',
    body: 'The gear opens admin settings: pick the default LLM, manage users, turn on powerful agent tools, and connect integrations like Google, Slack, Telegram and OAuth.' },

  { tab: 'account', selector: '#account-tutorial-global',
    title: 'Your account & this tour',
    body: 'You\'re set. Profile and password live here. You can turn the tour off - or replay it - with this toggle.' },
];

const TOTAL = TOUR.length;

// ── State ────────────────────────────────────────────────────────────────

let _currentPage = null;
let _overlay = null;
let _activePopover = null;
let _activeBadge = null;
let _tickTimer = null;
let _badges = []; // { el, hint, target, step }  step = 1-based global index
let _scrollListener = null;
let _resizeListener = null;

function _loadPrefs() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const p = JSON.parse(raw);
      if (p && typeof p === 'object') {
        const enabled =
          typeof p.enabled === 'boolean' ? p.enabled :
          typeof p.globalEnabled === 'boolean' ? p.globalEnabled : true;
        const rawStep = typeof p.currentStep === 'number' ? p.currentStep : 1;
        const currentStep = Math.max(1, Math.min(TOTAL, rawStep));
        return { enabled, currentStep };
      }
    }
  } catch (_) {}
  return { enabled: true, currentStep: 1 };
}

function _writeLocal(prefs) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs)); } catch (_) {}
  document.dispatchEvent(new CustomEvent('tutorial-prefs-changed', { detail: prefs }));
}

function _savePrefs(prefs) {
  _writeLocal(prefs);
  // Fire-and-forget server sync - localStorage is the local truth, the
  // server is the cross-device source of truth.
  _syncToServer(prefs);
}


// ── App-wide master switch (admin "Show tooltips") ─────────────────────────
// Cached so the render gate + account page can read it synchronously; defaults
// to enabled when unknown, then reconciled from ui-config for the next read.
function _adminEnabled() {
  try { return localStorage.getItem(ADMIN_ENABLED_KEY) !== '0'; } catch (_) { return true; }
}

// Public: is the tour enabled app-wide by the admin? Used by the account page to
// hide its "Show app tour" toggle when hints are globally off.
export function hintsGloballyEnabled() { return _adminEnabled(); }

// Set the cached master switch (called by the admin App Settings toggle after a
// successful server save) and re-render live.
function setAdminEnabled(enabled) {
  try { localStorage.setItem(ADMIN_ENABLED_KEY, enabled ? '1' : '0'); } catch (_) {}
  refreshTutorial();
}

// Fire-and-forget: refresh the cached master switch from the public ui-config so
// an admin's enable/disable lands on the next load. Re-renders if it changed.
function _refreshAdminEnabled() {
  fetch('/api/v1/auth/ui-config', { headers: _authHeaders() })
    .then((r) => (r.ok ? r.json() : null))
    .then((cfg) => {
      if (!cfg || typeof cfg.hints_enabled !== 'boolean') return;
      const prev = _adminEnabled();
      try { localStorage.setItem(ADMIN_ENABLED_KEY, cfg.hints_enabled ? '1' : '0'); } catch (_) {}
      if (prev !== cfg.hints_enabled) {
        refreshTutorial();
        document.dispatchEvent(new CustomEvent('tutorial-admin-changed'));
      }
    })
    .catch(() => {});
}

async function _syncToServer(prefs) {
  const uid = app.currentUserId;
  if (!uid) return;
  try {
    await fetch('/api/v1/user/tutorial-prefs', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify({ user_id: uid, prefs }),
    });
  } catch (e) {
    // Offline / 401 - local cache still wins for this session; next change
    // will retry.
    console.warn('tutorial: server sync failed', e);
  }
}

async function _hydrateFromServer() {
  const uid = app.currentUserId;
  if (!uid) return;
  try {
    const res = await fetch(`/api/v1/user/profile?user_id=${encodeURIComponent(uid)}`, {
      headers: { ..._authHeaders() },
    });
    if (!res.ok) return;
    const data = await res.json();
    const server = data && data.tutorial_prefs;
    if (!server || typeof server !== 'object') return;
    const enabled = typeof server.enabled === 'boolean' ? server.enabled : true;
    const rawStep = typeof server.currentStep === 'number' ? server.currentStep : 1;
    const currentStep = Math.max(1, Math.min(TOTAL, rawStep));
    // Skip the write if it'd be a no-op to avoid a redundant render + event.
    const cur = _loadPrefs();
    if (cur.enabled === enabled && cur.currentStep === currentStep) return;
    _writeLocal({ enabled, currentStep });
    refreshTutorial(_currentPage);
  } catch (e) {
    console.warn('tutorial: server hydrate failed', e);
  }
}

function _persistCurrentStep(n) {
  const prefs = _loadPrefs();
  const clamped = Math.max(1, Math.min(TOTAL, n));
  if (prefs.currentStep === clamped) return;
  _savePrefs({ enabled: prefs.enabled, currentStep: clamped });
}

// Returns a back-compat shape so callers using either `.enabled` or the old
// `.globalEnabled` keep working.
export function getPrefs() {
  const p = _loadPrefs();
  return { enabled: p.enabled, globalEnabled: p.enabled, currentStep: p.currentStep };
}

function setEnabled(enabled) {
  // Re-enabling rewinds to step 1 so the user gets the full tour again.
  const prefs = _loadPrefs();
  _savePrefs({
    enabled: !!enabled,
    currentStep: enabled ? 1 : prefs.currentStep,
  });
  refreshTutorial(_currentPage);
}

// Back-compat alias - account.js imports `setGlobalEnabled`.
export const setGlobalEnabled = setEnabled;

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
  // relative to another element.
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
  const pad = 8;

  if (vw <= 600) {
    const width = Math.max(220, vw - pad * 2);
    popover.style.width = width + 'px';
    popover.style.maxWidth = 'none';
    popover.style.left = pad + 'px';

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

    const badgeCenter = br.left + br.width / 2;
    const popLeft = pad;
    const arrowOffset = Math.max(10, Math.min(width - 20, badgeCenter - popLeft - 5));
    popover.style.setProperty('--tutorial-arrow-pos', arrowOffset + 'px');
    return;
  }

  popover.style.width = '';
  popover.style.maxWidth = '';
  popover.style.removeProperty('--tutorial-arrow-pos');

  const pw = popover.offsetWidth || 290;
  const ph = popover.offsetHeight || 160;

  let x, y, arrow;

  if (br.right + margin + pw <= vw - pad) {
    x = br.right + margin;
    y = br.top;
    arrow = 'left';
  } else if (br.left - margin - pw >= pad) {
    x = br.left - pw - margin;
    y = br.top;
    arrow = 'right';
  } else if (br.bottom + margin + ph <= vh - pad) {
    x = Math.max(pad, Math.min(vw - pw - pad, br.left - 8));
    y = br.bottom + margin;
    arrow = 'top';
  } else {
    x = Math.max(pad, Math.min(vw - pw - pad, br.left - 8));
    y = Math.max(pad, br.top - ph - margin);
    arrow = 'bottom';
  }

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

// ── Linear navigation across the global TOUR ────────────────────────────

function _isChatVisible() {
  const chatPanel = document.getElementById('chat-panel');
  return !!(chatPanel && chatPanel.style.display !== 'none' &&
    getComputedStyle(chatPanel).display !== 'none');
}

function _stepInCurrentContext(step) {
  if (step.tab === '*') return true;
  if (step.tab === _currentPage) return true;
  if (step.tab === 'chat' && _isChatVisible()) return true;
  return false;
}

function _switchToTab(tab, cb) {
  if (tab === '*') { cb(); return; }
  if (tab === 'chat') {
    // Mobile may have hidden the chat panel - reveal it so the chat-panel
    // badges can render.
    if (!_isChatVisible() && typeof window.__applyChatVisible === 'function') {
      window.__applyChatVisible(true);
    }
    refreshTutorial(_currentPage);
    requestAnimationFrame(() => requestAnimationFrame(cb));
    return;
  }
  // A main tab: drive the hidden <select> that tabs.js listens on.
  const sel = document.getElementById('main-tab-select');
  if (!sel) { cb(); return; }
  if (sel.value !== tab) {
    sel.value = tab;
    sel.dispatchEvent(new Event('change', { bubbles: true }));
  }
  // tabs.js's change handler calls refreshTutorial, which defers one frame
  // before rendering - wait two rAFs so badges are in place when we look
  // one up.
  requestAnimationFrame(() => requestAnimationFrame(cb));
}

function _gotoStep(targetStep, direction) {
  // Advancing past the final step finishes the tour and turns it off.
  if (targetStep > TOTAL) {
    setEnabled(false);
    return;
  }

  while (targetStep >= 1 && targetStep <= TOTAL) {
    const step = TOUR[targetStep - 1];
    if (_stepInCurrentContext(step)) {
      // Persist before re-rendering so badges below the new step are filtered.
      _persistCurrentStep(targetStep);
      _renderForPage(_currentPage);
      const b = _badges.find(x => x.step === targetStep);
      if (b) {
        _openPopover(b.el, b.hint, targetStep, TOTAL);
        return;
      }
      // In-context but the target didn't resolve (element hidden, etc.) -
      // keep walking.
      targetStep += direction;
      continue;
    }
    // Out of current context; switch and resume the walk on the other side.
    const want = targetStep;
    _persistCurrentStep(want);
    _switchToTab(step.tab, () => {
      const b2 = _badges.find(x => x.step === want);
      if (b2) {
        _openPopover(b2.el, b2.hint, want, TOTAL);
      } else {
        _gotoStep(want + direction, direction);
      }
    });
    return;
  }
  _closePopover();
}

// ── Popover ─────────────────────────────────────────────────────────────

function _openPopover(badge, step, stepNum, total) {
  _closePopover();
  badge.classList.add('active');
  _activeBadge = badge;

  const pop = document.createElement('div');
  pop.className = 'tutorial-popover';

  const header = document.createElement('div');
  header.className = 'tutorial-popover-header';

  const stepLabel = document.createElement('span');
  stepLabel.className = 'tutorial-popover-step';
  stepLabel.textContent = `Step ${stepNum} / ${total}`;
  header.appendChild(stepLabel);

  const title = document.createElement('div');
  title.className = 'tutorial-popover-title';
  title.textContent = step.title || '';
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
  body.textContent = step.body || '';
  pop.appendChild(body);

  const footer = document.createElement('div');
  footer.className = 'tutorial-popover-footer';

  const hideAll = document.createElement('label');
  hideAll.className = 'tutorial-popover-hideall';
  const hideCb = document.createElement('input');
  hideCb.type = 'checkbox';
  hideCb.addEventListener('change', (e) => {
    e.stopPropagation();
    if (hideCb.checked) setEnabled(false);
  });
  hideAll.appendChild(hideCb);
  const hideTxt = document.createElement('span');
  hideTxt.textContent = 'Hide all hints';
  hideAll.appendChild(hideTxt);
  footer.appendChild(hideAll);

  const nav = document.createElement('div');
  nav.className = 'tutorial-popover-nav';
  const prev = document.createElement('button');
  prev.type = 'button';
  prev.textContent = '‹';
  prev.title = 'Previous step';
  prev.disabled = stepNum <= 1;
  prev.addEventListener('click', (e) => {
    e.stopPropagation();
    _gotoStep(stepNum - 1, -1);
  });
  const next = document.createElement('button');
  next.type = 'button';
  // Last step's Next finishes the tour and disables it.
  const isLast = stepNum >= total;
  next.textContent = isLast ? '✓' : '›';
  next.title = isLast ? 'Finish tour' : 'Next step';
  next.addEventListener('click', (e) => {
    e.stopPropagation();
    _gotoStep(stepNum + 1, +1);
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

  const prefs = _loadPrefs();
  // Both gates must pass: the admin's app-wide master switch AND this user's own
  // "show app tour" preference.
  if (!prefs.enabled || !_adminEnabled()) return;

  const currentStep = prefs.currentStep;
  const chatVisible = _isChatVisible();
  const overlay = _ensureOverlay();

  TOUR.forEach((step, idx) => {
    const stepNum = idx + 1;
    // Hide badges for steps the user has already advanced past. Prev brings
    // them back by decrementing currentStep.
    if (stepNum < currentStep) return;
    const inContext =
      step.tab === '*' ||
      step.tab === pageId ||
      (step.tab === 'chat' && chatVisible);
    if (!inContext) return;

    const target = _resolveAnchor(step);
    if (!target) return;

    const badge = document.createElement('div');
    badge.className = 'tutorial-badge';
    badge.textContent = String(stepNum);
    badge.title = step.title || '';
    badge.setAttribute('role', 'button');
    badge.setAttribute('tabindex', '0');
    badge.setAttribute('aria-label', `Step ${stepNum}: ${step.title || ''}`);

    const open = (e) => {
      e.stopPropagation();
      if (_activeBadge === badge) {
        _closePopover();
      } else {
        _openPopover(badge, step, stepNum, TOTAL);
      }
    };
    badge.addEventListener('click', open);
    badge.addEventListener('mouseenter', () => {
      if (!_activePopover) _openPopover(badge, step, stepNum, TOTAL);
    });
    badge.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(e); }
    });

    overlay.appendChild(badge);
    _badges.push({ el: badge, hint: step, target, step: stepNum });
    _positionBadge(badge, target, target.position);
  });

  if (_badges.length === 0) return;

  _scrollListener = () => _repositionAll();
  window.addEventListener('scroll', _scrollListener, true);
  _resizeListener = () => _repositionAll();
  window.addEventListener('resize', _resizeListener);
  _tickTimer = setInterval(_repositionAll, TICK_MS);
}

// ── Public API ──────────────────────────────────────────────────────────

export function refreshTutorial(pageId) {
  _currentPage = pageId || _currentPage;
  // Defer one frame so newly-activated tab content has laid out.
  requestAnimationFrame(() => {
    _renderForPage(_currentPage);
  });
}

export function initTutorial() {
  _ensureOverlay();
  document.addEventListener('click', _outsideClickListener, true);
  document.addEventListener('keydown', _keyListener);
  document.addEventListener('tutorial-admin-changed', () => refreshTutorial());

  // Pick up the initial active tab from localStorage so hints render even
  // before any tab switch happens. tabs.js will call refreshTutorial() right
  // after with the same id, but this avoids a flash on first paint.
  const saved = localStorage.getItem('lastActiveTab');
  if (saved) refreshTutorial(saved);
  // Server hydrate deferred to startTutorial() - runs after init is complete.
}

/** Fetch tutorial state from the server. Called after init settles so it
 *  doesn't block the critical path. */
export function startTutorial() {
  _hydrateFromServer();
  // Reconcile the app-wide master switch (admin "Show tooltips") from ui-config.
  _refreshAdminEnabled();
}
