'use strict';

import { app } from './state.js';
import { startStream, stopStream } from './stream.js';
import { startLoop, stopLoop, setLoopLevel, toggleAutoScroll } from './loop.js';
import { startLoopVisual, stopLoopVisual } from './loop-visual.js';
import { initGithub, startGithub, stopGithub } from './github.js';
import { startAutoAgent, stopAutoAgent } from './autoagent.js';
import { startAgents, stopAgents } from './agents.js';

export function initTabs() {
  const tabSelect = document.getElementById('main-tab-select');
  if (!tabSelect) return;

  const savedTab = localStorage.getItem('lastActiveTab');
  if (savedTab && tabSelect.querySelector(`option[value="${savedTab}"]`)) {
    tabSelect.value = savedTab;
  }

  function setChatSideVisible(visible) {
    const chatSide = document.getElementById('chat-side');
    if (chatSide) {
      chatSide.style.display = visible ? '' : 'none';
    }
  }

  function activateTab(tabValue) {
    document.querySelectorAll('.tab-content').forEach((c) => c.classList.remove('active'));
    const targetContent = document.getElementById('tab-' + tabValue);
    if (targetContent) {
      targetContent.classList.add('active');
    }

    localStorage.setItem('lastActiveTab', tabValue);

    // Default: show chat side
    setChatSideVisible(true);

    if (tabValue === 'terminal') {
      stopStream();
      stopLoop();
      stopLoopVisual();
      stopAutoAgent();
      stopAgents();
      setTimeout(() => {
        if (app && app.fitAddon) {
          app.fitAddon.fit();
        }
      }, 50);
    } else if (tabValue === 'stream') {
      stopLoop();
      stopLoopVisual();
      stopAutoAgent();
      stopAgents();
      startStream();
    } else if (tabValue === 'flow') {
      stopStream();
      stopLoopVisual();
      stopAutoAgent();
      stopAgents();
      startLoop();
    } else if (tabValue === 'loop-visual') {
      stopStream();
      stopLoop();
      stopAutoAgent();
      stopAgents();
      startLoopVisual();
    } else if (tabValue === 'database') {
      stopStream();
      stopLoop();
      stopLoopVisual();
      stopGithub();
      stopAutoAgent();
      stopAgents();
    } else if (tabValue === 'github') {
      stopStream();
      stopLoop();
      stopLoopVisual();
      stopAgents();
      startGithub();
    } else if (tabValue === 'autoagent') {
      stopStream();
      stopLoop();
      stopLoopVisual();
      stopGithub();
      stopAgents();
      startAutoAgent();
    } else if (tabValue === 'agents') {
      stopStream();
      stopLoop();
      stopLoopVisual();
      stopGithub();
      stopAutoAgent();
      startAgents();
    }
  }

  tabSelect.addEventListener('change', (e) => {
    activateTab(e.target.value);
  });

  document.querySelectorAll('.loop-filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.loop-filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      setLoopLevel(btn.dataset.level);
    });
  });

  const autoScrollBtn = document.getElementById('loop-autoscroll');
  if (autoScrollBtn) {
    autoScrollBtn.addEventListener('click', toggleAutoScroll);
  }

  const githubMenuItem = document.getElementById('github-menu-item');
  if (githubMenuItem) {
    githubMenuItem.addEventListener('click', () => {
      const dropdown = document.getElementById('settings-dropdown-menu');
      if (dropdown) dropdown.style.display = 'none';
      activateTab('github');
    });
  }

  activateTab(tabSelect.value);
}
