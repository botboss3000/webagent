'use strict';

import { icon } from './icons.js';

/*
 * ============================================================================
 * SHARED-DELETE-CONTROL
 * ============================================================================
 * The two-click "trash → hazard → spin → gone" delete affordance. This is the
 * SINGLE source of truth for that interaction and is shared by TWO call sites
 * that must stay in sync. Grep `SHARED-DELETE-CONTROL` to find them:
 *
 *   1) Chat header session menu  →  ui/chat-side-panel/js/session-core.js + session-init.js
 *        - the trash button on every session row in the dropdown
 *        - the trash button in the chat header itself (#session-delete-header)
 *   2) Agents page agent card    →  ui/main-panel/agents/js/view.js
 *        - the trash button on each custom agent's card
 *
 * If you change the states, icons, timing, or behaviour, do it HERE so both
 * places pick it up. Do NOT re-implement the state machine inline in either
 * call site — call advanceDeleteBtn()/resetDeleteBtn() instead.
 *
 * Interaction (driven by the button's data-state attribute):
 *   "trash"   --click-->  "arming"   : spinner for 1s (clicks ignored)
 *   "arming"  --auto--->  "warning"  : swap to hazard icon, mark .warning
 *   "warning" --click-->  "deleting" : swap to spinner, run onConfirm()
 *   any reset (menu open/close, a sibling arming, a timeout, a failed delete)
 *             ------------ "trash"   : back to the trash icon
 *
 * Colour for the .warning and busy states lives in CSS next to each call site
 * (also tagged SHARED-DELETE-CONTROL). The spinner reuses the global
 * `.session-status-running` spin so it animates everywhere with no extra CSS.
 * ============================================================================
 */

const TRASH = 'trash';
const WARN  = 'warning';
const BUSY  = 'deleting';
const ARMING = 'arming';

const ARM_DELAY_MS = 400; // spinner duration before arming

/**
 * Return a button to its resting trash state. Safe to call on any state.
 */
export function resetDeleteBtn(btn, { size = '14px', title = 'Delete' } = {}) {
  if (!btn) return;
  clearTimeout(btn._armTimer);
  btn.dataset.state = TRASH;
  btn.classList.remove('warning', 'deleting');
  btn.title = title;
  btn.innerHTML = icon('trash-2', { size });
}

/**
 * Advance a delete button by one click. Returns the NEW state string:
 *   'arming'   — first click: spinner shown for 1s, clicks ignored
 *   'warning'  — after 1s: armed, showing the hazard icon
 *   'deleting' — second click: confirmed; the spinner is shown and onConfirm()
 *                has been scheduled (microtask, so the spinner paints first)
 *   'trash'    — no-op (the button was already mid-delete)
 *
 * Options:
 *   size      icon size for trash/hazard      (default '14px')
 *   spinSize  icon size for the spinner       (default matches `size`)
 *   armTitle  tooltip while armed
 *   busyTitle tooltip while deleting
 *   onArm(btn)     fired after the 1s arming delay, when the button shows the hazard
 *                  icon — use it to reset siblings / set an auto-revert timeout
 *   onConfirm(btn) fired (async) when the delete is confirmed
 */
export function advanceDeleteBtn(btn, {
  size = '14px',
  spinSize = null,
  armTitle = 'Click again to delete permanently',
  busyTitle = 'Deleting\u2026',
  onArm = null,
  onConfirm = null,
} = {}) {
  if (!btn) return TRASH;
  const spin = spinSize || size;
  const state = btn.dataset.state || TRASH;

  if (state === TRASH) {
    // --- ARMING phase: show spinner for ARM_DELAY_MS, clicks ignored ---
    btn.dataset.state = ARMING;
    btn.classList.remove('warning', 'deleting');
    btn.title = 'Arming\u2026';
    btn.innerHTML =
      '<span class="session-status-running" style="display:inline-flex;width:' +
      spin + ';height:' + spin + ';">' +
      icon('loader-2', { size: spin }) + '</span>';
    clearTimeout(btn._armTimer);
    btn._armTimer = setTimeout(() => {
      if (btn.dataset.state !== ARMING) return; // was reset in the meantime
      btn.dataset.state = WARN;
      btn.classList.add('warning');
      btn.classList.remove('deleting');
      btn.title = armTitle;
      btn.innerHTML = icon('alert-triangle', { size });
      if (onArm) onArm(btn);
    }, ARM_DELAY_MS);
    return ARMING;
  }

  if (state === ARMING) {
    return ARMING; // still arming — ignore clicks
  }

  if (state === WARN) {
    btn.dataset.state = BUSY;
    btn.classList.remove('warning');
    btn.classList.add('deleting');
    btn.title = busyTitle;
    // The global .session-status-running class spins its child icon.
    btn.innerHTML =
      '<span class="session-status-running" style="display:inline-flex;width:' +
      spin + ';height:' + spin + ';">' +
      icon('loader-2', { size: spin }) + '</span>';
    if (onConfirm) Promise.resolve().then(() => onConfirm(btn));
    return BUSY;
  }

  return TRASH; // already deleting — ignore extra clicks
}
