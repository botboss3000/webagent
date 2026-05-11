"use strict";

import { app } from "../state.js";
import { getPKColumns, getDisplayColumns, saveColumnOrder } from "./columns.js";
import { initColumnResize } from "./columnResize.js";
import { formatJsonAsHtml } from "../json-tree.js";
import { apiPath } from "../config.js";
import { authUrl } from "../left-login.js";

/**
 * ── Persistence helpers ──
 */
function saveSortState() {
  localStorage.setItem('dbSortState', JSON.stringify(app.dbSortState));
}
function saveExclusions() {
  localStorage.setItem('dbExclusions', JSON.stringify(app.dbExclusions));
}

/**
 * Load sort + exclusion state from localStorage on init.
 */
export function loadPersistedDbState() {
  try {
    const sort = localStorage.getItem('dbSortState');
    if (sort) app.dbSortState = JSON.parse(sort);
  } catch (e) { /* ignore */ }
  try {
    const excl = localStorage.getItem('dbExclusions');
    if (excl) app.dbExclusions = JSON.parse(excl);
  } catch (e) { /* ignore */ }
}

/**
 * Get active sort for current table (from state or default).
 */
function getSortForTable(tableName) {
  const s = app.dbSortState[tableName];
  if (s && s.col && s.dir) return s;
  return { col: 'created_at', dir: 'DESC' };
}

/**
 * Get active exclusions for a column on current table.
 */
function getExclusionsForColumn(tableName, colName) {
  if (app.dbExclusions[tableName] && app.dbExclusions[tableName][colName]) {
    return new Set(app.dbExclusions[tableName][colName]);
  }
  return new Set();
}

/**
 * Close any open column popup.
 */
function closeColPopup() {
  if (app.dbColPopup && app.dbColPopup.el) {
    app.dbColPopup.el.remove();
    app.dbColPopup = null;
  }
  // Also remove any lingering popups
  document.querySelectorAll('.db-col-popup').forEach(el => el.remove());
}

/**
 * Build and show the column filter/sort popup.
 */
async function openColPopup(th, tableName, colName) {
  // Close any existing popup first
  closeColPopup();

  // Fetch distinct values
  let values = [];
  let total = 0;
  try {
    const dbName = document.getElementById('db-select').value;
    const url = authUrl(apiPath(
      `/api/v1/db/column-values?db=${encodeURIComponent(dbName)}&table=${encodeURIComponent(tableName)}&column=${encodeURIComponent(colName)}`
    ));
    const res = await fetch(url);
    if (res.status === 401) {
      localStorage.removeItem('auth_token');
      window.location.reload();
      return;
    }
    const data = await res.json();
    values = data.values || [];
    total = data.total || 0;
  } catch (e) {
    values = [];
    total = 0;
  }

  const excluded = getExclusionsForColumn(tableName, colName);
  const sort = getSortForTable(tableName);

  // Build popup element
  const popup = document.createElement('div');
  popup.className = 'db-col-popup';

  // Sort section
  const sortAscActive = sort.col === colName && sort.dir === 'ASC';
  const sortDescActive = sort.col === colName && sort.dir === 'DESC';

  popup.innerHTML = `
    <div class="db-popup-sort">
      <button class="db-popup-sort-btn ${sortAscActive ? 'active' : ''}" data-sort="ASC">
        <span class="db-popup-sort-arrow">▲</span> Sort Ascending
      </button>
      <button class="db-popup-sort-btn ${sortDescActive ? 'active' : ''}" data-sort="DESC">
        <span class="db-popup-sort-arrow">▼</span> Sort Descending
      </button>
    </div>
    <div class="db-popup-search-wrap">
      <input type="text" class="db-popup-search" placeholder="Filter by values..." />
    </div>
    <div class="db-popup-select-header">
      <a href="#" class="db-popup-select-all">Select all</a>
      <a href="#" class="db-popup-clear">Clear</a>
    </div>
    <div class="db-popup-items"></div>
    <div class="db-popup-status"></div>
    <div class="db-popup-actions">
      <button class="db-popup-apply-btn">Apply</button>
    </div>
  `;

  // Position the popup
  const thRect = th.getBoundingClientRect();
  const viewer = document.getElementById('db-table-view');
  const viewerRect = viewer.getBoundingClientRect();
  popup.style.left = (thRect.left - viewerRect.left) + 'px';
  popup.style.top = (thRect.bottom - viewerRect.top) + 'px';
  popup.style.minWidth = Math.max(180, thRect.width) + 'px';

  viewer.appendChild(popup);

  const itemsEl = popup.querySelector('.db-popup-items');
  const statusEl = popup.querySelector('.db-popup-status');
  const searchInput = popup.querySelector('.db-popup-search');

  // Save popup ref
  app.dbColPopup = { el: popup, table: tableName, column: colName, values, excluded, total };

  /**
   * Render the checklist from current popup state.
   */
  function renderItems(filterText) {
    if (!app.dbColPopup) return;
    const { values: allValues, excluded } = app.dbColPopup;
    const ft = (filterText || '').toLowerCase();
    const isClearAll = excluded.has('__ALL__');

    const filtered = ft
      ? allValues.filter(v => v !== null && String(v).toLowerCase().includes(ft))
      : allValues;

    const selectedCount = isClearAll ? 0 : allValues.length - excluded.size;

    let html = '';
    for (const v of filtered) {
      const valStr = v === null ? '__NULL__' : String(v);
      const checked = isClearAll ? false : !excluded.has(valStr);
      const display = v === null ? '<i>NULL</i>' : String(v).replace(/</g, '&lt;').replace(/>/g, '&gt;');
      html += `<label class="db-popup-item ${checked ? '' : 'unchecked'}">
        <input type="checkbox" class="db-popup-check" data-val="${valStr.replace(/"/g, '&quot;')}" ${checked ? 'checked' : ''} />
        <span class="db-popup-val">${display}</span>
      </label>`;
    }

    const shown = filtered.length;
    const totalNote = isClearAll ? `${allValues.length} values` : `${allValues.length} values total`;
    const filteredNote = ft && ft.length > 0
      ? `${shown} of ${allValues.length} matched`
      : totalNote;

    itemsEl.innerHTML = html || '<div class="db-popup-empty">No matching values</div>';
    statusEl.innerHTML = `${filteredNote} &nbsp;|&nbsp; ${selectedCount} selected` +
      (excluded.size > 0 && !isClearAll ? ` &nbsp;<span style="color:#e9b143;">(${excluded.size} excluded)</span>` : '');

    // Wire checkbox changes
    itemsEl.querySelectorAll('.db-popup-check').forEach(cb => {
      cb.addEventListener('change', () => {
        const val = cb.dataset.val;
        if (!app.dbColPopup) return;
        const excl = app.dbColPopup.excluded;
        // Remove __ALL__ sentinel if present — we're now using individual exclusions
        excl.delete('__ALL__');
        if (cb.checked) {
          excl.delete(val);
        } else {
          excl.add(val);
        }
        // If every value is now excluded, switch to __ALL__ sentinel
        if (excl.size >= allValues.length) {
          excl.clear();
          excl.add('__ALL__');
        }
        // Persist exclusions to localStorage but don't re-query yet
        if (!app.dbExclusions[tableName]) app.dbExclusions[tableName] = {};
        app.dbExclusions[tableName][colName] = Array.from(excl);
        saveExclusions();
        // Re-render the checkbox list only
        renderItems(searchInput.value);
      });
    });
  }

  renderItems('');

  // Search input
  searchInput.addEventListener('input', () => {
    renderItems(searchInput.value);
  });

  // Select all
  popup.querySelector('.db-popup-select-all').addEventListener('click', (e) => {
    e.preventDefault();
    if (!app.dbColPopup) return;
    app.dbColPopup.excluded.clear();
    if (!app.dbExclusions[tableName]) app.dbExclusions[tableName] = {};
    app.dbExclusions[tableName][colName] = [];
    saveExclusions();
    renderItems(searchInput.value);
  });

  // Clear (exclude all)
  popup.querySelector('.db-popup-clear').addEventListener('click', (e) => {
    e.preventDefault();
    if (!app.dbColPopup) return;
    app.dbColPopup.excluded = new Set(['__ALL__']);
    if (!app.dbExclusions[tableName]) app.dbExclusions[tableName] = {};
    app.dbExclusions[tableName][colName] = ['__ALL__'];
    saveExclusions();
    renderItems(searchInput.value);
  });

  // Sort buttons
  popup.querySelectorAll('.db-popup-sort-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const dir = btn.dataset.sort;
      if (!app.dbSortState[tableName]) app.dbSortState[tableName] = {};
      app.dbSortState[tableName] = { col: colName, dir };
      saveSortState();
      closeColPopup();
      queryTable(tableName);
    });
  });

  // Apply button — re-query with accumulated exclusions
  popup.querySelector('.db-popup-apply-btn').addEventListener('click', () => {
    closeColPopup();
    queryTable(tableName);
  });

  // Prevent clicks inside popup from bubbling to document
  popup.addEventListener('click', (e) => e.stopPropagation());

  // Focus search
  setTimeout(() => searchInput.focus(), 50);
}

// Global click-outside handler — registered once
let _popupOutsideHandler = null;
function ensurePopupOutsideHandler() {
  if (_popupOutsideHandler) return;
  _popupOutsideHandler = (e) => {
    if (app.dbColPopup && app.dbColPopup.el) {
      if (!app.dbColPopup.el.contains(e.target)) {
        const tableName = app.dbColPopup.table;
        const wasOpen = !!app.dbColPopup;
        closeColPopup();
        if (wasOpen) queryTable(tableName);
      }
    }
  };
  document.addEventListener('click', _popupOutsideHandler);
}

function cancelEditing() {
  if (app.editingCell) {
    app.editingCell = null;
    if (app.dbCurrentResult) {
      renderTableData(app.dbCurrentResult);
    }
  }
}

/**
 * Build multi-column exclusion filters as JSON for the query URL.
 */
function getExclusionParams(tableName) {
  if (!app.dbExclusions[tableName]) return '';
  const specs = [];
  for (const [col, vals] of Object.entries(app.dbExclusions[tableName])) {
    if (!vals || vals.length === 0) continue;
    // Remove __NULL__ sentinel from the exclusion list — it needs special SQL handling
    const realVals = vals.filter(v => v !== '__NULL__');
    const includeNull = !vals.includes('__NULL__');
    const spec = { col, op: 'not_in', include_null: includeNull };
    if (realVals.length > 0) {
      spec.val = realVals.join(',');
    }
    specs.push(spec);
  }
  if (specs.length === 0) return '';
  return 'filters_json=' + encodeURIComponent(JSON.stringify(specs));
}

async function queryWithFilters() {
  if (app.dbSelectedTable) await queryTable(app.dbSelectedTable);
}

function renderTableData(result, silent) {
  const data = document.getElementById('db-table-data');
  const pkCols = getPKColumns(result.table);
  const displayCols = getDisplayColumns(result.columns, result.table);
  const tableName = result.table;
  const sort = getSortForTable(tableName);

  if (silent) {
    const tbody = data.querySelector('table.db-table tbody');
    if (tbody) {
      const existingRows = tbody.querySelectorAll('tr.db-row');
      for (let ri = 0; ri < result.rows.length && ri < existingRows.length; ri++) {
        const row = result.rows[ri];
        const cells = existingRows[ri].querySelectorAll('td');
        for (let ci = 0; ci < displayCols.length && ci < cells.length; ci++) {
          const col = displayCols[ci];
          const val = row[col];
          const display = val === null ? 'NULL' : String(val);
          const cell = cells[ci];
          if (cell.dataset.val !== String(val)) {
            cell.textContent = display;
            cell.dataset.val = val === null ? 'null' : String(val);
            cell.className = 'db-cell' + (val === null ? ' col-null' : '');
          }
        }
      }
      if (result.rows.length !== existingRows.length) {
        return renderTableData(result, false);
      }
      // Update header arrows (sort state may have changed)
      updateHeaderSortArrows(tableName);
      // Update filter indicators
      updateFilterIndicators(tableName);
      return;
    }
  }

  // ── Full render ──

  // Column width strategy
  function getColWidth(table, col) {
    const contentTables = ['interactions', 'context_defaults', 'context_templates', 'context'];
    if (table === 'interactions' && col === 'input') return '300px';
    if (contentTables.includes(table) && col === 'content') return '300px';
    if (app.COL_WIDTHS[col]) return app.COL_WIDTHS[col];
    const px = Math.max(40, col.length * 7.5 + 20);
    return Math.round(px) + 'px';
  }
  function isFixedCol(table, col) {
    const fixedCols = ['interactions/input', 'interactions/content', 'context_defaults/content', 'context_templates/content', 'context/content'];
    return fixedCols.includes(table + '/' + col);
  }

  let html = '<table class="db-table"><thead>';

  // Header row — column names clickable to open filter popup
  html += '<tr>';
  for (const col of displayCols) {
    const w = getColWidth(result.table, col);
    const style = w ? ` style="width:${w};min-width:${w};max-width:${w}"` : '';
    const noResize = isFixedCol(result.table, col);
    const isSortCol = col === sort.col;
    const sortArrow = isSortCol ? (sort.dir === 'ASC' ? ' ▲' : ' ▼') : '';
    const hasFilter = app.dbExclusions[tableName] && app.dbExclusions[tableName][col] && app.dbExclusions[tableName][col].length > 0;
    const filterClass = hasFilter ? ' filtered' : '';
    html += `<th class="db-th${filterClass}" data-col="${col}"${style} draggable="true">
      <span class="th-text" data-col="${col}">
        <span class="th-name">${col}</span>
        <span class="th-sort">${sortArrow}</span>
        <span class="th-funnel">${hasFilter ? ' 🔽' : ''}</span>
      </span>
      ${noResize ? '' : '<span class="th-resize"></span>'}
    </th>`;
  }
  html += '</tr>';

  html += '</thead><tbody>';

  function fmtCell(val) {
    if (val === null) return { html: 'NULL', isJson: false };
    if (typeof val === 'string' && val.length > 1 && (val[0] === '{' || val[0] === '[')) {
      const jsonHtml = formatJsonAsHtml(val);
      if (jsonHtml) return { html: jsonHtml, isJson: true };
    }
    return { html: String(val).replace(/</g, '&lt;').replace(/>/g, '&gt;'), isJson: false };
  }

  for (let ri = 0; ri < result.rows.length; ri++) {
    const row = result.rows[ri];
    const rowClass = ri % 2 === 0 ? 'db-row' : 'db-row db-row-even';
    const isInteractions = result.table === 'interactions';
    const trStyle = isInteractions ? ' style="height:100px"' : '';
    html += `<tr class="${rowClass}" data-ri="${ri}"${trStyle}>`;
    for (const col of displayCols) {
      const val = row[col];
      const w = getColWidth(result.table, col);
      const style = w ? ` style="width:${w};min-width:${w};max-width:${w}"` : '';
      const cls = val === null ? 'col-null' : '';
      const { html: display, isJson } = fmtCell(val);
      const safeVal = String(val).replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      html += `<td class="db-cell ${cls}"${style} data-row="${ri}" data-col="${col}" data-val="${safeVal}">${isJson ? `<div class="db-cell-json">${display}</div>` : `<pre class="db-cell-pre">${display}</pre>`}<button class="db-cell-expand" title="Expand editor">↗</button></td>`;
    }
    html += '</tr>';
    html += `<tr class="db-row-resize-row" data-ri="${ri}"><td colspan="${displayCols.length}" class="db-row-resize-td"><div class="db-row-resize-handle" data-ri="${ri}"></div></td></tr>`;
  }
  html += '</tbody></table>';
  data.innerHTML = html;

  // ── Column name click → open popup ──
  data.querySelectorAll('.th-text').forEach(span => {
    span.addEventListener('click', (e) => {
      e.stopPropagation();
      const th = span.closest('.db-th');
      const col = th.dataset.col;
      openColPopup(th, tableName, col);
    });
  });

  // ── Drag-to-reorder columns ──
  let dragCol = null;
  let dragTh = null;
  let dragPlaceholder = null;

  data.querySelectorAll('.db-th').forEach(th => {
    th.addEventListener('dragstart', (e) => {
      dragCol = th.dataset.col;
      dragTh = th;
      e.dataTransfer.effectAllowed = 'move';
      th.style.opacity = '0.4';
    });
    th.addEventListener('dragend', () => {
      if (dragTh) dragTh.style.opacity = '';
      if (dragPlaceholder) dragPlaceholder.remove();
      dragCol = null; dragTh = null; dragPlaceholder = null;
      data.querySelectorAll('.db-th').forEach(t => t.classList.remove('drop-target'));
    });
    th.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      data.querySelectorAll('.db-th').forEach(t => t.classList.remove('drop-target'));
      th.classList.add('drop-target');
    });
    th.addEventListener('dragleave', () => {
      th.classList.remove('drop-target');
    });
    th.addEventListener('drop', (e) => {
      e.preventDefault();
      if (!dragCol || dragCol === th.dataset.col) return;

      const currentOrder = getDisplayColumns(result.columns, result.table);
      const fromIdx = currentOrder.indexOf(dragCol);
      const toIdx = currentOrder.indexOf(th.dataset.col);
      if (fromIdx === -1 || toIdx === -1) return;

      currentOrder.splice(fromIdx, 1);
      currentOrder.splice(toIdx, 0, dragCol);
      saveColumnOrder(result.table, currentOrder);

      if (dragTh) dragTh.style.opacity = '';
      dragCol = null; dragTh = null;
      data.querySelectorAll('.db-th').forEach(t => t.classList.remove('drop-target'));
      queryTable(app.dbSelectedTable);
    });
  });

  // ── Drag row resize handles ──
  let resizeHandle = null;
  let resizeStartY = 0;
  let resizeStartHeight = 0;
  let resizeRow = null;

  function onMouseMove(e) {
    if (!resizeHandle) return;
    const delta = e.clientY - resizeStartY;
    const newHeight = Math.max(24, resizeStartHeight + delta);
    resizeRow.querySelectorAll('td').forEach(td => {
      td.style.height = newHeight + 'px';
      td.style.maxHeight = 'none';
    });
    resizeRow.querySelectorAll('.db-cell-pre').forEach(pre => {
      pre.style.maxHeight = 'none';
    });
  }

  function onMouseUp() {
    if (resizeHandle) {
      resizeHandle.classList.remove('resizing');
      resizeHandle = null;
      resizeRow = null;
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    }
  }

  data.querySelectorAll('.db-row-resize-handle').forEach(handle => {
    handle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      const ri = parseInt(handle.dataset.ri);
      const row = data.querySelector(`.db-row[data-ri="${ri}"]`);
      if (!row) return;
      resizeHandle = handle;
      resizeRow = row;
      resizeStartY = e.clientY;
      resizeStartHeight = row.getBoundingClientRect().height;
      handle.classList.add('resizing');
      document.body.style.cursor = 'row-resize';
      document.body.style.userSelect = 'none';
      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
    });
  });

  initColumnResize();
  ensurePopupOutsideHandler();
}

/**
 * Update sort arrows on existing header without full re-render.
 */
function updateHeaderSortArrows(tableName) {
  const sort = getSortForTable(tableName);
  document.querySelectorAll('.db-th .th-sort').forEach(span => {
    const col = span.closest('.th-text').dataset.col;
    if (col === sort.col) {
      span.textContent = sort.dir === 'ASC' ? ' ▲' : ' ▼';
    } else {
      span.textContent = '';
    }
  });
}

/**
 * Update filter funnel indicators on existing headers.
 */
function updateFilterIndicators(tableName) {
  document.querySelectorAll('.db-th').forEach(th => {
    const col = th.dataset.col;
    const hasFilter = app.dbExclusions[tableName] && app.dbExclusions[tableName][col] && app.dbExclusions[tableName][col].length > 0;
    const funnel = th.querySelector('.th-funnel');
    if (hasFilter) {
      th.classList.add('filtered');
      if (funnel) funnel.textContent = ' 🔽';
    } else {
      th.classList.remove('filtered');
      if (funnel) funnel.textContent = '';
    }
  });
}

async function queryTable(tableName, opts) {
  const dbName = document.getElementById('db-select').value;
  const sort = getSortForTable(tableName);
  const sortCol = sort.col;
  const sortDir = sort.dir;
  const data = document.getElementById('db-table-data');
  const silent = opts?.silent;

  if (!silent) {
    data.innerHTML = '';
    cancelEditing();
    closeColPopup();
    if (opts?.keepOffset) {
      // keep current offset
    } else {
      app.dbPageOffset = 0;
    }
  }

  let url = apiPath(`/api/v1/db/query?db=${encodeURIComponent(dbName)}&table=${encodeURIComponent(tableName)}&limit=${app.dbPageLimit}&offset=${app.dbPageOffset}`);
  url += `&order_by=${encodeURIComponent(sortCol)}&order_dir=${sortDir}`;

  // Add exclusion filters
  const exclParams = getExclusionParams(tableName);
  if (exclParams) {
    url += '&' + exclParams;
  }

  // Legacy filter support (kept for backward compat, not used by new UI)
  const filterEntries = Object.entries(app.dbFilters);
  if (filterEntries.length > 0) {
    const [fCol, fVal] = filterEntries[0];
    url += `&filter_col=${encodeURIComponent(fCol)}&filter_op=contains&filter_val=${encodeURIComponent(fVal)}`;
  }

  try {
    const res = await fetch(authUrl(url));
    if (res.status === 401) {
      localStorage.removeItem('auth_token');
      window.location.reload();
      return;
    }
    const result = await res.json();
    app.dbTotalRows = result.total || 0;
    app.dbCurrentResult = result;
    if (!result.columns || !result.columns.length) {
      if (!silent) {
        data.innerHTML = '<div class="db-hint">Empty or invalid table</div>';
      }
      updatePageInfo();
      return;
    }
    renderTableData(result, silent);
    updatePageInfo();
  } catch (e) {
    if (!silent) {
      data.innerHTML = `<div class="db-hint">Error: ${e.message}</div>`;
    }
  }
}

function updatePageInfo() {
  const info = document.getElementById('db-page-info');
  const prevBtn = document.getElementById('db-page-prev');
  const nextBtn = document.getElementById('db-page-next');
  if (!app.dbSelectedTable || app.dbTotalRows <= app.dbPageLimit) {
    info.textContent = '';
    prevBtn.style.display = 'none';
    nextBtn.style.display = 'none';
    return;
  }
  const start = app.dbPageOffset + 1;
  const end = Math.min(app.dbPageOffset + app.dbPageLimit, app.dbTotalRows);
  const totalPages = Math.ceil(app.dbTotalRows / app.dbPageLimit);
  const curPage = Math.floor(app.dbPageOffset / app.dbPageLimit) + 1;
  info.textContent = `${start}–${end} / ${app.dbTotalRows} (pg ${curPage}/${totalPages})`;
  prevBtn.style.display = '';
  nextBtn.style.display = '';
  prevBtn.disabled = app.dbPageOffset <= 0;
  nextBtn.disabled = app.dbPageOffset + app.dbPageLimit >= app.dbTotalRows;
}

export { cancelEditing, queryWithFilters, renderTableData, queryTable, updatePageInfo, closeColPopup, getSortForTable };
