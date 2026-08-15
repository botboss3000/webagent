'use strict';

import {
  appendChatSurfaceBubble,
  applyChatSurfaceProfile,
  applyChatSurfaceHeader,
  chatSurfaceStatsHtml,
  createChatSurfaceUsage,
  installChatSurfaceIcons,
  wireChatSurfaceComposer,
  wireStatsCarousel,
} from '../shared/js/chat-surface.js';

// Standalone embed chat client. No imports from the SPA — talks straight to the
// public HTTP API on the SAME origin it was served from:
//   POST /api/v1/agents/<id>/anon-session   → mint a guest token + session
//   POST /api/v1/chat/stream                → SSE turn (token deltas + final)
// Config + agent id arrive in window.__EMBED__ (server-injected). Runs inside
// the customer's iframe; talks to the host page's loader via postMessage.

const BOOT = window.__EMBED__ || {};
const CFG = BOOT.config || {};
const AGENT_ID = BOOT.agentId;
const VIEW = CFG.chat_ui || {};

const $ = (id) => document.getElementById(id);
	const els = {
	  root: $('ec-root'),
	  messages: $('ec-messages'),
	  header: document.querySelector('.ec-header'),
	  title: $('ec-title'),
	  subtitle: $('ec-subtitle'),
	  composer: $('ec-composer'),
	  input: $('ec-input'),
	  send: $('ec-send'),
	  close: $('ec-close'),
	  unavailable: $('ec-unavailable'),
	  unavailableText: $('ec-unavailable-text'),
	  pill: document.querySelector('.ec-pill'),
	  stats: document.querySelector('.ec-stats'),
	  pillButtons: document.querySelector('.ec-pill-buttons'),
	  attach: document.querySelector('.ec-attach'),
	  voice: document.querySelector('.ec-voice'),
	  above: document.querySelector('.ec-above-pill'),
	  below: document.querySelector('.ec-below-pill'),
	  stop: $('ec-stop'),
	  continue: $('ec-continue'),
	  headIcon: document.querySelector('.ec-head-icon'),
	  stopIcon: document.querySelector('.ec-stop-icon'),
	  continueIcon: document.querySelector('.ec-continue-icon'),
	  toggleIcon: document.querySelector('.ec-toggle-icon'),
	  abilitiesIcon: document.querySelector('.ec-abilities-icon'),
	  targetIcon: document.querySelector('.ec-target-icon'),
	  footerHandle: document.querySelector('.chat-footer-handle'),
	  dot: document.querySelector('.cw-dot'),
	  closeBtn: $('ec-close'),
	};
els.stats.innerHTML = chatSurfaceStatsHtml();
wireStatsCarousel(els.stats);
const usage = createChatSurfaceUsage(els.stats);

// ── Theming ────────────────────────────────────────────────────────────────
function applyTheme() {
  const accent = CFG.accent || '#4f46e5';
  document.documentElement.style.setProperty('--ec-accent', accent);
  document.documentElement.style.setProperty('--ec-accent-fg', contrastColor(accent));
  document.documentElement.style.setProperty('--brand', accent);
  document.documentElement.style.setProperty('--accent', accent);
  els.title.textContent = CFG.title || BOOT.agentName || 'Chat';
  if (CFG.subtitle) { els.subtitle.textContent = CFG.subtitle; els.subtitle.hidden = false; }
  if (CFG.placeholder) els.input.setAttribute('placeholder', CFG.placeholder);
  applyViewProfile();
}

// The embed consumes the exact same widget profile as the in-app widget.
function applyViewProfile() {
  applyChatSurfaceProfile(els, VIEW);
}

// Pick black or white text for a given hex background (WCAG-ish luminance).
function contrastColor(hex) {
  let h = (hex || '').replace('#', '');
  if (h.length === 3) h = h.split('').map((c) => c + c).join('');
  if (h.length !== 6) return '#ffffff';
  const r = parseInt(h.slice(0, 2), 16) / 255;
  const g = parseInt(h.slice(2, 4), 16) / 255;
  const b = parseInt(h.slice(4, 6), 16) / 255;
  const lin = (c) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
  const L = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  return L > 0.5 ? '#1a1a24' : '#ffffff';
}

// ── Parent-frame messaging ──────────────────────────────────────────────────
function toParent(action, data) {
  try { parent.postMessage(Object.assign({ source: 'webagent-embed', action }, data || {}), '*'); }
  catch (_) { /* not framed / cross-origin locked — harmless */ }
}

// ── Message rendering ────────────────────────────────────────────────────────
function addBubble(role, text) {
  const el = appendChatSurfaceBubble(els.messages, role, text || '');
  el.classList.add('ec-msg');
  return el;
}

function showTyping() {
  const el = document.createElement('div');
  el.className = 'chat-bubble ec-msg agent';
  el.innerHTML = '<span class="ec-typing"><span></span><span></span><span></span></span>';
  els.messages.appendChild(el);
  scrollToEnd();
  return el;
}

function scrollToEnd() {
  els.messages.scrollTop = els.messages.scrollHeight;
}

// ── Session bootstrap (lazy: minted on first send) ──────────────────────────
let session = null;      // { token, user_id, session_id }
let sending = false;
let activeController = null;

function browserId() {
  let id = null;
  try { id = localStorage.getItem('webagent_embed_bid'); } catch (_) {}
  if (!id) {
    id = (crypto && crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
    try { localStorage.setItem('webagent_embed_bid', id); } catch (_) {}
  }
  return id;
}

async function ensureSession() {
  if (session) return session;
  const res = await fetch(`/api/v1/agents/${encodeURIComponent(AGENT_ID)}/anon-session`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ browser_id: browserId() }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `Could not start chat (${res.status}).`);
  }
  session = await res.json();
  return session;
}

// ── Send a turn — SSE stream of {type, content} events ──────────────────────
async function sendMessage(text) {
  if (sending || !text.trim()) return;
  sending = true;
  usage.begin();
  if (els.below.classList.contains('expanded')) els.below.hidden = false;
  if ((VIEW.above_pill?.right || []).includes('stop')) els.stop.hidden = false;
  els.continue.hidden = true;
  els.send.disabled = true;
  els.input.value = '';
  els.pill.classList.remove('has-text');
  autoGrow();
  addBubble('user', text);

  const typing = showTyping();
  let bubble = null;        // becomes the agent bubble once text arrives
  let acc = '';             // accumulated token deltas

  const finish = () => {
    if (typing.isConnected) typing.remove();
    usage.finish();
    sending = false;
    els.stop.hidden = true;
    if ((VIEW.above_pill?.right || []).includes('continue')) els.continue.hidden = false;
    els.send.disabled = !els.input.value.trim();
    els.input.focus();
  };

  try {
    const s = await ensureSession();
    activeController = new AbortController();
    const res = await fetch('/api/v1/chat/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${s.token}`,
      },
      body: JSON.stringify({
        user_id: s.user_id,
        session_id: s.session_id,
        message: text,
        agent_id: AGENT_ID,
        execution_mode: 'ask',
      }),
      signal: activeController.signal,
    });
    if (!res.ok || !res.body) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `Chat failed (${res.status}).`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    const render = (t) => {
      if (!bubble) { if (typing.isConnected) typing.remove(); bubble = addBubble('agent', ''); }
      bubble.textContent = t;
      scrollToEnd();
    };

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // SSE frames are separated by a blank line.
      let idx;
      while ((idx = buf.indexOf('\n\n')) !== -1) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const line = frame.split('\n').find((l) => l.startsWith('data:'));
        if (!line) continue;
        const payload = line.slice(5).trim();
        if (!payload) continue;
        let ev;
        try { ev = JSON.parse(payload); } catch (_) { continue; }
        usage.event(ev);
        if (ev.type === 'stream' && typeof ev.content === 'string') {
          usage.stream(ev.content);
          acc += ev.content;              // token deltas → accumulate
          render(acc);
        } else if (ev.type === 'response' && typeof ev.content === 'string') {
          acc = ev.content;               // authoritative final answer
          render(acc);
        } else if (ev.type === 'error') {
          // Surface errors only in debug mode; otherwise show a generic message.
          if (VIEW.chat_common?.debug) {
            throw new Error(ev.message || 'The agent hit an error.');
          }
          // Non-debug: show a generic message and mark handled so the
          // fallback "I didn't catch a response" doesn't fire.
          addBubble('agent', 'Something went wrong. Please try again.');
          acc = '__error_handled__';
        }
        // tool_call events are intentionally ignored in this minimal widget.
      }
    }
    if (!bubble && !acc) addBubble('agent', "I didn't catch a response — please try again.");
  } catch (err) {
    if (typing.isConnected) typing.remove();
    if (err?.name === 'AbortError') addBubble('agent', 'Generation stopped.');
    else addBubble('error', (err && err.message) || 'Something went wrong. Please try again.');
  } finally {
    activeController = null;
    finish();
  }
}

// ── Input UX ────────────────────────────────────────────────────────────────
function autoGrow() {
  els.input.style.height = 'auto';
  const maxHeight = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--ec-input-max-height'), 10) || 120;
  els.input.style.height = Math.min(els.input.scrollHeight, maxHeight) + 'px';
}

function wireEvents() {
  els.composer.addEventListener('submit', (e) => { e.preventDefault(); sendMessage(els.input.value); });
  els.input.addEventListener('input', autoGrow);
  els.continue.addEventListener('click', () => sendMessage('continue'));
  els.stop.addEventListener('click', () => activeController?.abort());
  els.input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      if (window.matchMedia?.('(pointer: coarse)').matches) return;
      e.preventDefault();
      sendMessage(els.input.value);
    }
  });
  els.close.addEventListener('click', () => toParent('close'));
  wireChatSurfaceComposer(els, { isBusy: () => sending });
}

// ── Init ────────────────────────────────────────────────────────────────────
function init() {
  installChatSurfaceIcons(els, CFG.agent_icon || 'bot');
  applyTheme();
  wireEvents();
  toParent('ready');

  if (!BOOT.embeddable) {
    els.composer.hidden = true;
    els.messages.hidden = true;
    els.unavailable.hidden = false;
    els.unavailableText.textContent = !BOOT.enabled
      ? 'This chat widget has not been enabled by its owner.'
      : 'This agent is not open to anonymous visitors.';
    return;
  }
  if (CFG.greeting) addBubble('agent', CFG.greeting);
  els.input.focus();
}

init();
