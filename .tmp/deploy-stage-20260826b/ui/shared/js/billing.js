'use strict';

/**
 * Billing runtime + shared helpers.
 *
 * This module owns the GLOBAL billing chrome every user sees:
 *   - the header credit-balance pill,
 *   - the buy-credits / subscribe modal,
 *   - the HTTP 402 paywall interceptor.
 * It also EXPORTS the small generic builders the per-agent Monetization tab
 * (ui/main-panel/agents/billing/agent-billing.js) reuses. Optional billing
 * add-on views may import the same helpers.
 */

import { app } from './state.js';
import { authHeaders } from './left-login.js';

export const STRATEGIES = [
  { value: 'free',          label: 'Free' },
  { value: 'credits',       label: 'Credits' },
  { value: 'trial',         label: 'Trial' },
  { value: 'trial,credits', label: 'Trial + credits' },
];

const _CACHE = { processors: null };

// ── Utilities (exported for the panel modules) ─────────────────────────────

export function _userId() {
  return app && app.currentUserId ? app.currentUserId : (localStorage.getItem('auth_user_id') || '');
}

export function _qs(params) {
  return Object.entries(params)
    .filter(([_, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join('&');
}

export async function _api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}), ...authHeaders() };
  const r = await fetch(path, { ...opts, headers });
  if (!r.ok) {
    let detail = r.statusText;
    try { const j = await r.json(); detail = j.detail || j.error || detail; } catch (_) {}
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  if (r.status === 204) return null;
  return await r.json();
}

export function _cents(s) {
  if (s == null || s === '') return 0;
  const n = Math.round(parseFloat(s) * 100);
  return Number.isFinite(n) ? n : 0;
}

export function _fmt(c) {
  if (c == null) return '$0.00';
  return `$${(c / 100).toFixed(2)}`;
}

export function _el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'style' && typeof v === 'object') Object.assign(e.style, v);
    else if (k === 'class') e.className = v;
    else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
    else if (v === false || v == null) continue;
    else e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return e;
}

export async function _loadProcessors() {
  if (_CACHE.processors) return _CACHE.processors;
  try {
    const data = await _api('/api/v1/billing/processors');
    _CACHE.processors = data.processors || [];
  } catch (_) { _CACHE.processors = []; }
  return _CACHE.processors;
}

// ── Shared form builders (used by both panels) ─────────────────────────────

// Cost-based pricing knobs: credits are consumed as the platform's real
// provider cost × a multiplier (inherited models only). The image estimate
// covers providers that report no usage (OpenAI /images, Stability, …).
export function _buildCostFields(values) {
  const wrap = _el('div', {});
  wrap.appendChild(_el('div', {
    style: { fontSize: '11px', fontWeight: '600', color: 'var(--fg-2)', marginBottom: '6px', lineHeight: '1.4' },
  }, 'Credits are spent on inherited (platform-key) models as provider cost × multiplier. Users with their own LLM key are free.'));
  const inputs = {};
  for (const [key, lbl, defaultVal, step, min] of [
    ['cost_multiplier', 'Cost multiplier ×', 1.0, '0.1', '0'],
    ['min_charge_cents', 'Min charge (¢)', 1, '1', '0'],
    ['flat_image_cost_usd', 'Per-image estimate ($)', 0.01, '0.01', '0'],
  ]) {
    const i = _el('input', { type: 'number', min, step,
                               value: String(values[key] ?? defaultVal), style: _inputStyle() });
    inputs[key] = i;
    wrap.appendChild(_labelled(lbl, i, { compact: true }));
  }
  return {
    el: wrap,
    read: () => Object.fromEntries(Object.entries(inputs).map(([k, e]) => [k, parseFloat(e.value) || 0])),
  };
}

export function _labelled(label, input, opts = {}) {
  const wrap = _el('div', { style: { flex: '1' } });
  wrap.appendChild(_el('div', {
    style: { fontSize: opts.compact ? '10px' : '11px', color: 'var(--fg-3)', marginBottom: '2px' },
  }, label));
  wrap.appendChild(input);
  return wrap;
}

export function _inputStyle() {
  return {
    width: '100%', padding: '6px 8px', fontSize: '12px',
    background: 'var(--bg-1)', color: 'var(--fg-1)',
    border: '1px solid var(--border)', borderRadius: '4px',
  };
}

export function _selectStyle() {
  return { ..._inputStyle(), padding: '6px 8px' };
}

// Re-evaluate billing access for a (user, agent) via the read-only /access
// endpoint (never raises). Returns the AccessDecision shape. Used by the
// trial-ended panel's "Check again" button after the user buys credits or
// connects their own LLM key.
export async function _checkAccess(agentId) {
  const uid = _userId();
  if (!uid || !agentId) return { allow: true, reason: 'allow', detail: 'no-context' };
  try {
    return await _api(`/api/v1/billing/access?${_qs({ user_id: uid, agent_id: agentId })}`);
  } catch (e) {
    return { allow: false, reason: 'check_failed', detail: String(e.message || e) };
  }
}

// ── Header pill: live credit balance ────────────────────────────────────

async function _refreshBalancePill() {
  const pill = document.getElementById('billing-balance-pill');
  if (!pill) return;
  const uid = _userId();
  if (!uid) { pill.style.display = 'none'; return; }
  try {
    const data = await _api(`/api/v1/billing/wallet?${_qs({ user_id: uid })}`);
    const avail = data?.available_cents ?? data?.balance_cents ?? 0;
    pill.style.display = '';
    pill.textContent = `${_fmt(avail)} credit`;
    pill.title = `Available: ${_fmt(avail)}   Held: ${_fmt(data?.hold_cents || 0)}`;
    // A positive balance means a trial-expired gate no longer applies — clear
    // the block so the composer re-enables after the user buys credits.
    if (avail > 0 && app._billingBlocked && app._billingBlocked.reason === 'trial_expired') {
      app._billingBlocked = null;
      if (typeof app.applyChatGate === 'function') app.applyChatGate();
    }
  } catch (e) {
    pill.style.display = 'none';
  }
}

function _injectBalancePill() {
  if (document.getElementById('billing-balance-pill')) return;
  // Try a few likely mount points; fall back to fixed positioning.
  const candidates = ['header .header-right', 'header', '#app-header', 'body'];
  let mount = null;
  for (const sel of candidates) {
    const el = document.querySelector(sel);
    if (el) { mount = el; break; }
  }
  if (!mount) return;
  const pill = _el('button', {
    id: 'billing-balance-pill',
    class: 'billing-balance-pill',
    title: 'Your credit balance',
    style: {
      display: 'none',
      marginLeft: '8px',
      padding: '4px 10px',
      borderRadius: '999px',
      border: '1px solid var(--border)',
      background: 'var(--accent-soft)',
      color: 'var(--fg-1)',
      font: '500 12px/1.2 system-ui, sans-serif',
      cursor: 'pointer',
    },
    onclick: () => _openBuyCreditsModal(),
  }, '—');
  mount.appendChild(pill);
}

// ── Buy-credits modal ──────────────────────────────────────────────────

async function _openBuyCreditsModal({ agentId = null, reason = null, accepted = null } = {}) {
  const procs = await _loadProcessors();
  const filtered = accepted && accepted.length
    ? procs.filter(p => accepted.includes(p.name) && p.configured)
    : procs.filter(p => p.configured);

  const overlay = _el('div', {
    class: 'billing-modal-overlay',
    style: {
      position: 'fixed', inset: '0', background: 'rgba(0,0,0,0.6)', zIndex: '9999', /* modal scrim — intentionally black */
      display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '20px',
    },
    onclick: (e) => { if (e.target === overlay) overlay.remove(); },
  });
  const card = _el('div', {
    class: 'billing-modal-card',
    style: {
      background: 'var(--bg-1)', color: 'var(--fg-1)',
      borderRadius: '12px', padding: '24px', minWidth: '320px', maxWidth: '440px',
      boxShadow: '0 18px 60px rgba(var(--shadow-rgb), 0.5)',
    },
  });

  let title = 'Buy credits';
  if (reason === 'needs_credits') title = 'Add credits to keep chatting';
  else if (reason === 'trial_expired') title = 'Trial ended — keep chatting';
  else if (reason === 'needs_subscription') title = 'Subscribe to chat with this agent';
  card.appendChild(_el('h2', { style: { margin: '0 0 8px', fontSize: '18px' } }, title));

  if (filtered.length === 0) {
    card.appendChild(_el('p', { style: { color: 'var(--fg-3)' } },
      'No payment processors are enabled. Ask the app admin to enable one.'));
    card.appendChild(_el('button', {
      class: 'billing-btn',
      style: { marginTop: '12px' },
      onclick: () => overlay.remove(),
    }, 'Close'));
    overlay.appendChild(card);
    document.body.appendChild(overlay);
    return;
  }

  if (reason === 'needs_subscription' && agentId) {
    // Subscription flow — one click per processor.
    const subscriptionProcessors = filtered.filter(p => p.features?.subscriptions);
    card.appendChild(_el('p', { style: { color: 'var(--fg-2)', margin: '0 0 12px' } },
      subscriptionProcessors.length === 0
        ? 'No enabled payment service supports recurring subscriptions.'
        : 'Pick a payment method to subscribe:'));
    for (const p of subscriptionProcessors) {
      card.appendChild(_el('button', {
        class: 'billing-btn',
        style: { width: '100%', margin: '6px 0', padding: '10px' },
        onclick: async () => {
          try {
            const r = await _api(`/api/v1/billing/subscribe/${encodeURIComponent(agentId)}`, {
              method: 'POST', body: JSON.stringify({ user_id: _userId(), processor: p.name }),
            });
            if (r?.redirect_url) window.location.href = r.redirect_url;
          } catch (e) { alert('Subscription failed: ' + e.message); }
        },
      }, `Subscribe with ${p.display_name}`));
    }
  } else {
    // Credit-pack flow (also used for an expired trial — buy credits to keep
    // chatting; the accepted processors, Bitcoin here, decide the pay buttons).
    card.appendChild(_el('p', { style: { color: 'var(--fg-2)', margin: '0 0 12px' } },
      reason === 'trial_expired'
        ? 'Your trial has ended. Buy credits to keep chatting with this agent.'
        : 'Buy credits to spend on agents.'));

    const amountRow = _el('div', { style: { display: 'flex', gap: '6px', flexWrap: 'wrap', marginBottom: '10px' } });
    let selected = 1000;
    const amountInput = _el('input', {
      type: 'number', min: '1', step: '1', value: '10.00',
      style: { width: '100%', padding: '8px', fontSize: '14px',
                background: 'var(--bg-2)', border: '1px solid var(--border)',
                color: 'var(--fg-1)', borderRadius: '6px' },
    });
    for (const usd of [5, 10, 25, 50]) {
      const b = _el('button', {
        class: 'billing-chip',
        style: { padding: '6px 12px', borderRadius: '999px',
                  border: '1px solid var(--border)',
                  background: 'var(--bg-2)', cursor: 'pointer',
                  color: 'var(--fg-1)' },
        onclick: () => { amountInput.value = usd.toFixed(2); selected = usd * 100; },
      }, `$${usd}`);
      amountRow.appendChild(b);
    }
    card.appendChild(amountRow);
    card.appendChild(amountInput);

    card.appendChild(_el('p', { style: { fontSize: '12px', color: 'var(--fg-3)', margin: '12px 0 8px' } },
      'Pay with:'));
    for (const p of filtered) {
      card.appendChild(_el('button', {
        class: 'billing-btn',
        style: { width: '100%', margin: '6px 0', padding: '10px',
                  background: 'var(--accent)', color: 'white', /* contrast ink on accent fill */
                  border: 'none', borderRadius: '8px', cursor: 'pointer',
                  fontSize: '14px' },
        onclick: async () => {
          const cents = _cents(amountInput.value);
          if (cents <= 0) return alert('Enter a positive amount.');
          try {
            const r = await _api(`/api/v1/billing/wallet/purchase`, {
              method: 'POST', body: JSON.stringify({
                user_id: _userId(), amount_cents: cents,
                processor: p.name, currency: 'usd',
              }),
            });
            if (r?.redirect_url) window.location.href = r.redirect_url;
          } catch (e) { alert('Checkout failed: ' + e.message); }
        },
      }, `Pay with ${p.display_name}`));
    }
  }

  card.appendChild(_el('button', {
    style: { marginTop: '12px', padding: '6px 10px',
              background: 'transparent', border: '1px solid var(--border)',
              borderRadius: '6px', color: 'var(--fg-2)', cursor: 'pointer' },
    onclick: () => overlay.remove(),
  }, 'Cancel'));

  overlay.appendChild(card);
  document.body.appendChild(overlay);
}

// ── HTTP 402 interceptor: pop the paywall when chat is blocked ──────────

function _install402Interceptor() {
  const origFetch = window.fetch;
  if (origFetch._billingPatched) return;
  const patched = async (...args) => {
    const resp = await origFetch.apply(window, args);
    if (resp.status === 402) {
      try {
        const cloned = resp.clone();
        const j = await cloned.json();
        const d = j && j.detail ? j.detail : j;
        const detail = {
          agentId: d.agent_id || null,
          reason: d.reason || 'needs_payment',
          accepted: d.accepted_processors || null,
        };
        window.dispatchEvent(new CustomEvent('webagent-billing-blocked', { detail }));
        // An exhausted trial is a terminal composer state, not a modal loop.
        // The chat runtime disables the matching agent's input and explains how
        // to regain access in its placeholder.
        if (detail.reason !== 'trial_expired') {
          _openBuyCreditsModal(detail);
        }
      } catch (_) {
        _openBuyCreditsModal();
      }
    }
    return resp;
  };
  patched._billingPatched = true;
  window.fetch = patched;
}

// ── Init ───────────────────────────────────────────────────────────────

export function initBilling() {
  _injectBalancePill();
  _install402Interceptor();
  // One initial fetch for the header pill so it shows on page load.
  _refreshBalancePill();
  // Polling deferred to startBilling() — runs only when billing UI is visible.
}

/** Start billing polling. Called when billing UI becomes visible
 *  (admin billing view or agent monetization tab). */
export function startBilling() {
  if (!window.__billingInterval) {
    _refreshBalancePill();
    window.__billingInterval = setInterval(_refreshBalancePill, 60000);
  }
  // Refresh after the user returns from a Stripe/PayPal redirect.
  try {
    const url = new URL(window.location.href);
    if (url.searchParams.get('billing') === 'success' || url.searchParams.get('subscribed') === '1') {
      setTimeout(_refreshBalancePill, 1500);
    }
  } catch (_) {}
}

/** Stop billing polling. Called when billing UI is hidden. */
export function stopBilling() {
  if (window.__billingInterval) {
    clearInterval(window.__billingInterval);
    window.__billingInterval = null;
  }
}

// Expose the global runtime for non-module callers.
window.AppBilling = {
  refreshBalance: _refreshBalancePill,
  openBuyCreditsModal: _openBuyCreditsModal,
  checkAccess: _checkAccess,
};
