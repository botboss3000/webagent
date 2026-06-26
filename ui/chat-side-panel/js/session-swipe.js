'use strict';

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { _sessionsCache, _formatRelativeTime } from './session-list.js';
import { _messageCache, _CACHE_TTL_MS, _sessionFocus, _cacheAppendMessage } from './chat-message-cache.js';
import { _stripToolCalls } from './chat-stream.js';
import { switchToSession } from './session-core.js';

/**
 * Horizontal swipe navigation between pinned sessions.
 * Attaches touch/pointer handlers to the transcript wrapper for swipe
 * gestures, plus desktop prev/next buttons.
 */
function _initPinSwipeNavigation() {
  const inner = app.chatMessages;
  const wrapper = inner && inner.parentElement;
  if (!inner || !wrapper) return;

  let _startX = 0, _startY = 0, _startT = 0;
  let _active = false;
  let _stashedDirection = 0;
  let _nextPanel = null;
  let _warmAbort = null;       // AbortController for the in-flight neighbour warm
  let _warmScheduled = false;  // an idle warm pass is already queued
  const _WARM_NEWEST = 40;     // newest-N slice to pre-warm a neighbour at the bottom
  const _WARM_AROUND = 25;     // rows each side of a neighbour's saved scroll anchor

  function _pinnedInOrder() {
    const pinned = _sessionsCache.filter(s => s.pinned);
    const order = new Map(_sessionsCache.map((s, i) => [s.id, i]));
    return pinned.sort((a, b) => (order.get(a.id) || 0) - (order.get(b.id) || 0));
  }

  function _targetSid(direction) {
    const pinned = _pinnedInOrder();
    if (!pinned.length) return null;
    const cur = pinned.findIndex(s => s.id === app.currentSessionId);
    let idx;
    if (cur === -1) idx = direction === 1 ? 0 : pinned.length - 1;
    else idx = (cur + direction + pinned.length) % pinned.length;
    return pinned[idx].id;
  }

  // ── Background neighbour warming ──────────────────────────────────────────
  // Pre-load ONLY the next + previous pinned sessions (the two the header arrows
  // / a swipe can reach next), and ONLY when the app is idle — so arrow/swipe
  // navigation feels instant without ever competing with the active session.
  // Every warm fetch is idle-scheduled, runs one-at-a-time at low network
  // priority, is skipped while a turn is running or the active session is still
  // loading, and is aborted the instant a real load begins.

  function _idle(cb) {
    // requestIdleCallback only runs when the main thread is free; Safari / older
    // mobile lack it, so fall back to a short timer.
    if (typeof window !== 'undefined' && window.requestIdleCallback) {
      return window.requestIdleCallback(cb, { timeout: 2000 });
    }
    return setTimeout(cb, 1200);
  }

  function _appBusy() {
    if (app.isProcessing) return true;          // a turn is streaming → backend busy
    if (app._sessionLoadInFlight) return true;  // the on-screen session is loading
    if (typeof document !== 'undefined' && document.hidden) return true; // tab hidden
    return false;
  }

  function _isFreshlyCached(sid) {
    const c = _messageCache.get(sid);
    return !!(c && c.messages && (Date.now() - (c.loadedAt || 0)) < _CACHE_TTL_MS);
  }

  function _abortWarm() {
    try { if (_warmAbort) _warmAbort.abort(); } catch (_) {}
    _warmAbort = null;
  }

  async function fetchMessagesForCache(sid, signal) {
    const token = localStorage.getItem('auth_token');
    // Mirror what a real open would download so navigation is a pure cache hit:
    // centre on the neighbour's saved scroll position when it has one, else the
    // newest slice. Always light (headings only — no tool-call bodies). Low
    // network priority so it yields to the active session's own requests.
    const focus = _sessionFocus.get(sid);
    let limit = _WARM_NEWEST;
    let extra = '';
    if (focus && focus.atBottom === false && focus.anchorMsgId) {
      limit = _WARM_AROUND;
      extra = `&around_id=${encodeURIComponent(focus.anchorMsgId)}`;
    }
    const url = apiPath(`/api/v1/db/session-messages?db=local.db&session_id=${encodeURIComponent(sid)}&limit=${limit}&light=1${extra}${token ? '&token=' + encodeURIComponent(token) : ''}`);
    try {
      const res = await fetch(url, { signal: signal || undefined, priority: 'low' });
      const data = await res.json();
      if (!data.restricted && data.messages) {
        _messageCache.set(sid, {
          messages: data.messages,
          hasMore: !!data.has_more,
          hasNewer: !!data.has_newer,
          light: data.light !== false,
          maxSeq: data.max_session_seq || 0,
          contextTokens: data.context_tokens || 0,
          loadedAt: Date.now(),
        });
      }
    } catch (_) { /* aborted, offline, or restricted — non-fatal */ }
  }

  function _warmNeighbors() {
    if (_warmScheduled) return;        // coalesce rapid arrow-scrubbing into one pass
    _warmScheduled = true;
    _idle(async () => {
      _warmScheduled = false;
      if (_appBusy()) return;          // try again on the next switch / idle trigger
      const ids = [];
      for (const id of [_targetSid(1), _targetSid(-1)]) {
        if (id && id !== app.currentSessionId && !ids.includes(id) && !_isFreshlyCached(id)) {
          ids.push(id);
        }
      }
      if (!ids.length) return;
      _abortWarm();
      _warmAbort = (typeof AbortController !== 'undefined') ? new AbortController() : null;
      const signal = _warmAbort && _warmAbort.signal;
      for (const id of ids) {
        if (_appBusy()) break;         // active work appeared mid-pass — yield now
        await fetchMessagesForCache(id, signal);
      }
    });
  }

  // Let the active-session loader drive warming: it calls warmNeighborSessions
  // once a load settles, and abortNeighborWarm the instant a new load begins.
  app.warmNeighborSessions = _warmNeighbors;
  app.abortNeighborWarm = () => { _abortWarm(); _warmScheduled = false; };

  function _renderBubbles(sid, container) {
    const cached = _messageCache.get(sid);
    if (!cached || !cached.messages || !cached.messages.length) {
      container.innerHTML =
        '<div class="chat-loading-wrap">' +
          '<div class="chat-loading-spinner">' +
            '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"/><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"/></svg>' +
          '</div>' +
        '</div>';
      return;
    }
    container.innerHTML = '';
    for (const msg of cached.messages) {
      if (msg.tool_name || msg.role === 'tool') continue;
      if (msg.role !== 'user' && msg.role !== 'assistant') continue;
      let text = (msg.content || '').trim();
      text = _stripToolCalls(text);
      if (!text) continue;
      const bubble = document.createElement('div');
      bubble.className = 'chat-bubble ' + (msg.role === 'user' ? 'user' : 'agent');
      if (msg.status === 'error') bubble.classList.add('error');
      if (msg.status === 'interrupted') bubble.classList.add('interrupted');
      if (msg.id) bubble.setAttribute('data-msg-id', msg.id);
      {
        const createdAtMs = msg.created_at ? new Date(msg.created_at.replace(' ', 'T') + 'Z').getTime() : Date.now();
        bubble.setAttribute('data-created-at', createdAtMs);
        const elapsed = Date.now() - createdAtMs;
        const totalMin = Math.floor(elapsed / 60000);
        let timeText;
        if (totalMin < 1) timeText = '0m';
        else if (totalMin < 60) timeText = totalMin + 'm';
        else { const h = Math.floor(totalMin / 60); const m = totalMin % 60; timeText = h + 'h ' + m + 'm'; }
        const timeEl = document.createElement('span');
        timeEl.className = 'bubble-time';
        timeEl.setAttribute('data-created-at', createdAtMs);
        timeEl.textContent = timeText;
        bubble.appendChild(timeEl);
      }
      if (msg.role === 'user') {
        const label = document.createElement('span');
        label.className = 'label';
        label.textContent = 'You';
        bubble.appendChild(label);
        const body = document.createElement('div');
        body.className = 'bubble-body';
        body.textContent = text;
        bubble.appendChild(body);
      } else {
        const body = document.createElement('div');
        body.className = 'bubble-body';
        body.textContent = text;
        bubble.appendChild(body);
      }
      // Second time element (duplicated from above in original code — kept for backwards compat)
      {
        const createdAtMs = msg.created_at ? new Date(msg.created_at.replace(' ', 'T') + 'Z').getTime() : Date.now();
        const elapsed = Date.now() - createdAtMs;
        const totalMin = Math.floor(elapsed / 60000);
        let label;
        if (totalMin < 1) label = '0m';
        else if (totalMin < 60) label = totalMin + 'm';
        else { const h = Math.floor(totalMin / 60); const m = totalMin % 60; label = h + 'h ' + m + 'm'; }
        const timeEl = document.createElement('span');
        timeEl.className = 'bubble-time';
        timeEl.setAttribute('data-created-at', createdAtMs);
        timeEl.textContent = label;
        bubble.appendChild(timeEl);
      }
      container.appendChild(bubble);
    }
    container.scrollTop = container.scrollHeight;
  }

  function _ensureNextPanel(direction) {
    const sid = _targetSid(direction);
    if (!sid) { _cleanNextPanel(); return null; }
    if (_nextPanel && _nextPanel.dataset.sid === sid) return _nextPanel;

    _cleanNextPanel();
    const panel = document.createElement('div');
    panel.className = 'chat-messages-next';
    panel.dataset.sid = sid;
    panel.style.transform = 'translateX(' + (direction === 1 ? '100%' : '-100%') + ')';
    panel.scrollTop = 0;
    _renderBubbles(sid, panel);
    wrapper.appendChild(panel);
    _nextPanel = panel;
    return panel;
  }

  function _cleanNextPanel() {
    if (_nextPanel) { _nextPanel.remove(); _nextPanel = null; }
  }

  function _toast(msg) {
    const old = wrapper.querySelector('.pin-swipe-toast');
    if (old) old.remove();
    const t = document.createElement('div');
    t.className = 'pin-swipe-toast';
    t.textContent = msg;
    wrapper.prepend(t);
    requestAnimationFrame(() => t.classList.add('show'));
    setTimeout(() => t.classList.remove('show'), 1800);
    setTimeout(() => t.remove(), 2300);
  }

  function _go(direction) {
    _cleanNextPanel();
    const pinned = _pinnedInOrder();
    if (!pinned.length) { _toast('No pinned sessions for quick navigation'); return; }
    const cur = pinned.findIndex(s => s.id === app.currentSessionId);
    let idx;
    if (cur === -1) idx = direction === 1 ? 0 : pinned.length - 1;
    else idx = (cur + direction + pinned.length) % pinned.length;
    switchToSession(pinned[idx].id);
  }

  function _applyOffset(dx) {
    const dir = dx > 0 ? -1 : 1;
    if (dir !== _stashedDirection) {
      _stashedDirection = dir;
      _cleanNextPanel();
      _ensureNextPanel(dir);
    }
    const w = wrapper.clientWidth || 380;
    const frac = Math.max(-1, Math.min(1, dx / w));
    inner.style.transition = 'none';
    inner.style.transform = 'translateX(' + Math.round(dx) + 'px)';
    inner.style.opacity = String(Math.max(0.3, 1 - Math.abs(frac)));
    if (_nextPanel) {
      _nextPanel.style.transition = 'none';
      const offset = dir === 1 ? (1 + frac) * 100 : (-1 + frac) * 100;
      _nextPanel.style.transform = 'translateX(' + Math.round(offset) + '%)';
      _nextPanel.style.opacity = String(Math.min(1, Math.abs(frac) * 2));
    }
  }

  function _resetOffset(instant) {
    inner.style.transition = instant ? 'none' : 'transform 0.3s cubic-bezier(.22,.68,0,1), opacity 0.3s ease';
    inner.style.transform = 'translateX(0)';
    inner.style.opacity = '1';
    inner.style.willChange = '';
    if (_nextPanel) {
      _nextPanel.style.transition = instant ? 'none' : 'transform 0.3s cubic-bezier(.22,.68,0,1), opacity 0.25s ease';
      _nextPanel.style.transform = 'translateX(' + (_stashedDirection === 1 ? '100%' : '-100%') + ')';
      _nextPanel.style.opacity = '0';
    }
    _stashedDirection = 0;
  }

  setTimeout(_warmNeighbors, 2000);

  wrapper.addEventListener('pointerdown', (e) => {
    if (e.pointerType !== 'touch' && e.pointerType !== 'pen') return;
    if (e.target.closest('#chat-input-area, button, input, textarea, select, .chat-pill, #chat-header')) return;
    _startX = e.clientX; _startY = e.clientY; _startT = Date.now();
    _active = true;
    _warmNeighbors();
  });

  wrapper.addEventListener('pointermove', (e) => {
    if (!_active) return;
    const dx = e.clientX - _startX;
    const dy = e.clientY - _startY;
    if (Math.abs(dy) > Math.abs(dx) + 10) { _active = false; _resetOffset(); return; }
    if (Math.abs(dx) > 5) _applyOffset(dx);
  });

  wrapper.addEventListener('pointerup', (e) => {
    if (!_active) { _resetOffset(true); return; }
    _active = false;
    const dx = e.clientX - _startX;
    const dt = Date.now() - _startT;
    if (dt < 500 && Math.abs(dx) > 20) {
      _resetOffset(true);
      _go(dx > 0 ? -1 : 1);
    } else {
      _resetOffset(false);
    }
  });

  wrapper.addEventListener('pointercancel', () => { _active = false; _resetOffset(false); });
  wrapper.addEventListener('pointerleave', () => { _active = false; _resetOffset(false); });

  const prevBtn = document.getElementById('session-prev-btn');
  const nextBtn = document.getElementById('session-next-btn');
  if (prevBtn) {
    prevBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      _go(1);
    });
  }
  if (nextBtn) {
    nextBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      _go(-1);
    });
  }
}

export { _initPinSwipeNavigation };