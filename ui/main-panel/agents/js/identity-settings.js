'use strict';

/** Shared Identity section used by every persisted agent's Config tab. */

import { icon } from '../../../shared/js/icons.js';
import { openIconPicker } from '../../../shared/js/icon-picker.js';
import { _markSaving, _flashSaveCheck } from '../../../shared/js/dom-utils.js';
import { _debounced, _putAgentField, _renderAgentIcon } from './utils.js';
import { isAnonGuest } from '../../../shared/js/left-login.js';

function _field(host, agent, { label, field, value, multiline = false, placeholder = '' }) {
  const wrap = document.createElement('div');
  wrap.className = 'ac-cfg-field';
  const labelEl = document.createElement('label');
  labelEl.className = 'ac-label';
  labelEl.textContent = label;
  const indicator = document.createElement('span');
  indicator.className = 'ac-cfg-ind';
  labelEl.appendChild(indicator);
  const input = document.createElement(multiline ? 'textarea' : 'input');
  if (!multiline) input.type = 'text';
  else input.rows = 3;
  input.className = 'ac-input';
  input.dataset.field = field;
  input.value = value || '';
  input.placeholder = placeholder;
  input.readOnly = agent.source !== 'custom';
  wrap.appendChild(labelEl);
  wrap.appendChild(input);
  host.appendChild(wrap);

  if (!input.readOnly) {
    const save = _debounced(async () => {
      let next = input.value;
      if (field === 'name') {
        next = next.trim();
        if (!next) {
          if (agent.is_mock) {
            await _putAgentField(agent, { name: '' }, null, { silent: true });
            return;
          }
          input.value = agent.name || agent.id;
          _flashSaveCheck(indicator, false, 'Name cannot be blank');
          return;
        }
      }
      _markSaving(indicator);
      const ok = await _putAgentField(agent, { [field]: next }, null, { silent: true });
      _flashSaveCheck(indicator, ok, ok ? '' : 'Save failed');
    });
    input.addEventListener('input', save);
    input.addEventListener('blur', () => save.flush());
  }
}

export function renderAgentIdentitySettings(body, agent, {
  embedded = body.classList.contains('agent-config-topic-content'),
} = {}) {
  const anonymousDraft = !!agent.is_mock && isAnonGuest();
  const group = document.createElement('div');
  group.className = embedded
    ? 'agent-identity-settings agent-identity-settings--embedded'
    : 'ac-category-group agent-identity-settings';
  if (!embedded) group.innerHTML = `
    <div class="ac-category-summary" style="cursor:default">
      ${icon('badge', { size: '16px' })}
      <span class="ac-category-title">Identity</span>
    </div>`;
  const content = document.createElement('div');
  content.className = 'ac-category-body';
  group.appendChild(content);
  body.appendChild(group);

  const iconRow = document.createElement('div');
  iconRow.className = 'agent-identity-icon-row';
  const iconButton = document.createElement('button');
  iconButton.type = 'button';
  iconButton.className = 'agent-identity-icon-button';
  iconButton.innerHTML = _renderAgentIcon(agent, '26px');
  iconButton.disabled = agent.source !== 'custom';
  iconButton.title = iconButton.disabled ? 'This agent icon is managed by the system' : 'Change agent icon';
  iconButton.setAttribute('aria-label', iconButton.title);
  const iconText = document.createElement('span');
  iconText.innerHTML = '<strong>Agent icon</strong><small>Shown in the agent carousel and chats.</small>';
  iconRow.appendChild(iconButton);
  iconRow.appendChild(iconText);
  content.appendChild(iconRow);

  if (!iconButton.disabled) {
    iconButton.addEventListener('click', async () => {
      const chosen = await openIconPicker({ current: agent.icon || '', title: 'Choose an icon' });
      if (!chosen) return;
      const previous = agent.icon || '';
      agent.icon = chosen;
      iconButton.innerHTML = _renderAgentIcon(agent, '26px');
      const ok = await _putAgentField(agent, { icon: chosen }, null, { silent: true });
      if (!ok) {
        agent.icon = previous;
        iconButton.innerHTML = _renderAgentIcon(agent, '26px');
      }
    });
  }

  _field(content, agent, {
    label: 'Name', field: 'name', value: agent.is_mock ? (agent.name || '') : (agent.name || agent.id),
    placeholder: anonymousDraft
      ? 'Register an account to name and create an agent…'
      : 'Name this agent…',
  });
  _field(content, agent, {
    label: 'Description', field: 'description', value: agent.description || '', multiline: true,
    placeholder: 'Describe what this agent is for…',
  });
}
