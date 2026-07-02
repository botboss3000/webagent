'use strict';

/**
 * Danger Zone — App Configuration → Data Settings → Danger Zone card (bottom).
 *
 * Two destructive, admin-only actions on the running install:
 *   1. RESET DATA — tick any of five groups (database · vault · attachments ·
 *      genui · logs) and wipe them. The app can't erase its own open DB/vault
 *      in-process, so this writes a one-shot marker + self-restarts; the fresh
 *      boot clears the picked groups before anything opens them (server side:
 *      POST /admin/storage/reset → app/util/reset_boot.py). A backup lands under
 *      temp/. After the reboot, GET /admin/storage/reset/info reports the outcome,
 *      shown in the #ac-dz-last banner.
 *   2. DELETE INSTALLATION — self-destruct. Deletes the whole app folder from
 *      disk (stops + destroys this server). Armed only when the admin types the
 *      install folder's name (POST /admin/storage/delete-install).
 *
 * Sibling of deploy.js / app-access.js / social-auth.js; init'd by
 * data-settings.js. Markup: the #ac-danger-card block in data-settings.html.
 * Uses the shared hazard-confirm dialog + the auth-aware _fetch from utils.js.
 */

import { apiPath } from '../../../shared/js/config.js';
import { app } from '../../../shared/js/state.js';
import { isAdmin } from '../../../shared/js/left-login.js';
import { hazardConfirm } from '../../../shared/js/confirm-dialog.js';
import { _refreshLucideIcons } from '../../../shared/js/dom-utils.js';
import { _fetch, _qs, _esc, _fmtDate } from '../utils.js';

function uid() { return app.currentUserId || localStorage.getItem('auth_user_id') || ''; }

let _folderName = '';   // install folder name — the phrase that arms the delete
let _autoRestart = true;

// ── Info fetch (folder name · provider · auto-restart · last-reset outcome) ──
async function _loadInfo() {
  const u = uid();
  if (!u) return;
  let data;
  try {
    const res = await _fetch(apiPath(`/admin/storage/reset/info?requesting_user_id=${encodeURIComponent(u)}`));
    if (!res.ok) return;
    data = await res.json();
  } catch { return; }
  if (!data || !data.ok) return;

  _folderName = data.folder_name || '';
  _autoRestart = data.auto_restart !== false;

  const phrase = _qs('ac-dz-nuke-phrase');
  if (phrase && _folderName) phrase.placeholder = `type "${_folderName}" to confirm`;

  // Host can't self-restart → the reset can't run; say so + disable the button.
  const norestart = _qs('ac-dz-norestart');
  if (norestart) {
    if (_autoRestart) {
      norestart.style.display = 'none';
    } else {
      norestart.textContent = 'This host can’t restart the server automatically, so a data reset can’t run from here. '
        + (data.restart_reason || '') + ' Start the server via run.py or the supervisor, then retry.';
      norestart.style.display = 'block';
    }
  }

  _renderLast(data.last);
  _syncResetBtn();
}

// Render the outcome of the reset that last ran (after the reboot that did it).
function _renderLast(last) {
  const box = _qs('ac-dz-last');
  if (!box) return;
  if (!last || !Array.isArray(last.groups) || !last.groups.length) {
    box.style.display = 'none';
    return;
  }
  const parts = [];
  parts.push(`<strong>Last reset (${_esc(_fmtDate(last.at) || last.at)}):</strong> cleared ${last.groups.map(_esc).join(', ')}.`);
  if (last.postgres) {
    parts.push(last.postgres.ok
      ? ` Postgres ${_esc(last.postgres.detail || 'wiped')}.`
      : ` <span style="color:var(--danger);">Postgres wipe failed: ${_esc(last.postgres.detail || '')}.</span>`);
  }
  if (Array.isArray(last.failed) && last.failed.length) {
    parts.push(` <span style="color:var(--danger);">${last.failed.length} item(s) could not be removed.</span>`);
  }
  if (last.backup) parts.push(`<br><span style="color:var(--fg-3);">Backup: ${_esc(last.backup)}</span>`);
  if (last.note) parts.push(`<br><span style="color:var(--fg-3);">${_esc(last.note)}</span>`);
  parts.push(' <a href="#" id="ac-dz-last-dismiss" style="color:var(--fg-3);">Dismiss</a>');
  box.innerHTML = parts.join('');
  box.style.display = 'block';

  const dismiss = _qs('ac-dz-last-dismiss');
  if (dismiss) dismiss.addEventListener('click', async (e) => {
    e.preventDefault();
    box.style.display = 'none';
    try {
      await _fetch(apiPath('/admin/storage/reset/dismiss'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ requesting_user_id: uid() }),
      });
    } catch { /* cosmetic — the banner is already hidden */ }
  });
}

// ── Reset targets ─────────────────────────────────────────────────────────
function _checks() { return Array.from(document.querySelectorAll('#ac-dz-list .ac-dz-check')); }
function _selected() { return _checks().filter(c => c.checked).map(c => c.value); }

function _syncResetBtn() {
  const btn = _qs('ac-dz-reset-btn');
  if (!btn) return;
  btn.disabled = _selected().length === 0 || !_autoRestart;
}

const _LABELS = {
  db: 'the app database (chat, agents, users, memories)',
  secrets: 'the secrets vault (API keys, tokens)',
  attachments: 'chat attachments',
  genui: 'Gen UI pages',
  logs: 'logs & diagnostics',
};

async function _doReset() {
  const groups = _selected();
  if (!groups.length) return;
  const list = groups.map(g => '• ' + (_LABELS[g] || g)).join('\n');
  const ok = await hazardConfirm({
    tone: 'danger',
    title: 'Reset the selected data?',
    message: 'The server will restart and permanently clear:\n\n' + list
      + '\n\nA backup is kept under temp/ (except a shared Postgres database, which is dropped and cannot be undone). '
      + 'The app rebuilds a fresh database with the default admin on the way back up.',
    confirmLabel: 'Reset & restart',
  });
  if (!ok) return;

  const status = _qs('ac-dz-reset-status');
  const btn = _qs('ac-dz-reset-btn');
  if (btn) btn.disabled = true;
  if (status) { status.style.color = 'var(--fg-3)'; status.textContent = 'Scheduling reset & restarting…'; }

  try {
    const res = await _fetch(apiPath('/admin/storage/reset'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid(), groups }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      if (status) { status.style.color = 'var(--danger)'; status.textContent = data.error || 'Reset failed.'; }
      if (btn) btn.disabled = false;
      return;
    }
    // The server is now restarting; it will wipe the data on the way up. Wait for
    // it to come back, then reload so the admin lands on the fresh instance.
    if (status) { status.style.color = 'var(--success)'; status.textContent = 'Resetting… the app will reload when the server is back.'; }
    _waitForServerThenReload();
  } catch (e) {
    if (status) { status.style.color = 'var(--danger)'; status.textContent = 'Reset request failed: ' + (e?.message || e); }
    if (btn) btn.disabled = false;
  }
}

// Poll /health until the restarted server answers, then hard-reload.
function _waitForServerThenReload() {
  let tries = 0;
  const tick = async () => {
    tries += 1;
    try {
      const res = await fetch(apiPath('/health'), { cache: 'no-store' });
      if (res.ok) { location.reload(); return; }
    } catch { /* still down */ }
    if (tries < 60) setTimeout(tick, 1500);
    else {
      const status = _qs('ac-dz-reset-status');
      if (status) { status.style.color = 'var(--warning)'; status.textContent = 'Reset sent, but the server hasn’t come back yet — reload the page in a moment.'; }
    }
  };
  setTimeout(tick, 3000);
}

// ── Delete installation (self-destruct) ─────────────────────────────────────
function _syncNukeBtn() {
  const btn = _qs('ac-dz-nuke-btn');
  const phrase = _qs('ac-dz-nuke-phrase');
  if (!btn || !phrase) return;
  btn.disabled = !_folderName || phrase.value.trim() !== _folderName;
}

async function _doNuke() {
  const phrase = _qs('ac-dz-nuke-phrase');
  if (!phrase || phrase.value.trim() !== _folderName) return;
  const ok = await hazardConfirm({
    tone: 'danger',
    title: 'Delete the entire installation?',
    message: `This permanently deletes the "${_folderName}" folder and everything in it, then shuts the server down. `
      + 'The app does NOT come back — there is no backup and no undo. Only proceed if you mean to remove this install completely.',
    confirmLabel: 'Delete everything',
  });
  if (!ok) return;

  const status = _qs('ac-dz-nuke-status');
  const btn = _qs('ac-dz-nuke-btn');
  if (btn) btn.disabled = true;
  if (status) { status.style.color = 'var(--fg-3)'; status.textContent = 'Deleting installation…'; }

  try {
    const res = await _fetch(apiPath('/admin/storage/delete-install'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid(), confirm_phrase: phrase.value.trim() }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      if (status) { status.style.color = 'var(--danger)'; status.textContent = data.error || 'Delete failed.'; }
      if (btn) btn.disabled = false;
      return;
    }
    if (status) { status.style.color = 'var(--danger)'; status.textContent = 'Installation is being deleted and the server is shutting down. This app will stop responding.'; }
  } catch (e) {
    // A dropped connection here can simply mean the server already exited mid-delete.
    if (status) { status.style.color = 'var(--warning)'; status.textContent = 'Delete triggered — the server may have already shut down.'; }
  }
}

// ── Init ────────────────────────────────────────────────────────────────────
export function initDangerZone() {
  const card = _qs('ac-danger-card');
  if (!card || card.dataset.dzWired) return;
  card.dataset.dzWired = '1';

  // Reset target checkboxes → enable/disable the reset button.
  _checks().forEach(c => c.addEventListener('change', _syncResetBtn));
  _qs('ac-dz-reset-btn')?.addEventListener('click', () => {
    if (!isAdmin()) return;
    _doReset();
  });

  // Delete-install: arm the button only on an exact folder-name match.
  const phrase = _qs('ac-dz-nuke-phrase');
  phrase?.addEventListener('input', _syncNukeBtn);
  _qs('ac-dz-nuke-btn')?.addEventListener('click', () => {
    if (!isAdmin()) return;
    _doNuke();
  });

  _refreshLucideIcons(card);
  _loadInfo();
  // Re-fetch on each section-show so the last-reset banner appears after a reboot.
  window.__refreshDangerZone = _loadInfo;
}
