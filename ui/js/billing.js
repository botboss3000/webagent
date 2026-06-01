'use strict';

/**
 * Billing UI.
 *
 * - Exposes window.AppBilling.renderMonetizationPanel(scope, container, opts)
 *   where scope is 'platform' or 'agent:<id>'. agents.js calls this for the
 *   per-agent Monetization tab; the App Admin page wires up the platform scope.
 * - Renders a credit balance pill in the header (best-effort).
 * - Listens for HTTP 402 responses from chat endpoints and pops a paywall.
 */

import { app } from './state.js';

const STRATEGIES = [
  { value: 'free',         label: 'Free' },
  { value: 'credits',      label: 'Prepaid credits' },
  { value: 'per_message',  label: 'Per message' },
  { value: 'per_token',    label: 'Per token' },
  { value: 'subscription', label: 'Monthly subscription' },
  { value: 'trial',        label: 'Trial only' },
];

const _CACHE = {
  platform: null,
  processors: null,
};

// ── Utilities ────────────────────────────────────────────────────────────

function _userId() {
  return app && app.currentUserId ? app.currentUserId : (localStorage.getItem('auth_user_id') || '');
}

function _qs(params) {
  return Object.entries(params)
    .filter(([_, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join('&');
}

async function _api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  const tok = localStorage.getItem('auth_token');
  if (tok) headers['Authorization'] = `Bearer ${tok}`;
  const r = await fetch(path, { ...opts, headers });
  if (!r.ok) {
    let detail = r.statusText;
    try { const j = await r.json(); detail = j.detail || j.error || detail; } catch (_) {}
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  if (r.status === 204) return null;
  return await r.json();
}

function _cents(s) {
  if (s == null || s === '') return 0;
  const n = Math.round(parseFloat(s) * 100);
  return Number.isFinite(n) ? n : 0;
}

function _fmt(c) {
  if (c == null) return '$0.00';
  return `$${(c / 100).toFixed(2)}`;
}

function _el(tag, attrs = {}, ...children) {
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
      border: '1px solid var(--border, #444)',
      background: 'var(--accent-soft, rgba(80,140,255,0.12))',
      color: 'var(--fg-1, inherit)',
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
      position: 'fixed', inset: '0', background: 'rgba(0,0,0,0.6)', zIndex: '9999',
      display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '20px',
    },
    onclick: (e) => { if (e.target === overlay) overlay.remove(); },
  });
  const card = _el('div', {
    class: 'billing-modal-card',
    style: {
      background: 'var(--bg-1, #1c1c1f)', color: 'var(--fg-1, inherit)',
      borderRadius: '12px', padding: '24px', minWidth: '320px', maxWidth: '440px',
      boxShadow: '0 18px 60px rgba(0,0,0,0.5)',
    },
  });

  let title = 'Buy credits';
  if (reason === 'needs_credits') title = 'Add credits to keep chatting';
  else if (reason === 'trial_expired') title = 'Trial ended — keep chatting';
  else if (reason === 'needs_subscription') title = 'Subscribe to chat with this agent';
  card.appendChild(_el('h2', { style: { margin: '0 0 8px', fontSize: '18px' } }, title));

  if (filtered.length === 0) {
    card.appendChild(_el('p', { style: { color: 'var(--fg-3, #888)' } },
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
    card.appendChild(_el('p', { style: { color: 'var(--fg-2, #aaa)', margin: '0 0 12px' } },
      'Pick a payment method to subscribe:'));
    for (const p of filtered) {
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
    // Credit-pack flow
    card.appendChild(_el('p', { style: { color: 'var(--fg-2, #aaa)', margin: '0 0 12px' } },
      'Buy credits to spend on agents.'));

    const amountRow = _el('div', { style: { display: 'flex', gap: '6px', flexWrap: 'wrap', marginBottom: '10px' } });
    let selected = 1000;
    const amountInput = _el('input', {
      type: 'number', min: '1', step: '1', value: '10.00',
      style: { width: '100%', padding: '8px', fontSize: '14px',
                background: 'var(--bg-2, #2a2a2e)', border: '1px solid var(--border, #444)',
                color: 'var(--fg-1, inherit)', borderRadius: '6px' },
    });
    for (const usd of [5, 10, 25, 50]) {
      const b = _el('button', {
        class: 'billing-chip',
        style: { padding: '6px 12px', borderRadius: '999px',
                  border: '1px solid var(--border,#444)',
                  background: 'var(--bg-2,#2a2a2e)', cursor: 'pointer',
                  color: 'var(--fg-1, inherit)' },
        onclick: () => { amountInput.value = usd.toFixed(2); selected = usd * 100; },
      }, `$${usd}`);
      amountRow.appendChild(b);
    }
    card.appendChild(amountRow);
    card.appendChild(amountInput);

    card.appendChild(_el('p', { style: { fontSize: '12px', color: 'var(--fg-3,#888)', margin: '12px 0 8px' } },
      'Pay with:'));
    for (const p of filtered) {
      card.appendChild(_el('button', {
        class: 'billing-btn',
        style: { width: '100%', margin: '6px 0', padding: '10px',
                  background: 'var(--accent,#4a7cff)', color: 'white',
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
              background: 'transparent', border: '1px solid var(--border,#444)',
              borderRadius: '6px', color: 'var(--fg-2,#aaa)', cursor: 'pointer' },
    onclick: () => overlay.remove(),
  }, 'Cancel'));

  overlay.appendChild(card);
  document.body.appendChild(overlay);
}

async function _loadProcessors() {
  if (_CACHE.processors) return _CACHE.processors;
  try {
    const data = await _api('/api/v1/billing/processors');
    _CACHE.processors = data.processors || [];
  } catch (_) { _CACHE.processors = []; }
  return _CACHE.processors;
}

// ── Monetization panel (used by App Admin + per-agent tabs) ─────────────

async function renderMonetizationPanel(scope, container, opts = {}) {
  container.innerHTML = '';
  const isPlatform = scope === 'platform';
  const agentId = isPlatform ? null : (opts.agentId || scope.replace(/^agent:/, ''));

  container.appendChild(_el('div', {
    style: { fontSize: '12px', color: 'var(--fg-3,#888)', marginBottom: '12px', lineHeight: '1.5' },
  }, isPlatform
      ? 'Choose which monetization methods agent admins can use, set the platform fee, and pick which payment processors are available.'
      : 'Configure how this agent charges users. Only methods enabled by the app admin can be selected.'));

  const loading = _el('div', { style: { color: 'var(--fg-3,#888)' } }, 'Loading…');
  container.appendChild(loading);

  let cfg;
  let processors = [];
  try {
    const r = await _api(`/api/v1/billing/config?${_qs({
      user_id: _userId(), agent_id: agentId || '',
    })}`);
    cfg = r;
    processors = await _loadProcessors();
    _CACHE.platform = r.platform;
  } catch (e) {
    loading.textContent = `Failed to load billing config: ${e.message}`;
    return;
  }
  loading.remove();

  const platform = cfg.platform || {};
  const agent = cfg.agent || {};
  const effective = cfg.effective || {};
  const platformAllowedStrategies = platform.allowed_strategies || STRATEGIES.map(s => s.value);
  const platformAllowedProcessors = platform.allowed_processors || processors.map(p => p.name);

  const targetCfg = isPlatform ? platform : agent;
  const strategyOptions = isPlatform
    ? STRATEGIES
    : STRATEGIES.filter(s => s.value === 'free' || platformAllowedStrategies.includes(s.value));

  // ── Strategy select ──
  const stratWrap = _el('div', { style: { marginBottom: '14px' } });
  stratWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '4px' } }, 'Monetization method'));
  const stratSel = _el('select', { style: _selectStyle() });
  for (const s of strategyOptions) {
    const o = _el('option', { value: s.value }, s.label);
    if ((targetCfg.strategy || 'free') === s.value) o.selected = true;
    stratSel.appendChild(o);
  }
  stratWrap.appendChild(stratSel);
  container.appendChild(stratWrap);

  // ── Allowed strategies (platform only) ──
  if (isPlatform) {
    const allowedStratWrap = _el('div', { style: { marginBottom: '14px' } });
    allowedStratWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '4px' } },
      'Methods agent admins can enable'));
    const boxes = _el('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '8px' } });
    const checkboxes = {};
    for (const s of STRATEGIES.filter(s => s.value !== 'free')) {
      const c = _el('input', { type: 'checkbox' });
      c.checked = (platform.allowed_strategies || []).includes(s.value);
      checkboxes[s.value] = c;
      boxes.appendChild(_el('label', {
        style: { display: 'inline-flex', alignItems: 'center', gap: '4px',
                  fontSize: '13px', padding: '4px 8px', border: '1px solid var(--border,#444)',
                  borderRadius: '6px', cursor: 'pointer' },
      }, c, s.label));
    }
    allowedStratWrap.appendChild(boxes);
    container.appendChild(allowedStratWrap);
    container._allowedStrategyCheckboxes = checkboxes;
  }

  // ── Allowed processors (multi-select) ──
  const procWrap = _el('div', { style: { marginBottom: '14px' } });
  procWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '4px' } },
    isPlatform ? 'Payment processors users can pay with' : 'Payment methods this agent accepts'));
  const procRow = _el('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '8px' } });
  const procCheckboxes = {};
  const procPool = isPlatform ? processors : processors.filter(p => platformAllowedProcessors.includes(p.name));
  if (procPool.length === 0) {
    procRow.appendChild(_el('div', { style: { color: 'var(--fg-3,#888)', fontSize: '12px' } },
      isPlatform ? 'No processors configured. Set STRIPE_SECRET_KEY or PAYPAL_CLIENT_ID/SECRET in your env.' : 'No processors enabled by the app admin.'));
  }
  for (const p of procPool) {
    const c = _el('input', { type: 'checkbox' });
    c.checked = (targetCfg.allowed_processors || []).includes(p.name);
    procCheckboxes[p.name] = c;
    const dot = _el('span', {
      style: {
        display: 'inline-block', width: '8px', height: '8px',
        borderRadius: '50%', marginRight: '4px',
        background: p.configured ? '#4ade80' : '#888',
      },
    });
    procRow.appendChild(_el('label', {
      style: { display: 'inline-flex', alignItems: 'center', gap: '4px',
                fontSize: '13px', padding: '4px 8px', border: '1px solid var(--border,#444)',
                borderRadius: '6px', cursor: 'pointer' },
    }, c, dot, p.display_name + (p.configured ? '' : ' (not configured)')));
  }
  procWrap.appendChild(procRow);
  container.appendChild(procWrap);

  // ── Connect onboarding (per-agent only, for agent admins) ──
  if (!isPlatform) {
    const onboardWrap = _el('div', { style: { marginBottom: '14px', padding: '10px', background: 'var(--bg-2,#222)', borderRadius: '8px' } });
    onboardWrap.appendChild(_el('div', { style: { fontSize: '12px', fontWeight: '600', marginBottom: '6px' } }, 'Connect payouts'));
    const statusRow = _el('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '8px' } });
    onboardWrap.appendChild(statusRow);
    try {
      const stat = await _api(`/api/v1/billing/connect/status?${_qs({ user_id: _userId() })}`);
      const accounts = stat.accounts || [];
      for (const p of procPool) {
        const acct = accounts.find(a => a.processor === p.name);
        const complete = acct && acct.onboarding_complete;
        const b = _el('button', {
          class: 'billing-btn',
          style: { padding: '6px 10px', borderRadius: '6px', cursor: 'pointer',
                    border: '1px solid var(--border,#444)',
                    background: complete ? 'rgba(74,222,128,0.18)' : 'var(--bg-1,#1c1c1f)',
                    color: 'var(--fg-1,inherit)', fontSize: '12px' },
          onclick: async () => {
            try {
              const r = await _api('/api/v1/billing/connect/onboard', {
                method: 'POST',
                body: JSON.stringify({ user_id: _userId(), processor: p.name }),
              });
              if (r?.redirect_url) window.location.href = r.redirect_url;
            } catch (e) { alert('Onboarding failed: ' + e.message); }
          },
        }, `${p.display_name}: ${complete ? '✓ connected' : 'connect →'}`);
        statusRow.appendChild(b);
      }
    } catch (_) {}
    container.appendChild(onboardWrap);
  }

  // ── Rate card inputs (default-LLM vs BYO-LLM) ──
  const rateWrap = _el('div', { style: { marginBottom: '14px' } });
  rateWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '4px' } },
    'Rate card'));
  const rateGrid = _el('div', {
    style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px',
              padding: '10px', background: 'var(--bg-2,#222)', borderRadius: '8px' },
  });
  const defaultCard = _buildRateCard(targetCfg.rate_card_default_llm || {}, 'Default LLM (platform pays for tokens)');
  const byoCard = _buildRateCard(targetCfg.rate_card_byo_llm || {}, 'BYO LLM (agent brings own key)');
  rateGrid.appendChild(defaultCard.el);
  rateGrid.appendChild(byoCard.el);
  rateWrap.appendChild(rateGrid);
  container.appendChild(rateWrap);

  // ── Platform fee (platform only) ──
  let feePctInput = null, feeFlatInput = null;
  if (isPlatform) {
    const feeWrap = _el('div', { style: { marginBottom: '14px', display: 'flex', gap: '12px' } });
    feePctInput = _el('input', { type: 'number', step: '0.1', min: '0', max: '100',
                                  value: String(platform.platform_fee_pct || 0),
                                  style: _inputStyle() });
    feeFlatInput = _el('input', { type: 'number', step: '1', min: '0',
                                   value: String(platform.platform_fee_flat_cents || 0),
                                   style: _inputStyle() });
    feeWrap.appendChild(_labelled('Platform fee %', feePctInput));
    feeWrap.appendChild(_labelled('Platform flat fee (¢)', feeFlatInput));
    container.appendChild(feeWrap);
  }

  // ── Trial config ──
  const trial = targetCfg.trial_config || {};
  const trialWrap = _el('div', { style: { marginBottom: '14px', padding: '10px', background: 'var(--bg-2,#222)', borderRadius: '8px' } });
  trialWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '6px' } }, 'Trial period'));
  const trialGrid = _el('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '8px' } });
  const trialDays = _el('input', { type: 'number', min: '0', value: String(trial.days || 0), style: _inputStyle() });
  const trialMsgs = _el('input', { type: 'number', min: '0', value: String(trial.messages || 0), style: _inputStyle() });
  const trialToks = _el('input', { type: 'number', min: '0', value: String(trial.tokens || 0), style: _inputStyle() });
  trialGrid.appendChild(_labelled('Days', trialDays));
  trialGrid.appendChild(_labelled('Messages', trialMsgs));
  trialGrid.appendChild(_labelled('Tokens', trialToks));
  trialWrap.appendChild(trialGrid);
  container.appendChild(trialWrap);

  // ── Subscription price ──
  const subWrap = _el('div', { style: { marginBottom: '14px' } });
  const subPrice = _el('input', { type: 'number', min: '0', value: String(targetCfg.subscription_price_cents || 0), style: _inputStyle() });
  subWrap.appendChild(_labelled('Subscription price (¢ / month)', subPrice));
  container.appendChild(subWrap);

  // ── Exemptions ──
  container.appendChild(await _renderExemptionSection(isPlatform, agentId));

  // ── Save ──
  const saveBtn = _el('button', {
    class: 'billing-btn',
    style: { padding: '10px 16px', background: 'var(--accent,#4a7cff)', color: 'white',
              border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '14px', marginTop: '8px' },
    onclick: async () => {
      saveBtn.disabled = true;
      saveBtn.textContent = 'Saving…';
      try {
        const allowed_processors = Object.entries(procCheckboxes).filter(([_, c]) => c.checked).map(([n]) => n);
        const body = {
          user_id: _userId(),
          strategy: stratSel.value,
          allowed_processors,
          rate_card_default_llm: defaultCard.read(),
          rate_card_byo_llm: byoCard.read(),
          trial_config: {
            days: parseInt(trialDays.value, 10) || 0,
            messages: parseInt(trialMsgs.value, 10) || 0,
            tokens: parseInt(trialToks.value, 10) || 0,
          },
          subscription_price_cents: parseInt(subPrice.value, 10) || 0,
        };
        if (isPlatform) {
          body.allowed_strategies = Object.entries(container._allowedStrategyCheckboxes || {})
            .filter(([_, c]) => c.checked).map(([n]) => n);
          body.platform_fee_pct = parseFloat(feePctInput.value) || 0;
          body.platform_fee_flat_cents = parseInt(feeFlatInput.value, 10) || 0;
          await _api('/api/v1/billing/config/platform', { method: 'PUT', body: JSON.stringify(body) });
          _CACHE.platform = null;
        } else {
          await _api(`/api/v1/billing/config/agent/${encodeURIComponent(agentId)}`, {
            method: 'PUT', body: JSON.stringify(body),
          });
        }
        saveBtn.textContent = 'Saved ✓';
        setTimeout(() => { saveBtn.textContent = 'Save'; saveBtn.disabled = false; }, 1500);
      } catch (e) {
        alert('Save failed: ' + e.message);
        saveBtn.textContent = 'Save';
        saveBtn.disabled = false;
      }
    },
  }, 'Save');
  container.appendChild(saveBtn);
}

function _buildRateCard(values, label) {
  const wrap = _el('div', {});
  wrap.appendChild(_el('div', { style: { fontSize: '11px', fontWeight: '600', color: 'var(--fg-2,#aaa)', marginBottom: '6px' } }, label));
  const inputs = {};
  for (const [key, lbl, defaultVal] of [
    ['per_message_cents', 'Per msg (¢)', 0],
    ['per_1k_input_token_cents', 'Per 1k in tok (¢)', 0],
    ['per_1k_output_token_cents', 'Per 1k out tok (¢)', 0],
    ['credits_per_message_cents', 'Credits/msg (¢)', 0],
    ['credits_per_1k_input_tokens', 'Credits/1k in (¢)', 0],
    ['credits_per_1k_output_tokens', 'Credits/1k out (¢)', 0],
    ['llm_markup_pct', 'LLM markup %', 0],
    ['min_charge_cents', 'Min charge (¢)', 1],
  ]) {
    const i = _el('input', { type: 'number', min: '0', step: '0.1',
                               value: String(values[key] ?? defaultVal), style: _inputStyle() });
    inputs[key] = i;
    wrap.appendChild(_labelled(lbl, i, { compact: true }));
  }
  return {
    el: wrap,
    read: () => Object.fromEntries(Object.entries(inputs).map(([k, e]) => [k, parseFloat(e.value) || 0])),
  };
}

async function _renderExemptionSection(isPlatform, agentId) {
  const wrap = _el('div', { style: { marginBottom: '14px', padding: '10px',
                                       background: 'var(--bg-2,#222)', borderRadius: '8px' } });
  wrap.appendChild(_el('div', { style: { fontSize: '12px', fontWeight: '600', marginBottom: '6px' } },
    isPlatform ? 'Exemptions (no-pay users / agents)' : 'Collaborator exemptions (no-pay users for this agent)'));

  const listDiv = _el('div', { style: { marginBottom: '8px' } });
  wrap.appendChild(listDiv);

  async function reload() {
    listDiv.innerHTML = '';
    try {
      const r = await _api(`/api/v1/billing/exemptions?${_qs({ user_id: _userId(), agent_id: isPlatform ? '' : (agentId || '') })}`);
      const rows = r.exemptions || [];
      if (rows.length === 0) {
        listDiv.appendChild(_el('div', { style: { color: 'var(--fg-3,#888)', fontSize: '12px' } }, 'No exemptions yet.'));
      }
      for (const ex of rows) {
        const label = ex.kind === 'agent'
          ? `Agent ${ex.agent_id.slice(0, 8)} — entire agent is free`
          : ex.kind === 'user'
            ? `User ${ex.user_id.slice(0, 12)} — exempt globally`
            : `User ${ex.user_id.slice(0, 12)} for agent ${ex.agent_id.slice(0, 8)}`;
        const row = _el('div', { style: { display: 'flex', alignItems: 'center', gap: '8px',
                                              padding: '4px 0', fontSize: '12px' } });
        row.appendChild(_el('span', {}, label));
        row.appendChild(_el('button', {
          style: { marginLeft: 'auto', padding: '2px 6px', fontSize: '11px',
                    background: 'transparent', color: 'var(--fg-3,#999)',
                    border: '1px solid var(--border,#444)', borderRadius: '4px', cursor: 'pointer' },
          onclick: async () => {
            if (!confirm('Remove this exemption?')) return;
            await _api(`/api/v1/billing/exemptions/${ex.id}?user_id=${encodeURIComponent(_userId())}`,
                       { method: 'DELETE' });
            reload();
          },
        }, 'remove'));
        listDiv.appendChild(row);
      }
    } catch (e) {
      listDiv.appendChild(_el('div', { style: { color: 'var(--fg-3,#888)', fontSize: '12px' } },
        'Could not load exemptions: ' + e.message));
    }
  }
  reload();

  // Add controls
  const form = _el('div', { style: { display: 'flex', gap: '6px', marginTop: '8px', flexWrap: 'wrap' } });
  let kindSel = null;
  if (isPlatform) {
    kindSel = _el('select', { style: { ..._inputStyle(), width: 'auto', flex: '0 0 auto' } });
    kindSel.appendChild(_el('option', { value: 'user' }, 'Exempt a user globally'));
    kindSel.appendChild(_el('option', { value: 'agent' }, 'Exempt an entire agent'));
    form.appendChild(kindSel);
  }
  const idInput = _el('input', {
    placeholder: isPlatform ? 'user_id (or agent_id if kind=agent)' : 'user_id to exempt',
    style: { ..._inputStyle(), flex: '1 1 200px' },
  });
  form.appendChild(idInput);
  form.appendChild(_el('button', {
    style: { padding: '6px 10px', background: 'var(--accent,#4a7cff)', color: 'white',
              border: 'none', borderRadius: '4px', cursor: 'pointer', fontSize: '12px' },
    onclick: async () => {
      const id = (idInput.value || '').trim();
      if (!id) return;
      let body;
      if (isPlatform && kindSel && kindSel.value === 'agent') {
        body = { user_id: _userId(), kind: 'agent', agent_id: id };
      } else if (isPlatform) {
        body = { user_id: _userId(), kind: 'user', target_user_id: id };
      } else {
        body = { user_id: _userId(), kind: 'user_for_agent', agent_id: agentId, target_user_id: id };
      }
      try {
        await _api('/api/v1/billing/exemptions', { method: 'POST', body: JSON.stringify(body) });
        idInput.value = '';
        reload();
      } catch (e) { alert('Failed to add exemption: ' + e.message); }
    },
  }, 'Add'));
  wrap.appendChild(form);

  return wrap;
}

function _labelled(label, input, opts = {}) {
  const wrap = _el('div', { style: { flex: '1' } });
  wrap.appendChild(_el('div', {
    style: { fontSize: opts.compact ? '10px' : '11px', color: 'var(--fg-3,#888)', marginBottom: '2px' },
  }, label));
  wrap.appendChild(input);
  return wrap;
}

function _inputStyle() {
  return {
    width: '100%', padding: '6px 8px', fontSize: '12px',
    background: 'var(--bg-1,#1c1c1f)', color: 'var(--fg-1,inherit)',
    border: '1px solid var(--border,#444)', borderRadius: '4px',
  };
}

function _selectStyle() {
  return { ..._inputStyle(), padding: '6px 8px' };
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
        _openBuyCreditsModal({
          agentId: d.agent_id || null,
          reason: d.reason || 'needs_payment',
          accepted: d.accepted_processors || null,
        });
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
 *  (admin-tools tab or agent monetization tab). */
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

// Expose for non-module callers (agents.js renders the per-agent tab).
window.AppBilling = {
  renderMonetizationPanel,
  refreshBalance: _refreshBalancePill,
  openBuyCreditsModal: _openBuyCreditsModal,
};
