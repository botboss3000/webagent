'use strict';

export const CONTROL_MODE_KEY = 'terminalControlModeUi';

const proto = () => (location.protocol === 'https:' ? 'wss:' : 'ws:');

export function termWsUrl() {
  return `${proto()}//${location.host}/api/v1/terminal/ws`;
}

export function agentWsUrl() {
  return `${proto()}//${location.host}/api/v1/agent/ws`;
}
