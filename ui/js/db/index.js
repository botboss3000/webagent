'use strict';

import { queryTable } from './query-render.js';
import { setTableDeps } from './tables.js';
import { initGlobalColumnResizeListeners } from './columnResize.js';
import { initDbCellEditors } from './edit.js';
import {
  initDbPaginationAndToolbar,
  runInitialDbTableLoad,
  startAutoRefresh,
} from './pagination.js';
import { initCellModal } from './modal.js';

export function initDbViewer() {
  setTableDeps({ queryTable, startAutoRefresh });
  initGlobalColumnResizeListeners();
  initDbCellEditors();
  initDbPaginationAndToolbar();
  initCellModal();
  runInitialDbTableLoad();
}
