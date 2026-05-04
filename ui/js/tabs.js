'use strict';

import { app } from './state.js';
import { startStream, stopStream } from './stream.js';

export function initTabs() {
  document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab-btn').forEach((b) => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach((c) => c.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById('tab-' + btn.dataset.tab).classList.add('active');

      // Tab-specific setup
      if (btn.dataset.tab === 'terminal') {
        stopStream();
        setTimeout(() => {
          app.fitAddon.fit();
        }, 50);
      } else if (btn.dataset.tab === 'stream') {
        startStream();
      } else if (btn.dataset.tab === 'database') {
        stopStream();
      }
    });
  });

  // Initial activation for the default-active tab
  const activeBtn = document.querySelector('.tab-btn.active');
  if (activeBtn && activeBtn.dataset.tab === 'stream') {
    startStream();
  }
}
