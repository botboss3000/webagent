'use strict';

// Per-bubble action row — model tag, collapse/expand, read-aloud
// (SpeechSynthesis), copy, and two-click per-turn delete. Sets
// app._addBubbleActions. The footer shows on STREAMING bubbles too (live or
// being recovered from the DB), minus the delete button until the turn finalizes.
// Module map for this folder: ui/chat/js/README.md.

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

// Stamp a bubble with its model tag (display + full-id hover) for gutters to
// pick up when they're built. Also fires a live .premium glow when effort='high'
// so the bubble pulses like embers as soon as the premium model takes over.
function _setBubbleModel(bubble, model, effort) {
  if (!bubble) return;
  const label = _buildModelLabel(model, effort);
  if (!label) return;
  bubble.dataset.modelLabel = label;
  bubble.dataset.modelTitle = model || label;
  // Update existing gutters, inserting the tag when the footer was created
  // before the live llm event supplied its model metadata.
  bubble.querySelectorAll(':scope > .turn-gutter').forEach(gutter => {
    let tag = gutter.querySelector(':scope > .turn-gutter-model');
    if (!tag) {
      tag = document.createElement('span');
      tag.className = 'turn-gutter-model bubble-model';
      const inner = document.createElement('bdi');
      tag.appendChild(inner);
      gutter.insertBefore(tag, gutter.firstChild);
    }
    tag.title = model || label;
    const bdi = tag.querySelector('bdi');
    if (bdi) bdi.textContent = label;
  });
  // Premium model — fire glow on the bubble
  const eff = (effort || '').trim().toLowerCase();
  bubble.classList.toggle('premium', eff === 'high');
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

// Extracts the readable text from a bubble, excluding labels, the action row,
// and the turn-gutter footer (timestamp, model tag, buttons).
function _getBubbleText(bubble) {
  if (!bubble) return '';
  const clone = bubble.cloneNode(true);
  clone.querySelectorAll('.label, .bubble-actions, .stop-btn, .persistence-details, .turn-gutter').forEach(el => el.remove());
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

// ── Action confirmation notices ───────────────────────────────────────────
// Small floating pills ("Copied", "Messages undone", "Turn deleted", …) that
// confirm a bubble-footer action actually happened. Stacked above the chat
// pill, auto-dismissed, pointer-events none so they never block clicks.
// Styled by .bubble-action-notice in app1.css (design-system vars, both themes).
function _showActionNotice(text, isError) {
  let wrap = document.getElementById('bubble-action-notices');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.id = 'bubble-action-notices';
    wrap.setAttribute('aria-live', 'polite');
    wrap.style.cssText = 'position:fixed;left:0;right:0;bottom:88px;display:flex;flex-direction:column;align-items:center;gap:6px;pointer-events:none;z-index:10000;';
    document.body.appendChild(wrap);
  }
  const el = document.createElement('div');
  el.className = 'bubble-action-notice' + (isError ? ' err' : '');
  el.innerHTML = '<i data-lucide="' + (isError ? 'circle-alert' : 'check') + '" style="width:13px;height:13px;flex:none;"></i><span></span>';
  el.querySelector('span').textContent = text;
  wrap.appendChild(el);
  _renderActionIcons(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => {
      el.remove();
      if (!wrap.children.length) wrap.remove();
    }, 220);
  }, 1800);
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
    _showActionNotice('Copied');
    setTimeout(() => {
      btn.title = origTitle;
      btn.classList.remove('copied');
      _setActionIcon(btn, 'copy');
    }, 1200);
  } catch (e) {
    console.warn('Copy failed:', e);
    _showActionNotice('Copy failed', true);
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
  btn.title = 'Delete this message';
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
    const anchor = btn.dataset.deleteAnchor || _bubbleAnchorId(bubble);
    const section = btn.dataset.deleteSection ? 
      bubble.querySelector(`.turn-section.llm-section[data-section-idx="${btn.dataset.deleteSection}"]`) 
      : null;
    _deleteMessage(bubble, btn, anchor, section);
    return;
  }
  _resetAllBubbleDeleteBtns(btn);
  btn.dataset.state = 'warning';
  btn.classList.add('warning');
  btn.title = 'Click again to delete this message';
  _setDeleteIcon(btn, 'alert-triangle');
}

async function _deleteMessage(bubble, btn, anchorOverride, sectionEl) {
  const anchor = anchorOverride || _bubbleAnchorId(bubble);
  if (!anchor) { _resetBubbleDeleteBtn(btn); return; }
  const sid = app.currentSessionId;
  const uid = app.currentUserId || '';
  // Agent bubbles with tool calls: recycle the assistant interaction + its tool children.
  const hasToolCalls = bubble.classList.contains('agent') && bubble.querySelector('.bubble-tool-calls');
  const url = apiPath('/api/v1/db/interaction?session_id=' + encodeURIComponent(sid)
    + '&interaction_id=' + encodeURIComponent(anchor)
    + '&include_children=' + (hasToolCalls ? 'true' : 'false')
    + (uid ? '&user_id=' + encodeURIComponent(uid) : '')
    + '&db=user.db');
  try {
    const resp = await fetch(url, { method: 'DELETE', headers: { ...authHeaders() } });
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data || !Array.isArray(data.deleted_ids)) {
      console.warn('Delete message failed:', resp.status, data);
      _resetBubbleDeleteBtn(btn);
      if (resp.status === 404 || resp.status === 405) {
        alert('Couldn\'t delete this message: the server doesn\'t have this feature yet. Restart the app server and try again.');
      } else {
        alert('Couldn\'t delete this message (server responded ' + resp.status + ').');
      }
      return;
    }
    _showActionNotice('Message deleted');
    bubble.classList.remove('streaming', 'premium');

    if (sectionEl) {
      // ── Section-level delete (merged agent turn) ──
      sectionEl.classList.add('deleted');
      // Mark the gutter after this section as deleted too
      const gutter = sectionEl.nextElementSibling;
      if (gutter && gutter.classList.contains('turn-gutter')) {
        gutter.classList.add('deleted');
        _injectSectionDeletedActions(gutter, bubble, anchor);
      }
      // Only mark tool rows belonging to this interaction
      if (hasToolCalls) {
        bubble.querySelectorAll('.ca-tool-row').forEach(row => {
          if (row.dataset.detailMsgId === anchor) row.classList.add('deleted');
        });
      }
      // Only mark the whole bubble deleted if ALL sections are deleted
      const allSections = bubble.querySelectorAll(':scope > .turn-section.llm-section');
      const allDeleted = allSections.length > 0 && 
        Array.from(allSections).every(s => s.classList.contains('deleted'));
      if (allDeleted) {
        bubble.classList.add('deleted');
        // Keep per-section restore buttons — they have the correct per-section anchors.
        // A bubble-level restore would only reference the last anchor.
      }
    } else {
      // ── Whole-bubble delete (user message or single-section agent) ──
      _injectDeletedActions(bubble, anchor);
      // Mark only the tool-call rows that belong to THIS interaction
      if (hasToolCalls) {
        bubble.querySelectorAll('.ca-tool-row').forEach(row => {
          if (!row.dataset.detailMsgId || row.dataset.detailMsgId === anchor) {
            row.classList.add('deleted');
          }
        });
        // Only mark the whole bubble deleted if ALL its tool rows are now deleted
        const remaining = bubble.querySelectorAll('.ca-tool-row:not(.deleted)');
        if (remaining.length === 0) {
          bubble.classList.add('deleted');
        }
      } else {
        bubble.classList.add('deleted');
      }
    }
    // Refresh grouping
    if (typeof app._regroupBubbles === 'function') { try { app._regroupBubbles(); } catch (_) {} }
    if (typeof app.populateSessionSelect === 'function') {
      try { app.populateSessionSelect(app.currentUserId); } catch (_) {}
    }
  } catch (e) {
    console.warn('Delete message error:', e);
    _resetBubbleDeleteBtn(btn);
    alert('Couldn\'t delete this message — no response from the server. Is the app server running?');
  }
}

/**
 * Inject restore + permanent-delete buttons into a deleted SECTION's gutter.
 */
function _injectSectionDeletedActions(gutter, bubble, anchor) {
  if (!gutter) return;
  const existing = gutter.querySelector('.section-deleted-actions');
  if (existing) existing.remove();

  const actions = document.createElement('span');
  actions.className = 'section-deleted-actions';
  actions.style.cssText = 'display:inline-flex;gap:3px;margin-left:auto;flex:0 0 auto;';

  const restoreBtn = document.createElement('button');
  restoreBtn.type = 'button';
  restoreBtn.className = 'turn-gutter-btn restore-btn';
  restoreBtn.title = 'Restore this message';
  restoreBtn.innerHTML = '<i data-lucide="undo-2" style="width:14px;height:14px;"></i>';
  restoreBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (restoreBtn.dataset.state !== 'warning') {
      restoreBtn.dataset.state = 'warning';
      restoreBtn.title = 'Click again to restore';
      _setActionIcon(restoreBtn, 'alert-triangle');
      restoreBtn.classList.add('warning');
      const t = setTimeout(() => {
        restoreBtn.dataset.state = '';
        restoreBtn.title = 'Restore this message';
        _setActionIcon(restoreBtn, 'undo-2');
        restoreBtn.classList.remove('warning');
      }, 4000);
      return;
    }
    _restoreSection(gutter, bubble, anchor);
  });
  actions.appendChild(restoreBtn);

  const permBtn = document.createElement('button');
  permBtn.type = 'button';
  permBtn.className = 'turn-gutter-btn';
  permBtn.title = 'Permanently delete this message';
  permBtn.innerHTML = '<i data-lucide="trash-2" style="width:14px;height:14px;"></i>';
  permBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (permBtn.dataset.state !== 'warning') {
      permBtn.dataset.state = 'warning';
      permBtn.title = 'Click again to permanently erase';
      _setActionIcon(permBtn, 'alert-triangle');
      permBtn.classList.add('warning');
      const t = setTimeout(() => {
        permBtn.dataset.state = '';
        permBtn.title = 'Permanently delete this message';
        _setActionIcon(permBtn, 'trash-2');
        permBtn.classList.remove('warning');
      }, 4000);
      return;
    }
    _permanentDeleteSection(gutter, bubble, anchor);
  });
  actions.appendChild(permBtn);

  gutter.appendChild(actions);
  _renderActionIcons(actions);
}

async function _restoreSection(gutter, bubble, anchor) {
  const sid = app.currentSessionId;
  const uid = app.currentUserId || '';
  const hasToolCalls = bubble.classList.contains('agent') && bubble.querySelector('.bubble-tool-calls');
  try {
    const url = apiPath('/api/v1/db/interaction/restore?session_id=' + encodeURIComponent(sid)
      + '&interaction_id=' + encodeURIComponent(anchor)
      + '&include_children=' + (hasToolCalls ? 'true' : 'false')
      + (uid ? '&user_id=' + encodeURIComponent(uid) : '')
      + '&db=user.db');
    const resp = await fetch(url, { method: 'POST', headers: { ...authHeaders() } });
    if (resp.ok) {
      // Unmark the section before this gutter
      const section = gutter.previousElementSibling;
      if (section && section.classList.contains('turn-section')) {
        section.classList.remove('deleted');
      }
      gutter.classList.remove('deleted');
      gutter.querySelector('.section-deleted-actions')?.remove();
      // Unmark matching tool rows
      bubble.querySelectorAll('.ca-tool-row.deleted').forEach(row => {
        if (row.dataset.detailMsgId === anchor) row.classList.remove('deleted');
      });
      // Unmark bubble if not all sections are deleted
      bubble.classList.remove('deleted');
      bubble.querySelector('.bubble-actions.deleted-actions')?.remove();
    }
  } catch (_) {}
}

async function _permanentDeleteSection(gutter, bubble, anchor) {
  const sid = app.currentSessionId;
  const uid = app.currentUserId || '';
  const hasToolCalls = bubble.classList.contains('agent') && bubble.querySelector('.bubble-tool-calls');
  try {
    const url = apiPath('/api/v1/db/interaction?session_id=' + encodeURIComponent(sid)
      + '&interaction_id=' + encodeURIComponent(anchor)
      + '&include_children=' + (hasToolCalls ? 'true' : 'false')
      + (uid ? '&user_id=' + encodeURIComponent(uid) : '')
      + '&db=user.db&permanent=true');
    const resp = await fetch(url, { method: 'DELETE', headers: { ...authHeaders() } });
    if (resp.ok) {
      // Remove the section
      const section = gutter.previousElementSibling;
      if (section && section.classList.contains('turn-section')) section.remove();
      gutter.remove();
      // Remove matching tool rows
      bubble.querySelectorAll('.ca-tool-row').forEach(row => {
        if (row.dataset.detailMsgId === anchor) row.remove();
      });
      // If no sections remain, remove the bubble
      const remaining = bubble.querySelectorAll(':scope > .turn-section.llm-section');
      if (remaining.length === 0) bubble.remove();
    }
  } catch (_) {}
}

/**
 * Replace a deleted bubble's action row with restore + permanent-delete buttons.
 * Both use the two-click hazard confirm pattern.
 */
function _injectDeletedActions(bubble, anchor) {
  if (!bubble || !anchor) return;
  const existing = bubble.querySelector(':scope > .bubble-actions');
  if (existing) existing.remove();

  const actions = document.createElement('div');
  actions.className = 'bubble-actions deleted-actions';

  const restoreBtn = document.createElement('button');
  restoreBtn.type = 'button';
  restoreBtn.className = 'bubble-action-btn restore-btn';
  restoreBtn.title = 'Restore this message';
  restoreBtn.innerHTML = '<i data-lucide="undo-2" style="width:14px;height:14px;"></i>';
  restoreBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (restoreBtn.dataset.state !== 'warning') {
      _resetAllBubbleDeleteBtns(null);
      restoreBtn.dataset.state = 'warning';
      restoreBtn.title = 'Click again to restore this message';
      _setActionIcon(restoreBtn, 'alert-triangle');
      restoreBtn.classList.add('warning');
      const timer = setTimeout(() => {
        restoreBtn.dataset.state = '';
        restoreBtn.title = 'Restore this message';
        _setActionIcon(restoreBtn, 'undo-2');
        restoreBtn.classList.remove('warning');
      }, 4000);
      restoreBtn._timer = timer;
      return;
    }
    if (restoreBtn._timer) { clearTimeout(restoreBtn._timer); restoreBtn._timer = null; }
    _restoreMessage(bubble, restoreBtn);
  });
  actions.appendChild(restoreBtn);

  const permDeleteBtn = document.createElement('button');
  permDeleteBtn.type = 'button';
  permDeleteBtn.className = 'bubble-action-btn bubble-delete-btn';
  permDeleteBtn.title = 'Permanently delete this message';
  permDeleteBtn.innerHTML = '<i data-lucide="trash-2" style="width:14px;height:14px;"></i>';
  permDeleteBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (permDeleteBtn.dataset.state !== 'warning') {
      _resetAllBubbleDeleteBtns(null);
      permDeleteBtn.dataset.state = 'warning';
      permDeleteBtn.title = 'Click again to permanently erase this message';
      _setActionIcon(permDeleteBtn, 'alert-triangle');
      permDeleteBtn.classList.add('warning');
      const timer = setTimeout(() => {
        permDeleteBtn.dataset.state = '';
        permDeleteBtn.title = 'Permanently delete this message';
        _setActionIcon(permDeleteBtn, 'trash-2');
        permDeleteBtn.classList.remove('warning');
      }, 4000);
      permDeleteBtn._timer = timer;
      return;
    }
    if (permDeleteBtn._timer) { clearTimeout(permDeleteBtn._timer); permDeleteBtn._timer = null; }
    _permanentDeleteMessage(bubble, permDeleteBtn);
  });
  actions.appendChild(permDeleteBtn);

  bubble.appendChild(actions);
  _renderActionIcons(actions);
}

async function _restoreMessage(bubble, btn) {
  const anchor = _bubbleAnchorId(bubble);
  if (!anchor) return;
  const sid = app.currentSessionId;
  const uid = app.currentUserId || '';
  const hasToolCalls = bubble.classList.contains('agent') && bubble.querySelector('.bubble-tool-calls');
  const url = apiPath('/api/v1/db/interaction/restore?session_id=' + encodeURIComponent(sid)
    + '&interaction_id=' + encodeURIComponent(anchor)
    + '&include_children=' + (hasToolCalls ? 'true' : 'false')
    + (uid ? '&user_id=' + encodeURIComponent(uid) : '')
    + '&db=user.db');
  try {
    const resp = await fetch(url, { method: 'POST', headers: { ...authHeaders() } });
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data || !Array.isArray(data.restored_ids)) {
      console.warn('Restore message failed:', resp.status, data);
      alert('Could not restore this message (server responded ' + resp.status + ').');
      return;
    }
    _showActionNotice('Message restored');
    bubble.classList.remove('deleted');
    bubble.querySelector('.bubble-actions')?.remove();
    // Unmark all sections and section gutters within this bubble
    bubble.querySelectorAll('.turn-section.llm-section.deleted').forEach(s => s.classList.remove('deleted'));
    bubble.querySelectorAll('.turn-gutter.deleted').forEach(g => {
      g.classList.remove('deleted');
      g.querySelector('.section-deleted-actions')?.remove();
    });
    // Unmark only the tool-call rows that belong to THIS interaction
    bubble.querySelectorAll('.ca-tool-row.deleted').forEach(row => {
      if (!row.dataset.detailMsgId || row.dataset.detailMsgId === anchor) {
        row.classList.remove('deleted');
      }
    });
    if (typeof app._regroupBubbles === 'function') { try { app._regroupBubbles(); } catch (_) {} }
  } catch (e) {
    console.warn('Restore message error:', e);
    alert('Could not restore this message — no response from the server.');
  }
}

async function _permanentDeleteMessage(bubble, btn) {
  const anchor = _bubbleAnchorId(bubble);
  if (!anchor) return;
  const sid = app.currentSessionId;
  const uid = app.currentUserId || '';
  const hasToolCalls = bubble.classList.contains('agent') && bubble.querySelector('.bubble-tool-calls');
  const url = apiPath('/api/v1/db/interaction?session_id=' + encodeURIComponent(sid)
    + '&interaction_id=' + encodeURIComponent(anchor)
    + '&include_children=' + (hasToolCalls ? 'true' : 'false')
    + (uid ? '&user_id=' + encodeURIComponent(uid) : '')
    + '&db=user.db&permanent=true');
  try {
    const resp = await fetch(url, { method: 'DELETE', headers: { ...authHeaders() } });
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data || !Array.isArray(data.deleted_ids)) {
      console.warn('Permanent delete message failed:', resp.status, data);
      alert('Could not permanently delete this message (server responded ' + resp.status + ').');
      return;
    }
    _showActionNotice('Message permanently deleted');
    bubble.remove();
    if (typeof app._regroupBubbles === 'function') { try { app._regroupBubbles(); } catch (_) {} }
    if (typeof app.populateSessionSelect === 'function') {
      try { app.populateSessionSelect(app.currentUserId); } catch (_) {}
    }
  } catch (e) {
    console.warn('Permanent delete message error:', e);
    alert('Could not permanently delete this message — no response from the server.');
  }
}

function _makeBubbleDeleteBtn(bubble, anchorOverride, sectionIdx) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'bubble-action-btn bubble-delete-btn';
  btn.dataset.state = 'trash';
  btn.title = 'Delete this message';
  btn.innerHTML = '<i data-lucide="trash-2" style="width:14px;height:14px;"></i>';
  if (anchorOverride) btn.dataset.deleteAnchor = anchorOverride;
  if (sectionIdx != null) btn.dataset.deleteSection = String(sectionIdx);
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

// ── Undo bubble (delete from this message forward, put text in composer) ──

// Interactive choice panel shown when the composer already has text before an
// undo operation. Reuses the wa-confirm-overlay / wa-confirm-panel structure
// from confirm-dialog.js but neutral-styled — no hazard icon or tone class.
// Returns a Promise<string> resolving to 'replace', 'keep', or 'append'.
function _showComposerChoicePanel(undoText) {
  const preview = undoText.length > 80 ? undoText.slice(0, 77) + '…' : undoText;
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'wa-confirm-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');

    const panel = document.createElement('div');
    panel.className = 'wa-confirm-panel';
    panel.innerHTML = `
      <div class="wa-confirm-head">
        <div class="wa-confirm-title">Composer has content</div>
      </div>
      <div class="wa-confirm-msg">The composer already has text. How should the undone message be handled?</div>
      <div class="wa-confirm-preview" style="font-size:12px;color:var(--fg-muted);margin:8px 0;padding:8px;border-radius:6px;background:var(--bg-1);max-height:72px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">Undo text: “${preview}”</div>
      <div class="wa-confirm-actions" style="flex-wrap:wrap;gap:6px;">
        <button type="button" class="ac-btn ac-btn-ghost wa-undo-clear">Clear &amp; replace</button>
        <button type="button" class="ac-btn ac-btn-ghost wa-undo-keep">Keep current</button>
        <button type="button" class="ac-btn ac-btn-primary wa-undo-append">Append</button>
      </div>`;

    const clearBtn = panel.querySelector('.wa-undo-clear');
    const keepBtn  = panel.querySelector('.wa-undo-keep');
    const appendBtn = panel.querySelector('.wa-undo-append');

    overlay.appendChild(panel);
    document.body.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add('show'));

    let settled = false;
    const close = (result) => {
      if (settled) return;
      settled = true;
      document.removeEventListener('keydown', onKey, true);
      overlay.classList.remove('show');
      setTimeout(() => overlay.remove(), 160);
      resolve(result);
    };

    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); close('keep'); }
    }

    clearBtn.addEventListener('click', () => close('replace'));
    keepBtn.addEventListener('click', () => close('keep'));
    appendBtn.addEventListener('click', () => close('append'));
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close('keep'); });
    document.addEventListener('keydown', onKey, true);

    keepBtn.focus();
  });
}

async function _undoBubble(bubble) {
  const anchor = _bubbleAnchorId(bubble);
  if (!anchor) return;
  const sid = app.currentSessionId;
  const uid = app.currentUserId || '';

  // Extract the user message text
  const userText = _getBubbleText(bubble);

  // If composer already has content, let the user decide what to do
  const hasExisting = app.chatInput && app.chatInput.value.trim();
  let composerAction = 'replace';
  if (hasExisting) {
    composerAction = await _showComposerChoicePanel(userText);
  }

  // Collect all sibling bubbles from this one forward in the DOM
  const toDelete = [];
  let next = bubble;
  while (next) {
    const id = _bubbleAnchorId(next);
    if (id) toDelete.push({ el: next, id, isAgent: next.classList.contains('agent') });
    next = next.nextElementSibling;
  }

  // Soft-delete each interaction via the API (recycle bin)
  const failed = [];
  for (const item of toDelete) {
    const hasToolCalls = item.isAgent && item.el.querySelector('.bubble-tool-calls');
    const url = apiPath('/api/v1/db/interaction?session_id=' + encodeURIComponent(sid)
      + '&interaction_id=' + encodeURIComponent(item.id)
      + '&include_children=' + (hasToolCalls ? 'true' : 'false')
      + (uid ? '&user_id=' + encodeURIComponent(uid) : '')
      + '&db=user.db');
    try {
      const resp = await fetch(url, { method: 'DELETE', headers: { ...authHeaders() } });
      if (!resp.ok) {
        console.warn('Undo delete failed for', item.id, resp.status);
        failed.push(item.id);
      }
    } catch (e) {
      console.warn('Undo delete error for', item.id, e);
      failed.push(item.id);
    }
  }

  if (failed.length === toDelete.length && toDelete.length > 0) {
    _showActionNotice('Undo failed', true);
    return;
  }

  // Remove deleted bubbles from the DOM
  for (const item of toDelete) {
    item.el.remove();
  }

  // Fill composer based on the user's choice
  if (composerAction !== 'keep' && app.chatInput) {
    if (composerAction === 'replace') {
      app.chatInput.value = userText;
    } else if (composerAction === 'append') {
      app.chatInput.value = app.chatInput.value.trim() + '\n' + userText;
    }
    app.chatInput.dispatchEvent(new Event('input', { bubbles: true }));
    app.chatInput.focus();
  }

  _showActionNotice('Messages undone');

  // Refresh grouping and session list
  if (typeof app._regroupBubbles === 'function') { try { app._regroupBubbles(); } catch (_) {} }
  if (typeof app.populateSessionSelect === 'function') {
    try { app.populateSessionSelect(app.currentUserId); } catch (_) {}
  }
}

function _makeBubbleUndoBtn(bubble) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'bubble-action-btn bubble-undo-btn';
  btn.title = 'Undo from this message';
  btn.innerHTML = '<i data-lucide="undo-2" style="width:14px;height:14px;"></i>';
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    _undoBubble(bubble);
  });
  return btn;
}

// ── Fork bubble (fork session at this message) ─────────────────────────────
// Agent bubbles only — the fork button lives in the agent turn gutter. User
// bubbles keep the Undo button instead (see _buildUserTurnGutter).

async function _forkBubble(bubble) {
  const anchor = _bubbleAnchorId(bubble);
  if (!anchor) return;
  const sid = app.currentSessionId;
  const uid = app.currentUserId || '';
  try {
    const resp = await fetch(apiPath('/api/v1/db/sessions/' + encodeURIComponent(sid) + '/fork?db=user.db'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ up_to_interaction_id: anchor, user_id: uid }),
    });
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data || !data.session_id) {
      console.warn('Fork failed:', resp.status, data);
      alert('Couldn\'t fork session at this message (server responded ' + resp.status + ').');
      return;
    }
    _showActionNotice('Session forked');
    // Switch to the new forked session
    const { switchToSession } = await import('./session-core.js');
    await switchToSession(data.session_id);
  } catch (e) {
    console.warn('Fork error:', e);
    alert('Couldn\'t fork session — no response from the server.');
  }
}

function _makeBubbleForkBtn(bubble) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'bubble-action-btn bubble-fork-btn';
  btn.title = 'Fork session at this message';
  btn.innerHTML = '<i data-lucide="git-branch" style="width:14px;height:14px;"></i>';
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    _forkBubble(bubble);
  });
  return btn;
}

// ── Schema inspect (3-line icon — view the full schema sent to the LLM) ────────

function _hasSentSchema(bubble) {
  if (!bubble || !bubble.classList.contains('agent')) return false;
  const msgId = bubble.getAttribute('data-msg-id');
  if (!msgId) return false;
  // The slimmed output on initial load sets _has_sent_schema=true when the
  // full turn detail has a _sent_messages snapshot to fetch.
  try {
    const raw = bubble.getAttribute('data-output');
    if (!raw) return true; // unknown → show the button, let fetch decide
    const o = JSON.parse(raw);
    return !!o._has_sent_schema;
  } catch (_) { return true; }
}

function _toggleSchemaBubble(bubble) {
  const existing = bubble._schemaBubble;
  if (existing) {
    existing.remove();
    bubble._schemaBubble = null;
    bubble.classList.remove('schema-inspect-open');
    return;
  }
  _fetchAndShowSchema(bubble);
}

async function _fetchAndShowSchema(bubble) {
  const msgId = bubble.getAttribute('data-msg-id');
  if (!msgId) return;
  const sid = app.currentSessionId;
  if (!sid) return;
  const url = apiPath(`/api/v1/db/session-turn-detail?db=user.db&session_id=${encodeURIComponent(sid)}&ids=${encodeURIComponent(msgId)}`);
  let data;
  try {
    const resp = await fetch(url, { headers: { ...authHeaders() } });
    data = await resp.json().catch(() => null);
    if (!resp.ok || !data) throw new Error('fetch failed');
  } catch (_) {
    return; // silently fail — don't block the UI
  }
  const detail = data?.details?.[msgId];
  if (!detail) return;
  let output;
  try { output = JSON.parse(detail.output || '{}'); } catch (_) { output = {}; }
  const messages = output._sent_messages;
  if (!messages || !messages.length) return;
  _renderSchemaBubble(bubble, messages, output);
}

function _renderSchemaBubble(bubble, messages, output) {
  // Build the schema preview bubble — rendered above the agent bubble,
  // styled as a right-aligned technical overlay with a monospace code block.
  const schemaBubble = document.createElement('div');
  schemaBubble.className = 'chat-bubble schema-inspect-bubble';

  const header = document.createElement('div');
  header.className = 'schema-inspect-header';
  header.innerHTML = '<span class="schema-inspect-title">Sent to LLM</span>';
  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'schema-inspect-close';
  closeBtn.innerHTML = '&times;';
  closeBtn.title = 'Close';
  closeBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    schemaBubble.remove();
    bubble._schemaBubble = null;
    bubble.classList.remove('schema-inspect-open');
  });
  header.appendChild(closeBtn);
  schemaBubble.appendChild(header);

  // Build a display of the messages + tools in a readable format
  const body = document.createElement('div');
  body.className = 'schema-inspect-body';

  // System prompt is often huge — collapse it by default
  let hasSystem = false;
  for (const msg of messages) {
    if (msg.role === 'system') hasSystem = true;
    const block = document.createElement('div');
    block.className = 'schema-msg';

    const roleLabel = document.createElement('span');
    roleLabel.className = 'schema-msg-role';
    roleLabel.textContent = msg.role.toUpperCase();
    block.appendChild(roleLabel);

    if (msg.tool_calls) {
      const tcs = document.createElement('span');
      tcs.className = 'schema-msg-tool-calls';
      tcs.textContent = ' [tool_calls: ' + msg.tool_calls.map(tc => tc.function?.name || '?').join(', ') + ']';
      block.appendChild(tcs);
    } else if (msg.role === 'system' && msg.content && msg.content.length > 800) {
      const details = document.createElement('details');
      details.className = 'schema-system-fold';
      const summary = document.createElement('summary');
      summary.textContent = msg.content.slice(0, 200).replace(/\n/g, ' ') + '…';
      details.appendChild(summary);
      const pre = document.createElement('pre');
      pre.textContent = msg.content;
      details.appendChild(pre);
      block.appendChild(details);
    } else if (msg.content) {
      const pre = document.createElement('pre');
      pre.textContent = typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content, null, 2);
      block.appendChild(pre);
    }
    body.appendChild(block);
  }

  // Tool definitions
  const tools = output._sent_tools;
  if (tools && tools.length > 0) {
    const toolsBlock = document.createElement('div');
    toolsBlock.className = 'schema-msg';
    const toolsLabel = document.createElement('span');
    toolsLabel.className = 'schema-msg-role';
    toolsLabel.textContent = 'TOOLS (' + tools.length + ')';
    toolsBlock.appendChild(toolsLabel);
    const details = document.createElement('details');
    details.className = 'schema-system-fold';
    const summary = document.createElement('summary');
    summary.textContent = tools.map(t => t.function?.name || '?').join(', ');
    details.appendChild(summary);
    const pre = document.createElement('pre');
    pre.textContent = JSON.stringify(tools, null, 2);
    details.appendChild(pre);
    toolsBlock.appendChild(details);
    body.appendChild(toolsBlock);
  }

  schemaBubble.appendChild(body);
  bubble.parentNode.insertBefore(schemaBubble, bubble);
  bubble._schemaBubble = schemaBubble;
  bubble.classList.add('schema-inspect-open');
}

function _makeBubbleSchemaBtn(bubble) {
  if (!_hasSentSchema(bubble)) return null;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'bubble-action-btn bubble-schema-btn';
  btn.title = 'View schema sent to LLM';
  btn.innerHTML = '<i data-lucide="menu" style="width:14px;height:14px;"></i>';
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    _toggleSchemaBubble(bubble);
  });
  return btn;
}

// Build a gutter for user bubbles — time, copy, undo, delete (no model/speak/collapse/schema).
function _buildUserTurnGutter(bubble) {
  const gutter = document.createElement('div');
  gutter.className = 'turn-gutter';
  gutter.setAttribute('hidden', '');
  gutter.dataset.sectionIdx = '0';

  // Time
  const createdAtMs = bubble.getAttribute('data-created-at');
  const timeError = bubble.getAttribute('data-time-error');
  if (createdAtMs || (app.isDebug && timeError)) {
    const timeEl = document.createElement('span');
    timeEl.className = 'turn-gutter-time bubble-time';
    if (createdAtMs) {
      timeEl.setAttribute('data-created-at', createdAtMs);
      timeEl.textContent = _formatRelativeTime(Number(createdAtMs));
    } else {
      timeEl.setAttribute('data-time-error', timeError);
      timeEl.title = timeError;
      timeEl.textContent = `time error: ${timeError}`;
    }
    gutter.appendChild(timeEl);
  }

  // Copy
  const copyBtn = document.createElement('button');
  copyBtn.type = 'button';
  copyBtn.className = 'turn-gutter-btn';
  copyBtn.title = 'Copy text';
  copyBtn.innerHTML = '<i data-lucide="copy" style="width:14px;height:14px;"></i>';
  copyBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _copyBubble(copyBtn, bubble);
  });
  gutter.appendChild(copyBtn);

  // Undo
  const anchor = _bubbleAnchorId(bubble);
  if (anchor) gutter.appendChild(_makeBubbleUndoBtn(bubble));

  // Delete
  if (anchor) gutter.appendChild(_makeBubbleDeleteBtn(bubble));

  _renderActionIcons(gutter);
  return gutter;
}

// ── Turn-section gutter system ────────────────────────────────────────────
// Each agent turn is ONE bubble containing multiple .turn-section blocks
// (LLM text + tool calls). Clicking an LLM section toggles a .turn-gutter
// between it and the next section, showing time, model, and action buttons.

// Build a gutter element (hidden) for placement between turn sections.
function _buildTurnGutter(bubble, sectionIdx) {
  const gutter = document.createElement('div');
  gutter.className = 'turn-gutter';
  gutter.setAttribute('hidden', '');
  gutter.dataset.sectionIdx = sectionIdx;

  // Find the corresponding section in the bubble to get its interaction ID
  const sections = bubble.querySelectorAll(':scope > .turn-section.llm-section');
  const section = [...sections].find(s => s.dataset.sectionIdx === String(sectionIdx));
  const sectionAnchor = (section && (section.dataset.msgId || section.dataset.turnId))
    || _bubbleAnchorId(bubble);

  // Time
  const createdAtMs = bubble.getAttribute('data-created-at');
  const timeError = bubble.getAttribute('data-time-error');
  if (createdAtMs || (app.isDebug && timeError)) {
    const timeEl = document.createElement('span');
    timeEl.className = 'turn-gutter-time bubble-time';
    if (createdAtMs) {
      timeEl.setAttribute('data-created-at', createdAtMs);
      timeEl.textContent = _formatRelativeTime(Number(createdAtMs));
    } else {
      timeEl.setAttribute('data-time-error', timeError);
      timeEl.title = timeError;
      timeEl.textContent = `time error: ${timeError}`;
    }
    gutter.appendChild(timeEl);
  }

  // Model tag
  if (bubble.dataset.modelLabel) {
    const tag = document.createElement('span');
    tag.className = 'turn-gutter-model bubble-model';
    tag.title = bubble.dataset.modelTitle || bubble.dataset.modelLabel;
    const inner = document.createElement('bdi');
    inner.textContent = bubble.dataset.modelLabel;
    tag.appendChild(inner);
    gutter.appendChild(tag);
  }

  // Collapse
  const collapseBtn = document.createElement('button');
  collapseBtn.type = 'button';
  collapseBtn.className = 'turn-gutter-btn bubble-collapse-btn';
  collapseBtn.title = 'Collapse message';
  collapseBtn.innerHTML = '<i data-lucide="chevron-down" style="width:14px;height:14px;"></i>';
  collapseBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _toggleBubbleCollapse(collapseBtn, bubble);
  });
  gutter.appendChild(collapseBtn);

  // Read aloud
  const speakBtn = document.createElement('button');
  speakBtn.type = 'button';
  speakBtn.className = 'turn-gutter-btn';
  speakBtn.title = 'Read aloud';
  speakBtn.innerHTML = '<i data-lucide="volume-2" style="width:14px;height:14px;"></i>';
  speakBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _speakBubble(speakBtn, bubble);
  });
  gutter.appendChild(speakBtn);

  // Copy
  const copyBtn = document.createElement('button');
  copyBtn.type = 'button';
  copyBtn.className = 'turn-gutter-btn';
  copyBtn.title = 'Copy text';
  copyBtn.innerHTML = '<i data-lucide="copy" style="width:14px;height:14px;"></i>';
  copyBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _copyBubble(copyBtn, bubble);
  });
  gutter.appendChild(copyBtn);

  // Fork (agent bubbles only — fork session at this message)
  const anchor = _bubbleAnchorId(bubble);
  if (anchor && bubble.classList.contains('agent')) gutter.appendChild(_makeBubbleForkBtn(bubble));

  // Delete (only if not streaming and has anchor)
  // Use sectionAnchor for per-section targeting in merged agent turns
  const streaming = bubble.classList.contains('streaming');
  if (sectionAnchor && !streaming) gutter.appendChild(_makeBubbleDeleteBtn(bubble, sectionAnchor, sectionIdx));

  // Schema inspect
  if (bubble.classList.contains('agent')) {
    const schemaBtn = _makeBubbleSchemaBtn(bubble);
    if (schemaBtn) gutter.appendChild(schemaBtn);
  }

  _renderActionIcons(gutter);
  return gutter;
}

// Toggle the gutter below the given LLM section.
function _toggleSectionGutter(section) {
  let gutter = section.nextElementSibling;
  if (!gutter || !gutter.classList.contains('turn-gutter')) return;
  const isHidden = gutter.hasAttribute('hidden');
  if (isHidden) {
    gutter.removeAttribute('hidden');
  } else {
    gutter.setAttribute('hidden', '');
  }
}

// Clicking anywhere outside a gutter or its LLM section closes all gutters
// except the last section of a bubble (which stays in its user-selected state).
document.addEventListener('click', function(e) {
  const target = e.target;
  if (!target || !target.closest) return;
  // If the click is on an LLM section, inside a gutter, or on a gutter button, don't close
  if (target.closest('.llm-section') || target.closest('.turn-gutter') || target.closest('.turn-gutter-btn')) return;
  // Close all open gutters except those on the last LLM section of their bubble
  document.querySelectorAll('.turn-gutter:not([hidden])').forEach(g => {
    const parentBubble = g.closest('.chat-bubble');
    if (!parentBubble) return;
    const sections = parentBubble.querySelectorAll(':scope > .turn-section.llm-section');
    // If this gutter follows the last section, preserve its user-selected state.
    if (sections.length > 0) {
      const lastSection = sections[sections.length - 1];
      if (g === lastSection.nextElementSibling) return;
    }
    g.setAttribute('hidden', '');
  });
}, true);

// Show the gutter on the last LLM section of a bubble (the "last message" footer).
// Hides all other gutters in the same bubble so only the very last one starts visible.
// Works for BOTH agent and user bubbles — a user bubble's footer (copy / undo /
// delete) starts visible on its last section just like the agent's.
function _showLastSectionGutter(bubble) {
  if (!bubble) return;
  const sections = bubble.querySelectorAll(':scope > .turn-section.llm-section');
  if (!sections.length) return;
  const last = sections[sections.length - 1];
  // Hide gutters for all sections except the last
  sections.forEach((s, i) => {
    const g = s.nextElementSibling;
    if (g && g.classList.contains('turn-gutter')) {
      if (i < sections.length - 1) {
        g.setAttribute('hidden', '');
      } else {
        g.removeAttribute('hidden');
      }
    }
  });
}

// Wire an LLM section with its click handler and ensure a gutter exists after it.
function _wireLLMSection(section, bubble) {
  if (!section || section._turnWired) return;
  section._turnWired = true;

  // Ensure a gutter exists after this section
  let gutter = section.nextElementSibling;
  if (!gutter || !gutter.classList.contains('turn-gutter')) {
    const idx = section.dataset.sectionIdx || '0';
    gutter = _buildTurnGutter(bubble, idx);
    section.after(gutter);
  }

  section.addEventListener('click', (e) => {
    e.stopPropagation();
    // Every message section toggles its own footer, including the final section.
    _toggleSectionGutter(section);
  });

  // If this is the last LLM section in the bubble, auto-show its gutter
  const allSections = bubble.querySelectorAll(':scope > .turn-section.llm-section');
  if (allSections.length > 0 && section === allSections[allSections.length - 1]) {
    _showLastSectionGutter(bubble);
  }
}

// Wrap raw bubble content in an LLM section (used for legacy/streaming bubbles
// that don't yet have turn-section markup). Works for both agent and user bubbles.
function _ensureLLMSections(bubble) {
  if (!bubble || bubble._sectionsEnsured) return;
  bubble._sectionsEnsured = true;

  const relevant = bubble.classList.contains('agent') || bubble.classList.contains('user');
  if (!relevant) return;

  // Collect direct children that aren't sections, gutters, or legacy footers
  const raw = [];
  for (const child of Array.from(bubble.children)) {
    if (child.classList.contains('turn-section') || child.classList.contains('turn-gutter')
        || child.classList.contains('bubble-actions') || child.classList.contains('label')) continue;
    raw.push(child);
  }
  if (!raw.length) return;

  // Wrap them in an LLM section
  const section = document.createElement('div');
  section.className = 'turn-section llm-section';
  section.dataset.sectionIdx = '0';
  raw.forEach(c => section.appendChild(c));
  bubble.appendChild(section);
}

// ── Bubble actions (section-aware) ────────────────────────────────────────

function _addBubbleActions(bubble) {
  if (!bubble) return;

  const isAgent = bubble.classList.contains('agent');
  const isUser = bubble.classList.contains('user');

  // Tool-only groups own no response message. Their update rows render their
  // own footers within the disclosure.
  if (isAgent && bubble.classList.contains('tool-only')) return;

  if (isUser) {
    const txt = _getBubbleText(bubble);
    if (!txt) return;

    // Remove legacy footer if present
    const oldFooter = bubble.querySelector(':scope > .bubble-actions');
    if (oldFooter) oldFooter.remove();

    // Wrap raw content in LLM sections if needed
    _ensureLLMSections(bubble);

    // Wire user LLM sections with a user-appropriate gutter
    bubble.querySelectorAll(':scope > .turn-section.llm-section').forEach(s => {
      if (s._turnWired) return;
      s._turnWired = true;

      // Ensure a user-appropriate gutter exists after this section
      let gutter = s.nextElementSibling;
      if (!gutter || !gutter.classList.contains('turn-gutter')) {
        gutter = _buildUserTurnGutter(bubble);
        s.after(gutter);
      }

      // Every message section toggles its own footer, including the final section.
      s.addEventListener('click', (e) => {
        e.stopPropagation();
        _toggleSectionGutter(s);
      });
    });

    // Auto-show the last section's gutter
    _showLastSectionGutter(bubble);

    return;
  }

  if (!isAgent) return;

  const streaming = bubble.classList.contains('streaming');
  const txt = _getBubbleText(bubble);
  if (!txt || txt === '\u2026') return;

  // Remove legacy footer if present
  const oldFooter = bubble.querySelector(':scope > .bubble-actions');
  if (oldFooter) oldFooter.remove();

  // Wrap raw content in LLM sections if needed
  _ensureLLMSections(bubble);

  // Wire all existing LLM sections
  bubble.querySelectorAll(':scope > .turn-section.llm-section').forEach(s => {
    _wireLLMSection(s, bubble);
  });

  // If streaming, mark that sections need rewiring on finalize
  if (streaming) bubble._needsSectionWire = true;
}

// Called after a bubble is finalized (streaming → done) to ensure sections are wired.
function _finalizeBubbleSections(bubble) {
  if (!bubble) return;
  if (bubble._needsSectionWire) {
    bubble._needsSectionWire = false;
    _ensureLLMSections(bubble);
    bubble.querySelectorAll(':scope > .turn-section.llm-section').forEach(s => {
      _wireLLMSection(s, bubble);
    });
  }

  // Streaming gutters intentionally omit destructive actions. Once the saved
  // interaction is final, add the same per-section delete control a cold-loaded
  // bubble receives without rebuilding the gutter or duplicating listeners.
  if (!bubble.classList.contains('streaming')) {
    const sections = bubble.querySelectorAll(':scope > .turn-section.llm-section');
    sections.forEach(section => {
      const gutter = section.nextElementSibling;
      if (!gutter || !gutter.classList.contains('turn-gutter')) return;
      if (gutter.querySelector(':scope > .bubble-delete-btn')) return;
      const sectionIdx = section.dataset.sectionIdx || '0';
      const sectionAnchor = section.dataset.msgId || section.dataset.turnId || _bubbleAnchorId(bubble);
      if (!sectionAnchor) return;
      const deleteBtn = _makeBubbleDeleteBtn(bubble, sectionAnchor, sectionIdx);
      const schemaBtn = gutter.querySelector(':scope > .bubble-schema-btn');
      gutter.insertBefore(deleteBtn, schemaBtn || null);
      _renderActionIcons(gutter);
    });
  }
}

// Expose on app immediately for session-load.js / virtual scroll
app._addBubbleActions = _addBubbleActions;
app._finalizeBubbleSections = _finalizeBubbleSections;
app._ensureLLMSections = _ensureLLMSections;
app._wireLLMSection = _wireLLMSection;
app._showLastSectionGutter = _showLastSectionGutter;
app._injectDeletedActions = _injectDeletedActions;

export {
  _addBubbleActions,
  _finalizeBubbleSections,
  _ensureLLMSections,
  _wireLLMSection,
  _bubbleAnchorId,
  _getBubbleText,
  _setActionIcon,
  _setBubbleModel,
  _setBubbleModelFromMeta,
  _modelLabelFromMeta,
};
