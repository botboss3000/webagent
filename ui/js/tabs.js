'use strict';

import { app } from './state.js';
import { startStream, stopStream } from './stream.js';
import { startLoop, stopLoop, setLoopLevel, toggleAutoScroll } from './loop.js';
import { startLoopVisual, stopLoopVisual } from './loop-logic.js';
import { startAutoAgent, stopAutoAgent } from './autoagent.js';
import { startAgents, stopAgents } from './agents.js';
import { startAppConfig, stopAppConfig } from './app-config.js';
import { startAccount } from './account.js';
import { startFiles, stopFiles } from './files.js';
import { refreshTutorial } from './tutorial.js';

function setChatSideVisible(visible) {
  const chatSide = document.getElementById('chat-side');
  const resizeHandle = document.getElementById('chat-resize-handle');
  if (chatSide) chatSide.style.display = visible ? '' : 'none';
  if (resizeHandle) resizeHandle.style.display = visible ? '' : 'none';
}

export function initTabs() {
  const tabSelect = document.getElementById('main-tab-select');
  if (!tabSelect) return;

  const savedTab = localStorage.getItem('lastActiveTab');
  if (savedTab && tabSelect.querySelector(`option[value="${savedTab}"]`)) {
    tabSelect.value = savedTab;
  }

  function activateTab(tabValue, userInitiated) {
    document.querySelectorAll('.tab-content').forEach((c) => c.classList.remove('active'));
    const targetContent = document.getElementById('tab-' + tabValue);
    if (targetContent) {
      targetContent.classList.add('active');
    }

    localStorage.setItem('lastActiveTab', tabValue);

    // Mobile: explicit page pick from dropdown hides chat (reveals page).
    // Initial load on mobile must respect saved chat-visibility — don't override.
    // Desktop: always reflect saved chat toggle preference.
    const isMobile = typeof window.__isMobileChatLayout === 'function'
      ? window.__isMobileChatLayout() : (window.innerWidth <= 800);
    if (isMobile) {
      if (userInitiated) {
        if (typeof window.__applyChatVisible === 'function') {
          window.__applyChatVisible(false);
        } else {
          setChatSideVisible(false);
        }
      }
    } else {
      const stored = localStorage.getItem('chatPanelVisible');
      const visible = stored === null ? true : stored !== 'false';
      setChatSideVisible(visible);
    }

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
      stopAutoAgent();
      stopAgents();
      stopAppConfig();
    } else if (tabValue === 'autoagent') {
      stopStream();
      stopLoop();
      stopLoopVisual();
      stopAgents();
      stopAppConfig();
      startAutoAgent();
    } else if (tabValue === 'agents') {
      stopStream();
      stopLoop();
      stopLoopVisual();
      stopAutoAgent();
      stopAppConfig();
      startAgents();
    } else if (tabValue === 'app-config') {
      stopStream();
      stopLoop();
      stopLoopVisual();
      stopAutoAgent();
      stopAgents();
      startAppConfig();
    } else if (tabValue === 'account') {
      stopStream();
      stopLoop();
      stopLoopVisual();
      stopAutoAgent();
      stopAgents();
      stopAppConfig();
      stopFiles();
      startAccount();
    } else if (tabValue === 'files') {
      stopStream();
      stopLoop();
      stopLoopVisual();
      stopAutoAgent();
      stopAgents();
      stopAppConfig();
      startFiles();
    }

    // Re-render tutorial hint badges for the newly active tab. Defer a tick
    // so the tab's start*() routine has populated dynamic content first.
    try { refreshTutorial(tabValue); } catch (_) {}
  }

  tabSelect.addEventListener('change', (e) => {
    activateTab(e.target.value, true);
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
