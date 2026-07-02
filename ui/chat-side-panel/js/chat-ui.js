'use strict';

// Chat panel wiring — initChat(): binds chat input/keyboard/send, execution-mode
// toggle, continue/stop buttons (500ms poll), scroll-to-bottom chevron + the
// top-of-panel jump nav (start of session / last user message), gate +
// draft restore. The top-level entry for the chat side panel.
// Module map for this folder: ui/chat-side-panel/js/README.md.

import { _refreshLucideIcons } from '../../shared/js/dom-utils.js';
import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { getAccessMode, fetchAccessMode, authHeaders } from '../../shared/js/left-login.js';
import { addChatBubble, updateLastBubble, linkifyText, _renderMarkdownBody, _refreshAllBubbleTimes } from './chat-bubble.js';
import { _addBubbleActions } from './chat-bubble-actions.js';
import {
  appendStreamToActiveBubble,
  finalizeAgentResponse,
  ensureStreamingBubbleForActiveTurn,
  finalizeAgentStep,
  markAgentInterrupted,
  seedStreamingBubble,
  attachToolCallsToLastBubble,
} from './chat-stream.js';
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
import { initTaskFrames } from './chat-task-frames.js';
import { initTerminalChatEngine } from './chat-terminal-engine.js';
import { initTerminalKeys } from './chat-terminal-keys.js';

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
  // Double chevron → very top: relevant whenever there's content above.
  if (_toTopBtn) _toTopBtn.classList.toggle('visible', el.scrollTop > 60);
  // Single chevron → step up one user message: relevant whenever there's a
  // user message above the current top to step to.
  if (_toLastUserBtn) {
    _toLastUserBtn.classList.toggle('visible', _prevUserTarget(el) !== null);
  }
}

function _updateScrollChevron(el) {
  if (!el) return;
  _updateTopNav(el);
  if (!_scrollBtn) return;
  if (_programmaticScroll) {
    _programmaticScroll = false;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    _scrollBtn.classList.toggle('visible', !atBottom);
    return;
  }
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  _scrollBtn.classList.toggle('visible', !atBottom);
  if (!atBottom) _scrollLocked = false;
}

function _scrollToBottomIfNear(el) {
  if (!el) return;
  if (!_scrollLocked) {
    _updateScrollChevron(el);
    return;
  }
  const _ = el.offsetHeight;
  _programmaticScroll = true;
  el.scrollTop = el.scrollHeight;
  if (_scrollBtn) _scrollBtn.classList.remove('visible');
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
  // ── Relative-time ticker ──
  setInterval(_refreshAllBubbleTimes, 60000);

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

  // ── Event wiring ──
  app.chatSend.addEventListener('click', sendMessage);
  app.chatInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  // Prewarm the next send's read-only prep the moment the user engages the pill,
  // so the remote DB reads (tools/history/data-sources) happen DURING typing
  // instead of on the critical path. Throttled per session inside _prewarm.
  app.chatInput.addEventListener('focus', () => { _prewarm(); });

  // Terminal-tunnel UI
  try { initChatTunnel(); } catch (_) { /* non-fatal */ }
  try { initTaskFrames(); } catch (_) { /* non-fatal */ }
  // Terminal Chat engine — live xterm.js view when agent uses terminal_chat engine
  try { initTerminalChatEngine(); } catch (_) { /* non-fatal */ }
  // On-screen number-pad / shortcut keys for Terminal Chat (footer toggle).
  try { initTerminalKeys(); } catch (_) { /* non-fatal */ }

  app.chatInput.addEventListener('input', () => {
    if (!_canChat()) { app.chatSend.disabled = true; _updateInputRowState(); return; }
    app.chatSend.disabled = !app.chatInput.value.trim();
    _updateInputRowState();
    _saveDraft();
    // Keep the warmed prep fresh while a long message is composed (throttled).
    _prewarm();
  });

  // ── Execution mode toggle (per-session key) ──
  // Pill cycles Ask → Plan → Auto. Legacy saved values (the old Read/Write/Auto)
  // are migrated on read so existing sessions don't reset: read→plan, write→ask.
  const MODE_CYCLE = ['ask', 'plan', 'auto'];
  const MODE_LABELS = { ask: 'Ask', plan: 'Plan', auto: 'Auto' };
  const MODE_LEGACY = { read: 'plan', write: 'ask' };
  app.executionMode = 'ask';
  const modeBtn = document.getElementById('chat-mode-btn');

  // The active agent's configured "Default chat mode" (Config tab →
  // metadata.default_execution_mode, served on the agent record as
  // default_execution_mode). A FRESH session with no per-session saved mode
  // STARTS in this; once a session has its own saved mode (the user touched the
  // pill or sent a message) that wins. Falls back to 'ask' when unset.
  function _agentDefaultMode() {
    try {
      const id = app.currentAgentId;
      if (!id) return 'ask';
      const list = window.__agentsSharedData && window.__agentsSharedData.agents;
      if (Array.isArray(list)) {
        const a = list.find(x => x && x.id === id);
        let m = a && a.default_execution_mode;
        if (m && MODE_LEGACY[m]) m = MODE_LEGACY[m];
        if (m && MODE_CYCLE.includes(m)) return m;
      }
    } catch (_) { /* non-fatal — fall through to 'ask' */ }
    return 'ask';
  }

  function _applyExecutionMode() {
    if (!modeBtn) return;
    const fallback = _agentDefaultMode();
    const sid = app.currentSessionId;
    if (!sid) {
      app.executionMode = fallback;
      modeBtn.textContent = MODE_LABELS[fallback] || 'Ask';
      return;
    }
    try {
      let saved = localStorage.getItem('chat_execution_mode_' + sid);
      if (saved && MODE_LEGACY[saved]) saved = MODE_LEGACY[saved];
      if (saved && MODE_CYCLE.includes(saved)) {
        app.executionMode = saved;
      } else {
        app.executionMode = fallback;
      }
    } catch (_) {
      app.executionMode = fallback;
    }
    modeBtn.textContent = MODE_LABELS[app.executionMode] || 'Ask';
  }

  if (modeBtn) {
    _applyExecutionMode();
    modeBtn.addEventListener('click', () => {
      const idx = MODE_CYCLE.indexOf(app.executionMode);
      app.executionMode = MODE_CYCLE[(idx + 1) % MODE_CYCLE.length];
      modeBtn.textContent = MODE_LABELS[app.executionMode];
      const sid = app.currentSessionId;
      if (sid) {
        try { localStorage.setItem('chat_execution_mode_' + sid, app.executionMode); } catch (_) {}
      }
    });
  }

  // Export reload so session-core.js can call it on session switch
  app.reloadExecutionMode = _applyExecutionMode;

  // ── Target-device pill (per-session) ──
  // Picks which device in the fleet runs the next message. '' = this device
  // (runs locally, exactly like before); a chosen instance-id is sent as
  // request.target_device so the backend hands the turn to that device's worker
  // to run inside THIS session (the reply flows back over the shared DB). The
  // choice is remembered per session, like the execution mode above.
  app.targetDevice = '';
  const targetBtn = document.getElementById('chat-target-btn');

  function _renderTargetPill() {
    if (!targetBtn) return;
    const labelEl = targetBtn.querySelector('.chat-target-label');
    const inst = app.targetDevice || '';
    if (inst) {
      targetBtn.classList.add('targeting');
      const lbl = (window.DevicePicker && window.DevicePicker.labelFor)
        ? window.DevicePicker.labelFor(inst) : inst;
      if (labelEl) labelEl.textContent = lbl;
      targetBtn.title = 'Running on ' + lbl + ' — click to change';
    } else {
      targetBtn.classList.remove('targeting');
      if (labelEl) labelEl.textContent = '';
      targetBtn.title = 'Run on… choose which device runs your next message';
    }
  }

  function _applyTargetDevice() {
    const sid = app.currentSessionId;
    app.targetDevice = '';
    if (sid) {
      try {
        const saved = localStorage.getItem('chat_target_device_' + sid);
        if (saved) app.targetDevice = saved;
      } catch (_) { /* best-effort */ }
    }
    _renderTargetPill();
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
        title: 'Run this chat on',
        currentInstance: app.targetDevice || '',
        onSelect: (sel) => {
          app.targetDevice = sel.is_self ? '' : (sel.instance_id || '');
          const sid = app.currentSessionId;
          if (sid) {
            try {
              if (app.targetDevice) localStorage.setItem('chat_target_device_' + sid, app.targetDevice);
              else localStorage.removeItem('chat_target_device_' + sid);
            } catch (_) { /* best-effort */ }
          }
          _renderTargetPill();
        },
      });
    });
  }

  // Export reload so session switches re-read the per-session target (mirrors
  // app.reloadExecutionMode).
  app.reloadTargetDevice = _applyTargetDevice;

  // ── Agent-driven mode switch (set_execution_mode tool → WS echo) ──
  // The backend broadcasts an `execution_mode` event when the agent flips the
  // mode mid-conversation (e.g. Plan→Auto after the user approves a plan).
  // agentWs.js routes it here so the pill below the chat visibly switches and
  // the next message the user sends carries the new mode (persisted per-session).
  app.setExecutionMode = function (mode) {
    let m = String(mode || '').toLowerCase();
    if (MODE_LEGACY[m]) m = MODE_LEGACY[m];
    if (!MODE_CYCLE.includes(m)) return;
    app.executionMode = m;
    if (modeBtn) modeBtn.textContent = MODE_LABELS[m];
    const sid = app.currentSessionId;
    if (sid) {
      try { localStorage.setItem('chat_execution_mode_' + sid, m); } catch (_) {}
    }
  };

  // ── Continue button ──
  const continueBtn = document.getElementById('chat-continue-btn');
  if (continueBtn) {
    continueBtn.addEventListener('click', () => {
      if (!_canChat()) { applyChatGate(); return; }
      if (!app.currentAgentId) return;
      app.chatInput.value = 'continue';
      sendMessage();
    });
  }

  // ── Stop button ──
  const stopBtn = document.getElementById('chat-stop-btn');
  if (stopBtn) stopBtn.addEventListener('click', sendStopMessage);

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

  // ── Action row polling ──
  const actionRow = document.getElementById('chat-action-row');
  function _updateActionBtns() {
    const hasAgent = !!app.currentAgentId;
    if (actionRow) actionRow.classList.toggle('hidden', !hasAgent);
    if (stopBtn) stopBtn.classList.toggle('active', hasAgent && app.isProcessing);
    if (continueBtn) continueBtn.classList.toggle('active', hasAgent && !app.isProcessing);
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
  if (inputArea && messagesInner && typeof ResizeObserver !== 'undefined') {
    const syncPad = () => {
      const h = inputArea.offsetHeight;
      if (messagesEl) messagesEl.style.setProperty('--chat-input-h', h + 'px');
      messagesInner.style.paddingBottom = (h + 24) + 'px';
    };
    new ResizeObserver(syncPad).observe(inputArea);
    syncPad();
  }

  // ── Scroll-to-bottom chevron ──
  _scrollBtn = document.getElementById('chat-scroll-bottom-btn');
  if (messagesInner && _scrollBtn) {
    messagesInner.addEventListener('scroll', () => {
      _updateScrollChevron(messagesInner);
    }, { passive: true });

    _scrollBtn.addEventListener('click', () => {
      _scrollLocked = true;
      _programmaticScroll = true;
      messagesInner.scrollTop = messagesInner.scrollHeight;
      _scrollBtn.classList.remove('visible');
    });

    _scrollLocked = true;
    _scrollBtn.classList.remove('visible');
  }

  // ── Top-of-panel jump nav (start of session / step up one user message) ──
  _toTopBtn = document.getElementById('chat-scroll-top-btn');
  _toLastUserBtn = document.getElementById('chat-scroll-lastuser-btn');
  // Instant jumps (not behavior:'smooth' — smooth scrollTo is unreliable on
  // this overflow container, same reason the scroll-to-bottom button assigns
  // scrollTop directly). Releasing the auto-scroll lock keeps the jump from
  // being yanked back to the bottom by the next poll; then refresh the nav.
  if (messagesInner && _toTopBtn) {
    _toTopBtn.addEventListener('click', () => {
      _scrollLocked = false;
      messagesInner.scrollTop = 0;
      _updateTopNav(messagesInner);
    });
  }
  if (messagesInner && _toLastUserBtn) {
    // Each click steps up to the user message just above the current top, so
    // repeated clicks walk back through the conversation one message at a time.
    _toLastUserBtn.addEventListener('click', () => {
      const target = _prevUserTarget(messagesInner);
      if (target === null) return;
      _scrollLocked = false;
      messagesInner.scrollTop = target;
      _updateTopNav(messagesInner);
    });
  }
  if (messagesInner) _updateTopNav(messagesInner);

  // Expose scroll helpers for session-load.js
  app._scrollToBottomIfNear = _scrollToBottomIfNear;
  Object.defineProperty(app, '_scrollLocked', {
    get: () => _scrollLocked,
    set: (v) => { _scrollLocked = v; },
  });

  // ── Mobile focus-on-first-tap ──
  let _focused = false;
  const _firstTap = () => {
    if (_focused) return;
    _focused = true;
    if (app.chatInput) app.chatInput.focus();
  };
  document.addEventListener('touchstart', _firstTap, { once: true, passive: true });
  document.addEventListener('click', _firstTap, { once: true, passive: true });
}