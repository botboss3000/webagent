'use strict';

/**
 * GitHub tab for webAgent — full-featured git repo management UI.
 * Provides: status view, file changes, commit, push/pull, token config, commit log.
 */

import { apiPath } from './config.js';

// ── DOM refs ──
let GH = {};

function qs(id) {
  return document.getElementById(id);
}

function bindGHDom() {
  GH = {
    // gh-status-area does not exist in markup; fall back to gh-body as error container
    statusArea: qs('gh-status-area') || qs('gh-body'),
    branch: qs('gh-branch'),
    remoteUrl: qs('gh-remote-url'),
    lastCommit: qs('gh-last-commit'),
    aheadBehind: qs('gh-ahead-behind'),
    fileCount: qs('gh-file-count'),
    fileList: qs('gh-file-list'),
    commitMsg: qs('gh-commit-msg'),
    commitBtn: qs('gh-commit-btn'),
    commitSpinner: qs('gh-commit-spinner'),
    commitResult: qs('gh-commit-result'),
    pushBtn: qs('gh-push-btn'),
    pushSpinner: qs('gh-push-spinner'),
    pushResult: qs('gh-push-result'),
    pullBtn: qs('gh-pull-btn'),
    pullSpinner: qs('gh-pull-spinner'),
    pullResult: qs('gh-pull-result'),
    pullRestartBtn: qs('gh-pull-restart-btn'),
    pullRestartSpinner: qs('gh-pull-restart-spinner'),
    pullRestartResult: qs('gh-pull-restart-result'),
    logList: qs('gh-log-list'),
    refreshBtn: qs('gh-refresh-btn'),
    refreshSpinner: qs('gh-refresh-spinner'),
    tokenInput: qs('gh-token-input'),
    tokenSaveBtn: qs('gh-token-save-btn'),
    tokenStatus: qs('gh-token-status'),
    tokenConfigured: qs('gh-token-configured'),
    pushPullGroup: qs('gh-push-pull-group'),
    tokenSection: qs('gh-token-section'),
    commitSection: qs('gh-commit-section'),
  };
}

// ── Helpers ──

/* Include auth token so check-access knows who you are. */
function _ghFetch(url, opts = {}) {
  const token = localStorage.getItem('auth_token');
  if (token) {
    opts.headers = { ...(opts.headers || {}), Authorization: `Bearer ${token}` };
  }
  return fetch(url, opts);
}

function showResult(el, msg, type) {
  el.textContent = msg;
  el.style.color = type === 'error' ? '#f7768e' : type === 'success' ? '#9ece6a' : '#565f89';
  el.style.display = 'block';
}

function clearResult(el) {
  el.textContent = '';
  el.style.display = 'none';
}

function flagLabel(flag) {
  const map = {
    M: 'Modified',
    A: 'Added',
    D: 'Deleted',
    R: 'Renamed',
    C: 'Copied',
    U: 'Updated',
    '??': 'Untracked',
  };
  return map[flag] || flag;
}

function flagClass(flag) {
  const map = {
    M: 'gh-flag-m',
    A: 'gh-flag-a',
    D: 'gh-flag-d',
    R: 'gh-flag-r',
    '??': 'gh-flag-u',
  };
  return map[flag] || 'gh-flag-m';
}

function escapeHtml(str) {
  const d = document.createElement('div');
  d.textContent = str;
  return d.innerHTML;
}

// ── Core API ──

async function fetchStatus() {
  const res = await _ghFetch(apiPath('/api/v1/github/status'));
  if (!res.ok) {
    const body = await res.text();
    throw new Error(body || `HTTP ${res.status}`);
  }
  return res.json();
}

async function createCommit(message) {
  const res = await _ghFetch(apiPath('/api/v1/github/commit'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(body || `HTTP ${res.status}`);
  }
  return res.json();
}

async function pushRemote() {
  const res = await _ghFetch(apiPath('/api/v1/github/push'), { method: 'POST' });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(body || `HTTP ${res.status}`);
  }
  return res.json();
}

async function pullRemote() {
  const res = await _ghFetch(apiPath('/api/v1/github/pull'), { method: 'POST' });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(body || `HTTP ${res.status}`);
  }
  return res.json();
}

async function saveToken(token) {
  const res = await _ghFetch(apiPath('/api/v1/github/token'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(body || `HTTP ${res.status}`);
  }
  return res.json();
}

async function fetchTokenStatus() {
  const res = await _ghFetch(apiPath('/api/v1/github/token-status'));
  if (!res.ok) return { configured: false, masked: '' };
  return res.json();
}

// ── Render functions ──

function renderStatus(data) {
  // Branch
  GH.branch.textContent = data.branch || '—';

  // Remote URL
  GH.remoteUrl.textContent = data.remote_url || 'No remote configured';

  // Last commit
  if (data.last_commit && data.last_commit.hash) {
    const lc = data.last_commit;
    GH.lastCommit.innerHTML = `
      <strong>${escapeHtml(lc.message)}</strong>
      <span class="gh-meta">${escapeHtml(lc.author)} — ${escapeHtml(lc.date_relative)}</span>
      <code class="gh-hash">${lc.hash.slice(0, 7)}</code>
    `;
  } else {
    GH.lastCommit.innerHTML = '<span class="gh-meta">No commits yet</span>';
  }

  // Ahead / Behind
  if (data.has_remote && (data.ahead > 0 || data.behind > 0)) {
    GH.aheadBehind.innerHTML = `
      <span class="gh-ahead">↑ ${data.ahead} ahead</span>
      <span class="gh-behind">↓ ${data.behind} behind</span>
    `;
  } else if (data.has_remote) {
    GH.aheadBehind.innerHTML = '<span class="gh-meta">Up to date</span>';
  } else {
    GH.aheadBehind.innerHTML = '<span class="gh-meta">No remote</span>';
  }

  // File count
  GH.fileCount.textContent = data.file_count > 0 ? `${data.file_count} file(s) changed` : 'Clean working tree';

  // File list
  renderFileList(data);

  // Commit log
  renderLog(data.commits || []);

  // Push/Pull group visibility
  GH.pushPullGroup.style.display = data.has_remote ? 'flex' : 'none';

  // Commit section visibility
  GH.commitSection.style.display = data.file_count > 0 ? 'block' : 'none';
}

function renderFileList(data) {
  const items = [
    ...data.staged.map(f => ({ ...f, area: 'staged' })),
    ...data.unstaged.map(f => ({ ...f, area: 'unstaged' })),
    ...data.untracked.map(f => ({ ...f, flag: '??', area: 'untracked' })),
  ];

  if (items.length === 0) {
    GH.fileList.innerHTML = '<div class="gh-clean-msg">✓ No uncommitted changes</div>';
    return;
  }

  GH.fileList.innerHTML = items.map(f => {
    const areaLabel = f.area === 'staged' ? 'Staged' : f.area === 'unstaged' ? 'Unstaged' : 'Untracked';
    return `
      <div class="gh-file-item">
        <span class="gh-file-flag ${flagClass(f.flag)}">${escapeHtml(f.flag)}</span>
        <span class="gh-file-area">${areaLabel}</span>
        <span class="gh-file-path">${escapeHtml(f.path)}</span>
      </div>
    `;
  }).join('');
}

function renderLog(commits) {
  if (commits.length === 0) {
    GH.logList.innerHTML = '<div class="gh-meta">No commits</div>';
    return;
  }

  GH.logList.innerHTML = commits.map(c => {
    const msg = escapeHtml(c.message || '');
    const refs = (c.refs || '').trim();
    const author = escapeHtml(c.author || '');
    const dateRel = escapeHtml(c.date_relative || '');
    const dateIso = escapeHtml(c.date_iso || '');
    const stateClass = c.is_head ? 'gh-log-head' : (c.is_pulled ? 'gh-log-pulled' : 'gh-log-unpulled');
    const stateBadge = c.is_head
      ? '<span class="gh-log-badge gh-log-badge-head" title="Currently checked out on this VM">HEAD</span>'
      : (c.is_pulled
          ? '<span class="gh-log-badge gh-log-badge-pulled" title="Already pulled to this VM">✓ pulled</span>'
          : '<span class="gh-log-badge gh-log-badge-unpulled" title="On origin but not yet pulled to this VM">↓ not pulled</span>');

    return `
      <div class="gh-log-item ${stateClass}" data-hash="${escapeHtml(c.full_hash || c.hash)}" data-short="${escapeHtml(c.hash)}" tabindex="0" role="button">
        <div class="gh-log-row">
          <code class="gh-hash">${escapeHtml(c.hash)}</code>
          ${stateBadge}
          ${refs ? `<span class="gh-refs">${escapeHtml(refs)}</span>` : ''}
          <span class="gh-log-msg">${msg}</span>
        </div>
        <div class="gh-log-meta">
          <span class="gh-log-author">${author}</span>
          <span class="gh-log-date" title="${dateIso}">${dateRel}</span>
        </div>
        <div class="gh-log-detail" style="display:none;"></div>
      </div>
    `;
  }).join('');

  // Wire row click to toggle detail panel
  GH.logList.querySelectorAll('.gh-log-item').forEach(row => {
    row.addEventListener('click', () => toggleCommitDetail(row));
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        toggleCommitDetail(row);
      }
    });
  });
}

async function toggleCommitDetail(row) {
  const panel = row.querySelector('.gh-log-detail');
  if (!panel) return;
  const isOpen = panel.style.display !== 'none';
  if (isOpen) {
    panel.style.display = 'none';
    row.classList.remove('gh-log-item-open');
    return;
  }
  row.classList.add('gh-log-item-open');
  panel.style.display = 'block';
  // Only fetch once per row
  if (panel.dataset.loaded === '1') return;
  panel.innerHTML = '<div class="gh-meta">Loading commit details...</div>';
  const hash = row.dataset.hash || row.dataset.short;
  try {
    const res = await _ghFetch(apiPath(`/api/v1/github/commit/${encodeURIComponent(hash)}`));
    if (!res.ok) {
      const txt = await res.text();
      throw new Error(txt || `HTTP ${res.status}`);
    }
    const d = await res.json();
    panel.innerHTML = renderCommitDetail(d);
    panel.dataset.loaded = '1';
  } catch (e) {
    panel.innerHTML = `<div class="gh-meta" style="color:#f7768e;">Error: ${escapeHtml(e.message)}</div>`;
  }
}

function renderCommitDetail(d) {
  const filesHtml = (d.files || []).map(f => {
    const added = f.binary ? '—' : `+${f.added}`;
    const removed = f.binary ? '—' : `-${f.removed}`;
    return `
      <div class="gh-cd-file">
        <span class="gh-cd-added">${added}</span>
        <span class="gh-cd-removed">${removed}</span>
        <span class="gh-cd-path">${escapeHtml(f.path)}</span>
      </div>
    `;
  }).join('') || '<div class="gh-meta">No file changes</div>';

  const bodyHtml = (d.body || '').trim()
    ? `<pre class="gh-cd-body">${escapeHtml(d.body.trim())}</pre>`
    : '';

  return `
    <div class="gh-cd">
      <div class="gh-cd-meta">
        <div><span class="gh-cd-label">Full hash</span><code>${escapeHtml(d.full_hash)}</code></div>
        <div><span class="gh-cd-label">Author</span>${escapeHtml(d.author)} &lt;${escapeHtml(d.author_email)}&gt;</div>
        <div><span class="gh-cd-label">Authored</span>${escapeHtml(d.author_date)}</div>
        <div><span class="gh-cd-label">Committer</span>${escapeHtml(d.committer)} &lt;${escapeHtml(d.committer_email)}&gt;</div>
        <div><span class="gh-cd-label">Committed</span>${escapeHtml(d.commit_date)}</div>
        <div><span class="gh-cd-label">Parents</span><code>${(d.parents || []).map(p => escapeHtml(p.slice(0,7))).join(', ') || '(none)'}</code></div>
      </div>
      <div class="gh-cd-subject">${escapeHtml(d.subject)}</div>
      ${bodyHtml}
      <div class="gh-cd-files-title">Files (${(d.files || []).length})</div>
      <div class="gh-cd-files">${filesHtml}</div>
    </div>
  `;
}

// ── Actions ──

async function doRefresh() {
  clearResult(GH.commitResult);
  clearResult(GH.pushResult);
  clearResult(GH.pullResult);
  if (GH.refreshSpinner) GH.refreshSpinner.style.display = 'inline-block';
  if (GH.refreshBtn) GH.refreshBtn.disabled = true;
  try {
    const data = await fetchStatus();
    renderStatus(data);
  } catch (e) {
    if (GH.statusArea) {
      GH.statusArea.innerHTML = `
        <div class="gh-error-box">
          <strong>Failed to load repo status</strong><br>
          ${escapeHtml(e.message)}
        </div>
      `;
    }
  } finally {
    if (GH.refreshSpinner) GH.refreshSpinner.style.display = 'none';
    if (GH.refreshBtn) GH.refreshBtn.disabled = false;
  }
}

async function doCommit() {
  const msg = GH.commitMsg.value.trim();
  if (!msg) {
    showResult(GH.commitResult, 'Enter a commit message', 'error');
    return;
  }
  GH.commitBtn.disabled = true;
  GH.commitSpinner.style.display = 'inline-block';
  clearResult(GH.commitResult);
  try {
    const data = await createCommit(msg);
    showResult(GH.commitResult, data.message || 'Committed!', data.status === 'committed' ? 'success' : 'info');
    if (data.status === 'committed') {
      GH.commitMsg.value = '';
      await doRefresh();
    }
  } catch (e) {
    showResult(GH.commitResult, `Error: ${e.message}`, 'error');
  } finally {
    GH.commitBtn.disabled = false;
    GH.commitSpinner.style.display = 'none';
  }
}

async function doPush() {
  GH.pushBtn.disabled = true;
  GH.pushSpinner.style.display = 'inline-block';
  clearResult(GH.pushResult);
  try {
    const data = await pushRemote();
    showResult(GH.pushResult, data.message || 'Push successful!', 'success');
    await doRefresh();
  } catch (e) {
    showResult(GH.pushResult, `Push failed: ${e.message}`, 'error');
  } finally {
    GH.pushBtn.disabled = false;
    GH.pushSpinner.style.display = 'none';
  }
}

async function doPull() {
  GH.pullBtn.disabled = true;
  GH.pullSpinner.style.display = 'inline-block';
  clearResult(GH.pullResult);
  try {
    const data = await pullRemote();
    showResult(GH.pullResult, data.message || 'Pull successful!', 'success');
    await doRefresh();
  } catch (e) {
    showResult(GH.pullResult, `Pull failed: ${e.message}`, 'error');
  } finally {
    GH.pullBtn.disabled = false;
    GH.pullSpinner.style.display = 'none';
  }
}

async function doPullAndRestart() {
  if (!confirm('Pull latest from remote then restart the server?\n\nWebSocket and in-flight requests will be dropped. The server should come back within a few seconds (requires systemd or another supervisor with Restart=always).')) {
    return;
  }
  if (!GH.pullRestartBtn) return;
  GH.pullRestartBtn.disabled = true;
  if (GH.pullRestartSpinner) GH.pullRestartSpinner.style.display = 'inline-block';
  clearResult(GH.pullRestartResult);

  // Step 1: pull
  showResult(GH.pullRestartResult, 'Pulling...', 'info');
  try {
    await pullRemote();
  } catch (e) {
    showResult(GH.pullRestartResult, `Pull failed: ${e.message}`, 'error');
    GH.pullRestartBtn.disabled = false;
    if (GH.pullRestartSpinner) GH.pullRestartSpinner.style.display = 'none';
    return;
  }

  // Step 2: restart
  showResult(GH.pullRestartResult, 'Pull OK. Restarting server...', 'info');
  const token = localStorage.getItem('auth_token');
  const headers = token ? { Authorization: `Bearer ${token}` } : {};
  try {
    await fetch(apiPath('/api/v1/restart'), { method: 'POST', headers });
  } catch (_e) {
    // Server killed the connection mid-response — expected
  }

  // Step 3: poll /health until back
  showResult(GH.pullRestartResult, 'Server down. Waiting for relaunch...', 'info');
  const start = Date.now();
  const maxWait = 60000;
  while (Date.now() - start < maxWait) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      const r = await fetch(apiPath('/health'), { method: 'GET' });
      if (r.ok) {
        showResult(GH.pullRestartResult, 'Server back up. Refreshing...', 'success');
        await doRefresh();
        GH.pullRestartBtn.disabled = false;
        if (GH.pullRestartSpinner) GH.pullRestartSpinner.style.display = 'none';
        return;
      }
    } catch (_e) { /* still down */ }
  }
  showResult(GH.pullRestartResult, 'Timed out waiting for server. Check the VM supervisor (systemd).', 'error');
  GH.pullRestartBtn.disabled = false;
  if (GH.pullRestartSpinner) GH.pullRestartSpinner.style.display = 'none';
}

async function doSaveToken() {
  const token = GH.tokenInput.value.trim();
  if (!token) {
    showResult(GH.tokenStatus, 'Enter a GitHub Personal Access Token', 'error');
    return;
  }
  GH.tokenSaveBtn.disabled = true;
  try {
    await saveToken(token);
    showResult(GH.tokenStatus, 'Token saved!', 'success');
    GH.tokenInput.value = '';
    await refreshTokenStatus();
  } catch (e) {
    showResult(GH.tokenStatus, `Error: ${e.message}`, 'error');
  } finally {
    GH.tokenSaveBtn.disabled = false;
  }
}

async function refreshTokenStatus() {
  const st = await fetchTokenStatus();
  GH.tokenConfigured.textContent = st.configured ? `Token: ${st.masked}` : 'No GitHub token set';
  GH.tokenConfigured.style.color = st.configured ? '#9ece6a' : '#565f89';
}

// ── Tab lifecycle ──

let _initialized = false;

function showRestricted() {
  const overlay = document.getElementById('gh-restricted-overlay');
  const viewer = document.getElementById('gh-viewer');
  if (overlay) overlay.style.display = 'flex';
  if (viewer) viewer.style.display = 'none';
}

function showViewer() {
  const overlay = document.getElementById('gh-restricted-overlay');
  const viewer = document.getElementById('gh-viewer');
  if (overlay) overlay.style.display = 'none';
  if (viewer) viewer.style.display = 'flex';
}

export function initGithub() {
  if (_initialized) return;

  bindGHDom();
  if (!GH.refreshBtn) {
    // DOM not ready yet — let the next startGithub call retry
    return;
  }
  _initialized = true;

  // Wire events (only fires if DOM elements exist)
  if (GH.refreshBtn) GH.refreshBtn.addEventListener('click', doRefresh);
  if (GH.commitBtn) GH.commitBtn.addEventListener('click', doCommit);
  if (GH.pushBtn) GH.pushBtn.addEventListener('click', doPush);
  if (GH.pullBtn) GH.pullBtn.addEventListener('click', doPull);
  if (GH.tokenSaveBtn) GH.tokenSaveBtn.addEventListener('click', doSaveToken);
  if (GH.pullRestartBtn) GH.pullRestartBtn.addEventListener('click', doPullAndRestart);

  GH.commitMsg.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      doCommit();
    }
  });

  GH.tokenInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      doSaveToken();
    }
  });
}

export async function startGithub() {
  // Check admin access via API (server-authoritative)
  try {
    const res = await _ghFetch(apiPath('/api/v1/github/check-access'));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if (data.is_admin) {
      showViewer();
      // Ensure listeners are wired (initGithub may have bailed before DOM was ready)
      initGithub();
      doRefresh();
      refreshTokenStatus();
    } else {
      showRestricted();
    }
  } catch (e) {
    // Fallback: client-side check from localStorage
    const authUserId = localStorage.getItem('auth_user_id');
    const hasToken = !!localStorage.getItem('auth_token');
    if (authUserId === 'admin_default' && hasToken) {
      showViewer();
      initGithub();
      doRefresh();
      refreshTokenStatus();
    } else {
      showRestricted();
    }
  }
}

export function stopGithub() {
  // no-op
}
