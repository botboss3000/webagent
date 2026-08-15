'use strict';

// ── DB-tail reconcile (the durable "always-loaded" path) ────────────────────
// The chat WebSocket is the *smooth* real-time renderer, but it only works when
// the browser's socket and the running agent live in the SAME server process's
// memory. It also only carries turns this browser KNOWS are happening. Neither
// is true when a run STARTS somewhere else and writes into the session you're
// already looking at — most commonly an AUTOMATION / scheduled task / event
// trigger delivering "here", but also a message sent from another device, or a
// cross-worker (`gunicorn --workers N`) run. In all those cases the agent still
// streams its reply into the shared DB (assistant row `status='streaming'`,
// updated ~0.6s, and registered as the run's `assistant_interaction_id`).
//
// This module closes the gap by polling a cheap DB-tail endpoint for the OPEN
// session and rendering new/updated rows through the SAME bubble helpers the WS
// path uses. It runs at two cadences off one timer:
//   • a turn this browser is tracking (`isProcessing`) → poll every tick (fast).
//   • otherwise → poll slowly, just to DETECT a run that began elsewhere and
//     then light the live indicator + escalate to the fast cadence.
// While idle, a WebSocket-silence gate keeps this poll dormant on the happy
// (same-process) path. During an active turn we still poll: WS events provide
// smooth text, but only DB rows provide the durable interaction sequence. The
// time a session is open — it never stops itself when a turn finishes — so the
// NEXT externally-started run is still picked up without a manual refresh.

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { isDebugMode } from '../../shared/js/app-prompts.js';
import { addChatBubble, _setBubbleCreatedAt, _setBubbleSessionSeq } from './chat-bubble.js';
import { _setBubbleModelFromMeta, _addBubbleActions } from './chat-bubble-actions.js';
import { _cacheAppendMessage } from './chat-message-cache.js';
import {
  _stripToolCalls,
  seedStreamingBubble,
  finalizeAgentResponse,
  finalizeAgentStep,
  markAgentInterrupted,
  ensureStreamingBubbleForActiveTurn,
  _findAgentBubbleForTurn,
  _wsTurnBuffers,
} from './chat-stream.js';

const RECONCILE_INTERVAL_MS = 800;   // base timer cadence (the fast/active rate)
const WS_SILENCE_MS = 1200;          // only poll once the WS has been quiet this long
const IDLE_POLL_EVERY = 4;           // when idle, only fetch every Nth tick (~3.2s)

let _timer = null;
let _idleTicks = 0;

function startReconcileLoop() {
  if (_timer) return;
  _idleTicks = 0;
  _timer = setInterval(_tick, RECONCILE_INTERVAL_MS);
}

function stopReconcileLoop() {
  if (_timer) {
    clearInterval(_timer);
    _timer = null;
  }
}

function _tick() {
  const sid = app.currentSessionId;
  if (!sid) return;
  const last = (app._lastWsEventAt && app._lastWsEventAt[sid]) || 0;
  if (app.isProcessing) {
    _idleTicks = 0;
    // Rendering is idempotent by interaction id. Polling alongside WS is how
    // live bubbles promptly acquire their authoritative interaction sequence.
    _reconcileOnce(sid);
    return;
  }
  if (Date.now() - last < WS_SILENCE_MS) return;       // WS is delivering → stay dormant

  // Idle: poll on a slower cadence purely to DETECT a run that began elsewhere
  // (an automation, a cross-device message). Cheap query — usually returns
  // nothing — but it's what keeps the open session "always updated".
  _idleTicks += 1;
  if (_idleTicks < IDLE_POLL_EVERY) return;
  _idleTicks = 0;
  _reconcileOnce(sid);
}

// Cheap fingerprint of an assistant row so an idle poll doesn't repaint a row
// it already drew. The server unions the in-progress assistant row — and, since
// run-state keeps `assistant_interaction_id` after the run ends, the COMPLETED
// row too — into every tail, so without this guard a finished bubble would be
// re-finalized on every idle tick.
function _renderSig(msg) {
  return (msg.status || '') + ':' + ((msg.content || '').length);
}

async function _reconcileOnce(sid) {
  if (app._reconcileInFlight) return;                  // never overlap fetches
  app._reconcileInFlight = true;
  const wasProcessing = app.isProcessing;
  try {
    // Do not use the WebSocket replay cursor here. session_seq is allocated to
    // every stream/pipeline event, but this endpoint returns interaction rows.
    // Sharing the cursor lets a later non-row event skip an earlier row forever.
    const after = (app.lastInteractionSeq && app.lastInteractionSeq[sid]) || 0;
    window.__chatPollLastAt = new Date().toISOString();

    const token = localStorage.getItem('auth_token');
    let url = apiPath(
      `/api/v1/db/session-tail?db=user.db&session_id=${encodeURIComponent(sid)}`
      + `&after_session_seq=${after}`
      + (app.currentUserId ? `&user_id=${encodeURIComponent(app.currentUserId)}` : '')
      + (token ? `&token=${encodeURIComponent(token)}` : ''),
    );
    let data;
    try {
      const res = await fetch(url);
      if (!res.ok) return;
      data = await res.json();
    } catch (_) {
      return;                                          // network blip — try again next tick
    }
    if (!data || data.restricted) return;
    if (sid !== app.currentSessionId) return;          // user switched away mid-fetch

    if (!app._reconcileSig) app._reconcileSig = {};

    let maxSeq = after;
    const msgs = Array.isArray(data.messages) ? data.messages : [];
    for (const msg of msgs) {
      if (typeof msg.session_seq === 'number') maxSeq = Math.max(maxSeq, msg.session_seq);
      _cacheAppendMessage(sid, msg);

      if (msg.role === 'user') {
        _renderUserRow(msg);
        continue;
      }
      if (msg.role === 'system') {
        // Debug-only system messages are hidden when debug mode is off.
        const src = (msg.source || '');
        if (src.startsWith('system:debug:') && !isDebugMode()) continue;
        const key = msg.id;
        const sigKey = sid + '|' + key;
        const sig = _renderSig(msg);
        if (msg.status !== 'streaming' && app._reconcileSig[sigKey] === sig) continue;
        app._reconcileSig[sigKey] = sig;
        _renderSystemRow(msg);
        continue;
      }
      if (msg.role !== 'assistant') continue;          // tool rows are handled elsewhere

      const key = msg.id;
      const existingBubble = _findAgentBubbleForTurn(key);
      if (existingBubble) _setBubbleCreatedAt(existingBubble, msg.created_at);
      // Skip a re-render of an assistant row whose content/status is unchanged
      // since we last drew it (streaming always re-renders — its text grows).
      const sigKey = sid + '|' + key;
      const sig = _renderSig(msg);
      if (msg.status !== 'streaming' && app._reconcileSig[sigKey] === sig) continue;
      app._reconcileSig[sigKey] = sig;

      const text = _stripToolCalls(msg.content || '');
      if (msg.status === 'streaming') {
        // SET (not append) absolute content keyed by asst_id → idempotent, and
        // keep the WS appender's buffer in sync so it can resume cleanly later.
        seedStreamingBubble(key, text, msg.created_at);
        try { _wsTurnBuffers.set(key, text); } catch (_) { /* best-effort */ }
      } else if (msg.status === 'interrupted') {
        markAgentInterrupted(
          key, msg.created_at, msg.interaction_seq ?? msg.session_seq, msg.turn_id,
        );
      } else if (msg.status === 'error') {
        markAgentInterrupted(
          key, msg.created_at, msg.interaction_seq ?? msg.session_seq,
          msg.turn_id, 'Error',
        );
        continue;
        finalizeAgentResponse(text, key);
        const b = _findAgentBubbleForTurn(key);
        if (b) {
          b.classList.add('error');
          if (msg.metadata) _setBubbleModelFromMeta(b, msg.metadata);
        }
      } else {
        // 'complete' (or legacy null status) — finalize this step's bubble.
        let phase = String(msg.message_phase || '').toLowerCase();
        if (!phase && msg.metadata) {
          try { phase = String(JSON.parse(msg.metadata).message_phase || '').toLowerCase(); }
          catch (_) {}
        }
        if (phase === 'main' || phase === 'final') {
          finalizeAgentResponse(
            text, key, false, msg.created_at, msg.turn_id,
            msg.interaction_seq ?? msg.session_seq,
          );
        } else {
          finalizeAgentStep(
            text, key, msg.created_at, msg.turn_id,
            msg.interaction_seq ?? msg.session_seq,
          );
        }
        const b = _findAgentBubbleForTurn(key);
        if (b && msg.metadata) _setBubbleModelFromMeta(b, msg.metadata);
      }
      const renderedBubble = _findAgentBubbleForTurn(key);
      if (renderedBubble) {
        _setBubbleCreatedAt(renderedBubble, msg.created_at);
        _setBubbleSessionSeq(renderedBubble, msg.session_seq);
      }
    }

    // Re-project persisted assistant/tool rows through the turn-owned activity
    // renderer. Entry and tool-call ids make this idempotent with the WS path.
    if (msgs.length && typeof app._appendMessagesToTranscript === 'function') {
      app._appendMessagesToTranscript(msgs, true);
    }
    const run = data.run || null;

    // Make sure a placeholder exists for an in-flight turn even if no row text yet.
    if (run && run.active && run.assistant_interaction_id) {
      const activeRow = msgs.find(m => m.id === run.assistant_interaction_id);
      ensureStreamingBubbleForActiveTurn(
        run.assistant_interaction_id,
        activeRow && activeRow.created_at,
      );
    }

    // Advance the durable-row cursor only after the rows were consumed.
    if (!app.lastInteractionSeq) app.lastInteractionSeq = {};
    app.lastInteractionSeq[sid] = Math.max(app.lastInteractionSeq[sid] || 0, maxSeq);

    // The event cursor remains independent and is used only by WS replay.
    if (!app.lastSessionSeq) app.lastSessionSeq = {};
    const floor = run && typeof run.latest_session_seq === 'number' ? run.latest_session_seq : 0;
    app.lastSessionSeq[sid] = Math.max(app.lastSessionSeq[sid] || 0, floor);
    try {
      localStorage.setItem('webagent.lastSessionSeq.v1', JSON.stringify(app.lastSessionSeq));
    } catch (_) { /* quota / private mode — non-fatal */ }

    const runActive = !!(run && run.active);

    // A run we did NOT start (an automation, a cross-device message) just became
    // visible on the open session — light the live "thinking" indicator so the
    // user sees it working. ensureStreamingBubbleForActiveTurn / seedStreamingBubble
    // already flip app.isProcessing to true, which escalates us to the fast cadence.
    if (runActive && !wasProcessing && typeof app.chatActivityRestore === 'function') {
      try { app.chatActivityRestore(run.current_op || null); } catch (_) {}
    }

    // Turn finished — clear the working state, but KEEP the loop alive so the
    // next externally-started run on this open session is still picked up live
    // (the whole point: no manual refresh). Only act on the active→idle edge.
    if (run && run.active === false && wasProcessing) {
      app.isProcessing = false;
      if (app.chatActivityStop) { try { app.chatActivityStop(); } catch (_) {} }
      if (app.chatSend) app.chatSend.disabled = !(app.chatInput && app.chatInput.value.trim());
      if (app._healingTimer) { clearTimeout(app._healingTimer); app._healingTimer = null; }
    }
  } finally {
    app._reconcileInFlight = false;
  }
}

// Extract the friendly label for a GenUI-originated user row (page field/button
// sends). Returns null for normal user messages. Handles metadata as either a
// JSON string (DB endpoints) or an object (live WS path).
function _genuiLabelFromMeta(metaRaw) {
  if (!metaRaw) return null;
  let m = metaRaw;
  if (typeof m === 'string') { try { m = JSON.parse(m); } catch (_) { return null; } }
  if (!m || typeof m !== 'object' || !m.genui) return null;
  return String(m.genui_label || '').trim() || null;
}

// Render a user row the same way agentWs.js does: dedup by interaction id, and
// adopt an existing optimistic bubble (sent locally before its id was known).
function _renderUserRow(msg) {
  if (!app.chatMessages) return;
  const mid = msg.id || '';
  const cont = msg.content || '';
  // ── GenUI-originated page sends (field/button prompts) ──
  // Render the page's friendly label as a green notice instead of a "You"
  // bubble; the raw prompt only surfaces under the debug toggle.
  const genuiLabel = _genuiLabelFromMeta(msg.metadata);
  if (genuiLabel) {
    if (mid && app.chatMessages.querySelector(
          `.chat-bubble.info.system-genui[data-msg-id="${CSS.escape(String(mid))}"]`)) {
      return;
    }
    // Adopt the sender's own optimistic label notice (rendered locally before
    // the id was known) by matching text on an untagged one.
    if (mid) {
      const cands = app.chatMessages.querySelectorAll('.chat-bubble.info.system-genui:not([data-msg-id])');
      for (let i = cands.length - 1; i >= 0; i--) {
        const b = cands[i];
        const t = (b.querySelector('.bubble-body')?.textContent || '').trim();
        if (t === genuiLabel) {
          b.setAttribute('data-msg-id', String(mid));
          _setBubbleCreatedAt(b, msg.created_at);
          _setBubbleSessionSeq(b, msg.session_seq);
          return;
        }
      }
    }
    const gb = (typeof app.addChatBubble === 'function')
      ? app.addChatBubble('info', genuiLabel, 'system-genui', undefined, undefined, mid || undefined, msg.created_at)
      : addChatBubble('info', genuiLabel, 'system-genui', undefined, undefined, mid || undefined, msg.created_at);
    if (isDebugMode() && (cont || '').trim()) {
      const dbg = (typeof app.addChatBubble === 'function')
        ? app.addChatBubble('info', 'Raw prompt:\n' + cont, 'system-debug', undefined, undefined, undefined, msg.created_at)
        : addChatBubble('info', 'Raw prompt:\n' + cont, 'system-debug', undefined, undefined, undefined, msg.created_at);
      if (mid) dbg.setAttribute('data-debug-for', String(mid));
    }
    _setBubbleSessionSeq(gb, msg.session_seq);
    return;
  }
  if (mid) {
    const rendered = app.chatMessages.querySelector(
      `.chat-bubble.user[data-msg-id="${CSS.escape(String(mid))}"]`,
    );
    if (rendered) {
      _setBubbleCreatedAt(rendered, msg.created_at);
      _setBubbleSessionSeq(rendered, msg.session_seq);
      return;
    }
  }
  if (mid) {
    const candidates = app.chatMessages.querySelectorAll('.chat-bubble.user:not([data-msg-id])');
    for (let i = candidates.length - 1; i >= 0; i--) {
      const b = candidates[i];
      const t = (b.querySelector('.bubble-body')?.textContent || '').trim();
      if (t === cont.trim()) {
        b.setAttribute('data-msg-id', String(mid));
        _setBubbleCreatedAt(b, msg.created_at);
        _setBubbleSessionSeq(b, msg.session_seq);
        return;
      }
    }
  }
  const bubble = (typeof app.addChatBubble === 'function')
    ? app.addChatBubble('user', cont, undefined, undefined, undefined, mid || undefined, msg.created_at)
    : addChatBubble('user', cont, undefined, undefined, undefined, mid || undefined, msg.created_at);
  // addChatBubble doesn't build a footer; a user message recovered from the DB
  // (cross-device send, automation echo) should still get one like every other.
  if (bubble && bubble.nodeType === 1) _addBubbleActions(bubble);
  _setBubbleSessionSeq(bubble, msg.session_seq);
}

// Render a system message as a muted info bubble — visible to the user, never
// fed to the agent. Same idempotent dedup as user rows.
function _renderSystemRow(msg) {
  if (!app.chatMessages) return;
  const mid = msg.id || '';
  const cont = msg.content || '';
  const src = msg.source || '';
  const extraClass = src.startsWith('system:debug:') ? 'system-debug'
    : src === 'system:error' ? 'system-error'
    : src === 'system:retry' ? 'system-retry'
    : undefined;
  if (mid) {
    const rendered = app.chatMessages.querySelector(
      `.chat-bubble.info[data-msg-id="${CSS.escape(String(mid))}"]`,
    );
    if (rendered) {
      _setBubbleCreatedAt(rendered, msg.created_at);
      _setBubbleSessionSeq(rendered, msg.session_seq);
      return;
    }
  }
  const bubble = (typeof app.addChatBubble === 'function')
    ? app.addChatBubble('info', cont, extraClass, undefined, undefined, mid || undefined, msg.created_at)
    : addChatBubble('info', cont, extraClass, undefined, undefined, mid || undefined, msg.created_at);
  _setBubbleSessionSeq(bubble, msg.session_seq);
}

app.startReconcileLoop = startReconcileLoop;
app.stopReconcileLoop = stopReconcileLoop;

// Pre-seed the reconcile signature cache with already-rendered assistant rows
// so the first reconcile tick doesn't destroy-and-rebuild every bubble.
// Called from session-load.js after _renderSessionWindowed.
function seedReconcileSig(sid, messages) {
  if (!sid || !Array.isArray(messages)) return;
  if (!app._reconcileSig) app._reconcileSig = {};
  for (const msg of messages) {
    if ((msg.role !== 'assistant' && msg.role !== 'system') || !msg.id) continue;
    const key = sid + '|' + msg.id;
    app._reconcileSig[key] = _renderSig(msg);
  }
}
app.seedReconcileSig = seedReconcileSig;

export { startReconcileLoop, seedReconcileSig };
