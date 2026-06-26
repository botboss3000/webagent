'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Use CSS variables in inline styles (var(--accent), rgba(var(--success-rgb),a)).
// Never write hex/rgb colour literals here.

/**
 * Per-agent Monetization tab.
 *
 * Rendered by the agents card (ui/main-panel/agents/js/view.js) when the
 * Monetization tab is selected. The agent admin sets how THIS agent charges its
 * users; the agent keeps everything it earns. Shared builders + the global
 * runtime live in ui/shared/js/billing.js.
 * REMOVE-WHEN: the per-agent Monetization tab is dropped from the agent card.
 */

import {
  _el, _api, _qs, _userId, _loadProcessors,
  _buildRateCard, _labelled, _inputStyle, _selectStyle, STRATEGIES,
} from '../../../shared/js/billing.js';

export async function renderAgentMonetization(container, agentId) {
  container.innerHTML = '';
  container.appendChild(_el('div', {
    style: { fontSize: '12px', color: 'var(--fg-3)', marginBottom: '12px', lineHeight: '1.5' },
  }, 'Configure how this agent charges its users. The agent keeps everything it earns.'));

  const loading = _el('div', { style: { color: 'var(--fg-3)' } }, 'Loading…');
  container.appendChild(loading);

  let cfg, processors = [];
  try {
    cfg = await _api(`/api/v1/billing/config?${_qs({ user_id: _userId(), agent_id: agentId || '' })}`);
    processors = await _loadProcessors();
  } catch (e) {
    loading.textContent = `Failed to load billing config: ${e.message}`;
    return;
  }
  loading.remove();

  const agent = cfg.agent || {};
  const effective = cfg.effective || {};
  const allowedStrategies = (effective.allowed_strategies && effective.allowed_strategies.length)
    ? effective.allowed_strategies : STRATEGIES.map(s => s.value);
  const allowedProcessors = (effective.allowed_processors && effective.allowed_processors.length)
    ? effective.allowed_processors : processors.map(p => p.name);

  // ── Strategy ──
  const stratWrap = _el('div', { style: { marginBottom: '14px' } });
  stratWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '4px' } }, 'Monetization method'));
  const stratSel = _el('select', { style: _selectStyle() });
  for (const s of STRATEGIES.filter(s => s.value === 'free' || allowedStrategies.includes(s.value))) {
    const o = _el('option', { value: s.value }, s.label);
    if ((agent.strategy || 'free') === s.value) o.selected = true;
    stratSel.appendChild(o);
  }
  stratWrap.appendChild(stratSel);
  container.appendChild(stratWrap);

  // ── Accepted payment methods ──
  const procWrap = _el('div', { style: { marginBottom: '14px' } });
  procWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '4px' } }, 'Payment methods this agent accepts'));
  const procRow = _el('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '8px' } });
  const procCheckboxes = {};
  const procPool = processors.filter(p => allowedProcessors.includes(p.name));
  if (procPool.length === 0) {
    procRow.appendChild(_el('div', { style: { color: 'var(--fg-3)', fontSize: '12px' } }, 'No payment processors are enabled yet.'));
  }
  for (const p of procPool) {
    const c = _el('input', { type: 'checkbox' });
    c.checked = (agent.allowed_processors || []).includes(p.name);
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

  // ── Rate card ──
  const rateWrap = _el('div', { style: { marginBottom: '14px' } });
  rateWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '4px' } }, 'Rate card'));
  const rateGrid = _el('div', {
    style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px',
              padding: '10px', background: 'var(--bg-2)', borderRadius: '8px' },
  });
  const defaultCard = _buildRateCard(agent.rate_card_default_llm || {}, 'Default LLM (you pay for tokens)');
  const byoCard = _buildRateCard(agent.rate_card_byo_llm || {}, 'BYO LLM (agent brings own key)');
  rateGrid.appendChild(defaultCard.el);
  rateGrid.appendChild(byoCard.el);
  rateWrap.appendChild(rateGrid);
  container.appendChild(rateWrap);

  // ── Trial ──
  const trial = agent.trial_config || {};
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
  const subPrice = _el('input', { type: 'number', min: '0', value: String(agent.subscription_price_cents || 0), style: _inputStyle() });
  subWrap.appendChild(_labelled('Subscription price (¢ / month)', subPrice));
  container.appendChild(subWrap);

  // ── Free-access grants (this agent's own users) ──
  container.appendChild(await _renderAgentExemptions(agentId));

  // ── Save ──
  const saveBtn = _el('button', {
    class: 'billing-btn',
    style: { padding: '10px 16px', background: 'var(--accent)', color: 'white', /* contrast ink on accent fill */
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
        await _api(`/api/v1/billing/config/agent/${encodeURIComponent(agentId)}`, {
          method: 'PUT', body: JSON.stringify(body),
        });
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

async function _renderAgentExemptions(agentId) {
  const wrap = _el('div', { style: { marginBottom: '14px', padding: '10px', background: 'var(--bg-2)', borderRadius: '8px' } });
  wrap.appendChild(_el('div', { style: { fontSize: '12px', fontWeight: '600', marginBottom: '6px' } },
    'Free access (no-pay users for this agent)'));
  const listDiv = _el('div', { style: { marginBottom: '8px' } });
  wrap.appendChild(listDiv);

  async function reload() {
    listDiv.innerHTML = '';
    try {
      const r = await _api(`/api/v1/billing/exemptions?${_qs({ user_id: _userId(), agent_id: agentId || '' })}`);
      const rows = (r.exemptions || []).filter(ex => ex.kind === 'user_for_agent');
      if (rows.length === 0) {
        listDiv.appendChild(_el('div', { style: { color: 'var(--fg-3)', fontSize: '12px' } }, 'No free-access grants yet.'));
      }
      for (const ex of rows) {
        const row = _el('div', { style: { display: 'flex', alignItems: 'center', gap: '8px', padding: '4px 0', fontSize: '12px' } });
        row.appendChild(_el('span', {}, `User ${String(ex.user_id || '').slice(0, 12)} — free for this agent`));
        row.appendChild(_el('button', {
          style: { marginLeft: 'auto', padding: '2px 6px', fontSize: '11px', background: 'transparent',
                    color: 'var(--fg-3)', border: '1px solid var(--border)', borderRadius: '4px', cursor: 'pointer' },
          onclick: async () => {
            if (!confirm('Remove this grant?')) return;
            await _api(`/api/v1/billing/exemptions/${ex.id}?user_id=${encodeURIComponent(_userId())}`, { method: 'DELETE' });
            reload();
          },
        }, 'remove'));
        listDiv.appendChild(row);
      }
    } catch (e) {
      listDiv.appendChild(_el('div', { style: { color: 'var(--fg-3)', fontSize: '12px' } }, 'Could not load: ' + e.message));
    }
  }
  reload();

  const form = _el('div', { style: { display: 'flex', gap: '6px', marginTop: '8px', flexWrap: 'wrap' } });
  const idInput = _el('input', { placeholder: 'user_id to grant free access', style: { ..._inputStyle(), flex: '1 1 200px' } });
  form.appendChild(idInput);
  form.appendChild(_el('button', {
    style: { padding: '6px 10px', background: 'var(--accent)', color: 'white', /* contrast ink on accent fill */
              border: 'none', borderRadius: '4px', cursor: 'pointer', fontSize: '12px' },
    onclick: async () => {
      const id = (idInput.value || '').trim();
      if (!id) return;
      try {
        await _api('/api/v1/billing/exemptions', {
          method: 'POST',
          body: JSON.stringify({ user_id: _userId(), kind: 'user_for_agent', agent_id: agentId, target_user_id: id }),
        });
        idInput.value = '';
        reload();
      } catch (e) { alert('Failed to add: ' + e.message); }
    },
  }, 'Add'));
  wrap.appendChild(form);
  return wrap;
}
