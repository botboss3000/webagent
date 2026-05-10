/**
 * Optimizer settings panel — skill auto-improver configuration UI.
 *
 * Handles:
 *   - Mode toggle (Live vs Scheduled)
 *   - Intensity slider (1-5 Conservative → Aggressive)
 *   - Metric toggle cards
 *   - Model dropdowns per subagent role
 *   - Scope (app-wide + per-user samples)
 *   - Workflow diagram with run animation
 *   - Activity log expand/collapse
 *   - Save / Run Now / View History
 */

import { apiPath } from './config.js';
import { app } from './state.js';
import { loadSessionChat, populateSessionSelect } from './sessions.js';
import { loopSessionChanged } from './loop.js';
import { loopVisualSessionChanged } from './loop-visual.js';
import { streamSessionChanged } from './stream.js';

// ── DOM refs ──
const MENU_ITEM  = document.getElementById('optimizer-menu-item');
const MODAL      = document.getElementById('optimizer-modal');
const BACKDROP   = document.getElementById('optimizer-backdrop');
const CLOSE_BTN  = document.getElementById('optimizer-close');
const PANEL      = document.getElementById('optimizer-panel');

// Session table refs
const SESSION_TBODY   = document.getElementById('opt-session-tbody');
const SESSION_COUNT   = document.getElementById('opt-session-count');

const MODE_LIVE       = document.getElementById('opt-mode-live');
const MODE_SCHEDULED  = document.getElementById('opt-mode-scheduled');
const SCHEDULE_SEC    = document.getElementById('opt-schedule-section');
const SCHEDULE_INTVL  = document.getElementById('opt-schedule-interval');
const SCHEDULE_MIN    = document.getElementById('opt-schedule-min-interactions');

const FEEDBACK_ALWAYS  = document.getElementById('opt-feedback-always');
const FEEDBACK_FAILURE = document.getElementById('opt-feedback-failure');
const FEEDBACK_NEVER   = document.getElementById('opt-feedback-never');

const INTENSITY       = document.getElementById('opt-intensity');
const INTENSITY_LABEL = document.getElementById('opt-intensity-label');

const METRIC_CARDS    = document.querySelectorAll('.opt-metric-card');
const MODEL_ANALYZER  = document.getElementById('opt-model-analyzer');
const MODEL_PROPOSER  = document.getElementById('opt-model-proposer');
const MODEL_VALIDATOR = document.getElementById('opt-model-validator');

const SCOPE_APP_SAMPLE = document.getElementById('opt-scope-app-sample');
const SCOPE_APP_AGE    = document.getElementById('opt-scope-app-age');
const SCOPE_USER_SAMPLE= document.getElementById('opt-scope-user-sample');
const SCOPE_USER_AGE   = document.getElementById('opt-scope-user-age');

const PREVIEW_DIV      = document.getElementById('opt-preview');
const WORKFLOW         = document.getElementById('opt-workflow');

const TOGGLE_LOG_BTN   = document.getElementById('opt-toggle-log');
const ACTIVITY_LOG     = document.getElementById('opt-activity-log');

const NOTIFY_USER   = document.getElementById('opt-notify-user');
const NOTIFY_DEVS   = document.getElementById('opt-notify-devs');
const NOTIFY_CHANNEL= document.getElementById('opt-notify-channel');

const BTN_SAVE       = document.getElementById('opt-save');
const BTN_RUN_NOW    = document.getElementById('opt-run-now');
const BTN_HISTORY    = document.getElementById('opt-view-history');
const STATUS_EL      = document.getElementById('opt-status');

// ── State ──
let currentMode = 'live';
let currentFeedback = 'always';
let currentIntensity = 3;

const INTENSITY_DESCRIPTIONS = {
  1: 'Conservative — optimize only when ≥20% gain expected. High sample thresholds. Strict validation. Fewer changes, more certainty.',
  2: 'Cautious — optimize when ≥15% gain expected. Higher sample thresholds. Careful validation.',
  3: 'Balanced — optimize when ≥10% improvement expected. Standard thresholds. Moderate validation.',
  4: 'Eager — optimize when ≥7% improvement expected. Lower thresholds. Relaxed validation.',
  5: 'Aggressive — optimize for any improvement (≥5%). Minimal thresholds. Lenient validation. More changes, faster iteration.',
};

// ── Helpers ──

function _authFetch(url, opts = {}) {
  const token = localStorage.getItem('auth_token');
  if (token) {
    opts.headers = { ...(opts.headers || {}), Authorization: `Bearer ${token}` };
  }
  return fetch(url, opts);
}

function showStatus(msg, color = '#9ece6a') {
  STATUS_EL.textContent = msg;
  STATUS_EL.style.color = color;
  STATUS_EL.style.display = 'block';
  setTimeout(() => { STATUS_EL.style.display = 'none'; }, 3000);
}

// ── Open / Close ──

function _formatDuration(ms) {
  if (!ms || ms <= 0) return '—';
  if (ms < 1000) return ms + 'ms';
  if (ms < 60000) return (ms / 1000).toFixed(1) + 's';
  const min = Math.floor(ms / 60000);
  const sec = Math.round((ms % 60000) / 1000);
  return min + 'm ' + sec + 's';
}

function _formatDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  const now = new Date();
  const diffMs = now - d;
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return diffMin + 'm ago';
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return diffHr + 'h ago';
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay < 7) return diffDay + 'd ago';
  return d.toLocaleDateString();
}

function _formatTokens(n) {
  if (!n || n <= 0) return '—';
  if (n < 1000) return n.toString();
  if (n < 1000000) return (n / 1000).toFixed(1) + 'K';
  return (n / 1000000).toFixed(2) + 'M';
}

function _formatCost(c) {
  if (c === null || c === undefined) return '—';
  if (c < 0.001) return '$' + c.toFixed(6);
  if (c < 0.01) return '$' + c.toFixed(4);
  return '$' + c.toFixed(3);
}

async function _loadSessionStats() {
  const userId = app.currentUserId;
  if (!userId) {
    SESSION_TBODY.innerHTML = '<tr><td colspan="10" style="padding:24px;text-align:center;color:#565f89;">No user selected</td></tr>';
    SESSION_COUNT.textContent = '— no user';
    return;
  }

  try {
    const resp = await fetch(apiPath('/api/v1/db/session-stats?user_id=' + encodeURIComponent(userId) + '&db=local.db'));
    if (!resp.ok) {
      SESSION_TBODY.innerHTML = '<tr><td colspan="10" style="padding:24px;text-align:center;color:#fb4934;">Failed to load sessions</td></tr>';
      return;
    }
    const data = await resp.json();
    const sessions = data.sessions || [];
    SESSION_COUNT.textContent = '— ' + sessions.length + ' session' + (sessions.length !== 1 ? 's' : '');

    if (sessions.length === 0) {
      SESSION_TBODY.innerHTML = '<tr><td colspan="10" style="padding:24px;text-align:center;color:#565f89;">No sessions found</td></tr>';
      return;
    }

    const currentSid = app.currentSessionId;
    SESSION_TBODY.innerHTML = sessions.map(s => {
      const isActive = s.session_id === currentSid;
      const rowBg = isActive ? 'rgba(125,207,255,0.06)' : 'transparent';
      const borderLeft = isActive ? '3px solid #7dcfff' : '3px solid transparent';

      return '<tr style="background:' + rowBg + ';border-bottom:1px solid #1a1a2e;transition:background 0.15s;" ' +
        'onmouseover="this.style.background=\'rgba(125,207,255,0.04)\'" ' +
        'onmouseout="this.style.background=\'' + rowBg + '\'">' +
        '<td style="padding:8px 10px;color:#c0caf5;font-weight:600;border-left:' + borderLeft + ';white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:160px;" title="' + s.session_id + '">' +
        _escapeHtml(s.title) +
        '</td>' +
        '<td style="padding:8px 10px;text-align:center;color:#a9b1d6;">' + s.message_count + '</td>' +
        '<td style="padding:8px 10px;text-align:center;color:#a9b1d6;">' + s.turn_count + '</td>' +
        '<td style="padding:8px 10px;text-align:right;color:#9ece6a;">' + _formatTokens(s.total_input_tokens) + '</td>' +
        '<td style="padding:8px 10px;text-align:right;color:#7dcfff;">' + _formatTokens(s.total_output_tokens) + '</td>' +
        '<td style="padding:8px 10px;text-align:right;color:#e0af68;font-weight:600;">' + _formatTokens(s.total_tokens) + '</td>' +
        '<td style="padding:8px 10px;text-align:right;color:#bb9af7;">' + _formatDuration(s.total_duration_ms) + '</td>' +
        '<td style="padding:8px 10px;text-align:right;color:' + (s.total_cost !== null ? '#9ece6a' : '#565f89') + ';">' + _formatCost(s.total_cost) + '</td>' +
        '<td style="padding:8px 10px;color:#565f89;font-size:10px;white-space:nowrap;" title="' + (s.last_active || '') + '">' + _formatDate(s.last_active) + '</td>' +
        '<td style="padding:8px 10px;text-align:center;">' +
        '<button class="opt-session-switch" data-sid="' + s.session_id + '" ' +
        'style="padding:4px 10px;background:' + (isActive ? 'rgba(125,207,255,0.1)' : 'transparent') + ';border:1px solid ' + (isActive ? '#7dcfff' : '#2a2a4a') + ';border-radius:4px;color:' + (isActive ? '#7dcfff' : '#565f89') + ';cursor:' + (isActive ? 'default' : 'pointer') + ';font-size:10px;font-family:inherit;' + (isActive ? '' : '') + '"' + (isActive ? 'disabled' : '') + '>' +
        (isActive ? 'Active' : 'Switch') +
        '</button>' +
        '</td>' +
        '</tr>';
    }).join('');

    // Wire up switch buttons
    SESSION_TBODY.querySelectorAll('.opt-session-switch').forEach(btn => {
      if (btn.disabled) return;
      btn.addEventListener('click', () => _switchSession(btn.dataset.sid));
    });
  } catch (e) {
    console.warn('Failed to load session stats:', e);
    SESSION_TBODY.innerHTML = '<tr><td colspan="10" style="padding:24px;text-align:center;color:#fb4934;">Error loading sessions</td></tr>';
  }
}

function _escapeHtml(str) {
  const div = document.createElement('div');
  div.appendChild(document.createTextNode(str || ''));
  return div.innerHTML;
}

function _switchSession(sid) {
  if (!sid || sid === app.currentSessionId) return;

  app.currentSessionId = sid;
  localStorage.setItem('terminalSessionId', sid);

  // Reload chat messages for this session
  loadSessionChat(sid);

  // Update the header session dropdown to show the newly selected session
  if (app.currentUserId) {
    populateSessionSelect(app.currentUserId);
  }

  // Notify other UI components (WS is per-user, no reconnect needed)
  streamSessionChanged();
  loopSessionChanged();
  loopVisualSessionChanged();

  // Refresh session table to show active state
  _loadSessionStats();
}

function openModal() {
  MODAL.style.display = 'block';
  _loadConfig();
  _loadModels();
  _loadSessionStats();
}

function closeModal() {
  MODAL.style.display = 'none';
}

// ── Load current config (placeholder — will hit /admin/settings/optimizer later) ──

async function _loadConfig() {
  // In production, fetch from GET /admin/settings/optimizer
  // For now, load defaults or localStorage cache
  try {
    const resp = await _authFetch(apiPath('/admin/settings/optimizer'));
    if (!resp.ok) throw new Error('not available');
    const cfg = await resp.json();
    _applyConfig(cfg);
  } catch {
    // Backend not ready — use defaults or whatever UI currently shows
  }
}

async function _loadModels() {
  // Fetch available models and populate dropdowns with "Auto" default
  try {
    // Try to get provider config to fetch model list
    const provResp = await _authFetch(apiPath('/admin/settings/provider'));
    let provider = 'openrouter';
    if (provResp.ok) {
      try {
        const pdata = await provResp.json();
        provider = pdata.provider || provider;
      } catch {}
    }

    const modelResp = await _authFetch(apiPath(`/admin/settings/models?provider=${encodeURIComponent(provider)}`));
    if (!modelResp.ok) return;
    const mdata = await modelResp.json();
    const models = mdata.models || [];
    if (!models.length) return;

    const modelIds = models.map(m => typeof m === 'string' ? m : (m.id || m.name || ''));
    const dropdowns = [
      { el: MODEL_ANALYZER, role: 'analyzer' },
      { el: MODEL_PROPOSER, role: 'proposer' },
      { el: MODEL_VALIDATOR, role: 'validator' },
    ];

    dropdowns.forEach(({ el, role }) => {
      const savedVal = el.dataset.saved || '';
      el.innerHTML = '<option value="">Auto (same as chat)</option>' +
        modelIds.map(id => {
          const sel = (savedVal === id) ? 'selected' : '';
          return `<option value="${id}" ${sel}>${id}</option>`;
        }).join('');
      if (savedVal) el.value = savedVal;
    });
  } catch (e) {
    // Fallback: keep static options
  }
}

function _applyConfig(cfg) {
  if (cfg.mode) setMode(cfg.mode);
  if (cfg.user_feedback) setFeedback(cfg.user_feedback);
  if (cfg.intensity) setIntensity(cfg.intensity);
  if (cfg.metrics) {
    METRIC_CARDS.forEach(card => {
      const name = card.dataset.metric;
      const active = cfg.metrics.includes(name);
      toggleMetric(card, active, false);
    });
  }
  if (cfg.models) {
    if (cfg.models.analyzer)  MODEL_ANALYZER.value  = cfg.models.analyzer;
    if (cfg.models.proposer)  MODEL_PROPOSER.value  = cfg.models.proposer;
    if (cfg.models.validator) MODEL_VALIDATOR.value = cfg.models.validator;
  }
  if (cfg.scope) {
    if (cfg.scope.app_min_sample) SCOPE_APP_SAMPLE.value  = cfg.scope.app_min_sample;
    if (cfg.scope.app_min_age)    SCOPE_APP_AGE.value     = cfg.scope.app_min_age;
    if (cfg.scope.user_min_sample)SCOPE_USER_SAMPLE.value = cfg.scope.user_min_sample;
    if (cfg.scope.user_min_age)   SCOPE_USER_AGE.value    = cfg.scope.user_min_age;
  }
  if (cfg.schedule) {
    if (cfg.schedule.interval)        SCHEDULE_INTVL.value = cfg.schedule.interval;
    if (cfg.schedule.min_interactions)SCHEDULE_MIN.value   = cfg.schedule.min_interactions;
  }
  if (cfg.notifications) {
    if (cfg.notifications.notify_user !== undefined) NOTIFY_USER.checked = cfg.notifications.notify_user;
    if (cfg.notifications.notify_devs !== undefined) NOTIFY_DEVS.checked = cfg.notifications.notify_devs;
    if (cfg.notifications.channel) NOTIFY_CHANNEL.value = cfg.notifications.channel;
  }
}

// ── Mode ──

function setMode(mode) {
  currentMode = mode;
  if (mode === 'live') {
    MODE_LIVE.style.border      = '2px solid #7dcfff';
    MODE_LIVE.style.background  = 'rgba(125,207,255,0.08)';
    MODE_LIVE.style.color       = '#c0caf5';
    MODE_SCHEDULED.style.border     = '2px solid #2a2a4a';
    MODE_SCHEDULED.style.background = 'transparent';
    MODE_SCHEDULED.style.color      = '#565f89';
    SCHEDULE_SEC.style.display       = 'none';
  } else {
    MODE_SCHEDULED.style.border     = '2px solid #7dcfff';
    MODE_SCHEDULED.style.background = 'rgba(125,207,255,0.08)';
    MODE_SCHEDULED.style.color      = '#c0caf5';
    MODE_LIVE.style.border      = '2px solid #2a2a4a';
    MODE_LIVE.style.background  = 'transparent';
    MODE_LIVE.style.color       = '#565f89';
    SCHEDULE_SEC.style.display       = 'block';
  }
}

// ── Feedback ──

function setFeedback(mode) {
  currentFeedback = mode;
  FEEDBACK_ALWAYS.style.border  = mode === 'always'  ? '2px solid #7dcfff' : '2px solid #2a2a4a';
  FEEDBACK_ALWAYS.style.background = mode === 'always'  ? 'rgba(125,207,255,0.08)' : 'transparent';
  FEEDBACK_ALWAYS.style.color  = mode === 'always'  ? '#c0caf5' : '#565f89';

  FEEDBACK_FAILURE.style.border = mode === 'failure' ? '2px solid #7dcfff' : '2px solid #2a2a4a';
  FEEDBACK_FAILURE.style.background = mode === 'failure' ? 'rgba(125,207,255,0.08)' : 'transparent';
  FEEDBACK_FAILURE.style.color = mode === 'failure' ? '#c0caf5' : '#565f89';

  FEEDBACK_NEVER.style.border   = mode === 'never'   ? '2px solid #7dcfff' : '2px solid #2a2a4a';
  FEEDBACK_NEVER.style.background = mode === 'never'   ? 'rgba(125,207,255,0.08)' : 'transparent';
  FEEDBACK_NEVER.style.color   = mode === 'never'   ? '#c0caf5' : '#565f89';
}

// ── Intensity ──

function setIntensity(level) {
  currentIntensity = level;
  INTENSITY.value = level;
  INTENSITY_LABEL.textContent = INTENSITY_DESCRIPTIONS[level];
}

// ── Metric cards ──

function toggleMetric(card, forceNew, animate = true) {
  const isActive = forceNew !== undefined ? forceNew : !card.classList.contains('active');
  if (isActive) {
    card.classList.add('active');
    card.style.border = '1px solid #7dcfff';
    card.querySelector('div').style.color = '#c0caf5';
  } else {
    card.classList.remove('active');
    card.style.border = '1px solid #2a2a4a';
    card.querySelector('div').style.color = '#565f89';
  }
}

// ── Workflow animation ──

let animationTimer = null;

function animateWorkflow() {
  const steps = PANEL.querySelectorAll('.opt-wf-step');
  const arrows = PANEL.querySelectorAll('.opt-wf-arrow');

  // Reset
  steps.forEach(s => {
    s.style.background   = '#1a1a2e';
    s.style.border       = '1px solid #2a2a4a';
    s.style.color        = '#c0caf5';
  });
  arrows.forEach(a => { a.style.color = '#2a2a4a'; });

  let i = 0;
  const max = steps.length;
  const highlight = (idx) => {
    if (idx >= max) return;
    steps[idx].style.background   = 'rgba(125,207,255,0.12)';
    steps[idx].style.border       = '1px solid #7dcfff';
    steps[idx].style.color        = '#7dcfff';
    // Animate arrow before it
    if (idx > 0 && arrows[idx - 1]) {
      arrows[idx - 1].style.color    = '#7dcfff';
    }
    // Animate arrow after it
    if (idx < max - 1 && arrows[idx]) {
      arrows[idx].style.color = '#7dcfff';
    }
  };

  highlight(0);
  let step = 1;
  animationTimer = setInterval(() => {
    highlight(step);
    step++;
    if (step >= max) {
      clearInterval(animationTimer);
      animationTimer = null;
    }
  }, 350);
}

// ── Save config ──

async function saveConfig() {
  const metrics = [];
  METRIC_CARDS.forEach(card => {
    if (card.classList.contains('active')) metrics.push(card.dataset.metric);
  });

  const scanSources = {};
  document.querySelectorAll('.opt-metric-card[data-source]').forEach(card => {
    scanSources[card.dataset.source] = card.classList.contains('active');
  });

  const payload = {
    mode: currentMode,
    user_feedback: currentFeedback,
    intensity: currentIntensity,
    metrics,
    scan_sources: scanSources,
    models: {
      analyzer:  MODEL_ANALYZER.value,
      proposer:  MODEL_PROPOSER.value,
      validator: MODEL_VALIDATOR.value,
    },
    scope: {
      app_min_sample:  parseInt(SCOPE_APP_SAMPLE.value)  || 100,
      app_min_age:     parseInt(SCOPE_APP_AGE.value)     || 14,
      user_min_sample: parseInt(SCOPE_USER_SAMPLE.value) || 10,
      user_min_age:    parseInt(SCOPE_USER_AGE.value)    || 3,
    },
    schedule: {
      interval:         SCHEDULE_INTVL.value,
      min_interactions: parseInt(SCHEDULE_MIN.value) || 10,
    },
    notifications: {
      notify_user: NOTIFY_USER.checked,
      notify_devs: NOTIFY_DEVS.checked,
      channel:     NOTIFY_CHANNEL.value,
    },
  };

  try {
    const resp = await _authFetch(apiPath('/admin/settings/optimizer'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (resp.ok) {
      showStatus('✓ Saved', '#9ece6a');
      // Cache for offline
      localStorage.setItem('optimizer_config', JSON.stringify(payload));
    } else {
      showStatus('✗ Save failed', '#fb4934');
      // Save to localStorage as fallback
      localStorage.setItem('optimizer_config', JSON.stringify(payload));
      showStatus('Saved locally (backend not ready)', '#e0af68');
    }
  } catch {
    // Backend not ready — save to localStorage
    localStorage.setItem('optimizer_config', JSON.stringify(payload));
    showStatus('Saved locally', '#e0af68');
  }
}

// ── Run Now ──

async function runNow() {
  BTN_RUN_NOW.textContent = '⏳ Running...';
  BTN_RUN_NOW.disabled = true;
  animateWorkflow();

  try {
    const resp = await _authFetch(apiPath('/admin/optimizer/run'), { method: 'POST' });
    if (resp.ok) {
      showStatus('✓ Optimizer run started', '#9ece6a');
    } else {
      showStatus('Run started (backend pending)', '#e0af68');
    }
  } catch {
    // Backend not ready — show animation, log intent
    setTimeout(() => {
      showStatus('Backend not ready — simulated run', '#e0af68');
      BTN_RUN_NOW.textContent = '▶ Run Now';
      BTN_RUN_NOW.disabled = false;
    }, 3000);
    return;
  }

  setTimeout(() => {
    BTN_RUN_NOW.textContent = '▶ Run Now';
    BTN_RUN_NOW.disabled = false;
  }, 3000);
}

// ── Event wiring ──

function _init() {
  // Menu item
  MENU_ITEM.addEventListener('click', (e) => {
    e.stopPropagation();
    // Close the dropdown
    document.getElementById('settings-dropdown-menu').style.display = 'none';
    openModal();
  });

  // Close
  CLOSE_BTN.addEventListener('click', closeModal);
  BACKDROP.addEventListener('click', closeModal);

  // Mode
  MODE_LIVE.addEventListener('click', () => setMode('live'));
  MODE_SCHEDULED.addEventListener('click', () => setMode('scheduled'));

  // Feedback
  FEEDBACK_ALWAYS.addEventListener('click', () => setFeedback('always'));
  FEEDBACK_FAILURE.addEventListener('click', () => setFeedback('failure'));
  FEEDBACK_NEVER.addEventListener('click', () => setFeedback('never'));

  // Intensity
  INTENSITY.addEventListener('input', () => setIntensity(parseInt(INTENSITY.value)));

  // Metric cards
  METRIC_CARDS.forEach(card => {
    card.addEventListener('click', () => toggleMetric(card));
  });

  // Activity log toggle
  TOGGLE_LOG_BTN.addEventListener('click', () => {
    const shown = ACTIVITY_LOG.style.display !== 'none';
    ACTIVITY_LOG.style.display = shown ? 'none' : 'block';
    TOGGLE_LOG_BTN.textContent = shown ? 'Show ▾' : 'Hide ▴';
  });

  // Prompt templates
  const togglePromptsBtn = document.getElementById('opt-toggle-prompts');
  const promptTemplates = document.getElementById('opt-prompt-templates');
  const promptList = document.getElementById('opt-prompt-list');
  const promptEditor = document.getElementById('opt-prompt-editor');
  const promptSaveBtn = document.getElementById('opt-prompt-save');
  const promptResetBtn = document.getElementById('opt-prompt-reset');
  const promptStatus = document.getElementById('opt-prompt-status');
  let currentTemplateFile = '';
  let originalContent = '';

  togglePromptsBtn.addEventListener('click', () => {
    const shown = promptTemplates.style.display !== 'none';
    promptTemplates.style.display = shown ? 'none' : 'block';
    togglePromptsBtn.textContent = shown ? 'Show ▾' : 'Hide ▴';
    if (!shown) _loadPromptList();
  });

  async function _loadPromptList() {
    try {
      const resp = await _authFetch(apiPath('/admin/optimizer/prompts'));
      if (!resp.ok) return;
      const data = await resp.json();
      const templates = data.templates || [];
      promptList.innerHTML = templates.map(t => {
        const isFirst = t === templates[0];
        return `<button class="opt-prompt-tab" data-file="${t}" style="padding:4px 10px;border:1px solid ${isFirst ? '#7dcfff' : '#2a2a4a'};border-radius:4px;background:${isFirst ? 'rgba(125,207,255,0.08)' : 'transparent'};color:${isFirst ? '#c0caf5' : '#565f89'};cursor:pointer;font-size:10px;font-family:inherit;">${t.replace('-prompt.md','')}</button>`;
      }).join('');
      promptList.querySelectorAll('.opt-prompt-tab').forEach(btn => {
        btn.addEventListener('click', () => _loadTemplate(btn.dataset.file));
      });
      if (templates.length > 0) _loadTemplate(templates[0]);
    } catch {}
  }

  async function _loadTemplate(filename) {
    try {
      const resp = await _authFetch(apiPath(`/admin/optimizer/prompts/${filename}`));
      if (!resp.ok) return;
      const data = await resp.json();
      currentTemplateFile = filename;
      originalContent = data.content || '';
      promptEditor.value = originalContent;
      promptEditor.readOnly = false;
      // Highlight active tab
      promptList.querySelectorAll('.opt-prompt-tab').forEach(btn => {
        const active = btn.dataset.file === filename;
        btn.style.border = active ? '1px solid #7dcfff' : '1px solid #2a2a4a';
        btn.style.background = active ? 'rgba(125,207,255,0.08)' : 'transparent';
        btn.style.color = active ? '#c0caf5' : '#565f89';
      });
    } catch {
      promptStatus.textContent = 'Failed to load';
    }
  }

  promptSaveBtn.addEventListener('click', async () => {
    if (!currentTemplateFile) return;
    try {
      const resp = await _authFetch(apiPath(`/admin/optimizer/prompts/${currentTemplateFile}`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: promptEditor.value }),
      });
      if (resp.ok) {
        originalContent = promptEditor.value;
        promptStatus.textContent = 'Saved ✓';
        promptStatus.style.color = '#9ece6a';
      } else {
        promptStatus.textContent = 'Save failed';
        promptStatus.style.color = '#fb4934';
      }
    } catch {
      promptStatus.textContent = 'Save failed';
      promptStatus.style.color = '#fb4934';
    }
  });

  promptResetBtn.addEventListener('click', () => {
    if (originalContent) {
      promptEditor.value = originalContent;
      promptStatus.textContent = 'Reset ✓';
      promptStatus.style.color = '#e0af68';
    }
  });

  // Save
  BTN_SAVE.addEventListener('click', saveConfig);

  // Run Now
  BTN_RUN_NOW.addEventListener('click', runNow);

  // View History (placeholder)
  BTN_HISTORY.addEventListener('click', () => {
    showStatus('History view coming soon', '#e0af68');
  });

  // Keyboard: ESC closes
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && MODAL.style.display !== 'none') {
      closeModal();
    }
  });
}

// ── Export (called by main.js) ──

export function initOptimizer() {
  _init();
}

// Auto-init when loaded directly
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _init);
} else {
  _init();
}
