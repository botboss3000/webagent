'use strict';

import { randomUUID } from './uuid.js';

export const app = {
  aDot: null,
  aStat: null,
  chatMessages: null,
  chatInput: null,
  chatSend: null,
  toolLogContent: null,
  toolLogToggle: null,
  toolLogPanel: null,
  toolLogClose: null,
  dbToolbar: null,

  agentWs: null,
  isProcessing: false,
  agentBuffer: '',
  lastScreenshotUri: '',

  addChatBubble: null,
  updateLastBubble: null,

  populateUserSelect: null,

  currentSessionId: '',
  currentUserId: '',
  currentAgentId: '',
  localUserId: '',

  dbTables: [],
  dbSelectedTable: null,
  dbCurrentResult: null,
  editingCell: null,
  dbPageOffset: 0,
  dbPageLimit: 200,
  // Chunked client-side rendering inside a server-fetched page. We render
  // dbRenderLimit rows initially and grow by dbRenderStep when the user
  // scrolls near the bottom of the table viewport.
  dbRenderLimit: 30,
  dbRenderStep: 30,
  dbTotalRows: 0,
  dbFilters: {},
  dbColumnOrder: {},
  dbExclusions: {},
  dbSortState: {"interactions":{"col":"created_at","dir":"DESC"}},
  dbColPopup: null,
  dbShowHidden: false,
  dbHiddenCols: {"interactions":["session_id"]},
  COL_WIDTHS: {
    created_at: '85.375px',
    session_id: '20px',
    id: '72.21875px',
    role: '59.03125px',
    input: '200px',
    content: '300px',
    tool_name: '123.078125px',
    metadata: '113.28125px',
    parent_id: '20px',
    tool_call_id: '20px',
  },
  resizeData: null,
  autoRefreshInterval: null,
  cellPopupData: null,

  stopAutoRefresh: null,

  // Per-session highest session_seq observed via WS replay / live events.
  // Used to ask the server to replay only events newer than what we've seen
  // when the WS reconnects (refresh, session switch back, network blip).
  lastSessionSeq: {},          // { [sessionId]: int }
};

export function bindDom() {
  app.aDot = document.getElementById('agent-dot');
  app.aStat = document.getElementById('agent-status');
  app.chatMessages = document.getElementById('chat-messages-inner');
  app.chatInput = document.getElementById('chat-input');
  app.chatSend = document.getElementById('chat-send');
  app.toolLogContent = document.getElementById('tool-log-content');
  app.toolLogToggle = document.getElementById('tool-log-toggle');
  app.toolLogPanel = document.getElementById('tool-log-panel');
  app.toolLogClose = document.getElementById('tool-log-close');
  app.dbToolbar = document.getElementById('db-toolbar');

  // Use auth_user_id if logged in, otherwise anonymous UUID.
  // One-time migration: copy legacy `terminalUserId` to the new `anonUserId`
  // key so existing anon visitors keep their identity.
  const authUserId = localStorage.getItem('auth_user_id');
  const legacyAnon = localStorage.getItem('terminalUserId');
  if (legacyAnon && !localStorage.getItem('anonUserId')) {
    localStorage.setItem('anonUserId', legacyAnon);
  }
  if (legacyAnon) localStorage.removeItem('terminalUserId');
  app.localUserId =
    localStorage.getItem('anonUserId') || authUserId || 'ddbd80a2-e46f-436e-a165-4f63469218d9';

  try {
    const saved = localStorage.getItem('dbColumnOrder');
    if (saved) app.dbColumnOrder = JSON.parse(saved);
  } catch (e) {
    app.dbColumnOrder = {};
  }

  try {
    const savedWidths = localStorage.getItem('dbColWidths');
    if (savedWidths) Object.assign(app.COL_WIDTHS, JSON.parse(savedWidths));
  } catch (e) { /* ignore */ }

  const storedSessionId = localStorage.getItem('terminalSessionId');
  app.currentSessionId = storedSessionId || randomUUID();
  if (!storedSessionId) {
    localStorage.setItem('terminalSessionId', app.currentSessionId);
  }
  // If logged in, override with auth_user_id so sessions + LLM config follow auth
  const overrideUserId = authUserId && localStorage.getItem('auth_token')
    ? authUserId
    : null;
  app.currentUserId =
    new URLSearchParams(location.search).get('user_id') || overrideUserId || app.localUserId;
}
