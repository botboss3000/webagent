'use strict';

// URL + config helpers — appBasePath(), apiPath(), WebSocket URL builders, and the
// terminal control-mode key. Works under a subdirectory base and on plain HTTP.

const CONTROL_MODE_KEY = 'terminalControlModeUi';

const proto = () => (location.protocol === 'https:' ? 'wss:' : 'ws:');

/**
 * Base URL path when the app is hosted under a subdirectory (e.g. GitHub Pages
 * project site: /webagent). Empty string when served at the domain root.
 */
const _UUID_PATH_RE = /^\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// Server routes that serve the SAME single-page app shell as the domain root
// (see app/main.py: app_shell_direct `/app`, plus the `/setup` setup flow). These
// are ALIASES for the home page, NOT a subdirectory deploy — so the API base for
// them is the domain root (''). The PWA manifest's start_url is `/app`, so an
// installed-app launch lands here; without this, every relative fetch would be
// prefixed with `/app` (apiPath('/api/v1/chat/send') → '/app/api/v1/chat/send')
// and 404, which breaks chat send ("pending forever") and the agent/session
// lists. Keep this set in sync with the shell routes in main.py.
const _SHELL_ROUTES = new Set(['/app', '/setup', '/setup.html']);

export function appBasePath() {
  let path = window.location.pathname || '/';
  path = path.replace(/\/index\.html?$/i, '');
  if (path.length > 1 && path.endsWith('/')) {
    path = path.slice(0, -1);
  }
  // Public agent URLs (/{uuid}) are not a subdirectory base — treat as root
  if (_UUID_PATH_RE.test(path)) return '';
  // Known app-shell alias routes are root-served, not a subdirectory.
  if (_SHELL_ROUTES.has(path.toLowerCase())) return '';
  return path === '/' ? '' : path;
}

/** Prefix an absolute app path (must start with /) for fetch() and WebSocket URLs. */
export function apiPath(p) {
  if (!p.startsWith('/')) {
    throw new Error('apiPath expects a path starting with /');
  }
  return appBasePath() + p;
}

export function termWsUrl() {
  return `${proto()}//${location.host}${apiPath('/api/v1/terminal/ws')}`;
}

export function agentWsUrl() {
  return `${proto()}//${location.host}${apiPath('/api/v1/agent/ws')}`;
}

export function browserWsUrl() {
  return `${proto()}//${location.host}${apiPath('/api/v1/browser/ws')}`;
}

export function browserScreencastWsUrl() {
  return `${proto()}//${location.host}${apiPath('/api/v1/browser/screencast')}`;
}

export function desktopControlWsUrl() {
  return `${proto()}//${location.host}${apiPath('/api/v1/control/desktop')}`;
}
