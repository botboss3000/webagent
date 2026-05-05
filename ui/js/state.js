'use strict';

export const app = {
  container: null,
  tDot: null,
  tStat: null,
  aDot: null,
  aStat: null,
  chatMessages: null,
  chatInput: null,
  chatInput: null,
  chatSend: null,
  toolLogContent: null,
  toolLogToggle: null,
  toolLogPanel: null,
  toolLogClose: null,
  segCloud: null,
  segLocal: null,
  dbToolbar: null,

  term: null,
  fitAddon: null,
  termWs: null,

  agentWs: null,
  isProcessing: false,
  agentBuffer: '',
  lastScreenshotUri: '',

  addChatBubble: null,
  updateLastBubble: null,

  populateUserSelect: null,

  currentSessionId: '',
  currentUserId: '',
  localUserId: '',

  dbTables: [],
  dbSelectedTable: null,
  dbCurrentResult: null,
  editingCell: null,
  dbPageOffset: 0,
  dbPageLimit: 200,
  dbTotalRows: 0,
  dbFilters: {},
  dbColumnOrder: {},
  COL_WIDTHS: {
    created_at: '20px',
    session_id: '20px',
    id: '20px',
    role: '50px',
    input: '200px',
    content: '300px',
    tool_name: '100px',
    metadata: '150px',
    parent_id: '20px',
    tool_call_id: '20px',
  },
  resizeData: null,
  autoRefreshInterval: null,
  cellPopupData: null,

  stopAutoRefresh: null,

  streamEntryCount: 0,
  streamStartTime: null,
};

export function bindDom() {
  app.container = document.getElementById('terminal-container');
  app.tDot = document.getElementById('terminal-dot');
  app.tStat = document.getElementById('terminal-status');
  app.aDot = document.getElementById('agent-dot');
  app.aStat = document.getElementById('agent-status');
  app.chatMessages = document.getElementById('chat-messages-inner');
  app.chatInput = document.getElementById('chat-input');
  app.chatSend = document.getElementById('chat-send');
  app.toolLogContent = document.getElementById('tool-log-content');
  app.toolLogToggle = document.getElementById('tool-log-toggle');
  app.toolLogPanel = document.getElementById('tool-log-panel');
  app.toolLogClose = document.getElementById('tool-log-close');
  app.segCloud = document.getElementById('seg-cloud');
  app.segLocal = document.getElementById('seg-local');
  app.dbToolbar = document.getElementById('db-toolbar');

  app.localUserId =
    localStorage.getItem('terminalUserId') || 'ddbd80a2-e46f-436e-a165-4f63469218d9';

  try {
    const saved = localStorage.getItem('dbColumnOrder');
    if (saved) app.dbColumnOrder = JSON.parse(saved);
  } catch (e) {
    app.dbColumnOrder = {};
  }

  const storedSessionId = localStorage.getItem('terminalSessionId');
  app.currentSessionId = storedSessionId || crypto.randomUUID();
  if (!storedSessionId) {
    localStorage.setItem('terminalSessionId', app.currentSessionId);
  }
  app.currentUserId =
    new URLSearchParams(location.search).get('user_id') || app.localUserId;
}
