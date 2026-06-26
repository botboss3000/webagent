'use strict';

/**
 * Page-assistant context engine — the shared brain behind the per-page "advanced
 * chat pill" assistants (App Settings, Agent Settings, …).
 *
 * A page-assistant pill floats at the bottom of an admin config page and hands the
 * user's request to webAgent the MANAGER. Two things make it "context-aware":
 *   • the pill's PLACEHOLDER changes to match the section the mouse is over
 *     (each section carries `data-pa-area="<key>"`), animated like a typewriter
 *     through a few LLM-generated example requests for that section; and
 *   • on send the user's words are WRAPPED with that page's `intro` + the hovered
 *     area's `prompt` before being handed to the agent.
 * All the text (placeholders, idea hints, prompts) lives in
 * app/defaults/app-prompts.json → page_assistants.<page> (served by
 * GET /api/v1/app-prompts; rotating ideas via POST /app-prompts/page-suggestions).
 *
 * This module is a per-instance FACTORY (not module singletons) because each
 * consumer needs its own state bound to its OWN input element — App Settings'
 * input has a fixed id, but Agent Settings' pill (buildAbilitySearchPill) is built
 * in JS and its input has no stable id. So createPageAssistant() takes the input
 * ELEMENT directly and returns a controller.
 *
 * Two pill shapes, one engine:
 *   • COMPOSER mode (`wireComposer: true`) — a static HTML pill (App Settings'
 *     #ac-pa-bar-row). The engine owns send/Enter/Tab/voice/uploads and calls the
 *     injected `onSend({message, attachmentIds, title})`.
 *   • ATTACHED mode (`wireComposer: false`) — the JS-built buildAbilitySearchPill
 *     (Agent Settings), which already wires its own send/voice/uploads/filter. The
 *     engine owns only config-load + placeholder swap + suggestions/typewriter +
 *     Tab-to-fill, and exposes buildMessage()/currentArea()/title() so the caller's
 *     own onChatSend can assemble + send the context-aware message.
 *
 * Breadcrumbs: callers ui/admin-tools/app-config/app-settings/app-settings.js and
 * ui/admin-tools/app-config/agent-settings/agent-settings.js; the chat-widget
 * spawn lives in ui/chat-widget/js/chat-widget.js (spawnWebagentPageChat); the pill
 * geometry/skin in ui/shared/css/app1.css + index.css.
 */

import { apiPath } from '../../shared/js/config.js';
import { app } from '../../shared/js/state.js';
import { _fetch } from './utils.js';
import { wireChatPillUploads, startSpeechDictation } from '../../shared/js/attachments.js';

const _PA_TYPE_MS = 42;     // per-character type speed (a little jitter added)
const _PA_DELETE_MS = 18;   // per-character delete speed (snappier than typing)
const _PA_HOLD_MS = 2800;   // how long a fully-typed idea stays before deleting
const _PA_GAP_MS = 380;     // blank beat between deleting one idea and typing next

/**
 * @param {object} opts
 * @param {string}      opts.page          - app-prompts page key ('app_settings' | 'agent_settings')
 * @param {HTMLElement} opts.section       - section element the mouseover listener is bound to
 * @param {HTMLElement} opts.input         - the pill's <textarea> element
 * @param {boolean}     [opts.wireComposer=true] - own the whole composer (static-HTML pill)
 *
 *  Composer-mode only:
 * @param {HTMLElement} [opts.send]        - send button
 * @param {HTMLElement} [opts.voice]       - voice button
 * @param {HTMLElement} [opts.row]         - the .chat-pill row (gets .has-text)
 * @param {HTMLElement} [opts.previewBar]  - attachment preview bar
 * @param {Array}       [opts.pending]     - pending-attachments array
 * @param {function}    [opts.onSend]      - ({message, attachmentIds, title}) => void
 *
 * @returns {{init, buildMessage, currentArea, currentSuggestion, title,
 *            updatePlaceholder, startTyper, resetTyper}}
 */
export function createPageAssistant(opts = {}) {
  const {
    page,
    section,
    input,
    wireComposer = true,
    send = null,
    voice = null,
    row = null,
    previewBar = null,
    pending = [],
    onSend = null,
  } = opts;

  let config = null;     // { title, default_placeholder, intro, areas:{key:{label,placeholder,suggest,prompt}} }
  let area = null;       // key of the area the mouse last hovered (drives placeholder + prompt)
  let wired = false;
  const suggestCache = {}; // area key -> { items:[str], idx, loading, done }
  // Typewriter engine: one self-scheduling timer reads `area` fresh each tick so it
  // follows the hovered section; pauses (no churn) while the user is composing.
  const typer = { timer: null, area: null, idx: 0, pos: 0, phase: 'type' };

  // ── Config ────────────────────────────────────────────────────────────────
  async function loadConfig() {
    try {
      const res = await _fetch(apiPath('/api/v1/app-prompts'));
      if (res.ok) {
        const data = await res.json();
        config = (data.page_assistants || {})[page] || null;
      }
    } catch (_) { /* keep null — fall back to the static markup placeholder */ }
    updatePlaceholder();
  }

  function title() {
    return (config && config.title) || 'webAgent';
  }

  // ── Suggestions (rotating example requests per area) ────────────────────────
  // The FULL example request currently being typed/held for the hovered area (or
  // null when none have loaded). Tab-to-fill uses this, so it always inserts the
  // complete line even mid-type.
  function currentSuggestion() {
    const entry = area && suggestCache[area];
    if (entry && entry.items && entry.items.length) {
      const i = (typer.area === area) ? typer.idx : 0;
      return entry.items[i % entry.items.length];
    }
    return null;
  }

  // Static fallback placeholder (the area's `placeholder` hint). The typewriter
  // owns the placeholder once generated ideas exist; this shows before they load
  // or if generation failed.
  function updatePlaceholder() {
    if (!input) return;
    const areas = (config && config.areas) || {};
    const a = area && areas[area];
    input.placeholder = (a && a.placeholder)
      || (config && config.default_placeholder)
      || 'Tell webAgent what to change on this page…';
  }

  // Ask the backend for a few example requests for one area. Cached per area, so
  // each section costs at most one LLM call per page visit. Best-effort.
  async function fetchSuggestions(a) {
    if (!a || !app.currentUserId) return;   // admin-only page; no token → skip
    let entry = suggestCache[a];
    if (entry && (entry.loading || entry.done)) return;  // in flight / already done
    entry = suggestCache[a] = { items: [], idx: 0, loading: true, done: false };
    try {
      const res = await _fetch(apiPath('/api/v1/app-prompts/page-suggestions'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ page, area: a, count: 5 }),
      });
      if (res.ok) {
        const data = await res.json();
        entry.items = Array.isArray(data.suggestions)
          ? data.suggestions.filter(s => typeof s === 'string' && s.trim())
          : [];
      }
    } catch (_) { /* keep empty — static placeholder stays */ }
    entry.loading = false;
    entry.done = true;
    if (area === a) updatePlaceholder();
  }

  // ── Typewriter ──────────────────────────────────────────────────────────────
  function startTyper() {
    if (typer.timer) return;
    typerTick();
  }

  function resetTyper() {
    if (typer.timer) { clearTimeout(typer.timer); typer.timer = null; }
    typer.area = null; typer.idx = 0; typer.pos = 0; typer.phase = 'type';
  }

  function typerTick() {
    if (!input) { typer.timer = null; return; }
    const entry = area && suggestCache[area];
    const items = entry && entry.items;

    // No generated ideas yet (loading / failed) → keep the static hint, re-check.
    if (!items || !items.length) {
      updatePlaceholder();
      typer.area = null;
      typer.timer = setTimeout(typerTick, 500);
      return;
    }
    // User is composing → placeholder is hidden; don't churn, just wait.
    if (input.value.trim()) {
      typer.timer = setTimeout(typerTick, 700);
      return;
    }
    // Area changed → restart from the new area's first idea.
    if (typer.area !== area) {
      typer.area = area; typer.idx = 0; typer.pos = 0; typer.phase = 'type';
    }

    const target = items[typer.idx % items.length] || '';
    let delay;
    if (typer.phase === 'type') {
      if (typer.pos < target.length) {
        typer.pos++;
        delay = _PA_TYPE_MS + Math.random() * 45;   // human-ish jitter
      } else {
        typer.phase = 'hold';
        delay = _PA_HOLD_MS;
      }
    } else if (typer.phase === 'hold') {
      typer.phase = 'delete';
      delay = _PA_DELETE_MS;
    } else { // 'delete'
      if (typer.pos > 0) {
        typer.pos--;
        delay = _PA_DELETE_MS;
      } else {
        typer.idx = (typer.idx + 1) % items.length;  // advance to next idea
        typer.phase = 'type';
        delay = _PA_GAP_MS;
      }
    }

    input.placeholder = target.slice(0, typer.pos);
    typer.timer = setTimeout(typerTick, delay);
  }

  // ── Message assembly ────────────────────────────────────────────────────────
  // page context (intro) + the hovered area's prompt + the user's own words.
  function buildMessage(text) {
    const cfg = config || {};
    const a = (cfg.areas || {})[area] || null;
    const parts = [];
    if (cfg.intro) parts.push(cfg.intro);
    if (a && a.prompt) {
      parts.push((a.label ? `[Area: ${a.label}]\n` : '') + a.prompt);
    }
    parts.push('The admin asked:\n' + (text || ''));
    return parts.join('\n\n');
  }

  function currentArea() { return area; }

  // ── Composer-mode helpers (static-HTML pill only) ───────────────────────────
  function clearPending() {
    pending.length = 0;
    if (previewBar) { previewBar.innerHTML = ''; previewBar.style.display = 'none'; }
  }

  async function composerSend() {
    if (!input) return;
    const text = (input.value || '').trim();
    if (!text && pending.length === 0) return;
    if (!app.currentUserId) return;   // admin-only page; no session → no-op

    const message = buildMessage(text);
    const attachmentIds = pending.map(a => a.attachment_id).filter(Boolean);
    input.value = '';
    clearPending();
    if (send) send.disabled = true;

    try {
      if (typeof onSend === 'function') {
        await onSend({ message, attachmentIds, title: title() });
      }
    } catch (e) {
      console.warn('page-assistant: send failed', e);
    }
  }

  // ── Wiring ──────────────────────────────────────────────────────────────────
  // Tab-to-fill is wired in BOTH modes (the attached pill's own keydown only
  // handles Enter→send, so Tab is ours regardless).
  function wireTabToFill() {
    if (!input) return;
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Tab' && !e.shiftKey && !input.value.trim()) {
        const s = currentSuggestion();
        if (s) {
          e.preventDefault();
          input.value = s;
          // Fire `input` so whichever pill owns this field updates its armed state
          // (composer mode → our sync(); attached pill → its own updateArmed/filter).
          input.dispatchEvent(new Event('input', { bubbles: true }));
        }
      }
    });
  }

  // Composer mode: own send button / Enter / voice / uploads + the has-text swap.
  function sync() {
    if (!input) return;
    const has = !!input.value.trim() || pending.length > 0;
    if (row) row.classList.toggle('has-text', has);
    if (send) send.disabled = !has;
  }

  function wireComposerControls() {
    if (send) send.addEventListener('click', () => { composerSend(); });
    if (input) {
      input.addEventListener('input', sync);
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); composerSend(); }
      });
    }
    if (voice) voice.addEventListener('click', () => startSpeechDictation(voice, input));
    if (previewBar) {
      wireChatPillUploads(row, input, {
        previewBar,
        pending,
        onChange: (p) => {
          previewBar.style.display = p.length > 0 ? 'flex' : 'none';
          sync();
        },
      });
    }
    sync();
  }

  // Placeholder follows the mouse: closest('[data-pa-area]') walks the DOM tree,
  // so it resolves even though the section's groups are display:contents under the
  // sticky-nav. Hovering the pill / gaps returns null → keep the last area.
  function wireMouseover() {
    if (!section) return;
    section.addEventListener('mouseover', (e) => {
      const hit = e.target.closest('[data-pa-area]');
      if (!hit) return;
      const a = hit.dataset.paArea;
      if (a && a !== area) {
        area = a;
        updatePlaceholder();   // show the static hint immediately
        fetchSuggestions(a);   // lazily generate ideas for this area (cached)
        startTyper();          // typewriter takes over once ideas arrive
      }
    });
  }

  function init() {
    if (wired) return;
    if (!input) return;
    wired = true;
    wireTabToFill();
    if (wireComposer) wireComposerControls();
    wireMouseover();
    loadConfig();
  }

  return {
    init,
    buildMessage,
    currentArea,
    currentSuggestion,
    title,
    updatePlaceholder,
    startTyper,
    resetTyper,
  };
}
