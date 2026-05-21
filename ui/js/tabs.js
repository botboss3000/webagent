'use strict';

import { app } from './state.js';
import { startStream, stopStream } from './stream.js';
import { startLoop, stopLoop, setLoopLevel, toggleAutoScroll } from './loop.js';
import { startLoopVisual, stopLoopVisual } from './loop-logic.js';
import { initGithub, startGithub, stopGithub } from './github.js';
import { startAutoAgent, stopAutoAgent } from './autoagent.js';
import { startAgents, stopAgents } from './agents.js';
import { startAppConfig, stopAppConfig } from './app-config.js';

let chatVisible = localStorage.getItem('chatPanelVisible') !== 'false';

function setChatSideVisible(visible) {
  const chatSide = document.getElementById('chat-side');
  const resizeHandle = document.getElementById('chat-resize-handle');
  if (chatSide) chatSide.style.display = visible ? '' : 'none';
  if (resizeHandle) resizeHandle.style.display = visible ? '' : 'none';
}

export function initChatToggle() {
  const btn = document.getElementById('chat-toggle-btn');
  if (!btn) return;

  setChatSideVisible(chatVisible);
  btn.classList.toggle('active', chatVisible);

  btn.addEventListener('click', () => {
    chatVisible = !chatVisible;
    localStorage.setItem('chatPanelVisible', String(chatVisible));
    setChatSideVisible(chatVisible);
    btn.classList.toggle('active', chatVisible);
  });
}

export function initTabs() {
  const tabSelect = document.getElementById('main-tab-select');
  if (!tabSelect) return;

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

    localStorage.setItem('lastActiveTab', tabValue);

    // Respect user's chat toggle preference
    setChatSideVisible(chatVisible);

    if (tabValue === 'terminal') {
      stopStream();
      stopLoop();
      stopLoopVisual();
      stopAutoAgent();
      stopAgents();
      stopAppConfig();
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
      stopAppConfig();
      startStream();
    } else if (tabValue === 'flow') {
      stopStream();
      stopLoopVisual();
      stopAutoAgent();
      stopAgents();
      stopAppConfig();
      startLoop();
    } else if (tabValue === 'loop-visual') {
      stopStream();
      stopLoop();
      stopAutoAgent();
      stopAgents();
      stopAppConfig();
      startLoopVisual();
    } else if (tabValue === 'database') {
      stopStream();
      stopLoop();
      stopLoopVisual();
      stopGithub();
      stopAutoAgent();
      stopAgents();
      stopAppConfig();
    } else if (tabValue === 'github') {
      stopStream();
      stopLoop();
      stopLoopVisual();
      stopAgents();
      stopAppConfig();
      startGithub();
    } else if (tabValue === 'autoagent') {
      stopStream();
      stopLoop();
      stopLoopVisual();
      stopGithub();
      stopAgents();
      stopAppConfig();
      startAutoAgent();
    } else if (tabValue === 'agents') {
      stopStream();
      stopLoop();
      stopLoopVisual();
      stopGithub();
      stopAutoAgent();
      stopAppConfig();
      startAgents();
    } else if (tabValue === 'app-config') {
      stopStream();
      stopLoop();
      stopLoopVisual();
      stopGithub();
      stopAutoAgent();
      stopAgents();
      startAppConfig();
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

  activateTab(tabSelect.value);
}
