'use strict';

import { setLoopLevel, toggleAutoScroll } from './loop.js';
import { startAutoAgent, stopAutoAgent } from './autoagent.js';
import { startAgents, stopAgents } from './agents.js';
import { startAccount } from './account.js';
import { startAdminTools, stopAdminTools } from './files.js';
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
    // Back-compat: 'files' was the legacy id for what is now 'admin-tools'.
    // Saved state in older browsers will still hold 'files'.
    if (tabValue === 'files') tabValue = 'admin-tools';
    // Back-compat: 'database' was its own top-level tab; it now lives as a
    // sidebar view inside Admin Tools. Redirect and request that sub-view.
    if (tabValue === 'database') {
      tabValue = 'admin-tools';
      try { localStorage.setItem('files.sidebarView', 'database'); } catch (_) {}
    }
    // Back-compat: 'flow' and 'loop-visual' top-level tabs moved into Admin
    // Tools as the Interactions and Runtime Loop strip views.
    if (tabValue === 'stream' || tabValue === 'flow') {
      tabValue = 'admin-tools';
      try { localStorage.setItem('files.sidebarView', 'interactions'); } catch (_) {}
    }
    if (tabValue === 'loop-visual') {
      tabValue = 'admin-tools';
      try { localStorage.setItem('files.sidebarView', 'runtime-loop'); } catch (_) {}
    }

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

    // Flow + Runtime Loop are now sidebar views inside Admin Tools, so
    // stopAdminTools() owns their lifecycle when leaving admin-tools.
    if (tabValue === 'autoagent') {
      stopAgents();
      stopAdminTools();
      startAutoAgent();
    } else if (tabValue === 'agents') {
      stopAutoAgent();
      stopAdminTools();
      startAgents();
    } else if (tabValue === 'account') {
      stopAutoAgent();
      stopAgents();
      stopAdminTools();
      startAccount();
    } else if (tabValue === 'admin-tools') {
      stopAutoAgent();
      stopAgents();
      startAdminTools();
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
