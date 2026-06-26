'use strict';

// ── Per-agent External Data Sources editor ────────────────────────────────────
// Mounted into the Agent Config tab by ui/main-panel/agents/js/tab-config.js. Renders:
//   • Attached sources list (with enable/detach + tool alias + inject toggle)
//   • Inline "Add data source" form that creates and immediately attaches
//   • Test / Introspect / Ingest buttons for each attached source

import { _esc, _btn } from './dom-utils.js';

const API = '/api/v1';

async function _api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(`${API}${path}`, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* may be empty */ }
  if (!res.ok) {
    const msg = (data && (data.detail || data.message)) || res.statusText;
    throw new Error(msg);
  }
  return data || {};
}

async function _fetchTypes() {
  try {
    const r = await _api('GET', '/data-sources/types');
    return r.types || [];
  } catch (_) {
    return [];
  }
}

async function _fetchUserSources(userId) {
  try {
    const r = await _api('GET', `/data-sources?user_id=${encodeURIComponent(userId)}`);
    return r.data_sources || [];
  } catch (_) {
    return [];
  }
}

async function _fetchAttachments(agentId, userId) {
  try {
    const r = await _api(
      'GET',
      `/agents/${encodeURIComponent(agentId)}/data-sources?user_id=${encodeURIComponent(userId)}`,
    );
    return r.attachments || [];
  } catch (_) {
    return [];
  }
}

// ── Per-type config field schemas ─────────────────────────────────────────────
// Drives the inline "Add" form. Each entry: { key, label, placeholder, required }
const TYPE_FIELDS = {
  sql_postgres: [
    { key: 'host',     label: 'Host',     placeholder: 'db.example.com', required: true },
    { key: 'port',     label: 'Port',     placeholder: '5432' },
    { key: 'database', label: 'Database', placeholder: 'myapp_prod',     required: true },
    { key: 'user',     label: 'User',     placeholder: 'readonly_user',  required: true },
    { key: 'ssl',      label: 'SSL',      placeholder: 'require / disable' },
  ],
  sql_mysql: [
    { key: 'host',     label: 'Host',     placeholder: 'db.example.com', required: true },
    { key: 'port',     label: 'Port',     placeholder: '3306' },
    { key: 'database', label: 'Database', placeholder: 'myapp_prod',     required: true },
    { key: 'user',     label: 'User',     placeholder: 'readonly_user',  required: true },
  ],
  doc_store: [
    { key: 'backend',    label: 'Backend',    placeholder: 'local | supabase_storage' },
    { key: 'path',       label: 'Path',       placeholder: 'C:/docs/wiki  (local)' },
    { key: 'bucket',     label: 'Bucket',     placeholder: 'docs  (supabase_storage)' },
    { key: 'prefix',     label: 'Prefix',     placeholder: 'wiki/' },
    { key: 'extensions', label: 'Extensions', placeholder: 'md,txt,markdown' },
  ],
  rest_api: [
    { key: 'base_url', label: 'Base URL', placeholder: 'https://api.example.com', required: true },
    { key: 'auth_header_name', label: 'Auth header', placeholder: 'Authorization' },
  ],
  web_search_domain: [
    { key: 'domain', label: 'Domain', placeholder: 'support.acme.com', required: true },
    { key: 'hint',   label: 'Extra terms (optional)', placeholder: 'docs OR guides' },
  ],
};

function _typeFieldsFor(typeName) {
  return TYPE_FIELDS[typeName] || [];
}

function _parseConfigPayload(typeName, formValues) {
  const fields = _typeFieldsFor(typeName);
  const out = {};
  for (const f of fields) {
    let v = formValues[f.key];
    if (v == null) continue;
    v = String(v).trim();
    if (!v) continue;
    if (f.key === 'port') out[f.key] = parseInt(v, 10);
    else if (f.key === 'extensions') out[f.key] = v.split(',').map(s => s.trim()).filter(Boolean);
    else out[f.key] = v;
  }
  return out;
}

// ── Public API: mount() ───────────────────────────────────────────────────────
// Two render modes:
//   • default — a self-contained collapsible card (own header + "N attached"
//     badge), matching the legacy LLM card pattern.
//   • opts.bare — render the editor body DIRECTLY into `host` with no card/header
//     (used when the host is already an expandable row, e.g. the Config-tab
//     Settings table). The attachment count is reported via opts.onCount(n)
//     instead of an internal badge, so the host row can show its own status.
export async function mount(host, agent, userId, opts = {}) {
  if (!host || !agent || !userId) return;
  host.innerHTML = '';
  const bare = !!opts.bare;

  let badge = null;
  let body;
  if (bare) {
    body = host;
  } else {
    const card = document.createElement('div');
    card.className = 'agents-llm-card';
    host.appendChild(card);

    // Header
    const header = document.createElement('div');
    header.className = 'agents-llm-header';
    const title = document.createElement('span');
    title.className = 'agents-llm-title';
    title.textContent = 'External Data Sources';
    badge = document.createElement('span');
    badge.className = 'agents-llm-badge';
    badge.textContent = '…';
    const chevron = document.createElement('span');
    chevron.className = 'agents-llm-chevron';
    chevron.innerHTML =
      '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" ' +
      'stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>';
    header.appendChild(title);
    header.appendChild(badge);
    header.appendChild(chevron);
    card.appendChild(header);

    // Body (collapsed by default)
    body = document.createElement('div');
    body.className = 'agents-llm-body';
    body.style.display = 'none';
    card.appendChild(body);

    header.addEventListener('click', () => {
      const open = body.style.display === 'none';
      body.style.display = open ? 'flex' : 'none';
      chevron.style.transform = open ? 'rotate(90deg)' : 'rotate(0deg)';
    });
  }

  const hint = document.createElement('span');
  hint.className = 'agents-field-hint';
  hint.style.display = 'block';
  hint.textContent =
    'Attach Postgres DBs, document folders, or domain-restricted website search to this agent. ' +
    'Each source becomes a tool the agent can call.';
  body.appendChild(hint);

  const listWrap = document.createElement('div');
  listWrap.className = 'agents-data-sources-list';
  listWrap.style.cssText = 'margin-top:4px;display:flex;flex-direction:column;gap:8px;';
  body.appendChild(listWrap);

  const addBox = document.createElement('div');
  addBox.className = 'agents-data-sources-add';
  addBox.style.cssText =
    'margin-top:6px;padding:10px;border:1px dashed var(--border);' +
    'border-radius:6px;background:var(--bg-1);';
  body.appendChild(addBox);

  function _updateBadge(count) {
    if (typeof opts.onCount === 'function') opts.onCount(count);
    if (!badge) return;
    if (!count) {
      badge.textContent = 'None';
      badge.className = 'agents-llm-badge';
    } else {
      badge.textContent = `${count} attached`;
      badge.className = 'agents-llm-badge custom';
    }
  }

  const types = await _fetchTypes();
  const refresh = async () => {
    const count = await _renderList(listWrap, agent, userId);
    _updateBadge(count);
  };
  await refresh();
  _renderAddForm(addBox, types, agent, userId, refresh);
}

async function _renderList(listWrap, agent, userId) {
  listWrap.innerHTML = '<div class="agents-field-hint">Loading attached sources…</div>';
  const attachments = await _fetchAttachments(agent.id, userId);
  listWrap.innerHTML = '';
  if (!attachments.length) {
    const empty = document.createElement('div');
    empty.className = 'agents-field-hint';
    empty.textContent = 'No data sources attached. Use the form below to add one.';
    listWrap.appendChild(empty);
    return 0;
  }
  for (const att of attachments) {
    listWrap.appendChild(_renderAttachmentRow(att, agent, userId, () => _renderList(listWrap, agent, userId)));
  }
  return attachments.length;
}

function _renderAttachmentRow(att, agent, userId, onChanged) {
  const row = document.createElement('div');
  row.style.cssText =
    'border:1px solid var(--border);border-radius:6px;padding:10px;' +
    'background:var(--bg-1);display:flex;flex-direction:column;gap:6px;';

  const head = document.createElement('div');
  head.style.cssText = 'display:flex;align-items:center;gap:8px;flex-wrap:wrap;';
  const title = document.createElement('span');
  title.style.cssText = 'font-weight:600;color:var(--fg-1)';
  title.textContent = att.name || '(unnamed)';
  const typeTag = document.createElement('span');
  typeTag.style.cssText =
    'font-size:11px;padding:2px 6px;border-radius:4px;background:var(--bg-2);' +
    'color:var(--fg-2);';
  typeTag.textContent = att.type || '?';
  const statusTag = document.createElement('span');
  statusTag.style.cssText =
    'font-size:11px;padding:2px 6px;border-radius:4px;' +
    `background:${att.status === 'active' ? 'rgba(var(--success-rgb), 0.18)' : att.status === 'error' ? 'rgba(var(--danger-rgb), 0.18)' : 'var(--bg-2)'};` +
    `color:${att.status === 'active' ? 'var(--success)' : att.status === 'error' ? 'var(--danger)' : 'var(--fg-2)'};`;
  statusTag.textContent = att.status || 'unverified';
  head.appendChild(title);
  head.appendChild(typeTag);
  head.appendChild(statusTag);
  row.appendChild(head);

  const ctrls = document.createElement('div');
  ctrls.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;';

  // tool alias
  const aliasLbl = document.createElement('label');
  aliasLbl.style.cssText = 'font-size:12px;color:var(--fg-2);display:flex;align-items:center;gap:4px;';
  aliasLbl.textContent = 'Tool alias';
  const aliasInput = document.createElement('input');
  aliasInput.type = 'text';
  aliasInput.className = 'agents-input';
  aliasInput.style.width = '160px';
  aliasInput.value = att.tool_alias || '';
  aliasInput.placeholder = 'auto';
  aliasLbl.appendChild(aliasInput);

  // enabled toggle
  const enabledLbl = document.createElement('label');
  enabledLbl.style.cssText = 'font-size:12px;color:var(--fg-2);display:flex;align-items:center;gap:4px;cursor:pointer;';
  const enabledCb = document.createElement('input');
  enabledCb.type = 'checkbox';
  enabledCb.checked = !!att.enabled;
  enabledLbl.appendChild(enabledCb);
  enabledLbl.appendChild(document.createTextNode(' Enabled'));

  // inject schema toggle
  const injLbl = document.createElement('label');
  injLbl.style.cssText = 'font-size:12px;color:var(--fg-2);display:flex;align-items:center;gap:4px;cursor:pointer;';
  const injCb = document.createElement('input');
  injCb.type = 'checkbox';
  injCb.checked = !!att.inject_schema_in_prompt;
  injLbl.appendChild(injCb);
  injLbl.appendChild(document.createTextNode(' Inject schema in prompt'));

  ctrls.appendChild(aliasLbl);
  ctrls.appendChild(enabledLbl);
  ctrls.appendChild(injLbl);
  row.appendChild(ctrls);

  // Action buttons row
  const actions = document.createElement('div');
  actions.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap;';

  const saveBtn = _btn('Save', 'agents-btn primary');
  const testBtn = _btn('Test');
  const introBtn = _btn('Introspect');
  const ingestBtn = _btn('Ingest');
  const detachBtn = _btn('Detach', 'agents-btn danger');

  if (att.type !== 'doc_store') ingestBtn.style.display = 'none';

  const status = document.createElement('span');
  status.style.cssText = 'font-size:12px;color:var(--fg-2);margin-left:8px;';

  saveBtn.addEventListener('click', async () => {
    status.textContent = 'Saving…';
    try {
      await _api(
        'PUT',
        `/agents/${encodeURIComponent(agent.id)}/data-sources/${encodeURIComponent(att.data_source_id)}`,
        {
          user_id: userId,
          tool_alias: aliasInput.value || null,
          enabled: enabledCb.checked,
          inject_schema_in_prompt: injCb.checked,
        },
      );
      status.textContent = '✓ Saved';
    } catch (e) {
      status.textContent = `Error: ${e.message}`;
    }
  });

  testBtn.addEventListener('click', async () => {
    status.textContent = 'Testing…';
    try {
      const r = await _api(
        'POST',
        `/data-sources/${encodeURIComponent(att.data_source_id)}/test?user_id=${encodeURIComponent(userId)}`,
      );
      status.textContent = (r.ok ? '✓ ' : '✗ ') + (r.message || '');
      onChanged && onChanged();
    } catch (e) {
      status.textContent = `Error: ${e.message}`;
    }
  });

  introBtn.addEventListener('click', async () => {
    status.textContent = 'Introspecting…';
    try {
      const r = await _api(
        'POST',
        `/data-sources/${encodeURIComponent(att.data_source_id)}/introspect?user_id=${encodeURIComponent(userId)}`,
      );
      const tableCount = (r.schema_cache?.tables || []).length;
      const fileCount = r.schema_cache?.file_count;
      const domain = r.schema_cache?.domain;
      const bits = [];
      if (tableCount) bits.push(`${tableCount} tables`);
      if (fileCount !== undefined) bits.push(`${fileCount} files`);
      if (domain) bits.push(domain);
      status.textContent = `✓ ${bits.join(' · ') || 'schema cached'}`;
      onChanged && onChanged();
    } catch (e) {
      status.textContent = `Error: ${e.message}`;
    }
  });

  ingestBtn.addEventListener('click', async () => {
    status.textContent = 'Ingesting…';
    try {
      const r = await _api(
        'POST',
        `/data-sources/${encodeURIComponent(att.data_source_id)}/ingest?user_id=${encodeURIComponent(userId)}`,
      );
      status.textContent =
        `✓ ${r.files_changed || 0} files · ${r.chunks_written || 0} chunks` +
        (r.errors && r.errors.length ? ` · ${r.errors.length} errors` : '');
      onChanged && onChanged();
    } catch (e) {
      status.textContent = `Error: ${e.message}`;
    }
  });

  detachBtn.addEventListener('click', async () => {
    if (!confirm(`Detach data source "${att.name}" from this agent?`)) return;
    try {
      await _api(
        'DELETE',
        `/agents/${encodeURIComponent(agent.id)}/data-sources/${encodeURIComponent(att.data_source_id)}?user_id=${encodeURIComponent(userId)}`,
      );
      onChanged && onChanged();
    } catch (e) {
      status.textContent = `Error: ${e.message}`;
    }
  });

  actions.appendChild(saveBtn);
  actions.appendChild(testBtn);
  actions.appendChild(introBtn);
  actions.appendChild(ingestBtn);
  actions.appendChild(detachBtn);
  actions.appendChild(status);
  row.appendChild(actions);

  return row;
}

function _renderAddForm(host, types, agent, userId, onChanged) {
  host.innerHTML = '';

  const title = document.createElement('div');
  title.style.cssText = 'font-weight:600;margin-bottom:6px;color:var(--fg-1);';
  title.textContent = 'Add data source';
  host.appendChild(title);

  const grid = document.createElement('div');
  grid.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;gap:8px;';
  host.appendChild(grid);

  const nameInput = document.createElement('input');
  nameInput.type = 'text';
  nameInput.className = 'agents-input';
  nameInput.placeholder = 'Source name (e.g. petstore_inventory)';
  grid.appendChild(_wrapField('Name', nameInput));

  const typeSel = document.createElement('select');
  typeSel.className = 'agents-input';
  for (const t of types) {
    const opt = document.createElement('option');
    opt.value = t.type;
    opt.textContent = t.display_name || t.type;
    typeSel.appendChild(opt);
  }
  if (!types.length) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = '(no connector types registered)';
    typeSel.appendChild(opt);
  }
  grid.appendChild(_wrapField('Type', typeSel));

  const cfgWrap = document.createElement('div');
  cfgWrap.style.cssText = 'grid-column:1/-1;display:grid;grid-template-columns:1fr 1fr;gap:8px;';
  grid.appendChild(cfgWrap);

  const cfgInputs = {};
  function _rebuildConfigFields() {
    cfgWrap.innerHTML = '';
    for (const f of _typeFieldsFor(typeSel.value)) {
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.className = 'agents-input';
      inp.placeholder = f.placeholder || '';
      cfgInputs[f.key] = inp;
      cfgWrap.appendChild(_wrapField(f.label + (f.required ? ' *' : ''), inp));
    }
  }
  typeSel.addEventListener('change', _rebuildConfigFields);
  _rebuildConfigFields();

  // Optional credential picker — for now a free-text auth_element_id input.
  // Full Integrations dropdown wiring can replace this in a follow-up.
  const credInput = document.createElement('input');
  credInput.type = 'text';
  credInput.className = 'agents-input';
  credInput.placeholder = 'auth_elements.id (optional)';
  cfgWrap.appendChild(_wrapField('Credential id (optional)', credInput));

  const actions = document.createElement('div');
  actions.style.cssText = 'display:flex;gap:8px;margin-top:8px;align-items:center;';
  const createBtn = _btn('Create & Attach', 'agents-btn primary');
  const msg = document.createElement('span');
  msg.style.cssText = 'font-size:12px;color:var(--fg-2);';
  actions.appendChild(createBtn);
  actions.appendChild(msg);
  host.appendChild(actions);

  createBtn.addEventListener('click', async () => {
    msg.textContent = '';
    const name = (nameInput.value || '').trim();
    if (!name) { msg.textContent = 'Name is required'; return; }
    if (!typeSel.value) { msg.textContent = 'Pick a type'; return; }
    const formVals = {};
    for (const [k, inp] of Object.entries(cfgInputs)) formVals[k] = inp.value;
    const config = _parseConfigPayload(typeSel.value, formVals);
    try {
      msg.textContent = 'Creating…';
      const created = await _api('POST', '/data-sources', {
        user_id: userId,
        name,
        type: typeSel.value,
        config,
        auth_element_id: credInput.value.trim() || null,
        safety_policy: {},
      });
      const dsId = created.data_source?.id;
      if (!dsId) throw new Error('create failed (no id)');
      msg.textContent = 'Attaching…';
      await _api('POST', `/agents/${encodeURIComponent(agent.id)}/data-sources`, {
        user_id: userId,
        data_source_id: dsId,
        inject_schema_in_prompt: true,
      });
      msg.textContent = '✓ Added';
      nameInput.value = '';
      for (const inp of Object.values(cfgInputs)) inp.value = '';
      credInput.value = '';
      onChanged && onChanged();
    } catch (e) {
      msg.textContent = `Error: ${e.message}`;
    }
  });
}

function _wrapField(label, input) {
  const w = document.createElement('div');
  w.style.cssText = 'display:flex;flex-direction:column;gap:3px;';
  const lab = document.createElement('label');
  lab.textContent = label;
  lab.style.cssText = 'font-size:11px;color:var(--fg-2);';
  w.appendChild(lab);
  input.style.width = '100%';
  w.appendChild(input);
  return w;
}

// Also expose a global for non-module callers in agents.js.
if (typeof window !== 'undefined') {
  window.AgentDataSourcesUI = { mount };
}
