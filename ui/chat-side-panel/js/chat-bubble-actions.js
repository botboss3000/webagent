'use strict';

// Per-bubble action row — model tag, collapse/expand, read-aloud
// (SpeechSynthesis), copy, and two-click per-turn delete. Sets
// app._addBubbleActions. The footer shows on STREAMING bubbles too (live or
// being recovered from the DB), minus the delete button until the turn finalizes.
// Module map for this folder: ui/chat-side-panel/js/README.md.

import { _refreshLucideIcons } from '../../shared/js/dom-utils.js';
import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { icon } from '../../shared/js/icons.js';
import { copyText } from '../../shared/js/clipboard.js';
import { _formatRelativeTime } from './chat-bubble.js';

// ── Per-turn model tag ──────────────────────────────────────────────────────
// Each agent turn's footer shows which model (and reasoning-effort mode) ran it,
// e.g. "deepseek-v4-flash HIGH". The model + effort are persisted in the
// assistant interaction's metadata by the loop (_build_meta) and surfaced live
// via the pipeline llm_call_end event (captured in chat-activity.js →
// app._activeTurnModel / app._activeTurnEffort).

// Drop the provider prefix: "deepseek/deepseek-v4-flash" → "deepseek-v4-flash".
function _shortModelName(name) {
  if (!name) return '';
  return name.includes('/') ? name.slice(name.lastIndexOf('/') + 1) : name;
}

// "model EFFORT" — effort suffix only when a non-default level is set.
function _buildModelLabel(model, effort) {
  const short = _shortModelName(model);
  if (!short) return '';
  const eff = (effort || '').trim().toLowerCase();
  if (eff && eff !== 'default') return short + ' ' + eff.toUpperCase();
  return short;
}

// Parse an interaction's metadata JSON → display label (or '').
function _modelLabelFromMeta(metaStr) {
  if (!metaStr) return '';
  let m;
  try { m = JSON.parse(metaStr); } catch (_) { return ''; }
  if (!m || typeof m !== 'object') return '';
  return _buildModelLabel(m.model, m.effort);
}

// Stamp a bubble with its model tag (display + full-id hover) for the footer to
// pick up when its action row is (re)built. Safe to call before or after the row
// exists — if the row is already there, the tag is injected immediately.
function _setBubbleModel(bubble, model, effort) {
  if (!bubble) return;
  const label = _buildModelLabel(model, effort);
  if (!label) return;
  bubble.dataset.modelLabel = label;
  bubble.dataset.modelTitle = model || label;
  const actions = bubble.querySelector(':scope > .bubble-actions');
  if (actions && !actions.querySelector('.bubble-model')) _injectModelTag(actions, bubble);
}

// Same, sourced from a metadata JSON string (persisted-history path).
function _setBubbleModelFromMeta(bubble, metaStr) {
  if (!bubble || !metaStr) return;
  let m;
  try { m = JSON.parse(metaStr); } catch (_) { return; }
  if (m && typeof m === 'object') _setBubbleModel(bubble, m.model, m.effort);
}

// Build the .bubble-model span and place it at the front of the action row (it
// carries the auto-margin that pushes the buttons to the right — see app1.css).
function _injectModelTag(actions, bubble) {
  if (!actions || !bubble || !bubble.dataset.modelLabel) return;
  if (actions.querySelector('.bubble-model')) return;
  const tag = document.createElement('span');
  tag.className = 'bubble-model';
  tag.title = bubble.dataset.modelTitle || bubble.dataset.modelLabel;
  const inner = document.createElement('bdi');
  inner.textContent = bubble.dataset.modelLabel;
  tag.appendChild(inner);
  // After the timestamp (if any) so order reads: time · model … buttons.
  const time = actions.querySelector(':scope > .bubble-time');
  if (time && time.nextSibling) actions.insertBefore(tag, time.nextSibling);
  else if (time) actions.appendChild(tag);
  else actions.insertBefore(tag, actions.firstChild);
}

// ── Per-bubble action row helpers ──────────────────────────────────────────

// Extracts the readable text from a bubble, excluding labels and action buttons.
function _getBubbleText(bubble) {
  if (!bubble) return '';
  const clone = bubble.cloneNode(true);
  clone.querySelectorAll('.label, .bubble-actions, .stop-btn').forEach(el => el.remove());
  return clone.textContent.trim();
}

// Text to put on the clipboard when Copy is pressed.
function _getBubbleCopyText(bubble) {
  if (bubble && typeof bubble.__mdSource === 'string' && bubble.__mdSource.trim()) {
    return bubble.__mdSource;
  }
  return _getBubbleText(bubble);
}

function _renderActionIcons(container) {
  if (container && window.lucide && typeof window.lucide.createIcons === 'function') {
    try {
      window.lucide.createIcons({
        nodes: Array.from(container.querySelectorAll('[data-lucide]:not(.lucide)')),
      });
    } catch (_) {}
  }
}

function _setActionIcon(btn, iconName) {
  const i = btn.querySelector('i');
  if (!i) return;
  i.setAttribute('data-lucide', iconName);
  i.classList.remove('lucide');
  i.removeAttribute('stroke');
  while (i.firstChild) i.removeChild(i.firstChild);
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    try { window.lucide.createIcons({ nodes: [i] }); } catch (_) {}
  }
}

function _speakBubble(btn, bubble) {
  if (!('speechSynthesis' in window)) {
    alert('Text-to-speech is not supported in this browser.');
    return;
  }
  const synth = window.speechSynthesis;
  if (btn.dataset.speaking === 'true') {
    try { synth.cancel(); } catch (_) {}
    return;
  }
  const text = _getBubbleText(bubble);
  if (!text) return;
  try { synth.cancel(); } catch (_) {}
  document.querySelectorAll('.bubble-action-btn[data-speaking="true"]').forEach((other) => {
    delete other.dataset.speaking;
    other.title = 'Read aloud';
    _setActionIcon(other, 'volume-2');
  });
  const u = new SpeechSynthesisUtterance(text);
  const restore = () => {
    delete btn.dataset.speaking;
    btn.title = 'Read aloud';
    _setActionIcon(btn, 'volume-2');
  };
  u.onend = restore;
  u.onerror = restore;
  btn.dataset.speaking = 'true';
  btn.title = 'Stop reading';
  _setActionIcon(btn, 'square');
  synth.speak(u);
}

async function _copyBubble(btn, bubble) {
  const text = _getBubbleCopyText(bubble);
  if (!text) return;
  try {
    // copyText handles insecure http://<ip> contexts (e.g. phones on the LAN),
    // where navigator.clipboard is undefined, via an execCommand fallback.
    await copyText(text);
    const origTitle = btn.title;
    btn.title = 'Copied!';
    btn.classList.add('copied');
    _setActionIcon(btn, 'check');
    setTimeout(() => {
      btn.title = origTitle;
      btn.classList.remove('copied');
      _setActionIcon(btn, 'copy');
    }, 1200);
  } catch (e) {
    console.warn('Copy failed:', e);
  }
}

function _toggleBubbleCollapse(btn, bubble) {
  const isCollapsed = bubble.classList.toggle('collapsed');
  btn.title = isCollapsed ? 'Expand message' : 'Collapse message';
  _setActionIcon(btn, isCollapsed ? 'chevron-right' : 'chevron-down');
}

// ── Per-turn delete (two-click confirm) ────────────────────────────────────

function _bubbleAnchorId(bubble) {
  return bubble && (bubble.getAttribute('data-msg-id') || bubble.getAttribute('data-turn-id'));
}

function _setDeleteIcon(btn, name) {
  btn.innerHTML = '<i data-lucide="' + name + '" style="width:14px;height:14px;"></i>';
  _renderActionIcons(btn);
}

function _resetBubbleDeleteBtn(btn) {
  if (!btn) return;
  btn.dataset.state = 'trash';
  btn.classList.remove('warning');
  btn.title = 'Delete this turn';
  _setDeleteIcon(btn, 'trash-2');
}

function _resetAllBubbleDeleteBtns(except) {
  document.querySelectorAll('.bubble-delete-btn[data-state="warning"]').forEach((b) => {
    if (b !== except) _resetBubbleDeleteBtn(b);
  });
}

function _handleBubbleDeleteClick(btn, bubble) {
  if (btn.dataset.state === 'warning') {
    btn.dataset.state = 'deleting';
    _deleteTurn(bubble, btn);
    return;
  }
  _resetAllBubbleDeleteBtns(btn);
  btn.dataset.state = 'warning';
  btn.classList.add('warning');
  btn.title = 'Click again to delete this message and its whole turn';
  _setDeleteIcon(btn, 'alert-triangle');
}

async function _deleteTurn(bubble, btn) {
  const anchor = _bubbleAnchorId(bubble);
  if (!anchor) { _resetBubbleDeleteBtn(btn); return; }
  const sid = app.currentSessionId;
  const uid = app.currentUserId || '';
  const url = apiPath('/api/v1/db/turn?session_id=' + encodeURIComponent(sid)
    + '&interaction_id=' + encodeURIComponent(anchor)
    + (uid ? '&user_id=' + encodeURIComponent(uid) : '')
    + '&db=local.db');
  try {
    const resp = await fetch(url, { method: 'DELETE', headers: { ...authHeaders() } });
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data || !Array.isArray(data.deleted_ids)) {
      console.warn('Delete turn failed:', resp.status, data);
      _resetBubbleDeleteBtn(btn);
      if (resp.status === 404 || resp.status === 405) {
        alert('Couldn\'t delete this turn: the server doesn\'t have this feature yet. Restart the app server and try again.');
      } else {
        alert('Couldn\'t delete this turn (server responded ' + resp.status + ').');
      }
      return;
    }
    const ids = new Set(data.deleted_ids.map(String));
    app.chatMessages.querySelectorAll('.chat-bubble').forEach((b) => {
      const mid = b.getAttribute('data-msg-id');
      const tid = b.getAttribute('data-turn-id');
      if ((mid && ids.has(String(mid))) || (tid && ids.has(String(tid)))) b.remove();
    });
    if (bubble && bubble.isConnected) bubble.remove();
    // Removing a turn's bubbles can leave its neighbours mis-joined — rescan.
    if (typeof app._regroupBubbles === 'function') { try { app._regroupBubbles(); } catch (_) {} }
    if (typeof app.populateSessionSelect === 'function') {
      try { app.populateSessionSelect(app.currentUserId); } catch (_) {}
    }
  } catch (e) {
    console.warn('Delete turn error:', e);
    _resetBubbleDeleteBtn(btn);
    alert('Couldn\'t delete this turn — no response from the server. Is the app server running?');
  }
}

function _makeBubbleDeleteBtn(bubble) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'bubble-action-btn bubble-delete-btn';
  btn.dataset.state = 'trash';
  btn.title = 'Delete this turn';
  btn.innerHTML = '<i data-lucide="trash-2" style="width:14px;height:14px;"></i>';
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    _handleBubbleDeleteClick(btn, bubble);
  });
  return btn;
}

// Clicking anywhere that isn't an armed delete button disarms them all.
document.addEventListener('click', (e) => {
  if (!e.target.closest || !e.target.closest('.bubble-delete-btn')) {
    _resetAllBubbleDeleteBtns(null);
  }
}, true);

// ── Bubble actions ─────────────────────────────────────────────────────────

function _addBubbleActions(bubble) {
  if (!bubble) return;
  // Streaming bubbles DO get the footer (model tag, collapse, read-aloud, copy)
  // so a reply that's still live \u2014 or one being recovered from the DB by the
  // reconcile loop \u2014 isn't action-less. Only the destructive per-turn DELETE is
  // held back while streaming: a mid-stream delete would race the still-writing
  // run. _setBubbleText calls us again when the streaming class drops at
  // finalize, and the reuse branch below then adds the delete button.
  const streaming = bubble.classList.contains('streaming');
  const txt = _getBubbleText(bubble);
  if (!txt || txt === '\u2026') return;
  const anchor = _bubbleAnchorId(bubble);
  const canDelete = anchor && !streaming;
  const existingActions = bubble.querySelector(':scope > .bubble-actions');
  if (existingActions) {
    if (canDelete && !existingActions.querySelector('.bubble-delete-btn')) {
      existingActions.appendChild(_makeBubbleDeleteBtn(bubble));
      _renderActionIcons(existingActions);
    }
    // Model tag may have been stamped after the row was first built (live turn).
    if (bubble.dataset.modelLabel && !existingActions.querySelector('.bubble-model')) {
      _injectModelTag(existingActions, bubble);
    }
    return;
  }

  const actions = document.createElement('div');
  actions.className = 'bubble-actions';

  const createdAtMs = bubble.getAttribute('data-created-at');
  if (createdAtMs) {
    const timeEl = document.createElement('span');
    timeEl.className = 'bubble-time';
    timeEl.setAttribute('data-created-at', createdAtMs);
    timeEl.textContent = _formatRelativeTime(Number(createdAtMs));
    actions.appendChild(timeEl);
  }

  // Per-turn model tag (carries the auto-margin that right-aligns the buttons).
  _injectModelTag(actions, bubble);

  // Collapse / expand button
  const collapseBtn = document.createElement('button');
  collapseBtn.type = 'button';
  collapseBtn.className = 'bubble-action-btn bubble-collapse-btn';
  collapseBtn.title = 'Collapse message';
  collapseBtn.innerHTML = '<i data-lucide="chevron-down" style="width:14px;height:14px;"></i>';
  collapseBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _toggleBubbleCollapse(collapseBtn, bubble);
  });
  actions.appendChild(collapseBtn);

  const speakBtn = document.createElement('button');
  speakBtn.type = 'button';
  speakBtn.className = 'bubble-action-btn';
  speakBtn.title = 'Read aloud';
  speakBtn.innerHTML = '<i data-lucide="volume-2" style="width:14px;height:14px;"></i>';
  speakBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _speakBubble(speakBtn, bubble);
  });
  actions.appendChild(speakBtn);

  const copyBtn = document.createElement('button');
  copyBtn.type = 'button';
  copyBtn.className = 'bubble-action-btn';
  copyBtn.title = 'Copy text';
  copyBtn.innerHTML = '<i data-lucide="copy" style="width:14px;height:14px;"></i>';
  copyBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _copyBubble(copyBtn, bubble);
  });
  actions.appendChild(copyBtn);

  if (canDelete) actions.appendChild(_makeBubbleDeleteBtn(bubble));

  bubble.appendChild(actions);
  _renderActionIcons(actions);
}

// Expose on app immediately for session-load.js / virtual scroll
app._addBubbleActions = _addBubbleActions;

export {
  _addBubbleActions,
  _bubbleAnchorId,
  _getBubbleText,
  _setActionIcon,
  _setBubbleModel,
  _setBubbleModelFromMeta,
  _modelLabelFromMeta,
};