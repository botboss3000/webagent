'use strict';

// Chat panel wiring — initChat(): binds chat input/keyboard/send, execution-mode
// toggle, continue/stop buttons (500ms poll), scroll-to-bottom chevron + the
// top-of-panel jump nav (start of session / last user message), gate +
// draft restore. The top-level entry for the chat side panel.
// Module map for this folder: ui/chat/js/README.md.

import { _refreshLucideIcons, keepPillFocusOnFooterTap } from '../../shared/js/dom-utils.js';
import { applyRubberBand } from '../../shared/js/rubber-band.js';
import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { getAccessMode, fetchAccessMode, authHeaders } from '../../shared/js/left-login.js';
import { chatMsg, isDebugMode } from '../../shared/js/app-prompts.js';
import { addChatBubble, updateLastBubble, linkifyText, _renderMarkdownBody, _refreshAllBubbleTimes } from './chat-bubble.js';
import { _addBubbleActions, _finalizeBubbleSections } from './chat-bubble-actions.js';
import {
  appendStreamToActiveBubble,
  finalizeAgentResponse,
  ensureStreamingBubbleForActiveTurn,
  finalizeAgentStep,
  markAgentInterrupted,
  seedStreamingBubble,
  attachToolCallsToLastBubble,
} from './chat-stream.js?v=258';
import {
  sendMessage,
  sendStopMessage,
  abortChatStream,
  applyChatGate,
  _canChat,
  _autoResizePill,
  _updateScrollIndicator,
  _updateInputRowState,
  _saveDraft,
  _clearDraft,
  _restoreDraft,
  _renderPendingBubbles,
  _startOutboxPoll,
  _outboxHasPending,
  _prewarm,
} from './chat-send.js';
import { initChatTunnel } from '../../shared/js/chatTunnel.js';
import { initTerminalChatEngine } from './chat-terminal-engine.js';
import { kvRead, kvWrite, kvDelete } from '../../shared/js/kv-ui-state.js';
import { initChatComponents } from './chat-components.js';
import { switchFooterMode, getFooterMode } from '../../chat-controls/chat-controls-config.js';

/** Whether the user has explicitly locked auto-scroll. */
let _scrollLocked = true;
/** The scroll-to-bottom chevron button, cached after init. */
let _scrollBtn = null;
/** Top-of-panel jump buttons (double = session top, single = step up one user
 *  message from the current scroll position). */
let _toTopBtn = null;
let _toLastUserBtn = null;
/** Set to true while _scrollToBottomIfNear is doing a programmatic scroll. */
let _programmaticScroll = false;
/** Whether the top-nav click handlers have been wired. */
let _topNavWired = false;

/** Gap (px) the landed message sits below the viewport top — also the slack
 *  used when deciding which messages count as "above" the current top. */
const _USER_STEP_PAD = 12;

/** The scrollTop that brings the nearest user message *above* the current
 *  viewport top to the top — i.e. one step up. Returns null when there's no
 *  user message above the current position (nothing more to step to). Uses
 *  live rects (the bubbles' offsetParent isn't the scroller, so offsetTop
 *  would be wrong). Each click re-anchors on wherever the user now is, so
 *  repeated clicks walk up one user message at a time. */
function _prevUserTarget(el) {
  if (!el) return null;
  const innerTop = el.getBoundingClientRect().top;
  const cur = el.scrollTop;
  const users = el.querySelectorAll('.chat-bubble.user');
  let best = null;
  for (const ub of users) {
    const offset = cur + (ub.getBoundingClientRect().top - innerTop);
    // Strictly above the current top (small slack absorbs float jitter and the
    // landing pad, so the just-landed message never re-selects itself).
    if (offset < cur - 2) {
      const target = Math.max(0, offset - _USER_STEP_PAD);
      if (best === null || target > best) best = target;
    }
  }
  return best;
}

/** Show/hide the two top-of-panel jump buttons based on scroll position. */
function _updateTopNav(el) {
  if (!el) return;
  const topVisible = el.scrollTop > 60;
  // Single chevron is relevant when a user message is rendered above the
  // current top, OR when still-older rows exist in the DB that might contain
  // one (the click handler resolves the truth from the DB).
  const lastVisible = _prevUserTarget(el) !== null
    || (typeof app._hasOlderMessages === 'function' && app._hasOlderMessages(app.currentSessionId));
  // Double chevron → very top: relevant whenever there's content above.
  if (_toTopBtn) _toTopBtn.classList.toggle('visible', topVisible);
  // Single chevron → step up one user message: relevant whenever there's a
  // user message above the current top to step to.
  if (_toLastUserBtn) {
    _toLastUserBtn.classList.toggle('visible', lastVisible);
  }
  // Show container when any button is visible
  const nav = document.getElementById('chat-top-nav');
  if (nav) nav.classList.toggle('visible', topVisible || lastVisible);
}

function _updateScrollChevron(el) {
  if (!el) return;
  _updateTopNav(el);
  if (!_scrollBtn) return;
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  // Show the double-down chevron not only when scrolled up, but also when the
  // true latest messages haven't been loaded yet (opened mid-history): the
  // click then fetches the real tail from the DB instead of only scrolling.
  const hasNewer = typeof app._hasNewerMessages === 'function'
    && app._hasNewerMessages(app.currentSessionId);
  const showDown = !atBottom || hasNewer;
  if (_programmaticScroll) {
    _programmaticScroll = false;
    _scrollBtn.classList.toggle('visible', showDown);
    return;
  }
  _scrollBtn.classList.toggle('visible', showDown);
  if (!atBottom) _scrollLocked = false;
}

function _scrollToBottomIfNear(el) {
  if (!el) return;
  // 2-stage: new content arriving while pinned at the bottom invalidates the
  // stage-2 arm — the user's next over-scroll is a fresh stage 1.
  if (typeof app._resetBottomArm === 'function') app._resetBottomArm('content added near bottom');
  if (!_scrollLocked) {
    _updateScrollChevron(el);
    return;
  }
  const _ = el.offsetHeight;
  _programmaticScroll = true;
  el.scrollTop = el.scrollHeight;
  if (_scrollBtn) _scrollBtn.classList.remove('visible');
}

/** Swap a jump-nav FAB's chevron for a spinner while a DB reposition is in
 *  flight, then restore it. Pure CSS (.chat-nav-fab.busy hides the svg and
 *  shows a spinning ::after) — no icon juggling needed. */
function _setBtnBusy(btn, busy) {
  if (!btn) return;
  if (busy) {
    btn.classList.add('busy');
    btn.setAttribute('aria-busy', 'true');
  } else {
    btn.classList.remove('busy');
    btn.setAttribute('aria-busy', 'false');
  }
}

// ── Delegated pill resize listeners ────────────────────────────────────────
let _resizeTimer = null;
function _debouncedAutoResizePill(el) {
  if (_resizeTimer) clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(() => {
    _resizeTimer = null;
    _autoResizePill(el);
  }, 100);
}

document.addEventListener('input', (e) => {
  const t = e.target;
  if (t && t.classList && t.classList.contains('chat-pill-input')) {
    _debouncedAutoResizePill(t);
  }
}, true);

document.addEventListener('scroll', (e) => {
  const t = e.target;
  if (t && t.classList && t.classList.contains('chat-pill-input')) {
    _updateScrollIndicator(t);
  }
}, true);

function _resizeAllPills() {
  document.querySelectorAll('.chat-pill-input').forEach(_autoResizePill);
}
window.addEventListener('load', _resizeAllPills);
window.addEventListener('resize', _resizeAllPills);

window.addEventListener('access-mode-loaded',  applyChatGate);
window.addEventListener('access-mode-changed', applyChatGate);

export function initChat() {
  app.isDebug = isDebugMode();

  // ── DOM refs ───────────────────────────────────────────────────────────────
  // ── Relative-time ticker ──
  setInterval(_refreshAllBubbleTimes, 1000);

  // ── Expose chat functions on app (core functions set at module scope) ──
  app.updateLastBubble = updateLastBubble;
  app.appendStreamToActiveBubble = appendStreamToActiveBubble;
  app.finalizeAgentResponse = finalizeAgentResponse;
  app.ensureStreamingBubbleForActiveTurn = ensureStreamingBubbleForActiveTurn;
  app.seedStreamingBubble = seedStreamingBubble;
  app.finalizeAgentStep = finalizeAgentStep;
  app.markAgentInterrupted = markAgentInterrupted;
  app.attachToolCallsToLastBubble = attachToolCallsToLastBubble;
  app.autoResizeChatInput = () => _autoResizePill(app.chatInput);
  app.focusChatInput = () => { if (app.chatInput) app.chatInput.focus(); };
  app._sendStopMessage = sendStopMessage;
  // Let shared modules fire a chat send programmatically (e.g. the footer
  // compaction panel's "Compact now" button, which sends "/compact" through the
  // pipeline WITHOUT staging it in the pill — the user's draft stays untouched).
  app.sendChatMessage = sendMessage;

  // ── "Session not found" (open session permanently deleted elsewhere) ──
  // Fired from agentWs on a `session_deleted` event — another tab, or another
  // device via the hybrid tombstone sync. When it targets the OPEN session we
  // drop the (now-erased) transcript, show a centred notice, and lock the
  // composer. clearSessionNotFound() undoes the lock when the user navigates to a
  // live session (called at the top of loadSessionChat). A recycle does NOT fire
  // this — a binned session's transcript is still viewable.
  app.onSessionDeleted = function (event) {
    const targetId = (event && (event.session_id || event.sessionId)) || '';
    // Its local mirror row is already gone — refresh the sidebar so it drops out.
    if (app.currentUserId && typeof app.populateSessionSelect === 'function') {
      try { app.populateSessionSelect(app.currentUserId); } catch (_) { /* best-effort */ }
    }
    if (!targetId || targetId !== app.currentSessionId) return;
    app._sessionNotFound = targetId;
    if (app.chatMessages) {
      app.chatMessages.innerHTML = '';
      addChatBubble('agent', chatMsg('session_deleted_notice'), 'session-deleted-notice');
    }
    if (app.chatInput) { app.chatInput.value = ''; app.chatInput.disabled = true; }
    if (app.chatSend) app.chatSend.disabled = true;
  };
  app.clearSessionNotFound = function () {
    if (!app._sessionNotFound) return;
    app._sessionNotFound = null;
    if (app.chatInput) app.chatInput.disabled = false;
    // The send button's enabled state is re-derived by loadSessionChat.
  };

  // ── Event wiring ──
  app.chatSend.addEventListener('click', sendMessage);
  app.chatInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      // On touch devices (mobile), let Enter insert a newline; send via the button.
      if (window.matchMedia?.('(pointer: coarse)').matches) return;
      e.preventDefault();
      sendMessage();
    }
  });

  // ── Idle/active footer mode (rebuilds pill rows on mode switch) ──
  let _idleDebounce = null;

  // Last pointerdown target. The idle check uses it to tell "clicked a footer
  // control / floating panel" (keep active) from "clicked into the chat
  // content area" (collapse). Focus alone can't tell: floating-panel rows and
  // the chat content are non-focusable, so activeElement either stays pinned
  // on the footer button or falls back to <body> — neither reveals where the
  // user actually clicked.
  let _lastPointerTarget = null;
  let _pointerGestureActive = false;
  let _pendingFooterMode = null;
  let _footerModeSettleTimer = null;

  // Changing footer modes reparents the textarea and every pill button. If that
  // happens between pointerdown and click, the browser cancels the click because
  // its original target moved to a different DOM parent. Defer pointer-initiated
  // mode changes until the complete pointerup -> click sequence has settled.
  function _switchFooterModeSafely(mode) {
    if (_pointerGestureActive) {
      _pendingFooterMode = mode;
      return;
    }
    _pendingFooterMode = null;
    switchFooterMode(mode);
  }

  function _settlePointerGesture() {
    _pointerGestureActive = false;
    if (!_pendingFooterMode) return;
    if (_footerModeSettleTimer) clearTimeout(_footerModeSettleTimer);
    _footerModeSettleTimer = setTimeout(() => {
      _footerModeSettleTimer = null;
      if (_pointerGestureActive || !_pendingFooterMode) return;
      const mode = _pendingFooterMode;
      _pendingFooterMode = null;
      switchFooterMode(mode);
    }, 0);
  }

  document.addEventListener('pointerdown', (e) => {
    _lastPointerTarget = e.target;
    _pointerGestureActive = true;
    if (_footerModeSettleTimer) {
      clearTimeout(_footerModeSettleTimer);
      _footerModeSettleTimer = null;
    }
  }, true);
  document.addEventListener('pointerup', _settlePointerGesture, true);
  document.addEventListener('pointercancel', _settlePointerGesture, true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Tab') _lastPointerTarget = null; }, true);

  function _isFooterInteractionTarget(t) {
    if (!t || typeof t.closest !== 'function') return false;
    const footerRow = document.getElementById('chat-footer-row');
    if (footerRow && footerRow.contains(t)) return true;
    const pill = document.getElementById('chat-input-row');
    if (pill && pill.contains(t)) return true;
    const abovePill = document.getElementById('chat-above-pill');
    if (abovePill && abovePill.contains(t)) return true;
    return !!t.closest('.chat-model-list-panel, .chat-model-picker, .chat-skill-picker, .chat-claude-panel');
  }

  function _scheduleIdleCheck() {
    if (_idleDebounce) clearTimeout(_idleDebounce);
    _idleDebounce = setTimeout(() => {
      _idleDebounce = null;
      // Keep the active footer when the last interaction was with a footer
      // control or one of its floating panels; collapse to idle only when the
      // user clicked away into the chat content area and the pill is empty.
      if (_isFooterInteractionTarget(_lastPointerTarget)
          || _isFooterInteractionTarget(document.activeElement)) return;
      if (!app.chatInput.value.trim()) {
        _switchFooterModeSafely('idle');
      }
    }, 150);
  }

  // Clicking the idle pill background focuses the textarea → triggers keyboard
  // → focus handler switches to active mode. No pointer capture needed since
  // the active layout is always present in the DOM (just hidden via CSS).
  const _pillEl = document.getElementById('chat-input-row');
  if (_pillEl) {
    _pillEl.addEventListener('pointerdown', (e) => {
      if (getFooterMode() !== 'idle') return;
      const el = e.target;
      const tag = el?.tagName?.toLowerCase();
      if (tag === 'button' || tag === 'textarea' || tag === 'input') return;
      if (el?.closest?.('button')) return;
      // Presses in the right action zone (stop/continue/mic) must never be
      // swallowed by the pointer capture: a press on the zone's padding or
      // gaps would otherwise focus the textarea and synchronously rebuild
      // the pill into the active layout, hiding + reparenting the buttons
      // mid-press — the browser then retargets the click to the pill and
      // the first click on Continue silently does nothing (desktop-only).
      if (el?.closest?.('.chat-pill-layout-zone-right')) return;
      if (!app.chatInput) return;
      // Capture the pointer so that pointerup / click fire on the pill
      // element and NOT on active-layout buttons that appear under the
      // cursor after the synchronous mode switch below.
      _pillEl.setPointerCapture(e.pointerId);
      e.preventDefault();
      app.chatInput.focus();
    });
    // Release pointer capture once the finger lifts
    _pillEl.addEventListener('pointerup', (e) => {
      if (_pillEl.hasPointerCapture?.(e.pointerId)) {
        _pillEl.releasePointerCapture(e.pointerId);
      }
    });
    _pillEl.addEventListener('lostpointercapture', (e) => {
      // Belt-and-suspenders: release if the browser took it away
    });
  }

  app.chatInput.addEventListener('focus', () => {
    if (_idleDebounce) { clearTimeout(_idleDebounce); _idleDebounce = null; }
    _prewarm();
    document.body.classList.add('chat-pill-focused');
    _switchFooterModeSafely('active');
  });
  app.chatInput.addEventListener('blur', () => {
    document.body.classList.remove('chat-pill-focused');
    // Suppress idle-debounce while the pill is being rebuilt
    // (hideElement during applyChatPillLayout causes a synchronous blur).
    if (app.__rebuildingPill) return;
    _scheduleIdleCheck();
  });

  // When focus finally leaves the footer row — e.g. the user clicked into the
  // chat content area after using the model changer / abilities / mode button —
  // run the same idle check. Without this, the footer stays expanded forever
  // once a footer control holds focus (the textarea blur above was suppressed).
  const _footerRowEl = document.getElementById('chat-footer-row');
  if (_footerRowEl) {
    _footerRowEl.addEventListener('focusout', (e) => {
      if (e.relatedTarget && _isFooterInteractionTarget(e.relatedTarget)) return;
      if (app.__rebuildingPill) return;
      _scheduleIdleCheck();
    });
  }

  // On mobile, the Android OS nav buttons can dismiss the keyboard without
  // firing blur on the input — the input stays focused but the keyboard is
  // gone.  Detect this via visualViewport resize: when the viewport returns
  // to full height (keyboard closed) while the input still has focus, blur
  // the input so the natural focus/blur cycle stays intact for the next tap.
  if (window.visualViewport) {
    let _lastVpHeight = window.visualViewport.height;
    window.visualViewport.addEventListener('resize', () => {
      const vp = window.visualViewport;
      if (!vp) return;
      const grew = vp.height > _lastVpHeight + 80;
      if (grew && document.body.classList.contains('chat-pill-focused')) {
        // Blur the input so the header reappears AND next tap fires a fresh
        // focus event that re-hides it.  requestAnimationFrame avoids fighting
        // the resize cycle / focus re-entry.
        requestAnimationFrame(() => {
          if (document.body.classList.contains('chat-pill-focused') && app.chatInput) {
            // Keyboard-dismiss blur isn't a user click — clear the tracked
            // pointer target so the normal idle collapse still runs.
            _lastPointerTarget = null;
            app.chatInput.blur();
          }
        });
      }
      _lastVpHeight = vp.height;
    });
  }

  // Mobile keyboard: tapping a footer control (mode / model / abilities /
  // stop / continue / suggestion chips) while typing must NOT dismiss the
  // keyboard. These are typing-adjacent interactions — keep focus on the pill
  // so the user can pick a model / flip a mode and keep typing. The floating
  // picker panels are mounted on <body>, so listen on document with the full
  // footer-area selector (mirrors _isFooterInteractionTarget above).
  keepPillFocusOnFooterTap(
    document,
    app.chatInput,
    '#chat-input-area, .chat-model-list-panel, .chat-model-picker, .chat-skill-picker, .chat-claude-panel'
  );

  // Terminal-tunnel UI
  try { initChatTunnel(); } catch (_) { /* non-fatal */ }
  try { initChatComponents(); } catch (_) { /* non-fatal */ }
  // Terminal Chat engine — live xterm.js view when agent uses terminal_chat engine.
  // Its on-screen keys (footer toggle + number-pad) are plugin-owned and loaded
  // lazily by the engine only when a terminal-chat session mounts.
  try { initTerminalChatEngine(); } catch (_) { /* non-fatal */ }

  app.chatInput.addEventListener('input', () => {
    if (!_canChat()) { app.chatSend.disabled = true; _updateInputRowState(); return; }
    const hasText = !!app.chatInput.value.trim();
    app.chatSend.disabled = !hasText;
    _updateInputRowState();
    _saveDraft();
    // If user starts typing while idle, switch to active footer.
    // Skip when the continue button set the value programmatically — the
    // pill rebuild would move the button under the cursor and the 300ms
    // pill-no-clicks block would eat a natural double-click.
    if (hasText && getFooterMode() === 'idle' && !app._continueClick) {
      if (_idleDebounce) { clearTimeout(_idleDebounce); _idleDebounce = null; }
      _switchFooterModeSafely('active');
    }
    // Keep the warmed prep fresh while a long message is composed (throttled).
    _prewarm();
  });

  // ── Execution mode toggle (per-session key) ──
  // The pill is ENGINE-AWARE. Native WebAgent agents cycle their configured
  // modes (Ask / Plan / Auto by default);
  // Codex engine agents cycle Ask (read-only) → Wkspc (workspace-write) → Auto
  // (full access), because `codex exec` has no GUI confirmation — for Codex,
  // 'ask' means read-only and workspace-write is its own mode. Legacy saved
  // values (the old Read/Write/Auto) are migrated on read: read→plan, write→ask.
  const MODE_LEGACY = { read: 'plan', write: 'ask' };
  const MODE_CODEX_CYCLE = ['ask', 'wkspc', 'auto'];
  const MODE_CODEX_LABELS = { ask: 'Ask', wkspc: 'Wkspc', auto: 'Auto' };
  const MODE_CODEX_TITLES = { ask: 'Read-only (no writes)', wkspc: 'Workspace-write', auto: 'Full access' };
  app.executionMode = 'ask';
  const modeBtn = document.getElementById('chat-mode-btn');

  // Engine of the currently selected agent ('' / 'claude_code' / 'codex' …),
  // read from the same shared agents list the mode seeding uses.
  function _agentEngine() {
    try {
      const id = app.currentAgentId;
      if (!id) return '';
      const list = window.__agentsSharedData && window.__agentsSharedData.agents;
      if (Array.isArray(list)) {
        const a = list.find(x => x && x.id === id);
        return (a && a.engine) || '';
      }
    } catch (_) { /* non-fatal */ }
    return '';
  }

  function _activeAgent() {
    try {
      const list = window.__agentsSharedData && window.__agentsSharedData.agents;
      return Array.isArray(list) ? list.find(x => x && x.id === app.currentAgentId) : null;
    } catch (_) { return null; }
  }

  // Mode vocabulary for the current agent's engine — cycle order, labels, hints,
  // and coercion of legacy/foreign saved values ('plan' on a codex session was
  // the old read-only slot → ask; 'wkspc' on a native session → invalid).
  function _modeCtx() {
    const codex = _agentEngine() === 'codex';
    let cycle = MODE_CODEX_CYCLE;
    let labels = MODE_CODEX_LABELS;
    let titles = MODE_CODEX_TITLES;
    if (!codex) {
      const configured = (_activeAgent()?.execution_modes || []).filter(
        mode => mode && typeof mode.id === 'string' && mode.id
      );
      const modes = configured.length ? configured : [
        { id: 'ask', label: 'Ask', description: 'Proposal only', permission_policy: 'read_only' },
        { id: 'plan', label: 'Plan', description: 'Read-only planning', permission_policy: 'read_only' },
        { id: 'auto', label: 'Auto', description: 'Full access', permission_policy: 'write' },
      ];
      cycle = modes.map(mode => mode.id);
      labels = Object.fromEntries(modes.map(mode => [mode.id, mode.label || mode.id]));
      titles = Object.fromEntries(modes.map(mode => [
        mode.id,
        `${mode.description || mode.label || mode.id} · ${mode.permission_policy === 'write' ? 'Write-capable' : 'Read-only'}`,
      ]));
    }
    const coerce = (v) => {
      if (!v) return undefined;
      v = String(v).toLowerCase();
      if (MODE_LEGACY[v]) v = MODE_LEGACY[v];
      if (cycle.includes(v)) return v;
      if (codex && v === 'plan') return 'ask';
      return undefined;
    };
    return { codex, cycle, labels, titles, coerce };
  }

  // The active agent's configured "Default chat mode" (Config tab →
  // metadata.default_execution_mode, served on the agent record as
  // default_execution_mode). A FRESH session with no per-session saved mode
  // STARTS in this; once a session has its own saved mode (the user touched the
  // pill or sent a message) that wins. Falls back to 'ask' when unset.
  function _agentDefaultMode() {
    const ctx = _modeCtx();
    try {
      const id = app.currentAgentId;
      if (!id) return 'ask';
      const list = window.__agentsSharedData && window.__agentsSharedData.agents;
      if (Array.isArray(list)) {
        const a = list.find(x => x && x.id === id);
        const m = ctx.coerce(a && a.default_execution_mode);
        if (m) return m;
      }
    } catch (_) { /* non-fatal — fall through to 'ask' */ }
    return 'ask';
  }

  function _applyExecutionMode() {
    if (!modeBtn) return;
    const ctx = _modeCtx();
    const fallback = _agentDefaultMode();
    const sid = app.currentSessionId;
    if (!sid) {
      app.executionMode = fallback;
      modeBtn.textContent = ctx.labels[fallback] || 'Ask';
      modeBtn.title = ctx.titles[fallback] || '';
      return;
    }
    const saved = kvRead('chat:execMode:' + sid, 'chat_execution_mode_' + sid);
    app.executionMode = ctx.coerce(saved) || fallback;
    modeBtn.textContent = ctx.labels[app.executionMode] || 'Ask';
    modeBtn.title = ctx.titles[app.executionMode] || '';
  }
  app.refreshExecutionMode = _applyExecutionMode;

  if (modeBtn) {
    _applyExecutionMode();
    modeBtn.addEventListener('click', () => {
      const ctx = _modeCtx();
      const idx = ctx.cycle.indexOf(app.executionMode);
      app.executionMode = ctx.cycle[(idx + 1) % ctx.cycle.length];
      modeBtn.textContent = ctx.labels[app.executionMode];
      modeBtn.title = ctx.titles[app.executionMode] || '';
      // Live transcript notice next to the bubbles ("Ask mode enabled" etc.).
      // NOT inside setExecutionMode — that path also runs on session load,
      // which must not spam notices.
      if (typeof app.notifyExecutionMode === 'function') {
        try { app.notifyExecutionMode(app.executionMode); } catch (_) {}
      }
      const sid = app.currentSessionId;
      if (sid) {
        kvWrite('chat:execMode:' + sid, 'chat_execution_mode_' + sid, app.executionMode);
        // Persist server-side so the loop can re-read mid-turn (mirrors _selectModel).
        fetch(apiPath('/api/v1/chat/session-mode'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...authHeaders() },
          body: JSON.stringify({
            user_id: app.currentUserId || '',
            session_id: sid,
            mode: app.executionMode,
          }),
        }).catch(() => { /* fire-and-forget — best-effort */ });
      }
    });
  }

  // ── Mid-turn message visibility (show_mid_turn_messages) ──
  // No chat control button — the flag is managed via chat_ui.json
  // (chat_common.show_mid_turn_messages) or the agent's config tab. Applied
  // at boot; hand-edits saved in Explorer are picked up live via the
  // 'chat-ui-file-saved' custom event.
  function _applyThinkingVisibility() {
    const cc = window.__chatUiSnapshot && window.__chatUiSnapshot.chat_common;
    app._showMidTurn = cc && cc.show_mid_turn_messages !== undefined
      ? !!cc.show_mid_turn_messages
      : true;
  }
  _applyThinkingVisibility();
  window.addEventListener('chat-ui-file-saved', async () => {
    // Re-fetch the fresh config so the in-memory snapshot reflects the save.
    try {
      const url = apiPath('/api/v1/auth/ui-config');
      const res = await fetch(url);
      if (res.ok) {
        const body = await res.json();
        if (body && body.chat_ui) {
          window.__chatUiSnapshot = body.chat_ui;
        }
      }
    } catch (_) {}
    _applyThinkingVisibility();
  });

  // Export reload so session-core.js can call it on session switch
  app.reloadExecutionMode = _applyExecutionMode;
  app.reloadThinking = _applyThinkingVisibility;

  // ── Target-device pill (per-session) ──
  // Picks which device in the fleet runs the next message. '' = this device
  // (runs locally, exactly like before); a chosen instance-id is sent as
  // request.target_device so the backend hands the turn to that device's worker
  // to run inside THIS session (the reply flows back over the shared DB). The
  // choice is remembered per session, like the execution mode above.
  app.targetDevice = '';
  const targetBtn = document.getElementById('chat-target-btn');
  // Friendly-name fallback: the server (and the picker) hand us a device's label
  // when the executor is chosen/applied. Cache it so the pill can show the name
  // even for a device that's offline or not yet in the picker's loaded list
  // (DevicePicker.labelFor would otherwise fall back to the raw instance id).
  const _targetLabelCache = {};

  function _renderTargetPill() {
    if (!targetBtn) return;
    const labelEl = targetBtn.querySelector('.chat-target-label');
    const inst = app.targetDevice || '';
    if (inst) {
      targetBtn.classList.add('targeting');
      let lbl = (window.DevicePicker && window.DevicePicker.labelFor)
        ? window.DevicePicker.labelFor(inst) : inst;
      if ((!lbl || lbl === inst) && _targetLabelCache[inst]) lbl = _targetLabelCache[inst];
      if (labelEl) labelEl.textContent = lbl;
      targetBtn.title = 'Remote Control: running on ' + lbl + ' — click to change';
    } else {
      targetBtn.classList.remove('targeting');
      if (labelEl) labelEl.textContent = '';
      targetBtn.title = 'Remote Control — choose which device runs your next message';
    }
  }

  // The active agent's configured "Default device" (Config tab →
  // metadata.default_target_device, served on the agent record as
  // default_target_device). Returns the instance-id or '' (none configured).
  function _agentDefaultDevice() {
    try {
      const id = app.currentAgentId;
      if (!id) return '';
      const list = window.__agentsSharedData && window.__agentsSharedData.agents;
      if (Array.isArray(list)) {
        const a = list.find(x => x && x.id === id);
        const d = a && a.default_target_device;
        if (d && typeof d === 'string') return d;
      }
    } catch (_) { /* non-fatal — no configured default */ }
    return '';
  }

  // Apply the agent's configured default device for a session that has NO explicit
  // per-session executor. Only takes effect if that device is currently ONLINE;
  // when it's offline (or unknown) the chat stays on "this device". Async — needs
  // the live fleet list to check reachability. Deliberately does NOT persist (no
  // localStorage / session-metadata write): it's a soft default re-evaluated on
  // every open, so a device coming back online is auto-picked next time and the
  // user's own pick (which does persist) always wins. Guards on the session id +
  // an unset target both before and after the await so it can't clobber a choice
  // made while the fleet list was loading.
  async function _applyAgentDefaultTarget(sid) {
    const want = _agentDefaultDevice();
    if (!want) return;
    if (app.currentSessionId !== sid || app.targetDevice) return;
    try {
      const devices = (window.DevicePicker && window.DevicePicker.load)
        ? await window.DevicePicker.load() : [];
      if (app.currentSessionId !== sid || app.targetDevice) return;
      const dev = Array.isArray(devices) ? devices.find(d => d && d.instance_id === want) : null;
      if (dev && dev.online && !dev.is_self) {
        app.targetDevice = dev.instance_id;
        _targetLabelCache[dev.instance_id] = dev.label || dev.instance_id;
        _renderTargetPill();
      }
      // Offline / unknown → leave it on "this device" (the fall-back the user chose).
    } catch (_) { /* best-effort — stay local */ }
  }
  // Exposed so session-load.js can apply the agent default when a fetched session
  // has no stored Remote Control executor.
  app.applyAgentDefaultTarget = _applyAgentDefaultTarget;

  function _applyTargetDevice() {
    const sid = app.currentSessionId;
    app.targetDevice = '';
    if (sid) {
      const saved = kvRead('chat:targetDevice:' + sid, 'chat_target_device_' + sid);
      if (saved) app.targetDevice = saved;
    }
    _renderTargetPill();
    // No explicit per-session choice → fall back to the agent's configured default
    // device (applied only when it's online; otherwise the pill stays on "this
    // device"). Non-persisting, so it re-evaluates each open.
    if (!app.targetDevice) _applyAgentDefaultTarget(sid);
  }

  if (targetBtn) {
    _applyTargetDevice();
    // Warm the device list so the pill can show the chosen device's real name.
    if (window.DevicePicker && window.DevicePicker.load) {
      window.DevicePicker.load().then(_renderTargetPill).catch(() => {});
    }
    targetBtn.addEventListener('click', (e) => {
      e.preventDefault();
      if (!window.DevicePicker || !window.DevicePicker.open) return;
      window.DevicePicker.open(targetBtn, {
        title: 'Remote Control — run this chat on',
        currentInstance: app.targetDevice || '',
        onSelect: (sel) => {
          const inst = sel.is_self ? '' : (sel.instance_id || '');
          const label = sel.is_self ? '' : (sel.label || '');
          app.setTargetDevice(inst, label);
          // Persist on the SESSION (not just this browser) so any device that
          // opens this session routes to the same executor. Best-effort — the
          // local pill already reflects the choice.
          _persistTargetDeviceToSession(app.currentSessionId, inst, label);
        },
      });
    });
  }

  // Export reload so session switches re-read the per-session target (mirrors
  // app.reloadExecutionMode).
  app.reloadTargetDevice = _applyTargetDevice;

  // Set the Remote Control executor for the CURRENT session locally: update the
  // pill + the fast localStorage seed. Mirrors app.setExecutionMode. The server
  // (session metadata) is the cross-device source of truth — session-load.js
  // applies data.remote_executor through here on open, and the picker's onSelect
  // both calls this AND persists to the session. '' = run locally.
  app.setTargetDevice = function (instance, label) {
    const inst = instance || '';
    app.targetDevice = inst;
    if (inst && label) _targetLabelCache[inst] = label;
    const sid = app.currentSessionId;
    if (sid) {
      if (inst) kvWrite('chat:targetDevice:' + sid, 'chat_target_device_' + sid, inst);
      else kvDelete('chat:targetDevice:' + sid, 'chat_target_device_' + sid);
    }
    _renderTargetPill();
  };

  // Persist the chosen executor onto the session row (metadata) so it follows the
  // session across devices, not just this browser. Fire-and-forget.
  function _persistTargetDeviceToSession(sid, inst, label) {
    if (!sid) return;
    fetch(apiPath('/api/v1/db/sessions/' + encodeURIComponent(sid) + '?db=user.db'), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        remote_executor_instance: inst || '',
        remote_executor_label: label || '',
      }),
    }).catch(() => { /* pill already reflects the choice; sync is best-effort */ });
  }

  // ── Agent-driven mode switch (set_execution_mode tool → WS echo) ──
  // The backend broadcasts an `execution_mode` event when the agent flips the
  // mode mid-conversation (e.g. Plan→Auto after the user approves a plan).
  // agentWs.js routes it here so the pill below the chat visibly switches and
  // the next message the user sends carries the new mode (persisted per-session).
  app.setExecutionMode = function (mode) {
    const ctx = _modeCtx();
    const m = ctx.coerce(mode);
    if (m === undefined) return;
    app.executionMode = m;
    if (modeBtn) { modeBtn.textContent = ctx.labels[m]; modeBtn.title = ctx.titles[m] || ''; }
    const sid = app.currentSessionId;
    if (sid) {
      kvWrite('chat:execMode:' + sid, 'chat_execution_mode_' + sid, m);
    }
  };

  // ── Continue button ──
  const continueBtn = document.getElementById('chat-continue-btn');
  if (continueBtn) {
    continueBtn.addEventListener('click', async () => {
      if (!_canChat()) { applyChatGate(); return; }
      if (!app.currentAgentId) return;
      const sessionId = app.currentSessionId;
      if (sessionId) {
        try {
          const resp = await fetch(apiPath('/api/v1/chat/resume'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...authHeaders() },
            body: JSON.stringify({ session_id: sessionId }),
          });
          const data = resp.ok ? await resp.json().catch(() => ({})) : {};
          if (data.resumed) {
            app.isProcessing = true;
            if (app.chatActivityStart) app.chatActivityStart('Resuming…');
            _updateActionBtns();
            return;
          }
        } catch (_) { /* fall through to the ordinary new-turn path */ }
      }
      app._continueClick = true;
      const saved = app.chatInput ? app.chatInput.value : '';
      app.chatInput.value = 'continue';
      app._continueClick = false;
      sendMessage();
      // Restore whatever the user was typing so it isn't lost
      setTimeout(() => { app.chatInput.value = saved; _updateInputRowState(); _autoResizePill(app.chatInput); }, 100);
    });
  }

  // ── Stop button ──
  const stopBtn = document.getElementById('chat-stop-btn');
  if (stopBtn) stopBtn.addEventListener('click', () => {
    if (!app.isProcessing) return;
    abortChatStream();
    sendStopMessage();
  });

  // ── Global Escape: stop conversation ──
  document.addEventListener('keydown', function _escHandler(e) {
    if (e.key !== 'Escape') return;

    // Let dropdown/overlay Esc handlers take priority (agent menu, session menu, picker)
    const agentMenu = document.getElementById('chat-agent-menu');
    const sessionMenu = document.getElementById('chat-session-menu');
    if ((agentMenu && !agentMenu.hidden) || (sessionMenu && !sessionMenu.hidden)) return;

    if (!app.isProcessing) return;

    e.preventDefault();
    abortChatStream();
    sendStopMessage();
  });

  // ── In-pill stop / continue visibility ──
  function _updateActionBtns() {
    if (!stopBtn || !continueBtn) return;
    const hasAgent = !!app.currentAgentId;
    const hasInput = app.chatInput && app.chatInput.value.trim();
    // Stop: only when agent is processing
    // Continue: visible whenever there's no input text (processing or idle),
    //           but hide entirely when user is typing to avoid misclicks
    if (hasAgent) {
      stopBtn.hidden = !app.isProcessing;
      continueBtn.hidden = !!hasInput;
    } else {
      stopBtn.hidden = true;
      continueBtn.hidden = true;
    }
  }
  setInterval(_updateActionBtns, 500);
  _updateActionBtns();

  // ── Gate + draft restore ──
  applyChatGate();
  fetchAccessMode().then(() => {
    applyChatGate();
    _restoreDraft();
    if (_outboxHasPending()) {
      _renderPendingBubbles();
      _startOutboxPoll();
    }
  });
  _restoreDraft();

  // ── Input area padding observer ──
  const inputArea = document.getElementById('chat-input-area');
  const messagesInner = document.getElementById('chat-messages-inner');
  const messagesEl = document.getElementById('chat-messages');
  var _pullingBottom = false;  // true while a bottom-pull gesture is active
  var _prefetchWarming = false;  // true once we've pre-fetched for this gesture
  var _bottomArmed = false;  // 2-stage: true after the FIRST bottom over-scroll (stage 1 settle); only then does the next pull show the pill and refresh (stage 2)
  var _armedSession = null;  // session id the stage-2 arm belongs to — arm dies on session switch
  var _tailPadPx = 100;  // live tail-clearance padding of #chat-messages-inner (updated by syncPad)
  function pulLog(msg) {
    // Diagnostic only — no-op in production. Enable live from the console:
    //   window.__CHAT_PULL_DEBUG__ = true
    if (!(app && app.isDebug) && !window.__CHAT_PULL_DEBUG__) return;
    console.log('DEBUG-TAG:[chat-pull] ' + msg);
  }
  // Reset the stage-2 arm whenever the context the stage-1 settle happened in
  // is invalidated: user/content scrolled away from the bottom, new messages
  // arrived, the session changed, or the chat view was hidden. Stage 2 must
  // only ever fire directly after a stage-1 settle in the SAME session.
  function _resetBottomArm(reason) {
    if (_bottomArmed || _armedSession) {
      _bottomArmed = false;
      _armedSession = null;
      pulLog('bottom arm RESET — ' + reason);
    }
  }
  app._resetBottomArm = _resetBottomArm;  // module-scope hooks (content add, scroll) call this
  // 2-stage: hiding the chat view (body.chat-hidden — navigating to other
  // views) invalidates the stage-2 arm.
  if (document.body && typeof MutationObserver !== 'undefined') {
    const _bodyObs = new MutationObserver(function() {
      if (document.body.classList.contains('chat-hidden')) _resetBottomArm('view hidden');
    });
    _bodyObs.observe(document.body, { attributes: true, attributeFilter: ['class'] });
  }
  applyRubberBand(messagesEl, {
    axis: 'y',
    pullThreshold: 75,
    pullThresholdBottom: 50,  // swipe-up refresh fires at half the pull of the top load-older gesture
    // Refresh arms ONLY at the very bottom (within ~4px of the true maxS).
    // If you're still short of the bottom, the swipe is left to the native
    // scroll — it scrolls you the rest of the way instead of refreshing —
    // and the pull engages on a later move once the scroller reaches the
    // bottom. No jump, no hijack of normal scrolling, no refresh unless
    // you're actually at the bottom.
    bottomEdgeTol: 4,
    onPullTop: function(ratio) {
      var spinner = document.getElementById('chat-spinner-top');
      pulLog('onPullTop ratio=' + ratio + ' spinner=' + (spinner ? 'OK' : 'MISSING') + ' isDebug=' + app.isDebug);
      if (ratio > 0) {
        app._rbPullActive = true;
        if (app.isDebug && spinner) {
          spinner.classList.add('visible');
          spinner.classList.remove('loading');
          var label = spinner.querySelector('.chat-spinner-label');
          if (label) label.textContent = ratio >= 1 ? 'Release to load' : 'Pull to load';
          pulLog('top pill VISIBLE — label="' + (ratio >= 1 ? 'Release to load' : 'Pull to load') + '"');
        }
      } else {
        // When snapping back without having triggered, clear the flag and hide.
        // If loading has already started (onTriggerTop fired), don't touch the spinner.
        var loading = spinner && spinner.classList.contains('loading');
        if (!loading) {
          app._rbPullActive = false;
          if (app.isDebug && spinner) spinner.classList.remove('visible');
          pulLog('top pill HIDDEN (ratio=0, loading=' + loading + ')');
        }
      }
    },
    onTriggerTop: function() {
      var spinner = document.getElementById('chat-spinner-top');
      pulLog('onTriggerTop fired → loading older messages');
      if (app.isDebug && spinner) {
        spinner.classList.add('visible', 'loading');
        var label = spinner.querySelector('.chat-spinner-label');
        if (label) label.textContent = 'Loading...';
      }
      if (typeof app._checkHardScrollBoundary === 'function') {
        app._rbPullActive = false;  // allow the boundary check to pass through
        app._checkHardScrollBoundary(app.currentSessionId).then(function() {
          if (app.isDebug && spinner) {
            spinner.classList.remove('visible', 'loading');
          }
        });
      } else if (typeof app._maybeLoadMoreOnScrollTop === 'function') {
        app._rbPullActive = false;
        app._maybeLoadMoreOnScrollTop(app.currentSessionId);
        setTimeout(function() {
          if (app.isDebug && spinner) spinner.classList.remove('visible', 'loading');
        }, 1000);
      }
    },
    onPullBottom: function(ratio, absPull) {
      var spinner = document.getElementById('chat-spinner-bottom');
      pulLog('onPullBottom ratio=' + ratio + ' absPull=' + absPull + ' spinner=' + (spinner ? 'OK' : 'MISSING') + ' _pullingBottom=' + _pullingBottom + ' _prefetchWarming=' + _prefetchWarming + ' _bottomArmed=' + _bottomArmed);
      // 2-stage: the arm belongs to the session it was settled in. Any
      // session switch (dropdown, swipe, new-session, delete → navigate)
      // invalidates it — the first over-scroll in the new session is stage 1.
      if (_armedSession !== app.currentSessionId) _resetBottomArm('session changed');
      if (ratio > 0 && !_pullingBottom) {
        _pullingBottom = true;
        _prefetchWarming = false;
      } else if (ratio === 0) {
        if (_pullingBottom) {
          // Stage 1 complete: the first bottom over-scroll settled and
          // released without triggering. Arm stage 2 — the next bottom
          // over-scroll shows the pill and can refresh. (A successful
          // refresh resets _bottomArmed in onTriggerBottom, and it sets
          // _pullingBottom=false BEFORE the trailing ratio-0 callback, so
          // this arm can't re-fire after a refresh.)
          _bottomArmed = true;
          _armedSession = app.currentSessionId;
          pulLog('stage 1 settle complete → ARMED for stage 2 refresh');
        }
        _pullingBottom = false;
      }
      // Stage 1 (unarmed): rubber band + spring only — NO pill, even past
      // the threshold. Stage 2 (armed): pill shows and prefetch warms.
      var show = ratio > 0 && _bottomArmed;
      if (show && spinner) {
        spinner.classList.add('visible');
        spinner.classList.remove('loading');
        var label = spinner.querySelector('.chat-spinner-label');
        if (label) label.textContent = ratio >= 1 ? 'Release to refresh' : 'Pull to refresh';
        pulLog('bottom pill VISIBLE — label="' + (ratio >= 1 ? 'Release to refresh' : 'Pull to refresh') + '" classes="' + spinner.className + '"');
      } else if (spinner) {
        var loading = spinner.classList.contains('loading');
        if (!loading) spinner.classList.remove('visible');
        pulLog('bottom pill HIDDEN (ratio=' + ratio + ', loading=' + loading + ')');
      }
      // At threshold, pre-fetch the refresh data so the HTTP response is
      // cached in the browser by the time the user releases — no network
      // wait, only the DOM update happens on release. Stage 2 only: never
      // warm on the stage-1 settle pull.
      if (ratio >= 1 && _bottomArmed && !_prefetchWarming) {
        _prefetchWarming = true;
        pulLog('threshold reached → prefetch warming (session ' + app.currentSessionId + ')');
        var sid = app.currentSessionId;
        if (sid) {
          var token = localStorage.getItem('auth_token') || '';
          var url = apiPath('/api/v1/db/session-messages?db=user.db&session_id=' + encodeURIComponent(sid) + '&limit=60&light=1');
          if (token) url += '&token=' + encodeURIComponent(token);
          fetch(url).catch(function() {});
        }
      }
    },
    onTriggerBottom: function(absPull) {
      var spinner = document.getElementById('chat-spinner-bottom');
      pulLog('onTriggerBottom absPull=' + absPull + ' _bottomArmed=' + _bottomArmed + ' → REFRESH TRIGGERED');
      if (!_bottomArmed) {
        // Stage 1: the FIRST over-scroll must never refresh — even if pulled
        // past the threshold, it settles and springs, and arms stage 2 for
        // the next over-scroll.
        _bottomArmed = true;
        _armedSession = app.currentSessionId;
        pulLog('stage 1 hard pull swallowed (no refresh) → ARMED for stage 2');
        if (spinner) spinner.classList.remove('visible', 'loading');
        return;
      }
      // Stage 2: armed — consume the arm and reset _pullingBottom BEFORE the
      // trailing ratio-0 callback so it doesn't re-arm after the refresh.
      _bottomArmed = false;
      _armedSession = null;
      _pullingBottom = false;
      if (spinner) {
        spinner.classList.add('visible', 'loading');
        var label = spinner.querySelector('.chat-spinner-label');
        if (label) label.textContent = 'Refreshing\u2026';
        pulLog('bottom pill → "Refreshing\u2026" loading state, classes="' + spinner.className + '"');
      }
      // Fire the same refresh action as the header refresh button — forced, so
      // an explicit pull ALWAYS re-syncs from the DB instead of being silently
      // skipped by the remote-change manifest guard.
      if (typeof app.refreshTranscript === 'function') {
        pulLog('calling app.refreshTranscript(true) — forced DB re-sync');
        app.refreshTranscript(true).catch(function() {}).then(function() {
          pulLog('refreshTranscript resolved → hiding pill');
          if (spinner) spinner.classList.remove('visible', 'loading');
        });
      } else {
        // Fallback: dispatch the event that element-based refresh actions use.
        pulLog('app.refreshTranscript MISSING → dispatching chat-control:refresh-transcript');
        document.dispatchEvent(new CustomEvent('chat-control:refresh-transcript'));
        setTimeout(function() {
          if (spinner) spinner.classList.remove('visible', 'loading');
        }, 2000);
      }
    }
  });
  // Defensive spinner dismissal — belt-and-braces on top of the rubber-band's
  // own release notification (snap() always reports ratio 0 now). If a pull
  // spinner is ever left visible without an active pull, any real scroll away
  // from the edge it was pulled at removes it. Loading spinners are left
  // alone — their completion handler dismisses them. During an active pull
  // the scroller stays pinned at the edge (the rubber band translates the
  // element, it doesn't scroll), so this can't fight an in-progress gesture.
  if (messagesEl) {
    messagesEl.addEventListener('scroll', function() {
      var maxS = messagesEl.scrollHeight - messagesEl.clientHeight;
      if (messagesEl.scrollTop > 1) {
        var topSpin = document.getElementById('chat-spinner-top');
        if (topSpin && !topSpin.classList.contains('loading')) topSpin.classList.remove('visible');
      }
      if (messagesEl.scrollTop < maxS - 1) {
        // 2-stage: any scroll away from the bottom invalidates the stage-2
        // arm — the next approach to the bottom is a fresh stage 1.
        if (typeof app._resetBottomArm === 'function') app._resetBottomArm('scrolled away from bottom');
        var botSpin = document.getElementById('chat-spinner-bottom');
        if (botSpin && !botSpin.classList.contains('loading')) botSpin.classList.remove('visible');
      }
    }, { passive: true });
  }

  // Pill textarea rubberband — springy overscroll on both axes
  if (app.chatInput) {
    applyRubberBand(app.chatInput, { axis: 'y', pullThreshold: 20, maxPull: 50 });
  }

  if (inputArea && messagesInner && typeof ResizeObserver !== 'undefined') {
    const syncPad = () => {
      const h = inputArea.offsetHeight;
      if (messagesEl) messagesEl.style.setProperty('--chat-input-h', h + 'px');
      // Also set on #chat-panel so the bottom pull-to-refresh spinner can
      // position itself right above the input area.
      const panelEl = document.getElementById('chat-panel');
      if (panelEl) panelEl.style.setProperty('--chat-input-h', h + 'px');
      _tailPadPx = Math.max(100, h + 24);
      messagesInner.style.paddingBottom = _tailPadPx + 'px';
    };
    new ResizeObserver(syncPad).observe(inputArea);
    syncPad();
  }

  // ── Header padding observer — messages scroll behind the floating header ──
  const headerEl = document.getElementById('chat-header');
  const subHeaderEl = document.getElementById('chat-sub-header');
  const changesPanel = document.getElementById('chat-changes-panel');
  if ((headerEl || subHeaderEl) && messagesInner && typeof ResizeObserver !== 'undefined') {
    const syncHeaderPad = () => {
      // Check live DOM to detect if sub-header was reparented into the header
      // (via sub_agent_tabs control) after this observer was created.
      const subInHeader = subHeaderEl && headerEl && headerEl.contains(subHeaderEl);
      const hh = headerEl ? headerEl.offsetHeight : 0;
      const ch = changesPanel && !changesPanel.hidden ? changesPanel.offsetHeight : 0;
      const sh = (!subInHeader && subHeaderEl && subHeaderEl.style.display !== 'none') ? subHeaderEl.offsetHeight : 0;
      const totalH = hh + ch + sh;
      messagesInner.style.paddingTop = (totalH + 8) + 'px';
      // Position changes panel below the header
      if (changesPanel) {
        changesPanel.style.top = hh + 'px';
      }
      // Position sub-header below header + changes panel (only if NOT inside header).
      // When inside the header (reparented), clear stale top — the CSS zone rules
      // handle positioning; a leftover inline top would push the row open.
      if (subHeaderEl) {
        if (!subInHeader) {
          subHeaderEl.style.top = (hh + ch) + 'px';
        } else {
          subHeaderEl.style.removeProperty('top');
        }
      }
    };
    const obs = new ResizeObserver(syncHeaderPad);
    if (headerEl) obs.observe(headerEl);
    if (subHeaderEl) obs.observe(subHeaderEl);
    if (changesPanel) obs.observe(changesPanel);
    syncHeaderPad();
  }

  // Helper: use #chat-panel width for mobile/desktop breakpoint so that a narrow
  // side-panel on a wide viewport still gets the compact mobile layout.
  function _isNarrowPanel() {
    const panel = document.getElementById('chat-panel');
    return panel ? panel.clientWidth <= 768 : window.innerWidth <= 768;
  }

  // ── Chat fade mask — reads from chat_ui.json ──
  (async function applyChatFade() {
    try {
      const resp = await fetch(apiPath('/api/v1/auth/ui-config'), {
        headers: { ...authHeaders() },
      });
      if (!resp.ok) return;
      const data = await resp.json().catch(() => null);
      if (!data || !data.chat_ui?.chat_common) return;
      const isMobile = _isNarrowPanel();
      const overrides = window.__CHAT_PORTAL__
        ? data.chat_ui.chat_widget
        : (isMobile ? data.chat_ui.chat_mobile : data.chat_ui.chat_desktop);

      // ── Message scroller fade ──
      const baseFade = data.chat_ui.chat_common.fade || {};
      const ovrFade = overrides?.fade || {};
      const fade = { ...baseFade, ...ovrFade };
      const topPx = fade.top != null ? String(fade.top) + 'px' : '0px';
      const botPx = fade.bottom != null ? String(fade.bottom) + 'px' : '0px';
      if (messagesEl) {
        messagesEl.style.setProperty('--chat-fade-top', topPx);
        messagesEl.style.setProperty('--chat-fade-bottom', botPx);
      }

      // ── Pill textarea fade — set vars on the stationary row so the
      //     mask stays put while the text rubberbands behind it.
      const basePillFade = data.chat_ui.chat_common.pill_fade || {};
      const ovrPillFade = overrides?.pill_fade || {};
      const pillFade = { ...basePillFade, ...ovrPillFade };
      if (app.chatInput) {
        const pillRow = app.chatInput.closest('[data-pill-row="0"]');
        const target = pillRow || app.chatInput;
        if (pillFade.top != null) target.style.setProperty('--chat-pill-fade-top', String(pillFade.top) + 'px');
        if (pillFade.bottom != null) target.style.setProperty('--chat-pill-fade-bottom', String(pillFade.bottom) + 'px');
        if (pillFade.bottom_offset != null) target.style.setProperty('--chat-pill-fade-bottom-offset', String(pillFade.bottom_offset) + 'px');
      }
    } catch (_) { /* best-effort */ }
  })();

  // ── Footer drag handle (abilities / device / mode chips) ──
  const _footerHandle = document.getElementById('chat-footer-handle');
  const _footerRow = document.getElementById('chat-footer-row');

  // Hide the handle entirely when below_pill has no visible content.
  // Check after the row has been populated by chat-controls-config.
  function _updateFooterHandleVisibility() {
    if (!_footerHandle || !_footerRow) return;
    const controls = _footerRow.querySelectorAll('[data-header-control], [data-element-origin]');
    const hasContent = Array.from(controls).some(
      el => el.style.display !== 'none' && !el.hidden
    );
    _footerHandle.style.display = hasContent ? '' : 'none';
  }
  _updateFooterHandleVisibility();
  // Re-check when chat controls are re-applied (agent switch, theme change)
  if (app._reapplyChatControls) {
    const _origReapply = app._reapplyChatControls;
    const _patchedReapply = function() {
      const result = _origReapply.apply(this, arguments);
      setTimeout(_updateFooterHandleVisibility, 50);
      return result;
    };
    app._reapplyChatControls = _patchedReapply;
  }

  function _saveFooterExpanded(expanded) {
    const sid = app.currentSessionId;
    if (sid) {
      kvWrite('chat:footerExpanded:' + sid, 'chat_footer_expanded_' + sid, expanded ? '1' : '0');
    }
  }

  function _loadFooterExpanded() {
    const sid = app.currentSessionId;
    if (sid) {
      const saved = kvRead('chat:footerExpanded:' + sid, 'chat_footer_expanded_' + sid);
      if (saved !== undefined && saved !== null) return saved === '1';
    }
    return true; // default to expanded
  }

  function _applyFooterExpanded(expanded) {
    if (_footerHandle) {
      _footerHandle.classList.toggle('expanded', expanded);
      _footerHandle.setAttribute('aria-expanded', String(expanded));
    }
    if (_footerRow) _footerRow.classList.toggle('expanded', expanded);
    _saveFooterExpanded(expanded);
  }

  function _toggleFooter() {
    const current = _footerRow ? _footerRow.classList.contains('expanded') : false;
    _applyFooterExpanded(!current);
  }

  if (_footerHandle && _footerRow) {
    // Apply saved state on init
    _applyFooterExpanded(_loadFooterExpanded());

    // ── Drag to reveal / hide with spring-back ──
    let _dragStartY = 0;
    let _dragOpen = false;
    let _dragOffset = 0;       // cumulative vertical drag offset (negative = up)
    const _dragThreshold = 48;  // px of vertical drag to trigger expand/collapse
    const _dragMax = 80;        // px of linear travel before rubber band kicks in
    const _rubberResistance = 40; // higher = stiffer band past the limit
    const _pillPortion = 0.2;   // fraction of rubber-banded offset applied to the pill edge
    const _pillEl = _footerHandle.parentElement;

    // Asymptotic rubber-band: beyond `limit`, the output grows slower and slower
    // rather than stopping abruptly. Gives a springy feel.
    function _rubberBand(value, limit, resistance) {
      const sign = value < 0 ? -1 : 1;
      const abs = Math.abs(value);
      if (abs <= limit) return value;
      return sign * (limit + (abs - limit) * resistance / ((abs - limit) + resistance));
    }

    _footerHandle.addEventListener('pointerdown', (e) => {
      _dragStartY = e.clientY;
      _dragOpen = _footerRow.classList.contains('expanded');
      _dragOffset = 0;
      _footerHandle.classList.remove('spring-back');
      _pillEl.classList.remove('spring-back');
      _footerHandle.setPointerCapture(e.pointerId);
      e.preventDefault();
    });

    _footerHandle.addEventListener('pointermove', (e) => {
      if (!_footerHandle.hasPointerCapture(e.pointerId)) return;
      const dy = _dragStartY - e.clientY; // positive = dragging up
      const banded = _rubberBand(dy, _dragMax, _rubberResistance);
      _dragOffset = banded;

      // Split the visual offset: the pill edge gets a portion, the handle gets
      // the rest. Since the handle sits inside the pill, splitting keeps the
      // handle at the correct total displacement when both move together.
      const pillOffset = banded * _pillPortion;
      const handleOffset = banded * (1 - _pillPortion);

      // The pill edge shows the stretch (rubber-band visible on the pill border)
      _pillEl.style.setProperty('--pill-drag-y', (-pillOffset) + 'px');
      // The handle tracks the finger more aggressively
      _footerHandle.style.setProperty('--handle-drag-y', (-handleOffset) + 'px');

      // Toggle when threshold crossed
      if (!_dragOpen && dy > _dragThreshold) {
        _applyFooterExpanded(true);
        _dragOpen = true;
        _dragStartY = e.clientY; // reset to avoid re-trigger
      } else if (_dragOpen && dy < -_dragThreshold) {
        _applyFooterExpanded(false);
        _dragOpen = false;
        _dragStartY = e.clientY;
      }
    });

    _footerHandle.addEventListener('pointerup', (e) => {
      if (!_footerHandle.hasPointerCapture(e.pointerId)) return;
      _footerHandle.releasePointerCapture(e.pointerId);
      // Spring back to origin — on both pill edge and handle
      _footerHandle.classList.add('spring-back');
      _footerHandle.style.removeProperty('--handle-drag-y');
      _pillEl.classList.add('spring-back');
      _pillEl.style.removeProperty('--pill-drag-y');
      _dragOffset = 0;
    });

    // Click to toggle (when there was no significant drag)
    _footerHandle.addEventListener('click', (e) => {
      if (Math.abs(_dragOffset) > 4) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      e.preventDefault();
      const wasExpanded = _footerRow.classList.contains('expanded');
      _toggleFooter();
      // Impulse: boot the pill in the direction it should go, then spring-back
      // with overshoot so it bounces past the target and settles.
      //   Expanding  → pull UP  (negative) to reveal buttons, overshoot upward
      //   Collapsing → push DOWN (positive) to hide buttons, overshoot downward
      const impulse = wasExpanded ? 24 : -24;
      _footerHandle.classList.remove('spring-back', 'spring-back-click');
      _pillEl.classList.remove('spring-back', 'spring-back-click');
      _pillEl.style.setProperty('--pill-drag-y', impulse + 'px');
      _footerHandle.style.setProperty('--handle-drag-y', impulse + 'px');
      requestAnimationFrame(() => {
        _pillEl.classList.add('spring-back-click');
        _footerHandle.classList.add('spring-back-click');
        _pillEl.style.removeProperty('--pill-drag-y');
        _footerHandle.style.removeProperty('--handle-drag-y');
      });
    });
  }

  // Export for session switch reload
  app.reloadFooterExpanded = () => _applyFooterExpanded(_loadFooterExpanded());

  // ── Scroll-to-bottom chevron (listener on messagesEl — the real scroller) ──
  _scrollBtn = document.getElementById('chat-scroll-bottom-btn');
  if (messagesEl && messagesInner && _scrollBtn) {
    messagesEl.addEventListener('scroll', () => {
      _updateScrollChevron(messagesEl);
      _updateTopNav(messagesEl);
    }, { passive: true });

    _scrollBtn.addEventListener('click', async () => {
      _scrollLocked = true;
      // A clicked FAB must not retain focus — otherwise the ring re-paints
      // when the button reappears after scrolling. Blur immediately.
      _scrollBtn.blur();
      const sessionId = app.currentSessionId;
      // DB-truthful jump to the true latest message: when newer rows exist
      // beyond the loaded window, fetch them and render at the bottom, with
      // the chevron swapped for a spinner while the fetch runs.
      if (sessionId && typeof app._repositionToBottom === 'function') {
        const needsFetch = !(typeof app._jumpTargetInCache === 'function'
          && app._jumpTargetInCache(sessionId, { atBottom: true }));
        if (needsFetch) _setBtnBusy(_scrollBtn, true);
        const ok = await app._repositionToBottom(sessionId);
        if (needsFetch) _setBtnBusy(_scrollBtn, false);
        if (ok) {
          _programmaticScroll = true;
          _updateScrollChevron(messagesEl);
          return;
        }
      }
      _programmaticScroll = true;
      messagesEl.scrollTop = messagesEl.scrollHeight;
      _scrollBtn.classList.remove('visible');
    });

    _scrollLocked = true;
    _scrollBtn.classList.remove('visible');
  }

  // ── Top-of-panel jump nav (#chat-top-nav) ──
  // The element now lives in the HTML as a flat control (data-header-control="top_nav")
  // and is placed into a header row by chat-controls-config.js. This code wires
  // the button handlers and manages visibility.

  /** Wire click handlers on the top-nav buttons (idempotent). */
  const _wireTopNav = () => {
    if (_topNavWired) return;
    _toTopBtn = document.getElementById('chat-scroll-top-btn');
    _toLastUserBtn = document.getElementById('chat-scroll-lastuser-btn');
    if (messagesEl && _toTopBtn) {
      _toTopBtn.addEventListener('click', async () => {
        _scrollLocked = false;
        _toTopBtn.blur();
        const sessionId = app.currentSessionId;
        // DB-truthful jump to the real session start — loads the oldest window
        // when it isn't already in the cache, with a pure-scroll fallback.
        if (sessionId && typeof app._repositionTranscript === 'function') {
          const needsFetch = !(typeof app._jumpTargetInCache === 'function'
            && app._jumpTargetInCache(sessionId, { atStart: true }));
          if (needsFetch) _setBtnBusy(_toTopBtn, true);
          const ok = await app._repositionTranscript(sessionId, { atStart: true });
          if (needsFetch) _setBtnBusy(_toTopBtn, false);
          if (ok) { _updateTopNav(messagesEl); return; }
        }
        messagesEl.scrollTop = 0;
        _updateTopNav(messagesEl);
      });
    }
    if (messagesEl && _toLastUserBtn) {
      _toLastUserBtn.addEventListener('click', async () => {
        _toLastUserBtn.blur();
        // Fast path: a rendered user bubble above the viewport → pure scroll.
        const target = _prevUserTarget(messagesEl);
        if (target !== null) {
          _scrollLocked = false;
          messagesEl.scrollTop = target;
          _updateTopNav(messagesEl);
          return;
        }
        // DB-truthful path: walk up to the nearest user message, loading it
        // (and its surrounding window) from the DB when not already loaded.
        const sessionId = app.currentSessionId;
        if (sessionId && typeof app._stepToPrevUserMessage === 'function') {
          _setBtnBusy(_toLastUserBtn, true);
          // Yield to the browser so the spinner paints before the reposition
          // runs — a cache-hit reposition resolves in microtasks and would
          // otherwise clear the busy state before the first paint.
          await new Promise(r => requestAnimationFrame(() => setTimeout(r, 0)));
          try {
            await app._stepToPrevUserMessage(sessionId);
          } finally {
            _setBtnBusy(_toLastUserBtn, false);
          }
        }
        _updateTopNav(messagesEl);
      });
    }
    _topNavWired = true;
  };

  /** Ensure the top-nav element exists in the DOM (create if missing for backward compat). */
  const _ensureTopNav = () => {
    if (document.getElementById('chat-top-nav')) return;
    const header = document.getElementById('chat-header');
    if (!header) return;
    const nav = document.createElement('div');
    nav.id = 'chat-top-nav';
    nav.setAttribute('aria-label', 'Jump to top of conversation');
    nav.innerHTML = `
      <button id="chat-scroll-top-btn" class="chat-nav-fab" title="Jump to the start of the session" aria-label="Jump to the start of the session" type="button">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="7 11 12 6 17 11"/><polyline points="7 18 12 13 17 18"/></svg>
      </button>
      <button id="chat-scroll-lastuser-btn" class="chat-nav-fab" title="Jump up to the previous message you sent" aria-label="Jump up to the previous message you sent" type="button">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="7 14 12 9 17 14"/></svg>
      </button>
    `;
    header.appendChild(nav);
  };

  _ensureTopNav();
  _wireTopNav();
  // Initial visibility update
  if (messagesEl) _updateTopNav(messagesEl);
  // Expose for external callers (e.g., after header config changes)
  app._wireTopNav = _wireTopNav;

  // Expose scroll helpers for session-load.js
  app._scrollToBottomIfNear = _scrollToBottomIfNear;
  Object.defineProperty(app, '_scrollLocked', {
    get: () => _scrollLocked,
    set: (v) => { _scrollLocked = v; },
  });
}
