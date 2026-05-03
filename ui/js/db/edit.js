'use strict';

import { app } from '../state.js';
import { getPKColumns } from './columns.js';
import { cancelEditing, renderTableData } from './query-render.js';

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
    const res = await fetch('/api/v1/db/update', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
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
    if (!btn) return;
    e.stopPropagation();
    if (app.editingCell) {
      const oldCell = document.querySelector(
        `.db-cell[data-row="${app.editingCell.rowIndex}"][data-col="${app.editingCell.colName}"]`,
      );
      if (oldCell) {
        const ta = oldCell.querySelector('textarea');
        if (ta) saveEdit(oldCell, ta.value);
      } else {
        cancelEditing();
      }
    }
  });
}
