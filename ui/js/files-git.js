'use strict';

// Compact GitHub source-control manager for the File Editor sidebar.
// Reuses the existing GitHub API endpoints; renders a VS Code-style
// commit graph drawn next to the commit list so users can see branches
// other than `main` (merges, side branches, unpushed work).
//
// Public:
//   renderGitPanel(rootEl)  — paint into the given container
//   refreshGit(rootEl)      — re-fetch + repaint
//   openGitPanel(rootEl)    — first activation hook (fetches status/log)

import { apiPath } from './config.js';

// ── DOM building blocks ────────────────────────────────────────────

const GRAPH_LANE_W = 14;   // pixels per graph lane (column width)
const GRAPH_ROW_H  = 44;   // pixels per commit row (must match CSS)
const GRAPH_DOT_R  = 4;    // commit dot radius

// VS-Code-ish lane colour palette (works on dark + light backgrounds)
const LANE_COLORS = [
  '#3b82f6', // blue (main / HEAD)
  '#f59e0b', // amber
  '#10b981', // emerald
  '#ec4899', // pink
  '#8b5cf6', // violet
  '#ef4444', // red
  '#06b6d4', // cyan
  '#eab308', // yellow
];

function laneColor(i) {
  return LANE_COLORS[((i % LANE_COLORS.length) + LANE_COLORS.length) % LANE_COLORS.length];
}

function authHeaders() {
  const t = localStorage.getItem('auth_token');
  return t ? { Authorization: 'Bearer ' + t } : {};
}

async function ghFetch(path, opts = {}) {
  const headers = Object.assign({}, authHeaders(), opts.headers || {});
  if (opts.body && !('Content-Type' in headers)) {
    headers['Content-Type'] = 'application/json';
  }
  const res = await fetch(apiPath(path), Object.assign({}, opts, { headers }));
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail || detail; } catch (_) {
      try { detail = await res.text(); } catch (_) {}
    }
    throw new Error(detail || ('HTTP ' + res.status));
  }
  return res.json();
}

function escapeHtml(str) {
  const d = document.createElement('div');
  d.textContent = str == null ? '' : String(str);
  return d.innerHTML;
}

function flagClass(flag) {
  return ({ M: 'fg-flag-m', A: 'fg-flag-a', D: 'fg-flag-d', R: 'fg-flag-r', '??': 'fg-flag-u' })[flag] || 'fg-flag-m';
}

function flagLabel(flag) {
  return ({ M: 'Modified', A: 'Added', D: 'Deleted', R: 'Renamed', '??': 'Untracked', U: 'Updated' })[flag] || flag;
}

// ── Public render entry ────────────────────────────────────────────

let _state = {
  status: null,
  graph: null,
  loading: false,
  err: null,
};

export function renderGitPanel(rootEl) {
  if (!rootEl) return;
  const body = rootEl.querySelector('#fg-body');
  if (!body) return;
  if (_state.loading && !_state.status) {
    body.innerHTML = '<div class="fg-loading">Loading…</div>';
    return;
  }
  if (_state.err) {
    body.innerHTML = `<div class="fg-error">${escapeHtml(_state.err)}</div>
      <button class="fg-btn" data-act="retry">Retry</button>`;
    const r = body.querySelector('[data-act="retry"]');
    if (r) r.addEventListener('click', () => refreshGit(rootEl));
    return;
  }
  const s = _state.status;
  const g = _state.graph;
  if (!s) {
    body.innerHTML = '<div class="fg-loading">Loading…</div>';
    return;
  }
  body.innerHTML = renderHeader(s) +
                   renderChangesSection(s) +
                   renderCommitSection(s) +
                   renderSyncSection(s) +
                   renderTokenSection() +
                   renderGraphSection(s, g);
  wireEvents(rootEl, s, g);
  if (window.lucide) window.lucide.createIcons({ nodes: Array.from(body.querySelectorAll('[data-lucide]:not(.lucide)')) });
  drawGraph(rootEl, g);
}

// ── Sections ───────────────────────────────────────────────────────

function renderHeader(s) {
  const branch = escapeHtml(s.branch || '—');
  const remote = escapeHtml(s.remote_url || 'no remote');
  let sync = '';
  if (s.has_remote && (s.ahead > 0 || s.behind > 0)) {
    sync = `<span class="fg-ahead" title="ahead">↑${s.ahead}</span><span class="fg-behind" title="behind">↓${s.behind}</span>`;
  } else if (s.has_remote) {
    sync = '<span class="fg-sync-clean">in sync</span>';
  }
  return `
    <div class="fg-section fg-section-header">
      <div class="fg-branch-row">
        <i data-lucide="git-branch" class="lucide-icon"></i>
        <span class="fg-branch-name" title="current branch">${branch}</span>
        ${sync}
      </div>
      <div class="fg-remote-row" title="${remote}">${remote}</div>
    </div>
  `;
}

function renderChangesSection(s) {
  const items = [
    ...(s.staged || []).map(f => ({ ...f, area: 'Staged' })),
    ...(s.unstaged || []).map(f => ({ ...f, area: 'Unstaged' })),
    ...(s.untracked || []).map(f => ({ ...f, flag: '??', area: 'Untracked' })),
  ];
  const count = items.length;
  const list = count === 0
    ? '<div class="fg-clean">✓ Working tree clean</div>'
    : items.map(f => `
        <div class="fg-file" title="${escapeHtml(f.path)}">
          <span class="fg-file-flag ${flagClass(f.flag)}" title="${escapeHtml(flagLabel(f.flag))}">${escapeHtml(f.flag)}</span>
          <span class="fg-file-path">${escapeHtml(f.path)}</span>
        </div>
      `).join('');
  return `
    <details class="fg-section fg-changes" ${count ? 'open' : ''}>
      <summary><span>Changes</span><span class="fg-count">${count}</span></summary>
      <div class="fg-file-list">${list}</div>
    </details>
  `;
}

function renderCommitSection(s) {
  const enabled = s.file_count > 0;
  return `
    <div class="fg-section fg-commit">
      <div class="fg-commit-row">
        <input type="text" id="fg-commit-msg" class="fg-input" placeholder="${enabled ? 'Commit message…' : 'No changes to commit'}" ${enabled ? '' : 'disabled'}>
        <button class="fg-btn fg-btn-primary" id="fg-commit-btn" ${enabled ? '' : 'disabled'} title="Stage all + commit">
          <i data-lucide="check" class="lucide-icon"></i>
        </button>
      </div>
      <div id="fg-commit-result" class="fg-result" hidden></div>
    </div>
  `;
}

function renderSyncSection(s) {
  if (!s.has_remote) return '';
  return `
    <div class="fg-section fg-sync">
      <button class="fg-btn" id="fg-pull-btn" title="git pull"><i data-lucide="arrow-down" class="lucide-icon"></i> Pull</button>
      <button class="fg-btn" id="fg-push-btn" title="git push"><i data-lucide="arrow-up" class="lucide-icon"></i> Push</button>
      <button class="fg-btn fg-btn-deploy" id="fg-pull-restart-btn" title="Pull + restart server"><i data-lucide="rocket" class="lucide-icon"></i> Pull &amp; Restart</button>
      <div id="fg-sync-result" class="fg-result" hidden></div>
    </div>
  `;
}

function renderTokenSection() {
  return `
    <details class="fg-section fg-token">
      <summary><span>GitHub token</span><span class="fg-token-status" id="fg-token-status-label">—</span></summary>
      <div class="fg-token-row">
        <input type="password" id="fg-token-input" class="fg-input" placeholder="ghp_… / github_pat_…">
        <button class="fg-btn fg-btn-primary" id="fg-token-save-btn">Save</button>
      </div>
      <div id="fg-token-result" class="fg-result" hidden></div>
      <details class="fg-token-hint">
        <summary>How to create a GitHub token →</summary>
        <div class="fg-token-hint-body">
          <p><strong>Classic token:</strong></p>
          <ol>
            <li>Go to <a href="https://github.com/settings/tokens" target="_blank" rel="noopener">github.com/settings/tokens</a></li>
            <li>Click <strong>Generate new token (classic)</strong></li>
            <li>Give it a name (e.g. "webAgent")</li>
            <li>Select scope: <strong>repo</strong> (full control)</li>
            <li>Click <strong>Generate token</strong> and copy it</li>
          </ol>
          <p><strong>Fine-grained token:</strong></p>
          <ol>
            <li>Go to <a href="https://github.com/settings/tokens?type=beta" target="_blank" rel="noopener">tokens (fine-grained)</a></li>
            <li>Click <strong>Generate new token (fine-grained)</strong></li>
            <li>Select this repository only</li>
            <li>Permissions → <strong>Contents: Read and write</strong></li>
            <li>Click <strong>Generate token</strong> and copy it</li>
          </ol>
          <p class="fg-token-hint-foot">Paste the token above and click Save. It's stored locally in provider.json.</p>
        </div>
      </details>
    </details>
  `;
}

function renderGraphSection(s, g) {
  if (!g || !g.commits || !g.commits.length) {
    return '<div class="fg-section fg-graph"><div class="fg-section-title">Commit graph</div><div class="fg-loading">Loading…</div></div>';
  }
  const rows = g.commits.map((c, idx) => renderGraphRow(c, idx, g)).join('');
  return `
    <div class="fg-section fg-graph">
      <div class="fg-section-title">Commit graph <span class="fg-graph-hint">(all branches)</span></div>
      <div class="fg-graph-list" id="fg-graph-list" style="--lane-w:${GRAPH_LANE_W}px; --row-h:${GRAPH_ROW_H}px;">
        ${rows}
      </div>
    </div>
  `;
}

function renderGraphRow(c, idx, g) {
  const cls = c.is_head
    ? 'fg-graph-row head'
    : (c.is_pullable
        ? 'fg-graph-row unpulled'
        : (c.is_pulled ? 'fg-graph-row pulled' : 'fg-graph-row other-branch'));
  const badge = c.is_head
    ? '<span class="fg-graph-badge head">HEAD</span>'
    : (c.is_pullable
        ? '<span class="fg-graph-badge unpulled" title="On origin/' + escapeHtml(g.current_branch || 'current branch') + ' but not yet pulled — click Pull to bring it in">↓</span>'
        : '');
  // Branch tip labels — show every branch whose tip is this commit.
  const branchLabels = (g.branches && Object.keys(g.branches).length)
    ? Object.entries(g.branches)
        .filter(([_, h]) => h === c.hash)
        .map(([name]) => `<span class="fg-branch-tag" title="branch tip">${escapeHtml(name)}</span>`)
        .join('')
    : '';
  // Graph column width is fixed by max_lane * lane width
  const graphW = (g.max_lane || 1) * GRAPH_LANE_W;
  // Continuation lines for the detail panel: any lane that's "active" out
  // of this row (i.e. has a line entering the row from the top AND/OR
  // leaving toward the bottom) needs to render as a vertical line through
  // the detail area so the chain stays connected when the row expands.
  const passLanes = [];
  const lanesIn  = c.lanes_in  || [];
  const lanesOut = c.lanes_out || [];
  const laneCount = Math.max(lanesIn.length, lanesOut.length);
  for (let i = 0; i < laneCount; i++) {
    if (lanesIn[i] || lanesOut[i]) passLanes.push(i);
  }
  const passLines = passLanes.map(i =>
    `<div class="fg-graph-lane-line" style="left:${laneX(i) - 1}px;background:${laneColor(i)};"></div>`
  ).join('');
  return `
    <div class="${cls}" data-hash="${escapeHtml(c.full_hash)}" data-short="${escapeHtml(c.hash)}" data-lane="${c.lane}" data-idx="${idx}" tabindex="0" role="button">
      <div class="fg-graph-row-head">
        <div class="fg-graph-col" style="width:${graphW}px;">
          <svg class="fg-graph-svg" width="${graphW}" height="${GRAPH_ROW_H}" viewBox="0 0 ${graphW} ${GRAPH_ROW_H}" aria-hidden="true"></svg>
        </div>
        <div class="fg-graph-meta">
          <div class="fg-graph-msg-line">
            ${badge}${branchLabels}
            <span class="fg-graph-msg">${escapeHtml(c.message || '')}</span>
          </div>
          <div class="fg-graph-sub">
            <code class="fg-hash">${escapeHtml(c.hash)}</code>
            <span class="fg-graph-author">${escapeHtml(c.author || '')}</span>
            <span class="fg-graph-date" title="${escapeHtml(c.date_iso || '')}">${escapeHtml(c.date_relative || '')}</span>
          </div>
        </div>
      </div>
      <div class="fg-graph-detail" hidden>
        <div class="fg-graph-detail-lanes" aria-hidden="true" style="width:${graphW}px;">${passLines}</div>
        <div class="fg-graph-detail-content"></div>
      </div>
    </div>
  `;
}

// ── SVG graph drawing ──────────────────────────────────────────────

function drawGraph(rootEl, g) {
  if (!g) return;
  const list = rootEl.querySelector('#fg-graph-list');
  if (!list) return;
  const rows = list.querySelectorAll('.fg-graph-row');
  const commits = g.commits || [];
  rows.forEach((rowEl, i) => {
    const c = commits[i];
    if (!c) return;
    const svg = rowEl.querySelector('.fg-graph-svg');
    if (!svg) return;
    paintRow(svg, c, g.max_lane || 1);
  });
}

function laneX(i) { return i * GRAPH_LANE_W + GRAPH_LANE_W / 2; }

function paintRow(svg, c, maxLane) {
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const ns = 'http://www.w3.org/2000/svg';
  const top = 0;
  const mid = GRAPH_ROW_H / 2;
  const bot = GRAPH_ROW_H;
  const lanesIn  = c.lanes_in  || [];
  const lanesOut = c.lanes_out || [];
  const dot = c.lane;
  const parentLanes = c.parent_lanes || [];
  const mergeInLanes = c.merge_in_lanes || [];

  // Helper to add a path with a given stroke colour.
  function line(d, color) {
    const p = document.createElementNS(ns, 'path');
    p.setAttribute('d', d);
    p.setAttribute('stroke', color);
    p.setAttribute('stroke-width', '1.6');
    p.setAttribute('fill', 'none');
    p.setAttribute('stroke-linecap', 'round');
    svg.appendChild(p);
  }

  // 1. Pass-through lanes (line in AND line out, not the dot).
  const lanesCount = Math.max(lanesIn.length, lanesOut.length, maxLane);
  for (let i = 0; i < lanesCount; i++) {
    if (i === dot) continue;
    const hasIn  = !!lanesIn[i];
    const hasOut = !!lanesOut[i];
    if (hasIn && hasOut) {
      line(`M ${laneX(i)} ${top} L ${laneX(i)} ${bot}`, laneColor(i));
    } else if (hasIn && !hasOut) {
      // Line came in but didn't continue — only happens for merge_in_lanes
      // (a sibling child's line bending into the dot).
      if (mergeInLanes.includes(i)) {
        // top of lane i → mid, then curve to dot
        line(`M ${laneX(i)} ${top} L ${laneX(i)} ${mid - 6} Q ${laneX(i)} ${mid} ${laneX(i) + (dot > i ? 6 : -6)} ${mid} L ${laneX(dot)} ${mid}`, laneColor(i));
      } else {
        // Defensive: stray inbound line
        line(`M ${laneX(i)} ${top} L ${laneX(i)} ${mid}`, laneColor(i));
      }
    } else if (!hasIn && hasOut) {
      // Outgoing line that didn't come from this row's dot (= a parent
      // placed on a fresh lane that's not in parent_lanes? shouldn't
      // happen, but draw mid→bottom defensively).
      if (parentLanes.includes(i)) {
        // bend from dot center down to lane i, then to bottom
        const x1 = laneX(dot);
        const x2 = laneX(i);
        const yBend = mid + 6;
        line(`M ${x1} ${mid} L ${x2 + (i > dot ? -6 : 6)} ${mid} Q ${x2} ${mid} ${x2} ${yBend} L ${x2} ${bot}`, laneColor(i));
      } else {
        line(`M ${laneX(i)} ${mid} L ${laneX(i)} ${bot}`, laneColor(i));
      }
    }
  }

  // 2. Dot lane: incoming top→mid if a child placed our hash here.
  const childAbove = !!lanesIn[dot];
  if (childAbove) {
    line(`M ${laneX(dot)} ${top} L ${laneX(dot)} ${mid}`, laneColor(dot));
  }

  // 3. Outgoing lines: from dot center to each parent's lane at bottom.
  parentLanes.forEach((pl) => {
    if (pl === dot) {
      // Straight down — same lane
      line(`M ${laneX(dot)} ${mid} L ${laneX(dot)} ${bot}`, laneColor(dot));
    } else {
      // Bend horizontally from dot mid out to parent lane, then down.
      const xD = laneX(dot);
      const xP = laneX(pl);
      const yBend = mid + 6;
      const adj  = (pl > dot) ? -6 : 6;
      line(`M ${xD} ${mid} L ${xP + adj} ${mid} Q ${xP} ${mid} ${xP} ${yBend} L ${xP} ${bot}`, laneColor(pl));
    }
  });

  // 4. The commit dot itself.
  const circle = document.createElementNS(ns, 'circle');
  circle.setAttribute('cx', laneX(dot));
  circle.setAttribute('cy', mid);
  circle.setAttribute('r', GRAPH_DOT_R);
  circle.setAttribute('fill', laneColor(dot));
  if (c.is_head) {
    circle.setAttribute('stroke', 'var(--files-text-strong, #fff)');
    circle.setAttribute('stroke-width', '2');
  }
  svg.appendChild(circle);
}

// ── Wire events ────────────────────────────────────────────────────

function wireEvents(rootEl, s, g) {
  const body = rootEl.querySelector('#fg-body');
  if (!body) return;

  const commitBtn = body.querySelector('#fg-commit-btn');
  const commitMsg = body.querySelector('#fg-commit-msg');
  if (commitBtn) commitBtn.addEventListener('click', () => doCommit(rootEl));
  if (commitMsg) commitMsg.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doCommit(rootEl); }
  });

  const pushBtn = body.querySelector('#fg-push-btn');
  if (pushBtn) pushBtn.addEventListener('click', () => doSync(rootEl, 'push'));
  const pullBtn = body.querySelector('#fg-pull-btn');
  if (pullBtn) pullBtn.addEventListener('click', () => doSync(rootEl, 'pull'));
  const prBtn = body.querySelector('#fg-pull-restart-btn');
  if (prBtn) prBtn.addEventListener('click', () => doPullRestart(rootEl));

  const tokSave = body.querySelector('#fg-token-save-btn');
  if (tokSave) tokSave.addEventListener('click', () => doSaveToken(rootEl));
  const tokInput = body.querySelector('#fg-token-input');
  if (tokInput) tokInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doSaveToken(rootEl); }
  });

  // Commit graph rows → click the header to expand commit detail. Clicks
  // inside the detail panel (selecting text, etc.) shouldn't collapse it.
  body.querySelectorAll('.fg-graph-row').forEach((row) => {
    const head = row.querySelector('.fg-graph-row-head');
    if (head) head.addEventListener('click', () => toggleDetail(row));
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleDetail(row); }
    });
  });

  // Refresh token-status label
  refreshTokenStatus(rootEl);
}

async function toggleDetail(row) {
  const panel = row.querySelector('.fg-graph-detail');
  if (!panel) return;
  // We only swap content inside .fg-graph-detail-content — the lane-line
  // overlay above it stays in place so the chain stays connected when the
  // detail expands.
  const content = panel.querySelector('.fg-graph-detail-content');
  if (!panel.hidden) {
    panel.hidden = true;
    row.classList.remove('open');
    return;
  }
  row.classList.add('open');
  panel.hidden = false;
  if (panel.dataset.loaded === '1') return;
  if (content) content.innerHTML = '<div class="fg-loading">Loading…</div>';
  const hash = row.dataset.hash || row.dataset.short;
  try {
    const d = await ghFetch(`/api/v1/github/commit/${encodeURIComponent(hash)}`);
    if (content) content.innerHTML = renderCommitDetail(d);
    panel.dataset.loaded = '1';
  } catch (e) {
    if (content) content.innerHTML = `<div class="fg-error">${escapeHtml(e.message)}</div>`;
  }
}

function renderCommitDetail(d) {
  const files = (d.files || []).map(f => {
    const added = f.binary ? '—' : `+${f.added}`;
    const removed = f.binary ? '—' : `-${f.removed}`;
    return `<div class="fg-cd-file"><span class="fg-cd-added">${escapeHtml(added)}</span><span class="fg-cd-removed">${escapeHtml(removed)}</span><span class="fg-cd-path">${escapeHtml(f.path)}</span></div>`;
  }).join('') || '<div class="fg-loading">No file changes</div>';
  const body = (d.body || '').trim() ? `<pre class="fg-cd-body">${escapeHtml(d.body.trim())}</pre>` : '';
  const parents = (d.parents || []).map(p => escapeHtml(p.slice(0, 7))).join(', ') || '(none)';
  const subject = (d.subject || '').trim() ? `<div class="fg-cd-subject">${escapeHtml(d.subject)}</div>` : '';
  return `
    <div class="fg-cd">
      <div class="fg-cd-fields">
        <div class="fg-cd-field"><span class="fg-cd-label">Full hash</span><code class="fg-cd-val">${escapeHtml(d.full_hash || '')}</code></div>
        <div class="fg-cd-field"><span class="fg-cd-label">Author</span><span class="fg-cd-val">${escapeHtml(d.author || '')}${d.author_email ? ' &lt;' + escapeHtml(d.author_email) + '&gt;' : ''}</span></div>
        <div class="fg-cd-field"><span class="fg-cd-label">Authored</span><span class="fg-cd-val">${escapeHtml(d.author_date || '')}</span></div>
        <div class="fg-cd-field"><span class="fg-cd-label">Committer</span><span class="fg-cd-val">${escapeHtml(d.committer || '')}${d.committer_email ? ' &lt;' + escapeHtml(d.committer_email) + '&gt;' : ''}</span></div>
        <div class="fg-cd-field"><span class="fg-cd-label">Committed</span><span class="fg-cd-val">${escapeHtml(d.commit_date || '')}</span></div>
        <div class="fg-cd-field"><span class="fg-cd-label">Parents</span><code class="fg-cd-val">${parents}</code></div>
      </div>
      ${subject}
      ${body}
      <div class="fg-cd-files-title">Files (${(d.files || []).length})</div>
      <div class="fg-cd-files">${files}</div>
    </div>
  `;
}

// ── Actions ────────────────────────────────────────────────────────

function showResult(el, msg, type) {
  if (!el) return;
  el.hidden = false;
  el.textContent = msg;
  el.classList.remove('ok', 'err', 'info');
  el.classList.add(type === 'error' ? 'err' : type === 'success' ? 'ok' : 'info');
}

async function doCommit(rootEl) {
  const body = rootEl.querySelector('#fg-body');
  const msgInput = body.querySelector('#fg-commit-msg');
  const result = body.querySelector('#fg-commit-result');
  const btn = body.querySelector('#fg-commit-btn');
  const msg = (msgInput.value || '').trim();
  if (!msg) {
    showResult(result, 'Enter a commit message', 'error');
    return;
  }
  btn.disabled = true;
  try {
    const r = await ghFetch('/api/v1/github/commit', {
      method: 'POST', body: JSON.stringify({ message: msg }),
    });
    showResult(result, r.message || 'Committed', r.status === 'committed' ? 'success' : 'info');
    msgInput.value = '';
    await refreshGit(rootEl);
  } catch (e) {
    showResult(result, `Error: ${e.message}`, 'error');
  } finally {
    btn.disabled = false;
  }
}

async function doSync(rootEl, kind) {
  const body = rootEl.querySelector('#fg-body');
  const result = body.querySelector('#fg-sync-result');
  showResult(result, kind === 'push' ? 'Pushing…' : 'Pulling…', 'info');
  try {
    const r = await ghFetch(`/api/v1/github/${kind}`, { method: 'POST' });
    showResult(result, r.message || (kind === 'push' ? 'Push successful' : 'Pull successful'), 'success');
    await refreshGit(rootEl);
  } catch (e) {
    showResult(result, `${kind === 'push' ? 'Push' : 'Pull'} failed: ${e.message}`, 'error');
  }
}

async function doPullRestart(rootEl) {
  if (!confirm('Pull latest and restart the server?\n\nIn-flight requests and the agent WebSocket will be dropped while the server restarts.')) return;
  const body = rootEl.querySelector('#fg-body');
  const result = body.querySelector('#fg-sync-result');
  showResult(result, 'Pulling…', 'info');
  try {
    await ghFetch('/api/v1/github/pull', { method: 'POST' });
  } catch (e) {
    showResult(result, `Pull failed: ${e.message}`, 'error');
    return;
  }
  showResult(result, 'Pulled. Restarting…', 'info');
  try {
    await fetch(apiPath('/api/v1/restart'), { method: 'POST', headers: authHeaders() });
  } catch (_) { /* expected: server cut the connection */ }
  const start = Date.now();
  while (Date.now() - start < 60000) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      const r = await fetch(apiPath('/health'));
      if (r.ok) {
        showResult(result, 'Server back up — refreshing', 'success');
        await refreshGit(rootEl);
        return;
      }
    } catch (_) {}
  }
  showResult(result, 'Timed out waiting for server', 'error');
}

async function doSaveToken(rootEl) {
  const body = rootEl.querySelector('#fg-body');
  const input = body.querySelector('#fg-token-input');
  const result = body.querySelector('#fg-token-result');
  const token = (input.value || '').trim();
  if (!token) {
    showResult(result, 'Enter a GitHub token', 'error');
    return;
  }
  try {
    await ghFetch('/api/v1/github/token', { method: 'POST', body: JSON.stringify({ token }) });
    showResult(result, 'Token saved', 'success');
    input.value = '';
    await refreshTokenStatus(rootEl);
  } catch (e) {
    showResult(result, `Error: ${e.message}`, 'error');
  }
}

async function refreshTokenStatus(rootEl) {
  const label = rootEl.querySelector('#fg-token-status-label');
  if (!label) return;
  try {
    const r = await ghFetch('/api/v1/github/token-status');
    label.textContent = r.configured ? r.masked : 'not set';
    label.classList.toggle('ok', !!r.configured);
  } catch (_) {
    label.textContent = '—';
  }
}

// ── Refresh / open hooks ───────────────────────────────────────────

export async function refreshGit(rootEl) {
  if (!rootEl) return;
  _state.loading = true;
  _state.err = null;
  // Show a soft loading hint if we already have data
  const body = rootEl.querySelector('#fg-body');
  if (body && _state.status) {
    const h = body.querySelector('.fg-section-header');
    if (h) h.classList.add('loading');
  } else if (body) {
    body.innerHTML = '<div class="fg-loading">Loading…</div>';
  }
  try {
    const [status, graph] = await Promise.all([
      ghFetch('/api/v1/github/status'),
      ghFetch('/api/v1/github/log-graph?limit=80'),
    ]);
    _state.status = status;
    _state.graph = graph;
  } catch (e) {
    _state.err = e.message || 'Failed to load';
  } finally {
    _state.loading = false;
  }
  renderGitPanel(rootEl);
}

let _opened = false;
export async function openGitPanel(rootEl) {
  if (!rootEl) return;
  // Always re-render on open — the panel state may be stale.
  if (!_opened) {
    _opened = true;
    const refresh = rootEl.querySelector('#fg-refresh');
    if (refresh) refresh.addEventListener('click', () => refreshGit(rootEl));
  }
  await refreshGit(rootEl);
}
