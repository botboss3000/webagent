'use strict';

import { app } from '../state.js';
import { saveEdit } from './edit.js';
import { formatJsonAsHtml } from '../json-tree.js';

let isEditing = false;

function openCellPopup(cell, ri, col, originalValue) {
  app.cellPopupData = { cell, col, originalValue };
  isEditing = false;

  const titleEl = document.getElementById('cell-modal-title');
  const viewer = document.getElementById('cell-modal-viewer');
  const editor = document.getElementById('cell-modal-editor');
  const toggleBtn = document.getElementById('cell-modal-edit-toggle');

  titleEl.textContent = 'Viewing: ' + col;
  
  let formattedHtml = null;
  let displayValue = originalValue;

  if (typeof originalValue === 'string' && originalValue.length > 0) {
    if (originalValue[0] === '{' || originalValue[0] === '[') {
      formattedHtml = formatJsonAsHtml(originalValue);
      try {
        const parsed = JSON.parse(originalValue);
        displayValue = JSON.stringify(parsed, null, 2);
      } catch (e) { /* ignore */ }
    }
  }

  if (formattedHtml) {
    viewer.innerHTML = formattedHtml;
    viewer.style.display = 'block';
    editor.style.display = 'none';
    toggleBtn.textContent = '✎ Edit';
    toggleBtn.style.display = 'block';
  } else {
    viewer.style.display = 'none';
    editor.style.display = 'block';
    editor.value = originalValue;
    toggleBtn.style.display = 'none'; // Only show toggle if we have a nice viewer
    titleEl.textContent = 'Editing: ' + col;
    isEditing = true;
  }

  document.getElementById('cell-modal').classList.add('open');

  if (isEditing) {
    editor.focus();
    editor.scrollTop = 0;
  } else {
    viewer.scrollTop = 0;
  }
}

function toggleEdit() {
  const viewer = document.getElementById('cell-modal-viewer');
  const editor = document.getElementById('cell-modal-editor');
  const toggleBtn = document.getElementById('cell-modal-edit-toggle');
  const titleEl = document.getElementById('cell-modal-title');
  const { col, originalValue } = app.cellPopupData;

  isEditing = !isEditing;
  if (isEditing) {
    viewer.style.display = 'none';
    editor.style.display = 'block';
    
    let displayValue = originalValue;
    try {
      const parsed = JSON.parse(originalValue);
      displayValue = JSON.stringify(parsed, null, 2);
    } catch(e) {}
    
    editor.value = displayValue;
    toggleBtn.textContent = '👁 View';
    titleEl.textContent = 'Editing: ' + col;
    editor.focus();
  } else {
    viewer.style.display = 'block';
    editor.style.display = 'none';
    toggleBtn.textContent = '✎ Edit';
    titleEl.textContent = 'Viewing: ' + col;
  }
}

function closeCellPopup(discard) {
  document.getElementById('cell-modal').classList.remove('open');
  if (discard) {
    app.cellPopupData = null;
    return;
  }
  if (app.cellPopupData) {
    const { cell, col, originalValue } = app.cellPopupData;
    app.cellPopupData = null;
    
    // Only save if we were in editing mode AND the value actually changed
    if (isEditing) {
      const newValue = document.getElementById('cell-modal-editor').value;
      if (newValue !== originalValue) {
        const ri = parseInt(cell.dataset.row, 10);
        app.editingCell = { rowIndex: ri, colName: col, originalValue };
        saveEdit(cell, newValue);
      }
    }
  }
}

export function initCellModal() {
  document.getElementById('cell-modal-edit-toggle').addEventListener('click', toggleEdit);
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
