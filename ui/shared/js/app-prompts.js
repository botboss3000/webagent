'use strict';

// UI message catalog — the user-facing chat strings (the welcome bubble, the
// "new session" / "switched agent" system bubbles, and the composer pill
// placeholders) are authored in app/defaults/app-prompts.json under
// "ui_messages.chat" and served to every visitor via the public endpoint
// GET /api/v1/app-prompts/ui-messages (app/api/features.py).
//
// This module fetches that section once at boot (loadUiMessages, called from
// ui/shared/js/main.js) and exposes a SYNCHRONOUS accessor, chatMsg(key), with
// built-in fallbacks so a slow or failed fetch never blanks a string. The JSON
// is the driver; the fallbacks below mirror it as the safety net. Consumers:
//   • ui/chat-side-panel/js/session-load.js   (welcome + restricted new-session)
//   • ui/chat-side-panel/js/session-init.js   (new-session + switched-agent)
//   • ui/chat-side-panel/js/chat-send.js       (pill placeholder + lock message)
//   • ui/chat-side-panel/chat-side-panel.html  (static fallbacks, patched here)

import { apiPath } from './config.js';
import { app } from './state.js';

// Fallback defaults — KEEP IN SYNC with app/defaults/app-prompts.json
// ("ui_messages.chat"). Used until the fetch resolves, and if it ever fails.
const _DEFAULTS = {
  welcome_bubble: 'Welcome to WebAgent',
  new_session_bubble: 'New session. Start typing below.',
  switched_agent_bubble: 'Switched agent. New session started.',
  pill_placeholder: 'Chat with the agent',
  pill_locked_placeholder: 'Sign in to chat — this app does not allow anonymous use.',
};

let _chat = { ..._DEFAULTS };

/** Synchronous accessor for the app-wide chat UI string. Always usable. */
export function chatMsg(key) {
  return (_chat && _chat[key]) || _DEFAULTS[key] || '';
}

// Find a cached agent's per-agent chat UI override map. Agents are fetched once
// into window.__agentsSharedData.agents (each carries a `chat_ui` object served
// by app/api/agents.py _safe_agent); a blank/missing value means "use default".
function _agentChatUi(agentId) {
  if (!agentId) return null;
  try {
    const list = window.__agentsSharedData && window.__agentsSharedData.agents;
    if (Array.isArray(list)) {
      const a = list.find(x => x && x.id === agentId);
      if (a && a.chat_ui && typeof a.chat_ui === 'object') return a.chat_ui;
    }
  } catch (_) { /* non-fatal */ }
  return null;
}

/**
 * Per-agent chat UI string. Returns this agent's override for `key` when it has
 * a non-blank one, otherwise falls back to the app-wide chatMsg(key). `agentId`
 * defaults to the current chat agent. Use this (not chatMsg) for the welcome /
 * new-session / switched-agent bubbles and the composer pill placeholders.
 */
export function agentChatMsg(key, agentId) {
  const id = agentId || (app && app.currentAgentId) || '';
  const ov = _agentChatUi(id);
  const v = ov && ov[key];
  if (typeof v === 'string' && v.trim()) return v;
  return chatMsg(key);
}

/**
 * Fetch the live strings once and merge them over the defaults. Resolves even
 * on failure (keeps the fallbacks) so it can be awaited on the boot path
 * without risk of stalling the app.
 */
export async function loadUiMessages() {
  try {
    const res = await fetch(apiPath('/api/v1/app-prompts/ui-messages'));
    if (!res.ok) return;
    const data = await res.json();
    const chat = data && data.ui_messages && data.ui_messages.chat;
    if (chat && typeof chat === 'object') {
      _chat = { ..._DEFAULTS, ...chat };
      _applyStaticStrings();
    }
  } catch (_) { /* unreachable config — keep fallbacks */ }
}

// Patch the strings that live in static markup (the composer pill placeholder)
// once the JSON arrives, covering the window before any JS render. The welcome
// bubble is re-rendered from chatMsg() by session-load.js on session load, so
// it needs no static patch here. Never overwrite the chat-lock placeholder that
// chat-send.js swaps in for signed-out visitors (a disabled input).
function _applyStaticStrings() {
  try {
    const input = document.getElementById('chat-input');
    if (input && !input.disabled) input.placeholder = chatMsg('pill_placeholder');
  } catch (_) { /* DOM not ready / non-fatal */ }
}
