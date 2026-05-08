'use strict';

import { app } from './state.js';
import { startStream, stopStream } from './stream.js';
import { startLoop, stopLoop, setLoopLevel, toggleAutoScroll } from './loop.js';
import { initGithub, startGithub, stopGithub } from './github.js';
export function initTabs() {
  const tabSelect = document.getElementById('main-tab-select');
  if (!tabSelect) return;

  // ── Restore last active tab from localStorage ──
  const savedTab = localStorage.getItem('lastActiveTab');
  if (savedTab && tabSelect.querySelector(`option[value="${savedTab}"]`)) {
    tabSelect.value = savedTab;
  }

  function activateTab(tabValue) {
    document.querySelectorAll('.tab-content').forEach((c) => c.classList.remove('active'));
    const targetContent = document.getElementById('tab-' + tabValue);
    if (targetContent) {
      targetContent.classList.add('active');
    }

    // Save tab preference
    localStorage.setItem('lastActiveTab', tabValue);

    // Tab-specific setup
    if (tabValue === 'terminal') {
      stopStream();
      stopLoop();
      setTimeout(() => {
        if (app && app.fitAddon) {
          app.fitAddon.fit();
        }
      }, 50);
    } else if (tabValue === 'stream') {
      stopLoop();
      startStream();
    } else if (tabValue === 'loop') {
      stopStream();
      startLoop();
    } else if (tabValue === 'database') {
      stopStream();
      stopLoop();
      stopGithub();
    } else if (tabValue === 'github') {
      stopStream();
      stopLoop();
      startGithub();
    }
  }

  tabSelect.addEventListener('change', (e) => {
    activateTab(e.target.value);
  });

  // ── Wire up loop filter buttons ──
  document.querySelectorAll('.loop-filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.loop-filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      setLoopLevel(btn.dataset.level);
    });
  });

  // ── Wire up auto-scroll toggle ──
  const autoScrollBtn = document.getElementById('loop-autoscroll');
  if (autoScrollBtn) {
    autoScrollBtn.addEventListener('click', toggleAutoScroll);
  }

  // Initial activation for the restored/default active tab
  activateTab(tabSelect.value);
}
