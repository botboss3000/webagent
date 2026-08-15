'use strict';

// Trusted renderer for declarative chat components.  The agent supplies data only;
// this module owns all DOM creation and action routing.
import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';

let host;
let components = new Map();
let pendingInline = [];

function _host() {
  if (host && host.isConnected) return host;
  host = document.getElementById('chat-components-sticky');
  return host;
}

// ── Renderers per type ──────────────────────────────────────────────────────

function _todo(component) {
  const card = document.createElement('section');
  card.className = 'chat-component chat-component-todo';
  const title = document.createElement('h3'); title.textContent = component.title;
  card.appendChild(title);
  const list = document.createElement('div'); list.className = 'chat-component-list';
  for (const item of component.data.items || []) {
    const label = document.createElement('label'); label.className = 'chat-component-item';
    const input = document.createElement('input'); input.type = 'checkbox'; input.checked = !!item.done;
    input.addEventListener('change', async () => {
      input.disabled = true;
      try { await act(component.id, 'toggle_item', { item_id: item.id, done: input.checked }); }
      catch (_) { input.checked = !input.checked; }
      input.disabled = false;
    });
    const text = document.createElement('span'); text.textContent = item.label;
    label.append(input, text); list.appendChild(label);
  }
  card.appendChild(list); return card;
}

function _status(component) {
  const card = document.createElement('section');
  card.className = 'chat-component chat-component-status';
  const title = document.createElement('h3'); title.textContent = component.title;
  card.appendChild(title);
  const grid = document.createElement('div'); grid.className = 'chat-component-status-grid';
  for (const s of component.data.sections || []) {
    const row = document.createElement('div'); row.className = 'chat-component-status-row';
    const lbl = document.createElement('span'); lbl.className = 'chat-component-status-label'; lbl.textContent = s.label;
    const val = document.createElement('span'); val.className = 'chat-component-status-value';
    val.textContent = s.value;
    if (s.status) val.classList.add('status-' + s.status);
    row.append(lbl, val); grid.appendChild(row);
  }
  card.appendChild(grid); return card;
}

function _choice(component) {
  const card = document.createElement('section');
  card.className = 'chat-component chat-component-choice';
  const title = document.createElement('h3'); title.textContent = component.title;
  card.appendChild(title);
  const selected = component.data.selected || '';
  for (const opt of component.data.options || []) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chat-component-choice-btn';
    if (opt.id === selected) btn.classList.add('active');
    btn.textContent = opt.label;
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try { await act(component.id, 'select', { option_id: opt.id }); }
      catch (_) { btn.disabled = false; }
    });
    card.appendChild(btn);
  }
  return card;
}

function _form(component) {
  const card = document.createElement('section');
  card.className = 'chat-component chat-component-form';
  const title = document.createElement('h3'); title.textContent = component.title;
  card.appendChild(title);
  const submitted = component.data.submitted;
  if (submitted) {
    const done = document.createElement('p');
    done.className = 'chat-component-form-done';
    done.textContent = '✓ Submitted';
    card.appendChild(done);
    return card;
  }
  const fields = component.data.fields || [];
  const fieldEls = {};
  for (const f of fields) {
    const wrap = document.createElement('div'); wrap.className = 'chat-component-form-field';
    const lbl = document.createElement('label'); lbl.textContent = f.label;
    let input;
    if (f.type === 'select' && f.options) {
      input = document.createElement('select');
      for (const o of f.options) {
        const opt = document.createElement('option'); opt.value = o.value; opt.textContent = o.label;
        input.appendChild(opt);
      }
    } else if (f.multiline) {
      input = document.createElement('textarea'); input.rows = 3;
    } else {
      input = document.createElement('input'); input.type = f.type || 'text';
    }
    if (f.required) input.required = true;
    input.name = f.name;
    wrap.append(lbl, input); card.appendChild(wrap);
    fieldEls[f.name] = input;
  }
  const submit = document.createElement('button');
  submit.type = 'button'; submit.className = 'chat-component-form-submit'; submit.textContent = 'Submit';
  submit.addEventListener('click', async () => {
    const values = {};
    for (const f of fields) {
      const el = fieldEls[f.name];
      if (!el) continue;
      if (!el.reportValidity()) return;
      values[f.name] = el.value;
    }
    submit.disabled = true;
    try { await act(component.id, 'submit', { values }); }
    catch (_) { submit.disabled = false; }
  });
  card.appendChild(submit);
  return card;
}

function _approval(component) {
  const card = document.createElement('section');
  card.className = 'chat-component chat-component-approval';
  const title = document.createElement('h3'); title.textContent = component.title;
  card.appendChild(title);
  const status = component.data.status || '';
  if (status) {
    const done = document.createElement('p');
    done.className = 'chat-component-approval-result';
    done.textContent = status === 'approved' ? '✓ Approved' : '✗ Rejected';
    if (status === 'approved') done.classList.add('approved');
    else done.classList.add('rejected');
    card.appendChild(done);
    return card;
  }
  if (component.data.description) {
    const desc = document.createElement('p'); desc.textContent = component.data.description;
    card.appendChild(desc);
  }
  const actions = document.createElement('div'); actions.className = 'chat-component-approval-actions';
  const approve = document.createElement('button');
  approve.type = 'button'; approve.className = 'chat-component-approval-approve';
  approve.textContent = component.data.approve_label || 'Approve';
  approve.addEventListener('click', async () => {
    approve.disabled = true; reject.disabled = true;
    try { await act(component.id, 'approve', {}); }
    catch (_) { approve.disabled = false; reject.disabled = false; }
  });
  const reject = document.createElement('button');
  reject.type = 'button'; reject.className = 'chat-component-approval-reject';
  reject.textContent = component.data.reject_label || 'Reject';
  reject.addEventListener('click', async () => {
    approve.disabled = true; reject.disabled = true;
    try { await act(component.id, 'reject', {}); }
    catch (_) { approve.disabled = false; reject.disabled = false; }
  });
  actions.append(approve, reject); card.appendChild(actions);
  return card;
}

const RENDERERS = {
  todo_list: _todo,
  status:    _status,
  choice:    _choice,
  form:      _form,
  approval:  _approval,
};

// ── Core render / persist / act / remove ────────────────────────────────────

function _addDismiss(card, component) {
  const dismiss = document.createElement('button');
  dismiss.type = 'button';
  dismiss.className = 'chat-component-dismiss';
  dismiss.setAttribute('aria-label', 'Dismiss');
  dismiss.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="M6 6l12 12"/></svg>';
  dismiss.addEventListener('click', async (e) => {
    e.stopPropagation();
    dismiss.disabled = true;
    try {
      await fetch(apiPath('/api/v1/chat/components/' + encodeURIComponent(component.id) + '?session_id=' + encodeURIComponent(app.currentSessionId)), {
        method: 'DELETE', headers: authHeaders(),
      });
      card.remove();
      components.delete(component.id);
    } catch (_) {
      dismiss.disabled = false;
    }
  });
  card.appendChild(dismiss);
}

function render(component, target) {
  if (!component || !component.id) return null;
  const existing = document.querySelector(`[data-chat-component-id="${CSS.escape(component.id)}"]`);
  if (existing) existing.remove();
  const renderer = RENDERERS[component.type];
  const el = renderer ? renderer(component) : document.createElement('section');
  if (!renderer) {
    el.className = 'chat-component';
    const title = document.createElement('h3'); title.textContent = component.title;
    const text = document.createElement('p'); text.textContent = String(component.data.message || '');
    el.append(title, text);
  }
  el.dataset.chatComponentId = component.id;
  _addDismiss(el, component);
  (target || _host())?.appendChild(el);
  components.set(component.id, component); return el;
}

async function persist(component) {
  if (!app.currentSessionId) return null;
  const response = await fetch(apiPath('/api/v1/chat/components?session_id=' + encodeURIComponent(app.currentSessionId)), {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: JSON.stringify({ component }),
  });
  if (!response.ok) throw new Error('Could not save component');
  return (await response.json()).component;
}

async function act(id, action, payload) {
  const response = await fetch(apiPath('/api/v1/chat/components/' + encodeURIComponent(id) + '/actions?session_id=' + encodeURIComponent(app.currentSessionId)), {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: JSON.stringify({ action, payload }),
  });
  if (!response.ok) throw new Error('Could not update component');
  const component = (await response.json()).component;
  render(component); return component;
}

// ── Lifecycle ───────────────────────────────────────────────────────────────

async function load(sessionId) {
  components = new Map(); pendingInline = [];
  const sticky = _host(); if (sticky) sticky.innerHTML = '';
  if (!sessionId) return;
  try {
    const response = await fetch(apiPath('/api/v1/chat/components?session_id=' + encodeURIComponent(sessionId)), { headers: authHeaders() });
    if (!response.ok) return;
    for (const component of (await response.json()).components || []) {
      if (component.placement === 'sticky') render(component);
      else pendingInline.push(component);
    }
  } catch (_) { /* chat components are optional */ }
}

async function present(component) {
  const saved = await persist(component);
  if (!saved) return;
  if (saved.placement === 'sticky') render(saved);
  else pendingInline.push(saved);
}

function attachPendingInline(bubble) {
  if (!bubble || !pendingInline.length) return;
  const toAttach = pendingInline.splice(0);
  toAttach.forEach(component => render(component, bubble));
}

export function initChatComponents() {
  app.loadChatComponents = load; app.presentChatComponent = present; app.attachPendingChatComponents = attachPendingInline;
}
