'use strict';

export const CONTROL_MODE_KEY = 'terminalControlModeUi';

const proto = () => (location.protocol === 'https:' ? 'wss:' : 'ws:');

/**
 * Base URL path when the app is hosted under a subdirectory (e.g. GitHub Pages
 * project site: /webagent). Empty string when served at the domain root.
 */
export function appBasePath() {
  let path = window.location.pathname || '/';
  path = path.replace(/\/index\.html?$/i, '');
  if (path.length > 1 && path.endsWith('/')) {
    path = path.slice(0, -1);
  }
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
