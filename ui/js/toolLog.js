'use strict';

import { app } from './state.js';

export function logTool(event) {
  const el = document.createElement('div');
  el.className = 'tl-entry';

  switch (event.type) {
    case 'stream':
      el.className = 'tl-entry tl-think';
      el.textContent = '💭 ' + event.content;
      break;
    case 'tool_call':
      el.className = 'tl-entry tl-call';
      el.textContent =
        '🔧 ' + event.tool + '(' + JSON.stringify(event.args || {}) + ')';
      break;
    case 'tool_result': {
      el.className = 'tl-entry ' + (event.error ? 'tl-err' : 'tl-ok');
      const dur = event.duration_ms ? ` [${event.duration_ms}ms]` : '';
      const resultStr = event.result || '';
      if (resultStr.startsWith('/screenshots/')) {
        app.lastScreenshotUri = resultStr;
        el.innerHTML =
          '<span>' +
          (event.error ? '❌ ' : '✅ ') +
          event.tool +
          dur +
          ':</span> <br><img src="' +
          resultStr +
          '" style="max-width:100%;max-height:400px;border:1px solid #444;border-radius:4px;margin-top:4px;">';
      } else {
        el.textContent =
          (event.error ? '❌ ' : '✅ ') +
          event.tool +
          dur +
          ': ' +
          resultStr.slice(0, 300);
      }
      break;
    }
    case 'response':
      el.className = 'tl-entry tl-ok';
      el.textContent = '💬 Response: ' + (event.content || '').slice(0, 200);
      break;
    default:
      el.textContent = JSON.stringify(event);
  }

  if (!app.toolLogContent) return;
  app.toolLogContent.appendChild(el);
  app.toolLogContent.scrollTop = app.toolLogContent.scrollHeight;
}

export function initToolLog() {
  app.toolLogToggle.addEventListener('click', () =>
    app.toolLogPanel.classList.toggle('open'),
  );
  app.toolLogClose.addEventListener('click', () =>
    app.toolLogPanel.classList.remove('open'),
  );
}
