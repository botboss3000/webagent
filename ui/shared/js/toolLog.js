'use strict';

// Tool-call log panel — logTool(event) renders stream / tool_call / tool_result
// rows into the tool log. Fed by the agent WebSocket (see ui/shared/js/agentWs.js).

import { app } from './state.js';
import { icon } from './icons.js';

export function logTool(event) {
  const el = document.createElement('div');
  el.className = 'tl-entry';

  switch (event.type) {
    case 'stream':
      el.className = 'tl-entry tl-think';
      el.innerHTML = icon('message-square', { size: '12px' }) + ' ' + event.content;
      break;
    case 'tool_call':
      el.className = 'tl-entry tl-call';
      el.innerHTML = icon('wrench', { size: '12px' }) + ' ' +
        event.tool + '(' + JSON.stringify(event.args || {}) + ')';
      break;
    case 'tool_result': {
      el.className = 'tl-entry ' + (event.error ? 'tl-err' : 'tl-ok');
      const dur = event.duration_ms ? ` [${event.duration_ms}ms]` : '';
      const resultStr = event.result || '';
      const statusIcon = event.error ? icon('x-circle', { size: '12px' }) : icon('check-circle', { size: '12px' });
      if (resultStr.startsWith('/screenshots/')) {
        app.lastScreenshotUri = resultStr;
        el.innerHTML =
          '<span>' + statusIcon + ' ' + event.tool + dur +
          ':</span> <br><img src="' +
          resultStr +
          '" style="max-width:100%;max-height:400px;border:1px solid var(--border);border-radius:4px;margin-top:4px;">';
      } else {
        el.innerHTML = statusIcon + ' ' + event.tool + dur + ': ' + resultStr.slice(0, 300);
      }
      break;
    }
    case 'response':
      el.className = 'tl-entry tl-ok';
      el.innerHTML = icon('bot', { size: '12px' }) + ' Response: ' + (event.content || '').slice(0, 200);
      break;
    default:
      el.textContent = JSON.stringify(event);
  }

  if (!app.toolLogContent) return;
  app.toolLogContent.appendChild(el);
  app.toolLogContent.scrollTop = app.toolLogContent.scrollHeight;
}

function initToolLog() {
  app.toolLogToggle.addEventListener('click', () =>
    app.toolLogPanel.classList.toggle('open'),
  );
  app.toolLogClose.addEventListener('click', () =>
    app.toolLogPanel.classList.remove('open'),
  );
}
