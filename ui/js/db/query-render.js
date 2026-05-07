"use strict";

import { app } from "../state.js";
import { getPKColumns, getDisplayColumns, saveColumnOrder } from "./columns.js";
import { initColumnResize } from "./columnResize.js";
import { formatJsonAsHtml } from "../json-tree.js";
import { apiPath } from "../config.js";

function cancelEditing() {
  if (app.editingCell) {
    // Re-render the table to discard edits
    app.editingCell = null;
    if (app.dbCurrentResult) {
      renderTableData(app.dbCurrentResult);
    }
  }
}

async function queryWithFilters() {
  if (app.dbSelectedTable) await queryTable(app.dbSelectedTable);
}

function renderTableData(result, silent) {
  const data = document.getElementById('db-table-data');
  const pkCols = getPKColumns(result.table);
  const displayCols = getDisplayColumns(result.columns, result.table);

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
      return;
    }
  }

  // ── Full render ──
  // Get current sort from dropdowns
  const curSortCol = document.getElementById('db-sort-col').value;
  const curSortDir = document.getElementById('db-sort-dir').value;

  // Column width strategy: name-width for most, overrides for a few
  function getColWidth(table, col) {
    // Hard overrides — fixed 300px
    const contentTables = ['interactions', 'context_defaults', 'context_templates', 'context'];
    if (table === 'interactions' && col === 'input') return '300px';
    if (contentTables.includes(table) && col === 'content') return '300px';
    // Already user-resized via drag?
    if (app.COL_WIDTHS[col]) return app.COL_WIDTHS[col];
    // Default: width ≈ column name text (mono 12px ≈ 7.2px/char + padding)
    const px = Math.max(40, col.length * 7.5 + 20);
    return Math.round(px) + 'px';
  }
  function isFixedCol(table, col) {
    const fixedCols = ['interactions/input', 'interactions/content', 'context_defaults/content', 'context_templates/content', 'context/content'];
    return fixedCols.includes(table + '/' + col);
  }

  let html = '<table class="db-table"><thead>';

  // Header row (clickable to sort, draggable to reorder)
  html += '<tr>';
  for (const col of displayCols) {
    const w = getColWidth(result.table, col);
    const style = w ? ` style="width:${w};min-width:${w};max-width:${w}"` : '';
    const isActive = col === curSortCol;
    const activeAsc = isActive && curSortDir === 'ASC';
    const activeDesc = isActive && curSortDir === 'DESC';
    const noResize = isFixedCol(result.table, col);
    html += `<th class="db-th" data-col="${col}"${style} draggable="true">
      <span class="th-text">${col}</span>
      <span class="th-sort-arrows">
        <span class="th-sort-arrow th-sort-asc${activeAsc ? ' active' : ''}" title="Sort ascending">\u25B2</span>
        <span class="th-sort-arrow th-sort-desc${activeDesc ? ' active' : ''}" title="Sort descending">\u25BC</span>
      </span>
      ${noResize ? '' : '<span class="th-resize"></span>'}
    </th>`;
  }
  html += '</tr>';

  // Filter row (per-column filter inputs)


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
    // Resize handle after each row
    html += `<tr class="db-row-resize-row" data-ri="${ri}"><td colspan="${displayCols.length}" class="db-row-resize-td"><div class="db-row-resize-handle" data-ri="${ri}"></div></td></tr>`;
  }
  html += '</tbody></table>';
  data.innerHTML = html;

  // ── Click sort arrows to sort ──
  data.querySelectorAll('.th-sort-arrow').forEach(arrow => {
    arrow.addEventListener('click', (e) => {
      e.stopPropagation();
      const th = arrow.closest('.db-th');
      const col = th.dataset.col;
      const sortCol = document.getElementById('db-sort-col');
      const sortDir = document.getElementById('db-sort-dir');
      const dir = arrow.classList.contains('th-sort-asc') ? 'ASC' : 'DESC';

      // If already sorting by this column+direction, reset to default
      if (sortCol.value === col && sortDir.value === dir) {
        sortCol.value = 'created_at';
        sortDir.value = 'DESC';
      } else {
        sortCol.value = col;
        sortDir.value = dir;
      }
      if (app.dbSelectedTable) queryTable(app.dbSelectedTable);
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
      // Reset all drop indicators
      data.querySelectorAll('.db-th').forEach(t => t.classList.remove('drop-target'));
    });
    th.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      // Highlight drop target
      data.querySelectorAll('.db-th').forEach(t => t.classList.remove('drop-target'));
      th.classList.add('drop-target');
    });
    th.addEventListener('dragleave', () => {
      th.classList.remove('drop-target');
    });
    th.addEventListener('drop', (e) => {
      e.preventDefault();
      if (!dragCol || dragCol === th.dataset.col) return;

      // Get current order
      const currentOrder = getDisplayColumns(result.columns, result.table);
      const fromIdx = currentOrder.indexOf(dragCol);
      const toIdx = currentOrder.indexOf(th.dataset.col);
      if (fromIdx === -1 || toIdx === -1) return;

      // Move column
      currentOrder.splice(fromIdx, 1);
      currentOrder.splice(toIdx, 0, dragCol);

      // Save order
      saveColumnOrder(result.table, currentOrder);

      // Re-render
      if (dragTh) dragTh.style.opacity = '';
      dragCol = null; dragTh = null;
      data.querySelectorAll('.db-th').forEach(t => t.classList.remove('drop-target'));
      queryTable(app.dbSelectedTable);
    });
  });

  // ── Click column name to toggle filter input ──
  data.querySelectorAll('.th-text').forEach(span => {
    const th = span.closest('.db-th');
    const col = th.dataset.col;

    // Create hidden filter input inside the th (if not already there)
    let filterInput = th.querySelector('.db-filter-input');
    if (!filterInput) {
      filterInput = document.createElement('input');
      filterInput.type = 'text';
      filterInput.className = 'db-filter-input';
      filterInput.dataset.col = col;
      filterInput.placeholder = 'filter...';
      filterInput.value = app.dbFilters[col] || '';
      filterInput.style.display = 'none';
      filterInput.style.marginTop = '4px';
      filterInput.style.width = '100%';
      filterInput.style.boxSizing = 'border-box';
      th.appendChild(filterInput);

      filterInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          applyFilter(col, filterInput.value);
          if (!filterInput.value) filterInput.style.display = 'none';
        }
        if (e.key === 'Escape') {
          filterInput.style.display = 'none';
          filterInput.value = '';
          applyFilter(col, '');
        }
      });
      filterInput.addEventListener('blur', () => {
        applyFilter(col, filterInput.value);
      });
    }

    // Sync input value and visibility with current filter state
    filterInput.value = app.dbFilters[col] || '';
    filterInput.style.display = app.dbFilters[col] ? 'block' : 'none';

    // Click on column name toggles the filter
    span.addEventListener('click', (e) => {
      e.stopPropagation();
      data.querySelectorAll('.db-filter-input').forEach(inp => {
        if (inp !== filterInput) inp.style.display = 'none';
      });
      const show = filterInput.style.display === 'none';
      filterInput.style.display = show ? 'block' : 'none';
      if (show) { filterInput.focus(); filterInput.select(); }
    });
  });

  function applyFilter(col, val) {
    if (val) app.dbFilters[col] = val;
    else delete app.dbFilters[col];
    if (app.dbSelectedTable) queryTable(app.dbSelectedTable);
  }

  // ── Drag row resize handles to adjust row height ──
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

  // Event delegation for cell editing — registered once
  // (handled outside renderTableData to avoid duplicates)
}

function populateSortCol(columns, selectedCol) {
  const sel = document.getElementById('db-sort-col');
  sel.innerHTML = columns.map(c =>
    `<option value="${c}"${c === selectedCol ? ' selected' : ''}>${c}</option>`
  ).join('');
}

async function queryTable(tableName, opts) {
  const dbName = document.getElementById('db-select').value;
  const sortCol = document.getElementById('db-sort-col').value;
  const sortDir = document.getElementById('db-sort-dir').value;
  const data = document.getElementById('db-table-data');
  const silent = opts?.silent;

  if (!silent) {
    data.innerHTML = '';
    cancelEditing();
    // Reset offset on non-silent (user-initiated) queries
    if (opts?.keepOffset) {
      // keep current offset (used by pagination buttons)
    } else {
      app.dbPageOffset = 0;
    }
  }

  let url = apiPath(`/api/v1/db/query?db=${encodeURIComponent(dbName)}&table=${encodeURIComponent(tableName)}&limit=${app.dbPageLimit}&offset=${app.dbPageOffset}`);
  url += `&order_by=${encodeURIComponent(sortCol)}&order_dir=${sortDir}`;

  // Add filter
  const filterEntries = Object.entries(app.dbFilters);
  if (filterEntries.length > 0) {
    const [fCol, fVal] = filterEntries[0];
    url += `&filter_col=${encodeURIComponent(fCol)}&filter_op=contains&filter_val=${encodeURIComponent(fVal)}`;
  }

  try {
    const res = await fetch(url);
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
    populateSortCol(result.columns, sortCol);
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

export { cancelEditing, queryWithFilters, renderTableData, populateSortCol, queryTable, updatePageInfo };
