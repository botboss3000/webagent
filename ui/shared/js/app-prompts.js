'use strict';

// UI message catalog — the user-facing chat strings (the welcome bubble, the
// "new session" / "switched agent" system bubbles, and the composer pill
// placeholders) are authored in data/config/chat_ui.json under
// "chat_common.messages" and surface-specific message overrides, served in
// the public auth UI config.
//
// This module fetches the FULL chat_ui.json at boot (loadUiMessages, called
// from ui/shared/js/main.js) and keeps it. Per-agent overrides are fetched
// from the agent list (window.__agentsSharedData.agents) and deep-merged on
// demand so each agent can have completely custom chat chrome.
//
// Consumers:
//   • ui/chat/js/session-load.js   (welcome + restricted new-session)
//   • ui/chat/js/session-init.js   (new-session + switched-agent)
//   • ui/chat/js/chat-send.js       (pill placeholder + lock message)
//   • ui/chat/js/chat-ui.js         (full chat chrome rendering)
//   • ui/chat/chat-side-panel.html  (static fallbacks, patched here)

import { apiPath } from './config.js';
import { app } from './state.js';

// Fallback defaults — KEEP IN SYNC with chat_ui.json chat_common.messages.
const _DEFAULTS = {
  welcome_bubble: 'Welcome to WebAgent',
  new_session_bubble: 'New session. Start typing below.',
  switched_agent_bubble: 'Switched agent. New session started.',
  pill_placeholder: 'Chat with the agent',
  pill_locked_placeholder: 'Sign in to chat — this app does not allow anonymous use.',
  session_deleted_notice: 'Session not found — it was deleted.',
};

// The FULL app-wide chat_ui.json (all surfaces). Loaded once at boot.
// Stays null until the server responds — the pipeline MUST wait for agents
// to load before applying any config (see chat-controls-config.js).
let _fullChatUi = null;
// The flattened active-surface messages (chat_common.messages merged with the
// current surface's messages). Same shape as _DEFAULTS; kept for chatMsg().
let _chat = { ..._DEFAULTS };

/** Synchronous accessor for the app-wide chat UI string. Always usable. */
export function chatMsg(key) {
  return (_chat && _chat[key]) || _DEFAULTS[key] || '';
}

/**
 * Check if debug mode is active for the current (or given) agent.
 * Returns true when chat_ui.json chat_common.debug is truthy.
 */
export function isDebugMode(agentId) {
  const ui = getAgentChatUi(agentId || (app && app.currentAgentId));
  return !!(ui.chat_common?.debug);
}

/**
 * Read a boolean flag from the current agent's deep-merged chat_ui config
 * (chat_common.<key>). Falls back to `defaultVal` when absent/unreadable.
 */
export function chatUiFlag(key, defaultVal) {
  try {
    const ui = getAgentChatUi();
    const common = ui && ui.chat_common;
    if (common && common[key] !== undefined) return common[key];
    return defaultVal;
  } catch (_) { return defaultVal; }
}

// ── Per-agent chat UI ────────────────────────────────────────────────────────

/** Deep-merge two objects (nested dicts merged; scalars/arrays replaced).
 *  Returns a new object; neither argument is mutated. */
function _deepMerge(base, overrides) {
  if (!overrides || typeof overrides !== 'object') return base;
  const out = { ...base };
  for (const key of Object.keys(overrides)) {
    const ov = overrides[key];
    const bv = base[key];
    if (ov && typeof ov === 'object' && !Array.isArray(ov) && bv && typeof bv === 'object' && !Array.isArray(bv)) {
      out[key] = _deepMerge(bv, ov);
    } else {
      out[key] = ov;
    }
  }
  return out;
}

// Per-agent override cache: agentId → chat_ui override dict (from agents list).
const _agentOverrideCache = new Map();

/** Find a cached agent's per-agent chat UI override map. Agents are fetched once
 *  into window.__agentsSharedData.agents (each carries a `chat_ui` object served
 *  by app/api/agents.py _safe_agent); a blank/missing value means "use default". */
function _agentChatUiOverride(agentId) {
  if (!agentId) return null;
  let cached = _agentOverrideCache.get(agentId);
  if (cached !== undefined) return cached;
  let ov = null;
  try {
    const list = window.__agentsSharedData && window.__agentsSharedData.agents;
    if (Array.isArray(list)) {
      const a = list.find(x => x && x.id === agentId);
      if (a && a.chat_ui && typeof a.chat_ui === 'object' && !Array.isArray(a.chat_ui) && Object.keys(a.chat_ui).length > 0) {
        ov = a.chat_ui;
      }
    }
  } catch (_) { /* non-fatal */ }
  // Only cache if we found agents data — otherwise leave uncached so
  // a subsequent call after agents load will find the real override.
  if (window.__agentsSharedData && window.__agentsSharedData.agents) {
    _agentOverrideCache.set(agentId, ov);
  }
  return ov;
}

/** Clear the per-agent override cache — call after agents list refreshes. */
export function clearAgentChatUiCache() {
  _agentOverrideCache.clear();
}

/**
 * Return the FULL deep-merged chat_ui config for a specific agent.
 *
 * Starts with the app-wide chat_ui.json, then deep-merges the agent's
 * metadata.chat_ui override on top. Returns the full JSON (all surfaces,
 * chat_common, chat_pill, launcher, etc.) so the chat panel can render
 * per-agent chrome.
 */
export function getAgentChatUi(agentId) {
  const id = agentId || (app && app.currentAgentId) || '';
  const override = _agentChatUiOverride(id);
  if (!override) {
    // No per-agent override — return server config or a minimal stub that
    // always has chat_common so the pipeline doesn't bail early.
    const base = _fullChatUi || {};
    if (!base.chat_common) base.chat_common = {};
    return base;
  }
  const merged = _deepMerge(_fullChatUi || {}, override);
  // Surface-only overrides (chat_mobile/chat_desktop without chat_common)
  // must still carry chat_common so mergeProfile() has a base to build from.
  if (!merged.chat_common) merged.chat_common = {};
  return merged;
}

/**
 * Per-agent chat UI string. Looks up `key` inside the active surface's
 * messages block of the agent's deep-merged chat_ui, falling back to the
 * app-wide default. `agentId` defaults to the current chat agent.
 *
 * Use this (not chatMsg) for the welcome / new-session / switched-agent
 * bubbles and the composer pill placeholders.
 */
export function agentChatMsg(key, agentId) {
  const id = agentId || (app && app.currentAgentId) || '';
  // Try the agent's merged surface messages first
  const agentUi = getAgentChatUi(id);
  const surfaceKey = window.__CHAT_PORTAL__
    ? 'chat_widget'
    : (window.innerWidth <= 768 ? 'chat_mobile' : 'chat_desktop');
  const surface = agentUi[surfaceKey] || {};
  const surfaceMsgs = surface.messages || {};
  const commonMsgs = (agentUi.chat_common && agentUi.chat_common.messages) || {};
  const v = surfaceMsgs[key] || commonMsgs[key];
  if (typeof v === 'string' && v.trim()) return v;
  return chatMsg(key);
}

// ── Boot-time load ───────────────────────────────────────────────────────────

/**
 * Fetch the live strings once and merge them over the defaults. Resolves even
 * on failure (keeps the fallbacks) so it can be awaited on the boot path
 * without risk of stalling the app.
 */
export async function loadUiMessages(agentId) {
  try {
    const url = agentId
      ? apiPath('/api/v1/auth/ui-config?agent_id=' + encodeURIComponent(agentId))
      : apiPath('/api/v1/auth/ui-config');
    const res = await fetch(url);
    if (!res.ok) return;
    const data = await res.json();
    const ui = data?.chat_ui;
    if (ui) {
      _fullChatUi = ui;
      window.__chatUiSnapshot = ui;  // snapshot for pill defaults before agents load
      const surface = window.__CHAT_PORTAL__
        ? ui.chat_widget
        : (window.innerWidth <= 768 ? ui.chat_mobile : ui.chat_desktop);
      const chat = { ...(ui.chat_common?.messages || {}), ...(surface?.messages || {}) };
      if (chat && typeof chat === 'object') {
        _chat = { ..._DEFAULTS, ...chat };
        _applyStaticStrings();
      }
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
