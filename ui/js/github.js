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
    // Check for decorated refs like (HEAD -> main, origin/main)
    let msg = escapeHtml(c.message);
    let refs = '';
    const refMatch = msg.match(/^\((.+?)\)\s*/);
    if (refMatch) {
      refs = refMatch[1];
      msg = msg.slice(refMatch[0].length);
    }

    return `
      <div class="gh-log-item">
        <code class="gh-hash">${escapeHtml(c.hash)}</code>
        ${refs ? `<span class="gh-refs">${escapeHtml(refs)}</span>` : ''}
        <span class="gh-log-msg">${msg}</span>
      </div>
    `;
  }).join('');
}

// ── Actions ──

async function doRefresh() {
  console.log('[github] doRefresh start');
  clearResult(GH.commitResult);
  clearResult(GH.pushResult);
  clearResult(GH.pullResult);
  if (GH.refreshSpinner) GH.refreshSpinner.style.display = 'inline-block';
  if (GH.refreshBtn) GH.refreshBtn.disabled = true;
  try {
    const data = await fetchStatus();
    console.log('[github] fetchStatus ok', data);
    renderStatus(data);
    console.log('[github] renderStatus done');
  } catch (e) {
    console.error('[github] doRefresh error', e);
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
    console.log('[github] doRefresh end');
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
  console.log('[github] doPullAndRestart start');
  if (!confirm('Pull latest from remote then restart the server?\n\nWebSocket and in-flight requests will be dropped. The server should come back within a few seconds (requires systemd or another supervisor with Restart=always).')) {
    console.log('[github] doPullAndRestart cancelled by user');
    return;
  }
  if (!GH.pullRestartBtn) {
    console.error('[github] pullRestartBtn missing at click time');
    return;
  }
  GH.pullRestartBtn.disabled = true;
  if (GH.pullRestartSpinner) GH.pullRestartSpinner.style.display = 'inline-block';
  clearResult(GH.pullRestartResult);

  // Step 1: pull
  console.log('[github] step 1: pull');
  showResult(GH.pullRestartResult, 'Pulling...', 'info');
  try {
    const pullData = await pullRemote();
    console.log('[github] pull ok', pullData);
  } catch (e) {
    console.error('[github] pull failed', e);
    showResult(GH.pullRestartResult, `Pull failed: ${e.message}`, 'error');
    GH.pullRestartBtn.disabled = false;
    if (GH.pullRestartSpinner) GH.pullRestartSpinner.style.display = 'none';
    return;
  }

  // Step 2: restart
  console.log('[github] step 2: restart');
  showResult(GH.pullRestartResult, 'Pull OK. Restarting server...', 'info');
  const token = localStorage.getItem('auth_token');
  const headers = token ? { Authorization: `Bearer ${token}` } : {};
  try {
    const r = await fetch(apiPath('/api/v1/restart'), { method: 'POST', headers });
    console.log('[github] restart response status', r.status);
  } catch (e) {
    console.log('[github] restart fetch threw (expected — process killed):', e.message);
  }

  // Step 3: poll /health until back
  console.log('[github] step 3: polling /health');
  showResult(GH.pullRestartResult, 'Server down. Waiting for relaunch...', 'info');
  const start = Date.now();
  const maxWait = 60000;
  let attempt = 0;
  while (Date.now() - start < maxWait) {
    await new Promise(r => setTimeout(r, 1000));
    attempt++;
    try {
      const r = await fetch(apiPath('/health'), { method: 'GET' });
      console.log(`[github] /health attempt ${attempt} status`, r.status);
      if (r.ok) {
        console.log('[github] server back up');
        showResult(GH.pullRestartResult, 'Server back up. Refreshing...', 'success');
        await doRefresh();
        GH.pullRestartBtn.disabled = false;
        if (GH.pullRestartSpinner) GH.pullRestartSpinner.style.display = 'none';
        return;
      }
    } catch (e) {
      console.log(`[github] /health attempt ${attempt} threw`, e.message);
    }
  }
  console.error('[github] /health poll timed out');
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
  console.log('[github] initGithub called, _initialized=', _initialized);
  if (_initialized) return;

  bindGHDom();
  console.log('[github] bindGHDom complete. Keys:', Object.keys(GH).filter(k => !!GH[k]).join(','));
  console.log('[github] missing:', Object.keys(GH).filter(k => !GH[k]).join(',') || '(none)');
  if (!GH.refreshBtn) {
    console.warn('[github] refreshBtn missing — DOM not ready, will retry');
    return; // do NOT set _initialized — let next startGithub retry
  }
  _initialized = true;

  // Wire events (only fires if DOM elements exist)
  if (GH.refreshBtn) GH.refreshBtn.addEventListener('click', () => { console.log('[github] refresh click'); doRefresh(); });
  else console.warn('[github] refreshBtn missing');
  if (GH.commitBtn) GH.commitBtn.addEventListener('click', doCommit);
  if (GH.pushBtn) GH.pushBtn.addEventListener('click', doPush);
  if (GH.pullBtn) GH.pullBtn.addEventListener('click', doPull);
  if (GH.tokenSaveBtn) GH.tokenSaveBtn.addEventListener('click', doSaveToken);
  if (GH.pullRestartBtn) {
    GH.pullRestartBtn.addEventListener('click', () => { console.log('[github] pull-restart click'); doPullAndRestart(); });
    console.log('[github] pullRestartBtn wired');
  } else {
    console.warn('[github] pullRestartBtn missing in DOM');
  }

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
  console.log('[github] startGithub called');
  // Check admin access via API (server-authoritative)
  try {
    const res = await _ghFetch(apiPath('/api/v1/github/check-access'));
    console.log('[github] check-access status', res.status);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    console.log('[github] check-access data', data);
    if (data.is_admin) {
      showViewer();
      // Ensure listeners are wired (initGithub may not have run yet for this tab)
      initGithub();
      doRefresh();
      refreshTokenStatus();
    } else {
      console.log('[github] not admin — showing restricted');
      showRestricted();
    }
  } catch (e) {
    console.warn('[github] check-access failed, falling back to localStorage', e);
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
