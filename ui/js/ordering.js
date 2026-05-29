'use strict';

/**
 * ordering.js — shared row-ordering helpers for the chat-header dropdowns.
 *
 * Two concerns live here so the agent dropdown (sessions.js) and the Agents
 * page (agents.js) stay in sync:
 *
 *   1. Agent pin state + display sort. Pins are a per-user localStorage flag
 *      (kept here, formerly in sessions.js). `sortAgentsForDisplay` floats
 *      pinned agents to the top while otherwise preserving the order the
 *      server already returned (which is the synced `sort_order` column).
 *      Both the dropdown and the Agents page call it, so they match exactly.
 *
 *   2. Drag-to-reorder. `makeRowsReorderable` wires a pointer-based (mouse +
 *      touch) drag from a grip handle on each row, then hands the resulting
 *      top-to-bottom id list back to the caller, which persists it to the DB.
 *
 * The persisted order is account-level (DB), so it follows the user across
 * devices: agents via POST /api/v1/agents/reorder, sessions via
 * POST /api/v1/db/sessions/reorder.
 */

import { apiPath } from './config.js';

// ── Agent pin state (per-user, localStorage) ──────────────────────────────────

function _pinnedAgentsKey(userId) {
  return `pinnedAgents:${userId || 'anon'}`;
}

export function getPinnedAgents(userId) {
  try {
    const raw = localStorage.getItem(_pinnedAgentsKey(userId));
    return raw ? new Set(JSON.parse(raw)) : new Set();
  } catch (_) { return new Set(); }
}

export function setPinnedAgents(userId, set) {
  try {
    localStorage.setItem(_pinnedAgentsKey(userId), JSON.stringify(Array.from(set)));
  } catch (_) {}
}

/** Toggle an agent's pin and return the updated Set. */
export function toggleAgentPin(userId, agentId) {
  const pinned = getPinnedAgents(userId);
  if (pinned.has(agentId)) pinned.delete(agentId);
  else pinned.add(agentId);
  setPinnedAgents(userId, pinned);
  return pinned;
}

/**
 * Return a new array with pinned agents first, otherwise preserving the input
 * order (which is already the server's sort_order order). Stable: equal-pin
 * rows keep their incoming relative order.
 */
export function sortAgentsForDisplay(agents, userId) {
  const pinned = getPinnedAgents(userId);
  return agents
    .map((a, i) => ({ a, i }))
    .sort((x, y) => {
      const px = pinned.has(x.a.id) ? 0 : 1;
      const py = pinned.has(y.a.id) ? 0 : 1;
      if (px !== py) return px - py;
      return x.i - y.i;
    })
    .map((o) => o.a);
}

// ── Persistence ───────────────────────────────────────────────────────────────

export async function persistAgentOrder(userId, orderedIds) {
  if (!userId || !Array.isArray(orderedIds) || !orderedIds.length) return;
  try {
    await fetch(apiPath('/api/v1/agents/reorder'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId, order: orderedIds }),
    });
  } catch (e) {
    console.warn('Failed to persist agent order:', e);
  }
}

export async function persistSessionOrder(userId, orderedIds) {
  if (!userId || !Array.isArray(orderedIds) || !orderedIds.length) return;
  try {
    await fetch(apiPath('/api/v1/db/sessions/reorder?db=local.db'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId, order: orderedIds }),
    });
  } catch (e) {
    console.warn('Failed to persist session order:', e);
  }
}

// ── Pointer-based drag-to-reorder ──────────────────────────────────────────────
//
// Works for mouse and touch. Drag is initiated only from a grip handle inside a
// row (so row-body clicks still select and the kebab still opens). The handle
// must carry `touch-action: none` in CSS so the browser doesn't treat the touch
// as a scroll. Delegated from the container, so it survives innerHTML rebuilds.

const _DRAG_THRESHOLD = 4;   // px the pointer must travel before a drag "arms"
const _EDGE = 28;            // px from a scroll edge that triggers auto-scroll

/**
 * @param {HTMLElement} container  The scrollable menu element (rows live inside).
 * @param {Object} opts
 * @param {string} opts.rowSelector     CSS selector matching each reorderable row.
 * @param {string} opts.handleSelector  CSS selector for the drag grip inside a row.
 * @param {(orderedIds: string[]) => void} opts.onReorder  Called on drop with the
 *        new top-to-bottom list of row `data-id` values.
 */
export function makeRowsReorderable(container, opts) {
  if (!container || container.__reorderBound) return;
  container.__reorderBound = true;

  const { rowSelector, handleSelector, onReorder } = opts;
  let dragRow = null;     // the row being dragged
  let armed = false;      // true once movement passes the threshold
  let startY = 0;
  let scrollRAF = null;

  function rowIdList() {
    return Array.from(container.querySelectorAll(rowSelector))
      .map((r) => r.dataset.id)
      .filter(Boolean);
  }

  function autoScroll(clientY) {
    const rect = container.getBoundingClientRect();
    let dy = 0;
    if (clientY < rect.top + _EDGE) dy = -8;
    else if (clientY > rect.bottom - _EDGE) dy = 8;
    if (dy && container.scrollHeight > container.clientHeight) {
      container.scrollTop += dy;
    }
  }

  // Insert the dragged row at the position matching the pointer's Y.
  function reposition(clientY) {
    const rows = Array.from(container.querySelectorAll(rowSelector))
      .filter((r) => r !== dragRow);
    let inserted = false;
    for (const r of rows) {
      const rect = r.getBoundingClientRect();
      if (clientY < rect.top + rect.height / 2) {
        container.insertBefore(dragRow, r);
        inserted = true;
        break;
      }
    }
    if (!inserted) container.appendChild(dragRow);
  }

  function onMove(e) {
    if (!dragRow) return;
    const clientY = e.clientY;
    if (!armed) {
      if (Math.abs(clientY - startY) < _DRAG_THRESHOLD) return;
      armed = true;
      dragRow.classList.add('dragging');
    }
    e.preventDefault();          // stop touch-scrolling / text selection
    reposition(clientY);
    autoScroll(clientY);
  }

  function suppressNextClick() {
    // Block the click the browser synthesises after the pointer sequence so a
    // handle tap / drop doesn't also select the row. Self-removes.
    const handler = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      container.removeEventListener('click', handler, true);
    };
    container.addEventListener('click', handler, true);
    setTimeout(() => container.removeEventListener('click', handler, true), 350);
  }

  function endDrag() {
    document.removeEventListener('pointermove', onMove);
    document.removeEventListener('pointerup', onEnd);
    document.removeEventListener('pointercancel', onEnd);
    if (scrollRAF) cancelAnimationFrame(scrollRAF);
    if (dragRow) dragRow.classList.remove('dragging');
  }

  function onEnd() {
    const wasArmed = armed;
    const row = dragRow;
    endDrag();
    dragRow = null;
    armed = false;
    // Always suppress the trailing click (tap on handle, or drop on a row).
    suppressNextClick();
    if (wasArmed && row && typeof onReorder === 'function') {
      onReorder(rowIdList());
    }
  }

  container.addEventListener('pointerdown', (e) => {
    const handle = e.target.closest(handleSelector);
    if (!handle || !container.contains(handle)) return;
    const row = handle.closest(rowSelector);
    if (!row) return;
    e.preventDefault();
    e.stopPropagation();
    dragRow = row;
    armed = false;
    startY = e.clientY;
    document.addEventListener('pointermove', onMove, { passive: false });
    document.addEventListener('pointerup', onEnd);
    document.addEventListener('pointercancel', onEnd);
  });
}
