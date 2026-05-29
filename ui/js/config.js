'use strict';

export const CONTROL_MODE_KEY = 'terminalControlModeUi';

const proto = () => (location.protocol === 'https:' ? 'wss:' : 'ws:');

/**
 * Base URL path when the app is hosted under a subdirectory (e.g. GitHub Pages
 * project site: /webagent). Empty string when served at the domain root.
 */
const _UUID_PATH_RE = /^\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function appBasePath() {
  let path = window.location.pathname || '/';
  path = path.replace(/\/index\.html?$/i, '');
  if (path.length > 1 && path.endsWith('/')) {
    path = path.slice(0, -1);
  }
  // Public agent URLs (/{uuid}) are not a subdirectory base — treat as root
  if (_UUID_PATH_RE.test(path)) return '';
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
