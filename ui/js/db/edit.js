'use strict';

import { app } from '../state.js';
import { getPKColumns } from './columns.js';
import { cancelEditing, renderTableData } from './query-render.js';
import { openCellPopup } from './modal.js';
import { apiPath } from '../config.js';
import { authHeaders } from '../left-login.js';

export async function saveEdit(cell, newValue) {
  if (!app.editingCell || !app.dbCurrentResult) return;

  const row = app.dbCurrentResult.rows[app.editingCell.rowIndex];
  const pkCols = getPKColumns(app.dbCurrentResult.table);

  const where = {};
  for (const pk of pkCols) {
    where[pk] = row[pk];
  }
  if (!Object.keys(where).length) {
    for (const col of app.dbCurrentResult.columns) {
      where[col] = row[col];
    }
  }

  const values = {};
  values[app.editingCell.colName] = newValue;

  try {
    const dbName = document.getElementById('db-select').value;
    const res = await fetch(apiPath('/api/v1/db/update'), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        db: dbName,
        table: app.dbCurrentResult.table,
        where: where,
        values: values,
      }),
    });
    const result = await res.json();
    if (!result.success) {
      alert('Save failed');
      return;
    }
    row[app.editingCell.colName] = newValue;
    app.editingCell = null;
    renderTableData(app.dbCurrentResult);
  } catch (e) {
    alert('Error saving: ' + e.message);
  }
}

export function initDbCellEditors() {
  console.log('[DB] initDbCellEditors called');
  const tableData = document.getElementById('db-table-data');
  if (!tableData) { console.log('[DB] ERROR: #db-table-data not found'); return; }
  console.log('[DB] #db-table-data found, registering click handlers');
  document.getElementById('db-table-data').addEventListener('click', (e) => {
    const cell = e.target.closest('.db-cell');
    if (!cell) return;
    if (e.target.closest('.db-cell-expand')) return;

    const ri = parseInt(cell.dataset.row, 10);
    const col = cell.dataset.col;
    const pkCols = app.dbCurrentResult ? getPKColumns(app.dbCurrentResult.table) : [];
    if (pkCols.includes(col)) return;
    if (
      app.editingCell &&
      app.editingCell.rowIndex === ri &&
      app.editingCell.colName === col
    ) {
      return;
    }
    cancelEditing();

    const originalValue = cell.dataset.val === 'null' ? '' : cell.dataset.val;

    cell.innerHTML = `<textarea class="db-edit-input" rows="4">${originalValue.replace(/"/g, '&quot;')}</textarea>`;
    const textarea = cell.querySelector('textarea');
    textarea.focus();
    textarea.select();

    app.editingCell = { rowIndex: ri, colName: col, originalValue, textarea };

    textarea.addEventListener('keydown', (ev) => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key === 'Enter') {
        ev.preventDefault();
        saveEdit(cell, textarea.value);
      } else if (ev.key === 'Escape') {
        ev.preventDefault();
        cancelEditing();
      }
    });

    textarea.addEventListener('blur', () => {
      setTimeout(() => {
        if (app.editingCell && app.editingCell.textarea === textarea) {
          saveEdit(cell, textarea.value);
        }
      }, 100);
    });
  });

  document.getElementById('db-table-data').addEventListener('click', (e) => {
    const btn = e.target.closest('.db-cell-expand');
    if (!btn) { console.log('[DB] expand: no btn found'); return; }
    console.log('[DB] expand button clicked!', btn);
    e.stopPropagation();

    // Cancel any active inline editing before opening modal
    if (app.editingCell) {
      console.log('[DB] expand: cancelling active edit');
      cancelEditing();
    }

    const cell = btn.closest('.db-cell');
    if (!cell) { console.log('[DB] expand: no parent cell found'); return; }
    console.log('[DB] expand: cell found', cell.dataset);

    const ri = parseInt(cell.dataset.row, 10);
    const col = cell.dataset.col;
    const originalValue = cell.dataset.val === 'null' ? '' : cell.dataset.val;
    console.log('[DB] expand: calling openCellPopup with', { ri, col, originalValue });

    console.log('[DB] expand: openCellPopup =', openCellPopup);
    openCellPopup(cell, ri, col, originalValue);
    console.log('[DB] expand: openCellPopup returned');
  });
}
