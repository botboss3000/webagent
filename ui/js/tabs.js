'use strict';

import { setLoopLevel, toggleAutoScroll } from './loop.js';
import { startAutoAgent, stopAutoAgent } from './autoagent.js';
import { startAgents, stopAgents } from './agents.js';
import { startAccount } from './account.js';
import { startAdminTools, stopAdminTools } from './files.js';
import { startWeb, stopWeb } from './web.js';
import { startWiki, stopWiki } from './wiki.js';
import { startSessions, stopSessions } from './sessions-page.js';
import { refreshTutorial } from './tutorial.js';
import { initAppConfig } from './app-config.js';
import { initDbViewer } from './db/index.js';
import { initDataManagement } from './data-management.js';
import { initRemoteAccess } from './remote-access.js';

// ── Lazy Admin Tools init ───────────────────────────────────────────────────
// The Admin Tools sub-panels (App Config, Database viewer, Data Management,
// Remote Access) are NOT built at boot anymore — they're wired the first time
// the user actually opens the Admin Tools tab. This keeps startup light,
// especially on mobile where only one panel is ever on screen. All four inits
// are null-safe + idempotent; initFiles() is run by startAdminTools() itself.
let _adminInited = false;
function _ensureAdminInit() {
  if (_adminInited) return;
  _adminInited = true;
  try { initDbViewer(); }       catch (e) { console.error('initDbViewer failed', e); }
  try { initDataManagement(); }  catch (e) { console.error('initDataManagement failed', e); }
  try { initRemoteAccess(); }    catch (e) { console.error('initRemoteAccess failed', e); }
  try { initAppConfig(); }       catch (e) { console.error('initAppConfig failed', e); }
}

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
      stopWeb();
      stopWiki();
      stopSessions();
      startAutoAgent();
    } else if (tabValue === 'agents') {
      stopAutoAgent();
      stopAdminTools();
      stopWeb();
      stopWiki();
      stopSessions();
      startAgents();
    } else if (tabValue === 'account') {
      stopAutoAgent();
      stopAgents();
      stopAdminTools();
      stopWeb();
      stopWiki();
      stopSessions();
      startAccount();
    } else if (tabValue === 'admin-tools') {
      stopAutoAgent();
      stopAgents();
      stopWeb();
      stopWiki();
      stopSessions();
      _ensureAdminInit();
      startAdminTools();
    } else if (tabValue === 'sessions') {
      stopAutoAgent();
      stopAgents();
      stopAdminTools();
      stopWeb();
      stopWiki();
      startSessions();
    } else if (tabValue === 'web') {
      stopAutoAgent();
      stopAgents();
      stopAdminTools();
      stopWiki();
      stopSessions();
      startWeb();
    } else if (tabValue === 'terminal') {
      stopAutoAgent();
      stopAgents();
      stopAdminTools();
      stopWeb();
      stopWiki();
      stopSessions();
    } else if (tabValue === 'wiki') {
      stopAutoAgent();
      stopAgents();
      stopAdminTools();
      stopWeb();
      stopSessions();
      startWiki();
    } else if (tabValue.startsWith('plugin-')) {
      // Plugin pages: stop everything, show the iframe (already in DOM)
      stopAutoAgent();
      stopAgents();
      stopAdminTools();
      stopWeb();
      stopWiki();
      stopSessions();
    }

    // Re-render tutorial hint badges for the newly active tab. Defer a tick
    // so the tab's start*() routine has populated dynamic content first.
    try { refreshTutorial(tabValue); } catch (_) {}
  }

  // On load: if ?tab=<tabValue> is in the URL, activate that tab.
  // This is used when middle-clicking a header button opens a new window.
  (function () {
    try {
      var p = new URLSearchParams(location.search);
      var t = p.get('tab');
      if (t && tabSelect.querySelector('option[value="' + t + '"]')) {
        tabSelect.value = t;
        activateTab(t, true);
      }
    } catch (_) {}
  })();

  // Programmatic view switch for the App-control ability (agent-driven, via a
  // `ui_command` WebSocket event). Mirrors a user picking from the dropdown:
  // sync the select (when the option exists) then activate the tab.
  window.__setMainTab = function (view) {
    if (!view) return;
    try {
      if (tabSelect.querySelector(`option[value="${view}"]`)) {
        tabSelect.value = view;
      }
      activateTab(view, true);
    } catch (_) { /* ignore */ }
  };

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

  // ── Middle-click on header buttons opens a new tab ──
  // Middle-clicking a .main-tab opens the same page at that tab in a new window.
  // Middle-clicking the + button opens a fresh session in a new window.
  (function () {
    var base = location.pathname.replace(/\/+$/, '') || '/';
    document.querySelectorAll('#main-tabs .main-tab').forEach(function (btn) {
      btn.addEventListener('auxclick', function (e) {
        if (e.button !== 1) return;  // middle-click only
        e.preventDefault();
        var val = btn.dataset.value;
        if (val) {
          window.open(base + '?tab=' + encodeURIComponent(val), '_blank');
        }
      });
    });
    var plusBtn = document.getElementById('session-new-header-btn');
    if (plusBtn) {
      plusBtn.addEventListener('auxclick', function (e) {
        if (e.button !== 1) return;
        e.preventDefault();
        window.open(base, '_blank');
      });
    }
  })();

  // The spinner was shown in the header during init (pre-paint script
  // set body.is-booting). Now that the tab content is mounted, clear
  // is-booting so the spinner fades out immediately.
  document.body.classList.remove('is-booting');
}
