'use strict';

import { app } from './state.js';
import { startStream, stopStream } from './stream.js';
import { startLoop, stopLoop, setLoopLevel, toggleAutoScroll } from './loop.js';

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
        stopLoop();
        setTimeout(() => {
          app.fitAddon.fit();
        }, 50);
      } else if (btn.dataset.tab === 'stream') {
        stopLoop();
        startStream();
      } else if (btn.dataset.tab === 'loop') {
        stopStream();
        startLoop();
        console.log('[tabs] switched to loop tab, handler set:', !!app._loopHandler);
      } else if (btn.dataset.tab === 'database') {
        stopStream();
        stopLoop();
      }
    });
  });

  // ── Wire up loop filter buttons ──
  document.querySelectorAll('.loop-filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      setLoopLevel(btn.dataset.level);
    });
  });

  // ── Wire up auto-scroll toggle ──
  const autoScrollBtn = document.getElementById('loop-autoscroll');
  if (autoScrollBtn) {
    autoScrollBtn.addEventListener('click', toggleAutoScroll);
  }

  // Initial activation for the default-active tab
  const activeBtn = document.querySelector('.tab-btn.active');
  if (activeBtn && activeBtn.dataset.tab === 'stream') {
    startStream();
  }
}
