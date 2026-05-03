'use strict';

import { app } from '../state.js';

export function getPKColumns(tableName) {
  const t = app.dbTables.find((x) => x.name === tableName);
  if (!t) return [];
  return t.columns.filter((c) => c.pk).map((c) => c.name);
}

export function saveColumnOrder(tableName, cols) {
  app.dbColumnOrder[tableName] = cols;
  localStorage.setItem('dbColumnOrder', JSON.stringify(app.dbColumnOrder));
}

/** Single implementation (deduped from legacy duplicate). */
export function getDisplayColumns(columns, tableName) {
  if (app.dbColumnOrder[tableName]) {
    const ordered = [];
    for (const c of app.dbColumnOrder[tableName]) {
      if (columns.includes(c)) ordered.push(c);
    }
    for (const c of columns) {
      if (!app.dbColumnOrder[tableName].includes(c)) ordered.push(c);
    }
    if (ordered.length > 0) return ordered;
  }

  const TABLE_ORDERS = {
    interactions: [
      'role',
      'input',
      'content',
      'tool_name',
      'metadata',
      'session_id',
      'id',
      'parent_id',
      'tool_call_id',
      'created_at',
    ],
  };

  const order = TABLE_ORDERS[tableName];
  if (order) {
    const ordered = [];
    for (const c of order) {
      if (columns.includes(c)) ordered.push(c);
    }
    for (const c of columns) {
      if (!order.includes(c)) ordered.push(c);
    }
    return ordered;
  }

  const priority = ['created_at', 'role', 'content', 'session_id', 'id'];
  const ordered = [];
  for (const p of priority) {
    if (columns.includes(p)) ordered.push(p);
  }
  for (const c of columns) {
    if (!priority.includes(c)) ordered.push(c);
  }
  return ordered;
}
