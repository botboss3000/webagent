'use strict';

// Render Recorder header control.
//
// App Settings ▸ App Functions owns only this control's availability. Once the
// app function is enabled, this admin-only header button is the real capture
// switch: click ON, reproduce the chat irregularity, then click OFF. The client
// recorder also enforces its own configured safety ceiling.

import { apiPath } from './config.js';
import { authHeaders } from './left-login.js';

const STATUS_URL = '/api/v1/recordings/enabled';
let _button = null;
let _control = null;
let _available = false;
let _enabled = false;
let _busy = false;
let _isAdminUser = false;
let _autoStopTimer = null;
let _countdownTimer = null;
let _endsAt = 0;
let _config = {};

function _isAdmin() {
  return _isAdminUser || document.body.classList.contains('is-admin');
}

function _render() {
  if (!_button || !_control) return;
  const visible = _isAdmin() && _available;
  _control.hidden = !visible;
  _button.disabled = _busy;
  _button.classList.toggle('active', _enabled);
  _button.classList.toggle('is-busy', _busy);
  _control.classList.toggle('is-recording', _enabled);
  document.body.classList.toggle('render-recorder-active', visible && _enabled);
  _button.setAttribute('aria-pressed', String(_enabled));
  _button.setAttribute('aria-busy', String(_busy));
  const durationMs = _durationMs(_config);
  const seconds = Math.round(durationMs / 1000);
  let title = durationMs > 0
    ? `Start a chat render recording (automatic stop after ${seconds}s)`
    : 'Start a chat render recording (no automatic time limit)';
  if (_busy) title = _enabled ? 'Stopping render recording…' : 'Starting render recording…';
  else if (_enabled) title = 'Render recording is on — click to stop';
  _button.title = title;
  _button.setAttribute('aria-label', title);
  const live = _button.querySelector('.render-recorder-live');
  if (live) live.textContent = title;
  _renderPopover();
}

function _durationMs(config) {
  const configured = Number(config && config.capture_duration_ms);
  return Number.isFinite(configured) ? Math.max(0, configured) : 30000;
}

function _renderPopover() {
  if (!_control) return;
  const status = _control.querySelector('.render-recorder-status-title');
  const detail = _control.querySelector('.render-recorder-status-detail');
  const limits = _control.querySelector('.render-recorder-status-limits');
  const countdown = _control.querySelector('.render-recorder-countdown');
  const durationMs = _durationMs(_config);
  const durationSeconds = Math.round(durationMs / 1000);
  if (status) status.textContent = _busy ? (_enabled ? 'Stopping…' : 'Starting…') : (_enabled ? 'Recording UI changes' : 'Ready to record');
  if (detail) detail.textContent = _enabled
    ? 'Complete the test normally. This panel stays visible while capture is active.'
    : 'Toggle on, reproduce the UI issue, then toggle off.';
  if (limits) {
    const scope = _config.capture_whole_page ? 'Whole page' : 'Chat only';
    const megabytes = Math.max(0.25, Number(_config.capture_max_bytes || 2000000) / 1000000);
    const timeLimit = durationMs > 0 ? `${durationSeconds}s automatic stop` : 'No time limit';
    const backgrounds = _config.capture_decorative_animations === true
      ? 'Backgrounds included'
      : 'Backgrounds excluded';
    limits.textContent = `${scope} · ${timeLimit} · ${megabytes.toLocaleString(undefined, { maximumFractionDigits: 2 })} MB limit · ${backgrounds}`;
  }
  if (countdown) {
    const remaining = _enabled && _endsAt ? Math.max(0, Math.ceil((_endsAt - Date.now()) / 1000)) : 0;
    countdown.textContent = _enabled ? (_endsAt ? `${remaining}s left` : 'No limit') : '';
  }
}

function _stopCountdown() {
  if (_countdownTimer) clearInterval(_countdownTimer);
  _countdownTimer = null;
  _endsAt = 0;
}

function _scheduleAutoStop(durationMs) {
  if (!(durationMs > 0)) {
    _endsAt = 0;
    _stopCountdown();
    return;
  }
  _endsAt = Date.now() + durationMs;
  if (!_countdownTimer) _countdownTimer = setInterval(_renderPopover, 1000);
  const arm = () => {
    const remaining = _endsAt - Date.now();
    if (remaining <= 0) {
      _autoStopTimer = null;
      _toggle(true);
      return;
    }
    _autoStopTimer = setTimeout(arm, Math.min(remaining, 2147483647));
  };
  arm();
}

function _broadcast(config) {
  try {
    window.dispatchEvent(new CustomEvent('render-recorder-changed', {
      detail: { available: _available, enabled: _enabled, config: config || {} },
    }));
  } catch (_) {}
}

function _applyStatus(data, broadcast) {
  if (!data || typeof data !== 'object') return;
  _available = data.available === true;
  _enabled = data.enabled === true;
  _config = (data.config && typeof data.config === 'object') ? data.config : _config;
  if (!_enabled) {
    if (_autoStopTimer) clearTimeout(_autoStopTimer);
    _autoStopTimer = null;
    _stopCountdown();
  } else if (_enabled && _available && !_autoStopTimer) {
    const durationMs = _durationMs(_config);
    _scheduleAutoStop(durationMs);
  }
  _render();
  if (broadcast) _broadcast(data.config);
}

async function _fetchStatus(broadcast) {
  if (!_isAdmin()) { _render(); return; }
  try {
    const response = await fetch(apiPath(STATUS_URL), { headers: authHeaders(), cache: 'no-store' });
    if (!response.ok) {
      if (response.status === 403) {
        _available = false;
        _enabled = false;
        if (_autoStopTimer) clearTimeout(_autoStopTimer);
        _autoStopTimer = null;
        _stopCountdown();
      }
      _render();
      return;
    }
    _applyStatus(await response.json(), broadcast);
  } catch (_) {
    _render();
  }
}

async function _toggle(forceOff = false) {
  if (!_isAdmin() || _busy || (!_available && !forceOff)) return;
  const desired = forceOff ? false : !_enabled;
  _busy = true;
  _render();
  try {
    const response = await fetch(apiPath(STATUS_URL), {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      body: JSON.stringify({ enabled: desired }),
    });
    if (!response.ok) {
      let detail = response.statusText || `HTTP ${response.status}`;
      try { const body = await response.json(); detail = body.detail || detail; } catch (_) {}
      console.error('render recorder toggle failed: ' + detail);
      await _fetchStatus(false);
      return;
    }
    _applyStatus(await response.json(), true);
  } catch (error) {
    console.error('render recorder toggle failed: ' + (error.message || error));
    await _fetchStatus(false);
  } finally {
    _busy = false;
    _render();
  }
}

function initRenderRecorderToggle() {
  _control = document.getElementById('render-recorder-control');
  _button = document.getElementById('render-recorder-toggle');
  if (!_button || !_control) return;

  document.addEventListener('click', (event) => {
    if (event.target && typeof event.target.closest === 'function'
        && event.target.closest('#render-recorder-toggle')) _toggle(false);
  });

  window.addEventListener('admin-status-loaded', (event) => {
    _isAdminUser = !!(event.detail && event.detail.is_admin);
    _render();
    if (_isAdminUser) _fetchStatus(false);
  });

  // App Functions dispatches this after its visibility switch persists. Fetch
  // authoritative state so disabling immediately hides the header control and
  // enabling immediately reveals it without a reload.
  window.addEventListener('app-function-changed', (event) => {
    if (event.detail && event.detail.id === 'render_recorder') _fetchStatus(true);
  });

  _render();
  _fetchStatus(false);
  setInterval(() => { if (_isAdmin()) _fetchStatus(false); }, 5000);
}

initRenderRecorderToggle();
