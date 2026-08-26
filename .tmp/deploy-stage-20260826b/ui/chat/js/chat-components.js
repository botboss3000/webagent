'use strict';

// Trusted renderer for declarative chat components.  The agent supplies data only;
// this module owns all DOM creation and action routing.
// CHAT-COMPONENT-PANEL: every component renders into the single agent panel that
// expands from the top of the chat pill (#chat-component-panel / -body in
// chat-side-panel.html). Sticky/inline placement is no longer distinguished.
import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { icon, componentTypeIcon } from '../../shared/js/icons.js';

let host;
let panel;
let bar;
let components = new Map();
let panelUserClosed = false;   // user collapsed the panel; new agent content reopens it
let activeComponentId = null;  // which component the panel displays (tabs switch it)
let panelHeight = 150;         // current drawer height in px (grab bar / header drag)
let panelExpanded = false;     // full-height overlay mode (⛶ button)
let panelWidthPx = null;       // drawer width frozen at first content render (px)

function _host() {
  if (host && host.isConnected) return host;
  host = document.getElementById('chat-component-panel-body');
  return host;
}

function _panel() {
  if (panel && panel.isConnected) return panel;
  panel = document.getElementById('chat-component-panel');
  return panel;
}

function _bar() {
  if (bar && bar.isConnected) return bar;
  bar = document.getElementById('chat-component-panel-bar');
  return bar;
}

// Open the panel whenever it holds content and the user hasn't collapsed it.
// Called after every render/remove.
// States: empty → fully hidden (bar too); has content but user-collapsed →
// panel hidden, grabber bar floats above the pill as the pull handle; open →
// full drawer showing ONLY the active component (tabs switch which one).
// New agent content reopens and selects the freshly presented component.
function _syncPanel() {
  const p = _panel();
  const h = _host();
  if (!p || !h) return;
  const ids = [...components.keys()];
  const has = ids.length > 0;
  // Resolve selection: keep the current pick if it still exists, else first.
  if (has && !ids.includes(activeComponentId)) activeComponentId = ids[0];
  if (!has) {
    activeComponentId = null;
    // Nothing left in the panel → the expanded overlay has nothing to show.
    panelExpanded = false;
    const wrapEl = document.getElementById('chat-component-panel-wrap');
    if (wrapEl) wrapEl.classList.remove('expanded');
  }
  const open = has && !panelUserClosed;
  // Freeze the drawer width at the first content render, so it doesn't widen
  // as the composer pill widens while typing. Stays fixed for the session.
  if (has && !panelWidthPx) {
    const wrapEl = document.getElementById('chat-component-panel-wrap');
    if (wrapEl) {
      const w = Math.round(wrapEl.getBoundingClientRect().width);
      if (w > 0) { panelWidthPx = w; wrapEl.style.width = w + 'px'; }
    }
  }
  p.classList.toggle('has-components', has);
  p.classList.toggle('open', open);
  p.setAttribute('aria-hidden', has ? 'false' : 'true');
  // Show only the active card; keep the others mounted (state preserved).
  for (const card of h.querySelectorAll('[data-chat-component-id]')) {
    card.classList.toggle('cc-hidden', card.dataset.chatComponentId !== activeComponentId);
  }
  // Head title reflects the active component: its title, plus a done/total
  // counter for todo lists ("Plan Mode Checklist 5/6"). The type icon follows
  // the COMPONENT-TYPE-ICON-STANDARD (check-square / square-text / square-square).
  const titleEl = document.getElementById('chat-component-panel-title');
  const typeIconEl = document.getElementById('chat-component-panel-type-icon');
  if (titleEl) {
    const active = activeComponentId ? components.get(activeComponentId) : null;
    if (active) {
      let label = active.title || active.type || 'Agent panel';
      if (active.type === 'todo_list') {
        const items = (active.data && active.data.items) || [];
        const done = items.filter(i => i.done).length;
        label += ` ${done}/${items.length}`;
      }
      titleEl.textContent = label;
    } else {
      titleEl.textContent = 'Agent panel';
    }
  }
  if (typeIconEl) {
    const active = activeComponentId ? components.get(activeComponentId) : null;
    typeIconEl.innerHTML = active ? icon(componentTypeIcon(active.type), { size: '13px' }) : '';
  }
  const b = _bar();
  if (b) {
    b.classList.toggle('visible', has);
    b.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
}

// A component is "done" once its purpose is fulfilled — all todo items ticked,
// a choice selected, a form submitted, an approval resolved. Completed
// components leave the panel immediately and their header tab disappears.
function _isCompleted(component) {
  const data = component.data || {};
  if (component.type === 'todo_list') {
    const items = data.items || [];
    return items.length > 0 && items.every(i => i.done);
  }
  if (component.type === 'choice') return !!data.selected;
  if (component.type === 'form') return !!data.submitted;
  if (component.type === 'approval') return !!data.status;
  return false;
}

// Broadcast that the active component set changed (added / completed /
// deleted). The sub-agent tab bar listens and re-fetches so component tabs
// appear and clear live.
function _notifyChanged() {
  document.dispatchEvent(new CustomEvent('chat-components-changed', {
    detail: { sessionId: app.currentSessionId },
  }));
}

// ── Panel layout state persistence ─────────────────────────────────────────
// The drawer's layout (minimized, height, width, expanded, active tab) is
// per-session UI state, saved to the server so reopening the session restores
// it. Debounced so a burst of drag events coalesces into one PUT.
let _uiSaveTimer = null;
let _applyUiState = null;   // assigned inside initChatComponents (needs drag clamps)

function _uiState() {
  return {
    minimized: panelUserClosed,
    expanded: panelExpanded,
    height: Math.round(panelHeight),
    width: panelWidthPx,
    active: activeComponentId || '',
  };
}

function _scheduleUiSave() {
  if (_uiSaveTimer) clearTimeout(_uiSaveTimer);
  _uiSaveTimer = setTimeout(() => { _uiSaveTimer = null; _saveUiState(); }, 400);
}

async function _saveUiState() {
  const sid = app.currentSessionId;
  if (!sid || !components.size) return;   // empty panel: nothing layout-worthy to keep
  try {
    await fetch(apiPath('/api/v1/chat/components/ui-state?session_id=' + encodeURIComponent(sid)), {
      method: 'PUT', keepalive: true,
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ ui: _uiState() }),
    });
  } catch (_) { /* panel layout persistence is best-effort */ }
}

function _flushUiSave() {
  if (_uiSaveTimer) { clearTimeout(_uiSaveTimer); _uiSaveTimer = null; _saveUiState(); }
}

// ── Renderers per type ──────────────────────────────────────────────────────

function _todo(component) {
  const card = document.createElement('section');
  card.className = 'chat-component chat-component-todo';
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
  // Render as markdown (the plan overview is a markdown document, not a grid).
  const data = component.data || {};
  let md = data.markdown;
  if (!md && Array.isArray(data.sections)) {
    md = data.sections.map(s =>
      `**${s.label}**\n\n${s.status ? `*${s.status}* — ` : ''}${s.value || ''}`
    ).join('\n\n');
  }
  if (md && typeof app._renderMarkdownBody === 'function') {
    const body = app._renderMarkdownBody(md, false);
    if (body) { card.appendChild(body); return card; }
  }
  // Fallback: plain text
  const p = document.createElement('p');
  p.textContent = (md || '').replace(/[*#`>_-]/g, '') || component.data.message || '';
  card.appendChild(p);
  return card;
}

function _choice(component) {
  const card = document.createElement('section');
  card.className = 'chat-component chat-component-choice';
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

function render(component, target) {
  if (!component || !component.id) return null;
  if (_isCompleted(component)) { removeLocal(component.id); _notifyChanged(); return null; }
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
  _host()?.appendChild(el);
  components.set(component.id, component);
  if (!activeComponentId) activeComponentId = component.id;  // first card becomes active
  _syncPanel();
  _notifyChanged();
  return el;
}

// Remove a component card from the panel (no server call).
function removeLocal(id) {
  const existing = document.querySelector(`[data-chat-component-id="${CSS.escape(id)}"]`);
  if (existing) existing.remove();
  components.delete(id);
  if (activeComponentId === id) activeComponentId = null;  // _syncPanel picks the next
  _syncPanel();
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
  components = new Map(); panelUserClosed = false; activeComponentId = null;
  panelHeight = 150; panelExpanded = false; panelWidthPx = null;
  const wrapEl = document.getElementById('chat-component-panel-wrap');
  if (wrapEl) {
    wrapEl.classList.remove('expanded');
    wrapEl.style.width = '';
    wrapEl.style.top = ''; wrapEl.style.left = ''; wrapEl.style.right = ''; wrapEl.style.bottom = '';
  }
  const body = _host(); if (body) body.innerHTML = '';
  const pEl = _panel();
  if (pEl) pEl.style.removeProperty('--panel-height');   // back to default 150px
  _syncPanel();
  if (!sessionId) return;
  try {
    const [compRes, uiRes] = await Promise.all([
      fetch(apiPath('/api/v1/chat/components?session_id=' + encodeURIComponent(sessionId)), { headers: authHeaders() }),
      fetch(apiPath('/api/v1/chat/components/ui-state?session_id=' + encodeURIComponent(sessionId)), { headers: authHeaders() }),
    ]);
    if (compRes.ok) {
      for (const component of (await compRes.json()).components || []) render(component);
    }
    if (uiRes.ok && typeof _applyUiState === 'function') {
      try { _applyUiState((await uiRes.json()).ui || {}); } catch (_) { /* layout restore optional */ }
    }
  } catch (_) { /* chat components are optional */ }
}

async function present(component) {
  // The present tool already persisted this component server-side (durable
  // even if this tab missed the live event); the streamed result only drives
  // the render. Reloads hydrate via load() → GET /chat/components.
  panelUserClosed = false;   // fresh content from the agent reopens the panel
  activeComponentId = component.id;  // and focuses the new component
  render(component);
  _scheduleUiSave();         // persist the reopened layout (minimized=false)
}

// Compat no-op: components used to attach to the next agent bubble; they now
// render into the agent panel immediately (CHAT-COMPONENT-PANEL).
function attachPendingInline() { /* intentionally empty */ }

export function initChatComponents() {
  // Public controls for the tab bar: show = select a component (or keep the
  // current one) and slide the drawer open; hide = collapse it. toggle =
  // open that component, or collapse if it's the one already showing.
  // No-ops when the panel has no content.
  app.componentPanelShow = (id) => {
    if (id && components.has(id)) activeComponentId = id;
    panelUserClosed = false; _syncPanel(); _scheduleUiSave();
  };
  app.componentPanelHide = () => { panelUserClosed = true; _syncPanel(); _scheduleUiSave(); };
  app.componentPanelToggle = (id) => {
    const p = _panel();
    if (!p || !p.classList.contains('has-components')) return;
    if (p.classList.contains('open') && activeComponentId === id) app.componentPanelHide();
    else app.componentPanelShow(id);
  };
  // Minimize button (—) in the header — THE hide control: collapses the drawer
  // (tucks behind the pill) and exits the expanded overlay.
  const minBtn = document.getElementById('chat-component-panel-min');
  if (minBtn && !minBtn.dataset.wired) {
    minBtn.dataset.wired = '1';
    minBtn.addEventListener('click', () => {
      panelUserClosed = true;
      if (panelExpanded) _setExpanded(false);
      _syncPanel();
      _scheduleUiSave();
    });
  }

  // Expand button (⛶) — toggles the full-height overlay: pinned below the chat
  // header, covering the whole chat content + composer. Click again to restore
  // the normal drawer.
  const expandBtn = document.getElementById('chat-component-panel-expand');
  if (expandBtn && !expandBtn.dataset.wired) {
    expandBtn.dataset.wired = '1';
    expandBtn.addEventListener('click', () => {
      const p = _panel();
      if (!p || !p.classList.contains('has-components')) return;
      _setExpanded(!panelExpanded);
    });
  }
  window.addEventListener('resize', () => { if (panelExpanded) _positionExpanded(); });

  // Enter / leave the expanded overlay state.
  function _setExpanded(expanded) {
    const wrap = document.getElementById('chat-component-panel-wrap');
    if (!wrap) return;
    panelExpanded = expanded;
    if (expanded) {
      panelUserClosed = false;
      wrap.classList.add('expanded');
      wrap.style.width = '';          // expanded box is pinned by left/right, not width
      _positionExpanded();
      if (expandBtn) {
        expandBtn.title = 'Restore panel'; expandBtn.setAttribute('aria-label', 'Restore panel');
        expandBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3v3a2 2 0 0 1-2 2H3"/><path d="M21 8h-3a2 2 0 0 1-2-2V3"/><path d="M3 16h3a2 2 0 0 1 2 2v3"/><path d="M16 21v-3a2 2 0 0 1 2-2h3"/></svg>';
      }
    } else {
      wrap.classList.remove('expanded');
      wrap.style.top = ''; wrap.style.left = ''; wrap.style.right = ''; wrap.style.bottom = '';
      if (panelWidthPx) wrap.style.width = panelWidthPx + 'px';   // restore frozen drawer width
      if (expandBtn) {
        expandBtn.title = 'Expand panel'; expandBtn.setAttribute('aria-label', 'Expand panel');
        expandBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/></svg>';
      }
    }
    _syncPanel();
    _scheduleUiSave();
  }

  // Measure the chat column and pin the expanded overlay below the header,
  // covering the whole chat content + composer.
  function _positionExpanded() {
    const wrap = document.getElementById('chat-component-panel-wrap');
    const chatPanel = document.getElementById('chat-panel');
    if (!wrap || !chatPanel) return;
    const pr = chatPanel.getBoundingClientRect();
    let top = pr.top;
    const header = document.getElementById('chat-header');
    if (header) top = Math.max(top, header.getBoundingClientRect().bottom);
    const sub = document.getElementById('chat-sub-header');
    if (sub && sub.getBoundingClientRect().height > 0) {
      top = Math.max(top, sub.getBoundingClientRect().bottom);
    }
    const gap = 4;
    wrap.style.top = (top + gap) + 'px';
    wrap.style.left = (pr.left + gap) + 'px';
    wrap.style.right = (window.innerWidth - pr.right + gap) + 'px';
    wrap.style.bottom = (window.innerHeight - pr.bottom + gap) + 'px';
  }

  // Drag handles — BOTH the grabber bar and the header RESIZE the drawer's
  // height (drag up = taller, drag down = shorter, clamped to a range).
  // Dragging no longer minimizes — the (—) button is the minimize control.
  // A true click on the bar only OPENS a closed drawer; it never closes.
  const _PANEL_MIN_H = 90;
  const _PANEL_MAX_H = () => Math.min(560, Math.round(window.innerHeight * 0.6));
  function _currentHeight() {
    const p = _panel();
    const v = p && parseFloat(p.style.getPropertyValue('--panel-height'));
    return Number.isFinite(v) && v > 0 ? v : 150;
  }
  function _wirePanelDrag(handle, clickToggle) {
    if (!handle || handle.dataset.wired) return;
    handle.dataset.wired = '1';
    let _dragStartY = 0;
    let _startHeight = 150;
    let _dragged = false;        // became true once the pointer actually moved

    handle.addEventListener('pointerdown', (e) => {
      // The grab bar ITSELF is a <button> — only skip the press when it landed
      // on a DIFFERENT button (e.g. the header's expand/minimize controls).
      const hitBtn = e.target.closest('button');
      if (hitBtn && hitBtn !== handle) return;
      const p = _panel();
      if (!p || !p.classList.contains('has-components')) return;
      if (panelExpanded) return;                // expanded overlay: no resizing
      _dragStartY = e.clientY;
      _startHeight = _currentHeight();
      _dragged = false;
      p.classList.add('dragging');
      p.classList.remove('spring-back', 'spring-back-click');
      handle.classList.remove('spring-back', 'spring-back-click');
      handle.setPointerCapture(e.pointerId);
      e.preventDefault();
    });

    handle.addEventListener('pointermove', (e) => {
      if (!handle.hasPointerCapture(e.pointerId)) return;
      const p = _panel();
      if (!p) return;
      const dy = _dragStartY - e.clientY; // positive = dragging up (taller)
      if (Math.abs(dy) > 6) _dragged = true;
      // First upward pull from the closed state opens the drawer, then resizes.
      if (!p.classList.contains('open') && dy > 12) {
        panelUserClosed = false;
        _syncPanel();
      }
      const maxH = _PANEL_MAX_H();
      const h = Math.max(_PANEL_MIN_H, Math.min(maxH, _startHeight + dy));
      p.style.setProperty('--panel-height', h + 'px');
      panelHeight = h;
    });

    function _endDrag(e) {
      if (!handle.hasPointerCapture(e.pointerId)) return;
      handle.releasePointerCapture(e.pointerId);
      const p = _panel();
      if (!p) return;
      p.classList.remove('dragging');
      p.classList.remove('spring-back', 'spring-back-click');
      handle.classList.remove('spring-back', 'spring-back-click');
      p.style.removeProperty('--panel-drag-y');
      handle.style.removeProperty('--bar-drag-y');
      if (_dragged) _scheduleUiSave();   // persist the new drawer height
    }
    handle.addEventListener('pointerup', _endDrag);
    handle.addEventListener('pointercancel', _endDrag);

    if (clickToggle) {
      // True click on the bar (no drag): open a closed drawer. The bar never
      // closes it — minimizing is the (—) button's job.
      handle.addEventListener('click', (e) => {
        if (_dragged) { e.preventDefault(); e.stopPropagation(); return; }
        e.preventDefault();
        const p = _panel();
        if (!p || !p.classList.contains('has-components')) return;
        if (panelExpanded) return;
        if (p.classList.contains('open')) return;   // open only — no close here
        panelUserClosed = false;
        _syncPanel();
        p.classList.remove('spring-back', 'spring-back-click');
        handle.classList.remove('spring-back', 'spring-back-click');
        p.style.setProperty('--panel-drag-y', '-24px');
        handle.style.setProperty('--bar-drag-y', '-24px');
        requestAnimationFrame(() => {
          p.classList.add('spring-back-click');
          handle.classList.add('spring-back-click');
          p.style.removeProperty('--panel-drag-y');
          handle.style.removeProperty('--bar-drag-y');
        });
      });
    }
  }

  _wirePanelDrag(_bar(), true);          // grabber bar: drag-to-resize + click-to-open
  _wirePanelDrag(document.getElementById('chat-component-panel-head'), false);  // header: drag-to-resize only

  // Restore a saved panel layout after components mount (called from load()).
  // Clamped to the same bands the drag logic allows; the saved active tab is
  // honored only if that component still exists. Empty panel → stale state
  // ignored (nothing to show anyway).
  _applyUiState = (ui) => {
    if (!ui || !components.size) return;
    const pEl = _panel();
    if (typeof ui.height === 'number' && Number.isFinite(ui.height)) {
      const h = Math.max(_PANEL_MIN_H, Math.min(_PANEL_MAX_H(), Math.round(ui.height)));
      panelHeight = h;
      if (pEl) pEl.style.setProperty('--panel-height', h + 'px');
    }
    if (typeof ui.width === 'number' && Number.isFinite(ui.width) && ui.width > 0) {
      panelWidthPx = Math.round(ui.width);
      const wrap = document.getElementById('chat-component-panel-wrap');
      if (wrap && !wrap.classList.contains('expanded')) wrap.style.width = panelWidthPx + 'px';
    }
    if (typeof ui.active === 'string' && ui.active && components.has(ui.active)) {
      activeComponentId = ui.active;
    }
    if (ui.expanded) {
      _setExpanded(true);   // full-height overlay wins over minimized
    } else {
      panelUserClosed = !!ui.minimized;
      _syncPanel();
    }
  };

  // Flush any pending layout save when the tab closes/hides, so the last
  // minimize/resize survives a quick tab switch.
  window.addEventListener('pagehide', _flushUiSave);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') _flushUiSave();
  });

  app.loadChatComponents = load; app.presentChatComponent = present; app.attachPendingChatComponents = attachPendingInline;
}
