'use strict';

import { setLoopLevel, toggleAutoScroll } from './loop.js';
import { startAutoAgent, stopAutoAgent } from './autoagent.js';
import { startAgents, stopAgents } from './agents.js';
import { startAccount } from './account.js';
import { startAdminTools, stopAdminTools } from './files.js';
import { refreshTutorial } from './tutorial.js';

function setChatPanelVisible(visible) {
  const chatPanel = document.getElementById('chat-panel');
  const resizeHandle = document.getElementById('chat-resize-handle');
  if (chatPanel) chatPanel.style.display = visible ? '' : 'none';
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
          setChatPanelVisible(false);
        }
      }
    } else {
      const stored = localStorage.getItem('chatPanelVisible');
      const visible = stored === null ? true : stored !== 'false';
      setChatPanelVisible(visible);
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

  // Reveal the saved tab. The pre-paint script (index.html) sets
  // body.boot-chat-only so chat fills the stage at first paint and the
  // wrong tab never flashes. Now that the saved tab content has been
  // mounted by activateTab(), play the chosen transition: add
  // .boot-revealing FIRST (so its transition rules are registered),
  // then on the next frame drop .boot-chat-only so the layout changes
  // animate instead of snapping. Remove .boot-revealing after the
  // animation duration so normal interactions don't pay the ease cost.
  if (document.body.classList.contains('boot-chat-only')) {
    requestAnimationFrame(() => {
      document.body.classList.add('boot-revealing');
      requestAnimationFrame(() => {
        document.body.classList.remove('boot-chat-only');
        setTimeout(() => {
          document.body.classList.remove('boot-revealing');
        }, 480);
      });
    });
  }
}
