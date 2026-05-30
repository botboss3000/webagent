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
import { app } from './state.js';

// ── DOM building blocks ────────────────────────────────────────────

const GRAPH_LANE_W = 14;   // pixels per graph lane (column width)
const GRAPH_ROW_H  = 38;   // pixels per commit row (must match CSS)
const GRAPH_DOT_R  = 4;    // commit dot radius

// Max lane index this commit row actually touches (dot, in/out/parent/merge
// lanes). Used to size the SVG column per-row so the text snaps as close to
// the tree as possible — VS-Code style, instead of reserving the global
// max-lane width for every single row.
function rowLaneSpan(c) {
  let max = c.lane || 0;
  const arrIn  = c.lanes_in  || [];
  const arrOut = c.lanes_out || [];
  for (let i = 0; i < arrIn.length;  i++) if (arrIn[i]  && i > max) max = i;
  for (let i = 0; i < arrOut.length; i++) if (arrOut[i] && i > max) max = i;
  if (c.parent_lanes)    for (const p of c.parent_lanes)    if (p > max) max = p;
  if (c.merge_in_lanes)  for (const m of c.merge_in_lanes)  if (m > max) max = m;
  return max + 1;
}

// Compact relative-time formatter: "now", "12m", "2h", "3d", "2w", "5mo",
// "1y". Falls back to whatever git's %ar string is if the ISO is missing
// or unparseable.
function shortRelativeTime(iso, fallback) {
  if (!iso) return fallback || '';
  const t = Date.parse(iso);
  if (isNaN(t)) return fallback || '';
  const sec = Math.max(0, (Date.now() - t) / 1000);
  if (sec < 45)              return 'now';
  if (sec < 60 * 60)         return Math.round(sec / 60) + 'm';
  if (sec < 60 * 60 * 24)    return Math.round(sec / 3600) + 'h';
  const day = sec / 86400;
  if (day < 7)               return Math.round(day) + 'd';
  if (day < 30)              return Math.round(day / 7) + 'w';
  if (day < 365)             return Math.round(day / 30.4375) + 'mo';
  return Math.round(day / 365.25) + 'y';
}

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
      <button type="button" class="fg-branch-row fg-branch-picker-trigger" id="fg-branch-picker-trigger" title="Switch branch">
        <i data-lucide="git-branch" class="lucide-icon"></i>
        <span class="fg-branch-name">${branch}</span>
        <i data-lucide="chevron-down" class="lucide-icon fg-branch-chev"></i>
        ${sync}
      </button>
      <div class="fg-remote-token-row">
        <div class="fg-remote-row" title="${remote}">${remote}</div>
        <span class="fg-token-status-dot" id="fg-token-status-label" title="Token status">—</span>
        <input type="password" id="fg-token-input" class="fg-input fg-token-input-inline" placeholder="GitHub token…" autocomplete="off" data-lpignore="true" data-1p-ignore="true" title="GitHub token">
        <button class="fg-btn fg-btn-primary fg-token-save-inline" id="fg-token-save-btn" title="Save GitHub token">Save</button>
        <a class="fg-token-help-link" href="https://github.com/settings/tokens" target="_blank" rel="noopener" title="Create or manage GitHub tokens (opens in new tab)" aria-label="GitHub token help">?</a>
      </div>
      <div id="fg-token-result" class="fg-result fg-token-result-inline" hidden></div>
      <div class="fg-branch-menu" id="fg-branch-menu" hidden role="listbox" aria-label="Switch branch">
        <div class="fg-branch-menu-loading">Loading branches…</div>
      </div>
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
  // The star hands the whole job to the Source Controller agent: it reviews
  // the changes, writes a commit note, and commits + pushes. Enabled whenever
  // there's something to do — uncommitted changes OR unpushed local commits.
  const starEnabled = s.file_count > 0 || (s.ahead || 0) > 0;
  const starTitle = starEnabled
    ? 'Hand off to the Source Controller agent — review changes, write a note, commit & push'
    : 'Nothing to commit or push';
  return `
    <div class="fg-section fg-commit">
      <div class="fg-commit-row">
        <input type="text" id="fg-commit-msg" class="fg-input" placeholder="${enabled ? 'Commit message…' : 'No changes to commit'}" ${enabled ? '' : 'disabled'}>
        <button class="fg-btn fg-star-btn" id="fg-sc-star-btn" ${starEnabled ? '' : 'disabled'} title="${starTitle}" aria-label="Source Controller: review, commit & push">
          <i data-lucide="star" class="lucide-icon"></i>
        </button>
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
      <button class="fg-btn" id="fg-pull-btn" title="Pull current branch"><i data-lucide="arrow-up" class="lucide-icon"></i> Pull</button>
      <button class="fg-btn" id="fg-push-btn" title="Push current branch"><i data-lucide="arrow-down" class="lucide-icon"></i> Push</button>
      <button class="fg-btn" id="fg-merge-btn" title="Merge another branch into the current branch"><i data-lucide="git-merge" class="lucide-icon"></i> Merge…</button>
      <button class="fg-btn fg-refresh-btn" id="fg-refresh-btn" title="Refresh source control — re-check recent changes and reload the commit graph" aria-label="Refresh source control"><i data-lucide="refresh-cw" class="lucide-icon"></i></button>
      <div id="fg-sync-result" class="fg-result" hidden></div>
      <div class="fg-merge-menu" id="fg-merge-menu" hidden role="listbox" aria-label="Pick branch to merge in">
        <div class="fg-branch-menu-loading">Loading branches…</div>
      </div>
    </div>
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
  const baseCls = c.is_head
    ? 'fg-graph-row head'
    : (c.is_pullable
        ? 'fg-graph-row unpulled'
        : (c.is_pulled ? 'fg-graph-row pulled' : 'fg-graph-row other-branch'));
  const selected = (_gitMainState.mode === 'commit' && _gitMainState.payload && _gitMainState.payload.hash === c.full_hash);
  const cls = baseCls + (selected ? ' selected' : '');
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
  // Per-row graph width: only as wide as the lanes this row actually
  // touches, so single-lane rows snap their text right next to the dot
  // and rows with branches push the text out only as far as needed.
  const graphW = rowLaneSpan(c) * GRAPH_LANE_W;
  const shortDate = shortRelativeTime(c.date_iso, c.date_relative);
  return `
    <div class="${cls}" data-hash="${escapeHtml(c.full_hash)}" data-short="${escapeHtml(c.hash)}" data-lane="${c.lane}" data-idx="${idx}" tabindex="0" role="button">
      <div class="fg-graph-row-head">
        <div class="fg-graph-col" style="width:${graphW}px;">
          <svg class="fg-graph-svg" width="${graphW}" height="${GRAPH_ROW_H}" viewBox="0 0 ${graphW} ${GRAPH_ROW_H}" aria-hidden="true"></svg>
        </div>
        <div class="fg-graph-meta">
          <div class="fg-graph-msg-line">
            <span class="fg-graph-msg">${escapeHtml(c.message || '')}</span>
          </div>
          <div class="fg-graph-sub">
            ${badge || branchLabels ? `<span class="fg-graph-tags">${badge}${branchLabels}</span>` : ''}
            <code class="fg-hash">${escapeHtml(c.hash)}</code>
            <span class="fg-graph-date" title="${escapeHtml(c.date_iso || '')} — ${escapeHtml(c.date_relative || '')}">${escapeHtml(shortDate)}</span>
            <span class="fg-graph-author" title="${escapeHtml(c.author || '')}">${escapeHtml(c.author || '')}</span>
          </div>
        </div>
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

  const scStarBtn = body.querySelector('#fg-sc-star-btn');
  if (scStarBtn) scStarBtn.addEventListener('click', () => handoffToSourceController(rootEl));

  const pushBtn = body.querySelector('#fg-push-btn');
  if (pushBtn) pushBtn.addEventListener('click', () => doPush(rootEl));
  const pullBtn = body.querySelector('#fg-pull-btn');
  if (pullBtn) pullBtn.addEventListener('click', () => doPull(rootEl));
  const mergeBtn = body.querySelector('#fg-merge-btn');
  if (mergeBtn) mergeBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleMergeMenu(rootEl);
  });
  const refreshBtn = body.querySelector('#fg-refresh-btn');
  if (refreshBtn) refreshBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    // Re-fetch git status (recent changes) + commit graph and repaint only
    // this sidebar. The spinner clears automatically when refreshGit() repaints
    // the panel with a fresh button.
    refreshBtn.classList.add('is-spinning');
    refreshGit(rootEl);
  });
  const branchTrig = body.querySelector('#fg-branch-picker-trigger');
  if (branchTrig) branchTrig.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleBranchMenu(rootEl);
  });

  // Close any open picker menu when clicking outside
  if (!body.__fgMenuCloser) {
    body.__fgMenuCloser = (ev) => {
      const inMerge = ev.target.closest('#fg-merge-menu') || ev.target.closest('#fg-merge-btn');
      const inBranch = ev.target.closest('#fg-branch-menu') || ev.target.closest('#fg-branch-picker-trigger');
      if (!inMerge) {
        const m = body.querySelector('#fg-merge-menu');
        if (m) m.hidden = true;
      }
      if (!inBranch) {
        const b = body.querySelector('#fg-branch-menu');
        if (b) b.hidden = true;
      }
    };
    document.addEventListener('click', body.__fgMenuCloser);
  }

  const tokSave = body.querySelector('#fg-token-save-btn');
  if (tokSave) tokSave.addEventListener('click', () => doSaveToken(rootEl));
  const tokInput = body.querySelector('#fg-token-input');
  if (tokInput) {
    tokInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doSaveToken(rootEl); }
    });
    // When the user focuses while the masked saved value is shown, clear
    // the field and switch back to password type so they can paste a new
    // token. On blur with nothing typed, restore the mask so it stays
    // visible that a token is configured.
    tokInput.addEventListener('focus', () => {
      const masked = tokInput.dataset.maskedValue;
      if (masked && tokInput.value === masked) {
        tokInput.value = '';
        tokInput.type = 'password';
      }
    });
    tokInput.addEventListener('blur', () => {
      const masked = tokInput.dataset.maskedValue;
      if (masked && !tokInput.value) {
        tokInput.value = masked;
        tokInput.type = 'text';
      }
    });
  }

  // Commit graph rows → click the row to load full commit detail in the
  // git main panel. The sidebar row gets a `.selected` class to mark the
  // currently-displayed commit.
  body.querySelectorAll('.fg-graph-row').forEach((row) => {
    const select = () => {
      const hash = row.dataset.hash || row.dataset.short;
      if (!hash) return;
      body.querySelectorAll('.fg-graph-row.selected').forEach((r) => r.classList.remove('selected'));
      row.classList.add('selected');
      renderGitMain('commit', { hash });
    };
    const head = row.querySelector('.fg-graph-row-head');
    if (head) head.addEventListener('click', select);
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(); }
    });
  });

  // Refresh token-status label
  refreshTokenStatus(rootEl);
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

// ── Branch picker / merge picker ───────────────────────────────────

async function fetchBranches() {
  return ghFetch('/api/v1/github/branches');
}

function renderBranchMenu(menu, data, opts) {
  const { current, branches } = data;
  const { mode, onPick } = opts; // mode: 'switch' | 'merge'
  const items = (branches || []).filter(b => {
    if (mode === 'merge') return b.name !== current;
    return true;
  });
  if (!items.length) {
    menu.innerHTML = '<div class="fg-branch-menu-empty">No other branches</div>';
    return;
  }
  menu.innerHTML = items.map((b) => {
    const isCurrent = b.name === current;
    const tag = b.local && b.remote
      ? '<span class="fg-branch-tag-local" title="local + remote">local</span>'
      : (b.local
          ? '<span class="fg-branch-tag-local" title="local only">local only</span>'
          : '<span class="fg-branch-tag-remote" title="remote only">remote</span>');
    const cur = isCurrent ? '<span class="fg-branch-tag-cur">current</span>' : '';
    return `
      <button type="button" class="fg-branch-item${isCurrent ? ' is-current' : ''}" data-branch="${escapeHtml(b.name)}" ${isCurrent && mode === 'switch' ? 'disabled' : ''}>
        <span class="fg-branch-item-name">${escapeHtml(b.name)}</span>
        <span class="fg-branch-item-meta">${cur}${tag}<code>${escapeHtml(b.hash || '')}</code></span>
      </button>
    `;
  }).join('');
  menu.querySelectorAll('.fg-branch-item').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const name = btn.dataset.branch;
      if (!name) return;
      menu.hidden = true;
      onPick(name);
    });
  });
}

async function toggleBranchMenu(rootEl) {
  const menu = rootEl.querySelector('#fg-branch-menu');
  if (!menu) return;
  // Close the other menu if open
  const otherMenu = rootEl.querySelector('#fg-merge-menu');
  if (otherMenu) otherMenu.hidden = true;
  if (!menu.hidden) { menu.hidden = true; return; }
  menu.hidden = false;
  menu.innerHTML = '<div class="fg-branch-menu-loading">Loading branches…</div>';
  try {
    const data = await fetchBranches();
    renderBranchMenu(menu, data, {
      mode: 'switch',
      onPick: (name) => doCheckout(rootEl, name),
    });
  } catch (e) {
    menu.innerHTML = `<div class="fg-branch-menu-err">${escapeHtml(e.message)}</div>`;
  }
}

async function toggleMergeMenu(rootEl) {
  const menu = rootEl.querySelector('#fg-merge-menu');
  if (!menu) return;
  const otherMenu = rootEl.querySelector('#fg-branch-menu');
  if (otherMenu) otherMenu.hidden = true;
  if (!menu.hidden) { menu.hidden = true; return; }
  menu.hidden = false;
  menu.innerHTML = '<div class="fg-branch-menu-loading">Loading branches…</div>';
  try {
    const data = await fetchBranches();
    renderBranchMenu(menu, data, {
      mode: 'merge',
      onPick: (name) => doMerge(rootEl, name),
    });
  } catch (e) {
    menu.innerHTML = `<div class="fg-branch-menu-err">${escapeHtml(e.message)}</div>`;
  }
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

async function doPush(rootEl) {
  const body = rootEl.querySelector('#fg-body');
  const result = body.querySelector('#fg-sync-result');
  showResult(result, 'Pushing…', 'info');
  try {
    const r = await ghFetch('/api/v1/github/push', { method: 'POST' });
    showResult(result, r.message || 'Push successful', 'success');
    await refreshGit(rootEl);
  } catch (e) {
    showResult(result, `Push failed: ${e.message}`, 'error');
  }
}

async function doPull(rootEl) {
  const body = rootEl.querySelector('#fg-body');
  const result = body.querySelector('#fg-sync-result');
  showResult(result, 'Pulling…', 'info');
  let r;
  try {
    r = await ghFetch('/api/v1/github/pull', { method: 'POST' });
  } catch (e) {
    showResult(result, `Pull failed: ${e.message}`, 'error');
    return;
  }
  await applyPostSync(rootEl, result, r, 'Pulled');
}

async function doMerge(rootEl, branch) {
  const body = rootEl.querySelector('#fg-body');
  const result = body.querySelector('#fg-sync-result');
  showResult(result, `Merging ${branch}…`, 'info');
  let r;
  try {
    r = await ghFetch('/api/v1/github/merge', {
      method: 'POST',
      body: JSON.stringify({ branch }),
    });
  } catch (e) {
    showResult(result, `Merge failed: ${e.message}`, 'error');
    return;
  }
  await applyPostSync(rootEl, result, r, `Merged ${branch}`);
}

async function doCheckout(rootEl, branch) {
  const body = rootEl.querySelector('#fg-body');
  const result = body.querySelector('#fg-sync-result');
  showResult(result, `Switching to ${branch}…`, 'info');
  let r;
  try {
    r = await ghFetch('/api/v1/github/checkout', {
      method: 'POST',
      body: JSON.stringify({ branch }),
    });
  } catch (e) {
    showResult(result, `Checkout failed: ${e.message}`, 'error');
    return;
  }
  await applyPostSync(rootEl, result, r, `Switched to ${branch}`);
}

// Restart the server, wait for /health to come back, then hard-reload
// the page. Used by the refresh buttons in the source-control and
// terminal sidebars. Safe to call without confirmation — the caller
// is responsible for that.
export async function restartServerAndReload({ timeoutMs = 60000 } = {}) {
  try {
    await fetch(apiPath('/api/v1/restart'), { method: 'POST', headers: authHeaders() });
  } catch (_) { /* expected: server cut the connection */ }
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      const h = await fetch(apiPath('/health'));
      if (h.ok) {
        window.location.reload();
        return true;
      }
    } catch (_) {}
  }
  return false;
}

// Shared follow-through for any sync action (pull / merge / checkout).
// Always just refreshes the sidebar — no whole-app reload, no automatic
// server restart. If the response indicates backend code came in we
// surface a hint so the user can restart manually when convenient.
async function applyPostSync(rootEl, resultEl, response, doneLabel) {
  const summary = response.message || `${doneLabel} successful`;
  const hint = response.backend_changed
    ? ' (backend code changed — restart server when convenient)'
    : '';
  showResult(resultEl, summary + hint, 'success');
  await refreshGit(rootEl);
}

async function doSaveToken(rootEl) {
  const body = rootEl.querySelector('#fg-body');
  const input = body.querySelector('#fg-token-input');
  const result = body.querySelector('#fg-token-result');
  const token = (input.value || '').trim();
  // The field auto-fills with the masked saved token (e.g. "ghp_****")
  // on load — don't try to save that back, it's a marker, not a token.
  if (!token || token === input.dataset.maskedValue) {
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
  const input = rootEl.querySelector('#fg-token-input');
  if (!label) return;
  try {
    const r = await ghFetch('/api/v1/github/token-status');
    // Dot-only indicator now: tooltip carries the detail so the row
    // stays one line even in a narrow sidebar.
    label.title = r.configured ? `GitHub token configured (${r.masked})` : 'No GitHub token set';
    label.textContent = '';
    label.classList.toggle('ok', !!r.configured);
    // Show the saved (masked) token in the input so it's visually obvious
    // a token is configured. The mask is `ghp_****` — already safe to
    // display. We switch the input to plain text while the mask is shown,
    // and back to password as soon as the user focuses to enter a new
    // value (see focus/blur wiring in wireEvents).
    if (input) {
      if (r.configured && r.masked) {
        input.dataset.maskedValue = r.masked;
        input.value = r.masked;
        input.type = 'text';
      } else {
        delete input.dataset.maskedValue;
        if (input.value === input.dataset.maskedValue) input.value = '';
        input.type = 'password';
      }
    }
  } catch (_) {
    label.title = 'Token status unknown';
    label.textContent = '';
    label.classList.remove('ok');
  }
}

// ── Source Controller handoff (the ⭐ button) ──────────────────────
//
// Clicking the star hands the whole commit+push job to a dedicated
// "Source Controller" agent (cloned from the `source-controller` system
// template). We find-or-create that agent for the user, make sure its
// source-control ability (codebase_admin → git_tool) is on, switch the
// chat to it in a fresh session, reveal the chat, and submit a ready-made
// message that already carries the remote URL + author email so the agent
// doesn't have to look anything up. Mirrors the Dashboard→chat handoff in
// autoagent.js.

const SOURCE_CONTROLLER_TEMPLATE_ID = 'source-controller';
let _scAgentId = null;  // per-page cache: userId-scoped agent id

function _currentUserId() {
  return (app && app.currentUserId) || localStorage.getItem('webagent_active_user_id') || '';
}

async function _findSourceControllerAgent(userId) {
  try {
    const res = await fetch(apiPath(`/api/v1/agents?user_id=${encodeURIComponent(userId)}`));
    if (!res.ok) return null;
    const data = await res.json();
    const match = (data.agents || []).find(a => a.template_id === SOURCE_CONTROLLER_TEMPLATE_ID);
    return match ? match.id : null;
  } catch (_) {
    return null;
  }
}

// Turn on the source-control ability so git_tool loads for this agent.
// Idempotent — safe to call on an agent that already has it enabled.
async function _enableSourceControlAbility(userId, agentId) {
  try {
    await fetch(apiPath(`/api/v1/agents/${encodeURIComponent(agentId)}/connections/codebase_admin`), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId, enabled: true }),
    });
  } catch (_) { /* if this fails the agent will report it lacks git access */ }
}

async function _createSourceControllerAgent(userId) {
  const res = await fetch(apiPath('/api/v1/agents'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      user_id: userId,
      name: 'Source Controller',
      description: 'Reviews the changes, writes a commit note, then commits and pushes.',
      template_id: SOURCE_CONTROLLER_TEMPLATE_ID,
    }),
  });
  if (!res.ok) throw new Error(`agent create failed (${res.status})`);
  const data = await res.json();
  const id = data.agent && data.agent.id;
  if (!id) throw new Error('agent create returned no id');
  await _enableSourceControlAbility(userId, id);
  // Refresh the chat header's agent dropdown so the new agent is selectable.
  if (app && typeof app.populateAgentSelect === 'function') {
    try { await app.populateAgentSelect(userId); } catch (_) {}
  }
  return id;
}

async function _ensureSourceControllerAgent(userId) {
  if (_scAgentId) return _scAgentId;
  let id = await _findSourceControllerAgent(userId);
  if (id) {
    await _enableSourceControlAbility(userId, id);  // guarantee git is on
  } else {
    id = await _createSourceControllerAgent(userId);
  }
  _scAgentId = id;
  return id;
}

function _buildSourceControlMessage(rootEl) {
  const s = _state.status || {};
  const remote = s.remote_url || 'the configured GitHub remote';
  const branch = s.branch || 'the current branch';
  let email = localStorage.getItem('auth_username') || '';
  if (email && email.indexOf('@') === -1) email = '';  // only use it if it looks like an email
  const typedNote = ((rootEl.querySelector('#fg-commit-msg') || {}).value || '').trim();

  let msg = `Commit and push the local changes in this repository to the GitHub remote ${remote}`;
  if (email) msg += `, authored with the email ${email}`;
  msg += ` (current branch: ${branch}). First review the changes (git status + diff), then write an appropriate commit note, run your safety checks, and verify there are no issues before committing and pushing. When done, report the note you used, the commit hash, and the push result.`;
  if (typedNote) {
    msg += `\n\nThe user typed this commit note — refine and use it if it fits: "${typedNote}"`;
  }
  return msg;
}

async function handoffToSourceController(rootEl) {
  const body = rootEl.querySelector('#fg-body');
  const result = body ? body.querySelector('#fg-commit-result') : null;
  const starBtn = body ? body.querySelector('#fg-sc-star-btn') : null;

  const userId = _currentUserId();
  if (!userId) {
    showResult(result, 'Sign in to use the Source Controller', 'error');
    return;
  }

  const message = _buildSourceControlMessage(rootEl);

  if (starBtn) starBtn.disabled = true;
  showResult(result, 'Starting Source Controller…', 'info');

  let agentId;
  try {
    agentId = await _ensureSourceControllerAgent(userId);
  } catch (e) {
    if (starBtn) starBtn.disabled = false;
    showResult(result, `Could not start Source Controller: ${e.message}`, 'error');
    return;
  }

  // Switch the chat to the Source Controller (starts a fresh session), make
  // sure the chat panel is visible, then drop in the message and send it.
  if (app && typeof app.switchToAgent === 'function') {
    app.switchToAgent(agentId);
  }
  if (typeof window.__applyChatVisible === 'function') {
    try { window.__applyChatVisible(true); } catch (_) {}
  }
  if (app && app.chatInput && app.chatSend) {
    app.chatInput.value = message;
    app.chatInput.dispatchEvent(new Event('input', { bubbles: true }));
    app.chatSend.click();
  }

  if (starBtn) starBtn.disabled = false;
  showResult(result, 'Source Controller is on the chat →', 'success');
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
  // Keep the git main in sync with the latest status — the Commit page's
  // overview strip reads from `_state.status`, so re-render whenever the
  // status changes and Commit is the active mode.
  if (_gitMainState.mode === 'commit') {
    try { renderGitMain(); } catch (_) {}
  }
}

let _opened = false;
export async function openGitPanel(rootEl) {
  if (!rootEl) return;
  if (!_opened) {
    _opened = true;
    initGitMainPills();
  }
  await refreshGit(rootEl);
}

// ── Git main panel ─────────────────────────────────────────────────
//
// The Source Control view's right-side <main> is driven by clicks in the
// sidebar. `overview` is the default (a small summary); other modes
// render when the user clicks something — a commit row, a file in the
// Changes list, a branch in the branch picker. Mode pills mirror the
// most-recently-viewed payload of each mode so users can switch back.

const _gitMainState = {
  mode: 'commit',
  payload: null,
  // Per-mode "last payload" memory so the user can click a disabled mode
  // pill once it has data behind it.
  byMode: { commit: null, diff: null, compare: null },
};

let _gitMainPillsWired = false;
function initGitMainPills() {
  if (_gitMainPillsWired) return;
  const pills = document.getElementById('fg-main-mode-pills');
  if (!pills) return;
  _gitMainPillsWired = true;
  pills.addEventListener('click', (e) => {
    const pill = e.target.closest('.fg-main-mode-pill');
    if (!pill || pill.disabled) return;
    const mode = pill.dataset.mode;
    if (!mode || mode === _gitMainState.mode) return;
    const payload = _gitMainState.byMode[mode] || null;
    renderGitMain(mode, payload);
  });
}

function updateGitMainPills() {
  const pills = document.getElementById('fg-main-mode-pills');
  if (!pills) return;
  pills.querySelectorAll('.fg-main-mode-pill').forEach((pill) => {
    const mode = pill.dataset.mode;
    const active = mode === _gitMainState.mode;
    pill.classList.toggle('active', active);
    pill.setAttribute('aria-selected', active ? 'true' : 'false');
    // Commit is always enabled (it's the default landing view, with an
    // overview strip on top). Diff/compare enable once they've ever
    // been rendered with a payload.
    if (mode === 'commit') {
      pill.disabled = false;
    } else {
      pill.disabled = !_gitMainState.byMode[mode];
    }
  });
}

// `renderGitMain()` with no args re-renders the current mode (typically
// called when the view becomes visible). With (mode, payload) it switches
// modes and remembers the payload.
export function renderGitMain(mode, payload) {
  const body = document.getElementById('fg-main-body');
  if (!body) return;
  if (mode) {
    _gitMainState.mode = mode;
    _gitMainState.payload = payload || null;
    _gitMainState.byMode[mode] = payload || _gitMainState.byMode[mode] || null;
  }
  body.dataset.mode = _gitMainState.mode;
  updateGitMainPills();
  switch (_gitMainState.mode) {
    case 'diff':
      return _gmRenderDiffPlaceholder(body, _gitMainState.payload);
    case 'compare':
      return _gmRenderComparePlaceholder(body, _gitMainState.payload);
    case 'commit':
    default:
      return _gmRenderCommit(body, _gitMainState.payload);
  }
}

// Compact one-line overview strip for the Commit page header — branch,
// remote, working-tree state. Shows what the old Overview tab showed but
// in a single horizontal band so the commit detail itself stays the
// focus of the page.
function renderOverviewStrip() {
  const s = _state.status;
  if (!s) return '';
  const branch = escapeHtml(s.branch || '—');
  const remote = escapeHtml(s.remote_url || 'no remote');
  const dirty = (s.staged || []).length + (s.unstaged || []).length + (s.untracked || []).length;
  let sync = '';
  if (s.has_remote && (s.ahead > 0 || s.behind > 0)) {
    sync = `<span class="fg-strip-sync"><span class="fg-ahead">↑${s.ahead}</span><span class="fg-behind">↓${s.behind}</span></span>`;
  } else if (s.has_remote) {
    sync = '<span class="fg-strip-sync fg-sync-clean">in sync</span>';
  } else {
    sync = '<span class="fg-strip-sync fg-sync-clean">no remote</span>';
  }
  const tree = dirty
    ? `<span class="fg-strip-tree dirty">${dirty} uncommitted</span>`
    : '<span class="fg-strip-tree clean">working tree clean</span>';
  return `
    <div class="fg-strip-overview">
      <span class="fg-strip-cell" title="Current branch"><i data-lucide="git-branch" class="lucide-icon"></i><span class="fg-strip-val">${branch}</span>${sync}</span>
      <span class="fg-strip-cell fg-strip-remote" title="${remote}"><i data-lucide="cloud" class="lucide-icon"></i><span class="fg-strip-val">${remote}</span></span>
      <span class="fg-strip-cell" title="Working tree state"><i data-lucide="file-diff" class="lucide-icon"></i>${tree}</span>
    </div>
  `;
}

async function _gmRenderCommit(body, payload) {
  if (!payload || !payload.hash) {
    body.innerHTML = renderOverviewStrip() + '<div class="fg-main-empty">No commit selected.</div>';
    if (window.lucide) window.lucide.createIcons({ nodes: Array.from(body.querySelectorAll('[data-lucide]:not(.lucide)')) });
    return;
  }
  body.innerHTML = renderOverviewStrip() + '<div class="fg-main-loading">Loading commit…</div>';
  if (window.lucide) window.lucide.createIcons({ nodes: Array.from(body.querySelectorAll('[data-lucide]:not(.lucide)')) });
  try {
    const d = await ghFetch(`/api/v1/github/commit/${encodeURIComponent(payload.hash)}`);
    body.innerHTML = renderOverviewStrip() + `<div class="fg-main-commit">${renderCommitDetail(d)}</div>`;
    if (window.lucide) window.lucide.createIcons({ nodes: Array.from(body.querySelectorAll('[data-lucide]:not(.lucide)')) });
  } catch (e) {
    body.innerHTML = renderOverviewStrip() + `<div class="fg-error">${escapeHtml(e.message || e)}</div>`;
    if (window.lucide) window.lucide.createIcons({ nodes: Array.from(body.querySelectorAll('[data-lucide]:not(.lucide)')) });
  }
}

function _gmRenderDiffPlaceholder(body, payload) {
  const path = payload && payload.path ? escapeHtml(payload.path) : '';
  body.innerHTML = `
    <div class="fg-main-empty">
      <div class="fg-main-empty-title">Working-tree diff${path ? ': ' + path : ''}</div>
      <div class="fg-main-empty-text">File diff rendering ships in a follow-up.</div>
    </div>`;
}

function _gmRenderComparePlaceholder(body, payload) {
  const base = payload && payload.base ? escapeHtml(payload.base) : '';
  const head = payload && payload.head ? escapeHtml(payload.head) : '';
  body.innerHTML = `
    <div class="fg-main-empty">
      <div class="fg-main-empty-title">Compare${base && head ? ': ' + base + ' → ' + head : ''}</div>
      <div class="fg-main-empty-text">Branch comparison ships in a follow-up.</div>
    </div>`;
}
