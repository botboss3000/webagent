'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Use CSS variables in inline styles; never write hex/rgb colour literals here.

/**
 * Platform (marketplace) billing — OPTIONAL, drop-in Admin Tools view.
 *
 * This whole view is the PLATFORM tier and is stripped from a public /
 * agent-only edition. Discovered via ui/admin-tools/billing/page.json and driven
 * by the admin shell, which dynamically imports this module and calls the
 * exported startView / stopView when the view is shown / hidden. It sets the
 * platform-wide policy, the platform fee, payout (Connect) onboarding and
 * platform exemptions, and talks to the platform routes (/api/v1/billing/
 * config/platform, /connect/*) that only exist when this package is installed.
 *
 * Sister panel (agent tier, always present): ui/main-panel/agents/billing/agent-billing.js.
 * REMOVE-WHEN: the platform billing tier is excluded from the edition.
 */

import {
  _el, _api, _qs, _userId, _loadProcessors,
  _buildRateCard, _labelled, _inputStyle, _selectStyle, STRATEGIES,
  startBilling, stopBilling,
} from '../../shared/js/billing.js';

export function startView() {
  startBilling();
  _renderPlatformPanel();
}

export function stopView() {
  stopBilling();
}

async function _renderPlatformPanel() {
  const container = document.getElementById('billing-platform-panel');
  if (!container) return;
  container.innerHTML = '';
  container.appendChild(_el('div', {
    style: { fontSize: '12px', color: 'var(--fg-3)', marginBottom: '12px', lineHeight: '1.5' },
  }, 'Choose which monetization methods agent admins can use, set the platform fee, and pick which payment processors are available.'));

  const loading = _el('div', { style: { color: 'var(--fg-3)' } }, 'Loading…');
  container.appendChild(loading);

  let platform = {}, processors = [];
  try {
    const r = await _api(`/api/v1/billing/config/platform?${_qs({ user_id: _userId() })}`);
    platform = r.platform || {};
    processors = await _loadProcessors();
  } catch (e) {
    loading.textContent = `Failed to load platform billing config: ${e.message}`;
    return;
  }
  loading.remove();

  // ── Default monetization method ──
  const stratWrap = _el('div', { style: { marginBottom: '14px' } });
  stratWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '4px' } }, 'Default monetization method'));
  const stratSel = _el('select', { style: _selectStyle() });
  for (const s of STRATEGIES) {
    const o = _el('option', { value: s.value }, s.label);
    if ((platform.strategy || 'free') === s.value) o.selected = true;
    stratSel.appendChild(o);
  }
  stratWrap.appendChild(stratSel);
  container.appendChild(stratWrap);

  // ── Methods agent admins can enable ──
  const allowedStratWrap = _el('div', { style: { marginBottom: '14px' } });
  allowedStratWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '4px' } }, 'Methods agent admins can enable'));
  const boxes = _el('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '8px' } });
  const allowedStrategyCheckboxes = {};
  for (const s of STRATEGIES.filter(s => s.value !== 'free')) {
    const c = _el('input', { type: 'checkbox' });
    c.checked = (platform.allowed_strategies || []).includes(s.value);
    allowedStrategyCheckboxes[s.value] = c;
    boxes.appendChild(_el('label', {
      style: { display: 'inline-flex', alignItems: 'center', gap: '4px', fontSize: '13px',
                padding: '4px 8px', border: '1px solid var(--border)', borderRadius: '6px', cursor: 'pointer' },
    }, c, s.label));
  }
  allowedStratWrap.appendChild(boxes);
  container.appendChild(allowedStratWrap);

  // ── Payment processors users can pay with ──
  const procWrap = _el('div', { style: { marginBottom: '14px' } });
  procWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '4px' } }, 'Payment processors users can pay with'));
  const procRow = _el('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '8px' } });
  const procCheckboxes = {};
  if (processors.length === 0) {
    procRow.appendChild(_el('div', { style: { color: 'var(--fg-3)', fontSize: '12px' } },
      'No processors configured. Set STRIPE_SECRET_KEY or PAYPAL_CLIENT_ID/SECRET in your env.'));
  }
  for (const p of processors) {
    const c = _el('input', { type: 'checkbox' });
    c.checked = (platform.allowed_processors || []).includes(p.name);
    procCheckboxes[p.name] = c;
    const dot = _el('span', {
      style: { display: 'inline-block', width: '8px', height: '8px', borderRadius: '50%',
                marginRight: '4px', background: p.configured ? 'var(--success)' : 'var(--fg-3)' },
    });
    procRow.appendChild(_el('label', {
      style: { display: 'inline-flex', alignItems: 'center', gap: '4px', fontSize: '13px',
                padding: '4px 8px', border: '1px solid var(--border)', borderRadius: '6px', cursor: 'pointer' },
    }, c, dot, p.display_name + (p.configured ? '' : ' (not configured)')));
  }
  procWrap.appendChild(procRow);
  container.appendChild(procWrap);

  // ── Rate card (defaults agent admins inherit) ──
  const rateWrap = _el('div', { style: { marginBottom: '14px' } });
  rateWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '4px' } }, 'Default rate card'));
  const rateGrid = _el('div', {
    style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px',
              padding: '10px', background: 'var(--bg-2)', borderRadius: '8px' },
  });
  const defaultCard = _buildRateCard(platform.rate_card_default_llm || {}, 'Default LLM (platform pays for tokens)');
  const byoCard = _buildRateCard(platform.rate_card_byo_llm || {}, 'BYO LLM (agent brings own key)');
  rateGrid.appendChild(defaultCard.el);
  rateGrid.appendChild(byoCard.el);
  rateWrap.appendChild(rateGrid);
  container.appendChild(rateWrap);

  // ── Platform fee ──
  const feeWrap = _el('div', { style: { marginBottom: '14px', display: 'flex', gap: '12px' } });
  const feePctInput = _el('input', { type: 'number', step: '0.1', min: '0', max: '100',
                                      value: String(platform.platform_fee_pct || 0), style: _inputStyle() });
  const feeFlatInput = _el('input', { type: 'number', step: '1', min: '0',
                                       value: String(platform.platform_fee_flat_cents || 0), style: _inputStyle() });
  feeWrap.appendChild(_labelled('Platform fee %', feePctInput));
  feeWrap.appendChild(_labelled('Platform flat fee (¢)', feeFlatInput));
  container.appendChild(feeWrap);

  // ── Trial defaults ──
  const trial = platform.trial_config || {};
  const trialWrap = _el('div', { style: { marginBottom: '14px', padding: '10px', background: 'var(--bg-2)', borderRadius: '8px' } });
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
  const subPrice = _el('input', { type: 'number', min: '0', value: String(platform.subscription_price_cents || 0), style: _inputStyle() });
  subWrap.appendChild(_labelled('Subscription price (¢ / month)', subPrice));
  container.appendChild(subWrap);

  // ── Payout (Connect) onboarding ──
  container.appendChild(await _renderConnectSection(processors));

  // ── Exemptions (no-pay users / agents) ──
  container.appendChild(await _renderPlatformExemptions());

  // ── Save ──
  const saveBtn = _el('button', {
    class: 'billing-btn',
    style: { padding: '10px 16px', background: 'var(--accent)', color: 'white', /* contrast ink on accent fill */
              border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '14px', marginTop: '8px' },
    onclick: async () => {
      saveBtn.disabled = true;
      saveBtn.textContent = 'Saving…';
      try {
        const body = {
          user_id: _userId(),
          strategy: stratSel.value,
          allowed_strategies: Object.entries(allowedStrategyCheckboxes).filter(([_, c]) => c.checked).map(([n]) => n),
          allowed_processors: Object.entries(procCheckboxes).filter(([_, c]) => c.checked).map(([n]) => n),
          rate_card_default_llm: defaultCard.read(),
          rate_card_byo_llm: byoCard.read(),
          platform_fee_pct: parseFloat(feePctInput.value) || 0,
          platform_fee_flat_cents: parseInt(feeFlatInput.value, 10) || 0,
          trial_config: {
            days: parseInt(trialDays.value, 10) || 0,
            messages: parseInt(trialMsgs.value, 10) || 0,
            tokens: parseInt(trialToks.value, 10) || 0,
          },
          subscription_price_cents: parseInt(subPrice.value, 10) || 0,
        };
        await _api('/api/v1/billing/config/platform', { method: 'PUT', body: JSON.stringify(body) });
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

async function _renderConnectSection(processors) {
  const wrap = _el('div', { style: { marginBottom: '14px', padding: '10px', background: 'var(--bg-2)', borderRadius: '8px' } });
  wrap.appendChild(_el('div', { style: { fontSize: '12px', fontWeight: '600', marginBottom: '6px' } }, 'Connect payouts'));
  const statusRow = _el('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '8px' } });
  wrap.appendChild(statusRow);
  try {
    const stat = await _api(`/api/v1/billing/connect/status?${_qs({ user_id: _userId() })}`);
    const accounts = stat.accounts || [];
    for (const p of processors) {
      const acct = accounts.find(a => a.processor === p.name);
      const complete = acct && acct.onboarding_complete;
      statusRow.appendChild(_el('button', {
        class: 'billing-btn',
        style: { padding: '6px 10px', borderRadius: '6px', cursor: 'pointer', border: '1px solid var(--border)',
                  background: complete ? 'rgba(var(--success-rgb), 0.18)' : 'var(--bg-1)', color: 'var(--fg-1)', fontSize: '12px' },
        onclick: async () => {
          try {
            const r = await _api('/api/v1/billing/connect/onboard', {
              method: 'POST', body: JSON.stringify({ user_id: _userId(), processor: p.name }),
            });
            if (r?.redirect_url) window.location.href = r.redirect_url;
          } catch (e) { alert('Onboarding failed: ' + e.message); }
        },
      }, `${p.display_name}: ${complete ? '✓ connected' : 'connect →'}`));
    }
  } catch (_) {}
  return wrap;
}

async function _renderPlatformExemptions() {
  const wrap = _el('div', { style: { marginBottom: '14px', padding: '10px', background: 'var(--bg-2)', borderRadius: '8px' } });
  wrap.appendChild(_el('div', { style: { fontSize: '12px', fontWeight: '600', marginBottom: '6px' } }, 'Exemptions (no-pay users / agents)'));
  const listDiv = _el('div', { style: { marginBottom: '8px' } });
  wrap.appendChild(listDiv);

  async function reload() {
    listDiv.innerHTML = '';
    try {
      const r = await _api(`/api/v1/billing/exemptions?${_qs({ user_id: _userId() })}`);
      const rows = r.exemptions || [];
      if (rows.length === 0) {
        listDiv.appendChild(_el('div', { style: { color: 'var(--fg-3)', fontSize: '12px' } }, 'No exemptions yet.'));
      }
      for (const ex of rows) {
        const label = ex.kind === 'agent'
          ? `Agent ${String(ex.agent_id || '').slice(0, 8)} — entire agent is free`
          : ex.kind === 'user'
            ? `User ${String(ex.user_id || '').slice(0, 12)} — exempt globally`
            : `User ${String(ex.user_id || '').slice(0, 12)} for agent ${String(ex.agent_id || '').slice(0, 8)}`;
        const row = _el('div', { style: { display: 'flex', alignItems: 'center', gap: '8px', padding: '4px 0', fontSize: '12px' } });
        row.appendChild(_el('span', {}, label));
        row.appendChild(_el('button', {
          style: { marginLeft: 'auto', padding: '2px 6px', fontSize: '11px', background: 'transparent',
                    color: 'var(--fg-3)', border: '1px solid var(--border)', borderRadius: '4px', cursor: 'pointer' },
          onclick: async () => {
            if (!confirm('Remove this exemption?')) return;
            await _api(`/api/v1/billing/exemptions/${ex.id}?user_id=${encodeURIComponent(_userId())}`, { method: 'DELETE' });
            reload();
          },
        }, 'remove'));
        listDiv.appendChild(row);
      }
    } catch (e) {
      listDiv.appendChild(_el('div', { style: { color: 'var(--fg-3)', fontSize: '12px' } }, 'Could not load exemptions: ' + e.message));
    }
  }
  reload();

  const form = _el('div', { style: { display: 'flex', gap: '6px', marginTop: '8px', flexWrap: 'wrap' } });
  const kindSel = _el('select', { style: { ..._inputStyle(), width: 'auto', flex: '0 0 auto' } });
  kindSel.appendChild(_el('option', { value: 'user' }, 'Exempt a user globally'));
  kindSel.appendChild(_el('option', { value: 'agent' }, 'Exempt an entire agent'));
  form.appendChild(kindSel);
  const idInput = _el('input', { placeholder: 'user_id (or agent_id if kind=agent)', style: { ..._inputStyle(), flex: '1 1 200px' } });
  form.appendChild(idInput);
  form.appendChild(_el('button', {
    style: { padding: '6px 10px', background: 'var(--accent)', color: 'white', /* contrast ink on accent fill */
              border: 'none', borderRadius: '4px', cursor: 'pointer', fontSize: '12px' },
    onclick: async () => {
      const id = (idInput.value || '').trim();
      if (!id) return;
      const body = kindSel.value === 'agent'
        ? { user_id: _userId(), kind: 'agent', agent_id: id }
        : { user_id: _userId(), kind: 'user', target_user_id: id };
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
