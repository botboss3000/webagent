'use strict';

import { app } from '../state.js';
import { saveEdit } from './edit.js';

function openCellPopup(cell, ri, col, originalValue) {
  app.cellPopupData = { cell, col, originalValue };

  let displayValue = originalValue;
  if (
    typeof originalValue === 'string' &&
    originalValue.length > 0 &&
    (originalValue[0] === '{' || originalValue[0] === '[')
  ) {
    try {
      const parsed = JSON.parse(originalValue);
      displayValue = JSON.stringify(parsed, null, 2);
    } catch (e) {
      /* ignore */
    }
  }

  document.getElementById('cell-modal-title').textContent = 'Editing: ' + col;
  const editor = document.getElementById('cell-modal-editor');
  editor.value = displayValue;
  document.getElementById('cell-modal').classList.add('open');

  editor.focus();
  editor.scrollTop = 0;
}

function closeCellPopup(discard) {
  document.getElementById('cell-modal').classList.remove('open');
  if (discard) {
    app.cellPopupData = null;
    return;
  }
  if (app.cellPopupData) {
    const newValue = document.getElementById('cell-modal-editor').value;
    const { cell, col, originalValue } = app.cellPopupData;
    const ri = parseInt(cell.dataset.row, 10);
    app.cellPopupData = null;
    app.editingCell = { rowIndex: ri, colName: col, originalValue };
    saveEdit(cell, newValue);
  }
}

export function initCellModal() {
  document.getElementById('cell-modal-close').addEventListener('click', () =>
    closeCellPopup(true),
  );
  document.getElementById('cell-modal-backdrop').addEventListener('click', () =>
    closeCellPopup(false),
  );
  document.getElementById('cell-modal-editor').addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      closeCellPopup(false);
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      closeCellPopup(true);
    }
  });
  document.getElementById('cell-modal-editor').addEventListener('blur', () => {
    setTimeout(() => {
      if (app.cellPopupData) {
        closeCellPopup(false);
      }
    }, 150);
  });
}

export { openCellPopup };
