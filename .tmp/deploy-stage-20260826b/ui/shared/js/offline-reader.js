'use strict';

// App-wide cached-reader state. The health poll owns server reachability;
// navigator.onLine is only an immediate hint for a cold boot. In this mode the
// already-cached shell, sessions and transcripts remain navigable, while every
// composer/session mutation affordance is made inert.

let _active = false;
let _observer = null;

const MUTATION_SELECTORS = [
  '#session-new-header-btn',
  '#session-dropdown-kebab',
  '#session-dropdown-delete',
  '.session-row-kebab',
  '.session-row-delete',
  '.session-row-action',
  '.session-row-visibility',
  '.session-manage-delete-selected',
];

function _ensureComposerNotice() {
  let notice = document.getElementById('offline-reader-composer-notice');
  const inputArea = document.getElementById('chat-input-area');
  if (!inputArea || notice) return notice;
  notice = document.createElement('div');
  notice.id = 'offline-reader-composer-notice';
  notice.setAttribute('role', 'status');
  notice.textContent = 'Cached transcript · reconnect to make changes';
  inputArea.parentNode?.insertBefore(notice, inputArea);
  return notice;
}

function _applyDomState() {
  if (!document.body) return;
  document.body.dataset.offlineReadonly = _active ? 'true' : 'false';
  _ensureComposerNotice();

  // The chat aura is the app's single connection indicator. Keep its existing
  // red pulse active for the entire cached-reader period; do not add a second
  // flag to the header.
  const chatButton = document.getElementById('chat-toggle-btn');
  if (chatButton) {
    chatButton.classList.toggle('chat-aura-red', _active);
    if (_active) chatButton.title = 'Offline · cached data only';
    else if (chatButton.title === 'Offline · cached data only') {
      chatButton.title = 'Toggle chat panel (long-press for a new session)';
    }
  }

  const inputArea = document.getElementById('chat-input-area');
  if (inputArea) {
    if (_active) {
      inputArea.dataset.offlineReaderInert = 'true';
      inputArea.inert = true;
    } else if (inputArea.dataset.offlineReaderInert === 'true') {
      delete inputArea.dataset.offlineReaderInert;
      inputArea.inert = false;
    }
  }

  for (const el of document.querySelectorAll(MUTATION_SELECTORS.join(','))) {
    if (_active) {
      el.dataset.offlineReaderInert = 'true';
      el.inert = true;
      el.setAttribute('aria-disabled', 'true');
    } else if (el.dataset.offlineReaderInert === 'true') {
      delete el.dataset.offlineReaderInert;
      el.inert = false;
      el.removeAttribute('aria-disabled');
    }
  }
}

export function isOfflineReadOnly() {
  return _active;
}

export function setOfflineReadOnly(active, detail = {}) {
  const next = active === true;
  const changed = next !== _active;
  _active = next;
  window.__webagentOfflineReadOnly = next;
  _applyDomState();
  if (changed) {
    window.dispatchEvent(new CustomEvent('webagent-offline-readonly-changed', {
      detail: { active: next, ...detail },
    }));
  }
}

export function initOfflineReader() {
  setOfflineReadOnly(navigator.onLine === false, { source: 'navigator' });
  window.addEventListener('offline', () => {
    setOfflineReadOnly(true, { source: 'navigator' });
  });
  // Do not clear the state on navigator "online": internet access does not
  // prove that this WebAgent server is reachable. The health poll clears it.
  if (!_observer && document.documentElement) {
    _observer = new MutationObserver(() => _applyDomState());
    _observer.observe(document.documentElement, { childList: true, subtree: true });
  }
}
