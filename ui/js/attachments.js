'use strict';

/**
 * Attachments module — file picker, voice recording, drag & drop,
 * preview chips, and inline rendering in chat bubbles.
 *
 * Depends on the global `app` object from state.js.
 * Must be loaded after state.js and chat.js.
 */

import { app } from './state.js';
import { apiPath } from './config.js';
import { icon } from './icons.js';
import { putAttachment, getObjectUrl, deleteAttachment as idbDelete } from './attachments-idb.js';

// Active server-side attachment backend. Synced opportunistically by the
// Chat Attachments card's bootstrap fetch (see ui/js/data-management.js).
// We also do a best-effort fetch the first time an upload happens so this
// works even before the user opens App Configuration → Data Management.
let _backendCache = null;
let _backendFetched = false;

async function _resolveBackend() {
  if (_backendCache) return _backendCache;
  try {
    if (window.__webagentAttachmentBackend) {
      _backendCache = window.__webagentAttachmentBackend;
      return _backendCache;
    }
  } catch {}
  if (_backendFetched) return 'local';
  _backendFetched = true;
  try {
    const uid = (typeof localStorage !== 'undefined' && localStorage.getItem('auth_user_id')) || '';
    const res = await fetch(apiPath('/admin/storage/attachments/status?requesting_user_id=' + encodeURIComponent(uid)));
    if (res.ok) {
      const data = await res.json();
      _backendCache = data.mode || 'local';
      try { window.__webagentAttachmentBackend = _backendCache; } catch {}
    }
  } catch {
    // Non-admin or unauthenticated — the endpoint will 403/401. Treat the
    // default as 'local' (the server still owns the actual decision).
  }
  return _backendCache || 'local';
}

// ── State ──────────────────────────────────────────────────────────────────

/** Attachments ready to send with next message */
const pendingAttachments = [];

/** Currently streaming attachment metadata (for rendering in agent bubbles) */
let streamAttachments = [];

// ── Init ───────────────────────────────────────────────────────────────────

export function initAttachments() {
  const attachBtn = document.getElementById('chat-attach-btn');
  const fileInput = document.getElementById('chat-file-input');
  const voiceBtn = document.getElementById('chat-voice-btn');
  const previewBar = document.getElementById('chat-preview-bar');
  const inputArea = document.getElementById('chat-input-area');

  if (!attachBtn || !fileInput) return;

  // File picker click
  attachBtn.addEventListener('click', () => fileInput.click());

  // File selected
  fileInput.addEventListener('change', async () => {
    const files = Array.from(fileInput.files);
    fileInput.value = ''; // reset so same file can be re-selected
    for (const file of files) {
      await uploadAndPreview(file);
    }
  });

  // Voice dictation: browser Web Speech API transcribes the user's voice
  // straight into the chat-input textarea. Falls back to .no-voice when the
  // browser doesn't expose SpeechRecognition at all (Firefox desktop, some
  // mobile browsers). See startSpeechDictation() below for details.
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (voiceBtn && SR) {
    voiceBtn.addEventListener('click', () => startSpeechDictation(voiceBtn));
  } else if (voiceBtn) {
    const row = document.getElementById('chat-input-row');
    if (row) row.classList.add('no-voice');
  }

  // Main chat pill: paste + drop scoped to the chat field (composer pill).
  // Other pills (Pages, Agents, admin) wire themselves via wireChatPillUploads.
  wireChatPillUploads(
    document.getElementById('chat-input-row'),
    document.getElementById('chat-input'),
  );

  // Expose attachment_ids to chat send flow
  app.getPendingAttachmentIds = () => pendingAttachments.map(a => a.attachment_id);
  app.clearPendingAttachments = clearPendingAttachments;

  // Override send flow to include attachment_ids
  patchSendMessage();
}

// ── Upload ─────────────────────────────────────────────────────────────────

export async function uploadAndPreview(file, opts = {}) {
  if (!file) return;

  const maxSize = 25 * 1024 * 1024; // 25MB
  if (file.size > maxSize) {
    alert(`File too large: ${file.name} (${(file.size / 1024 / 1024).toFixed(1)}MB). Max 25MB.`);
    return;
  }

  const previewBar = opts.previewBar || document.getElementById('chat-preview-bar');
  const targetPending = opts.pending || pendingAttachments;
  const onChange = opts.onChange;
  if (!previewBar) return;
  const chip = document.createElement('span');
  chip.className = 'chat-attachment-pill uploading';
  chip.innerHTML = `${icon('upload', { size: '12px' })} ${file.name}`;
  previewBar.appendChild(chip);
  previewBar.style.display = 'flex';

  try {
    const backend = await _resolveBackend();
    let data;
    if (backend === 'browser') {
      // Metadata-only POST; bytes stay in the user's IndexedDB.
      const fd = new FormData();
      fd.append('user_id', app.currentUserId);
      fd.append('session_id', app.currentSessionId);
      fd.append('original_name', file.name);
      fd.append('mime_type', file.type || 'application/octet-stream');
      fd.append('size_bytes', String(file.size));
      const r1 = await fetch(apiPath('/api/v1/upload'), { method: 'POST', body: fd });
      if (!r1.ok) {
        const err = await r1.json().catch(() => ({ detail: r1.statusText }));
        throw new Error(err.detail || 'Upload (metadata) failed');
      }
      data = await r1.json();
      // Stash the bytes locally under the assigned attachment_id.
      await putAttachment({
        id: data.attachment_id,
        blob: file,
        mime_type: data.mime_type,
        name: data.original_name,
        size: data.size_bytes,
      });
      // Mint a local object URL so the preview chip + chat bubble can render.
      data.url = await getObjectUrl(data.attachment_id) || '';
    } else {
      const formData = new FormData();
      formData.append('file', file);
      formData.append('user_id', app.currentUserId);
      formData.append('session_id', app.currentSessionId);

      const res = await fetch(apiPath('/api/v1/upload'), {
        method: 'POST',
        body: formData,
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || 'Upload failed');
      }

      data = await res.json();
    }

    // Replace chip with success preview
    chip.className = 'chat-attachment-pill';
    chip.innerHTML = '';

    // Thumbnail for images
    if (data.mime_type.startsWith('image/')) {
      const img = document.createElement('img');
      img.src = data.url;
      img.alt = data.original_name;
      img.className = 'chat-attachment-thumb';
      chip.appendChild(img);
    } else if (data.mime_type.startsWith('audio/')) {
      chip.innerHTML = `${icon('mic', { size: '12px' })} ${data.original_name}`;
    } else {
      chip.innerHTML = `${icon('paperclip', { size: '12px' })} ${data.original_name}`;
    }

    // Remove button
    const removeBtn = document.createElement('button');
    removeBtn.className = 'chat-attachment-remove';
    removeBtn.innerHTML = icon('x', { size: '11px' });
    removeBtn.title = 'Remove attachment';
    removeBtn.addEventListener('click', () => {
      chip.remove();
      const idx = targetPending.findIndex(a => a.attachment_id === data.attachment_id);
      if (idx >= 0) targetPending.splice(idx, 1);
      if (targetPending.length === 0) previewBar.style.display = 'none';
      if (onChange) onChange(targetPending);
      // If the bytes lived in IndexedDB, evict them along with the row.
      if (data.storage_provider === 'browser') {
        idbDelete(data.attachment_id).catch(() => {});
        fetch(apiPath('/api/v1/upload/' + data.attachment_id), { method: 'DELETE' }).catch(() => {});
      }
    });
    chip.appendChild(removeBtn);

    // Store
    targetPending.push(data);
    if (onChange) onChange(targetPending);
  } catch (err) {
    chip.className = 'chat-attachment-pill error';
    chip.textContent = `${file.name}: ${err.message}`;
    setTimeout(() => { chip.remove(); if (targetPending.length === 0) previewBar.style.display = 'none'; }, 3000);
  }
}

// ── Shared paste + drag/drop wiring for any chat pill ──────────────────────
// Drop targets the pill row (= the chat field). Paste targets the input.
// Caller can route uploads to a custom preview bar / pending list via opts.

function _dragHasFiles(e) {
  if (!e.dataTransfer) return false;
  const types = e.dataTransfer.types;
  if (!types) return false;
  for (let i = 0; i < types.length; i++) {
    if (types[i] === 'Files') return true;
  }
  return false;
}

export function wireChatPillUploads(rowEl, inputEl, opts = {}) {
  if (rowEl && !rowEl.dataset.chatPillUploadsWired) {
    rowEl.dataset.chatPillUploadsWired = '1';
    let counter = 0;
    rowEl.addEventListener('dragenter', (e) => {
      if (!_dragHasFiles(e)) return;
      e.preventDefault();
      e.stopPropagation();
      counter++;
      if (counter === 1) rowEl.classList.add('drag-over');
    });
    rowEl.addEventListener('dragover', (e) => {
      if (!_dragHasFiles(e)) return;
      e.preventDefault();
      e.stopPropagation();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
    });
    rowEl.addEventListener('dragleave', (e) => {
      if (!_dragHasFiles(e)) return;
      e.preventDefault();
      e.stopPropagation();
      counter--;
      if (counter <= 0) { counter = 0; rowEl.classList.remove('drag-over'); }
    });
    rowEl.addEventListener('drop', async (e) => {
      if (!_dragHasFiles(e)) return;
      e.preventDefault();
      e.stopPropagation();
      counter = 0;
      rowEl.classList.remove('drag-over');
      const files = Array.from(e.dataTransfer.files);
      for (const file of files) await uploadAndPreview(file, opts);
    });
  }

  if (inputEl && !inputEl.dataset.chatPillPasteWired) {
    inputEl.dataset.chatPillPasteWired = '1';
    inputEl.addEventListener('paste', async (e) => {
      const items = e.clipboardData && e.clipboardData.items;
      if (!items) return;

      // Phase 1: direct File items (Chrome/Edge/Safari)
      const files = [];
      for (const item of items) {
        if (item.kind === 'file') {
          const f = item.getAsFile();
          if (f) files.push(f);
        }
      }
      if (files.length > 0) {
        e.preventDefault();
        for (const file of files) await uploadAndPreview(file, opts);
        return;
      }

      // Phase 2: Firefox - images are in string items
      const hasImageType = Array.from(items).some(
        i => i.kind === 'string' && i.type.startsWith('image/')
      );
      const hasHtml = Array.from(items).some(
        i => i.kind === 'string' && i.type === 'text/html'
      );

      if (!hasImageType && !hasHtml) return;  // plain text paste

      // Prevent default so Firefox doesn't show its error message
      e.preventDefault();

      // Try async Clipboard API first (works in Firefox with permissions)
      try {
        const clipboardItems = await navigator.clipboard.read();
        const imageFiles = [];
        for (const ci of clipboardItems) {
          for (const type of ci.types) {
            if (type.startsWith('image/')) {
              const blob = await ci.getType(type);
              const ext = type.split('/')[1] || 'png';
              imageFiles.push(new File(
                [blob],
                'clipboard-' + Date.now() + '.' + ext,
                { type }
              ));
            }
          }
        }
        if (imageFiles.length > 0) {
          for (const file of imageFiles) await uploadAndPreview(file, opts);
          return;
        }
      } catch {
        // Clipboard API not available
      }

      // Fallback: extract data URL from HTML string items
      for (const item of items) {
        if (item.kind === 'string' && item.type === 'text/html') {
          item.getAsString(function(html) {
            var re = /src\s*=\s*"(data:image\/[^"]+)"/;
            var m = html.match(re);
            if (m && m[1]) {
              fetch(m[1]).then(function(r) { return r.blob(); }).then(function(blob) {
                var ext = blob.type.split('/')[1] || 'png';
                var f = new File([blob], 'clipboard-' + Date.now() + '.' + ext, { type: blob.type });
                uploadAndPreview(f, opts);
              });
            }
          });
        }
      }
    });
  }
}

function clearPendingAttachments() {
  pendingAttachments.length = 0;
  const previewBar = document.getElementById('chat-preview-bar');
  if (previewBar) {
    previewBar.innerHTML = '';
    previewBar.style.display = 'none';
  }
}

// ── Voice Dictation (Web Speech API) ───────────────────────────────────────
// The mic button drives the browser's built-in speech-to-text. We keep a
// single recognition instance per page; toggling the mic starts or stops it.
// Interim results stream into the textarea in real time so the user sees
// what's being transcribed; final results are committed on phrase boundaries.

let recognition = null;        // SpeechRecognition instance (kept across toggles)
let isDictating = false;
let dictationBaseText = '';    // textarea value when dictation started — final results append to this
let dictationInputEl = null;   // active textarea for the in-progress session

function _ensureRecognition() {
  if (recognition) return recognition;
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return null;
  recognition = new SR();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = (navigator.language || 'en-US');
  return recognition;
}

function _fireInput(el) {
  // Programmatic value changes don't dispatch 'input' on their own — fire it
  // so chat.js sees the new content (has-text toggle, send-button enable,
  // draft save, _autoResizePill via the delegated listener in chat.js).
  if (!el) return;
  el.dispatchEvent(new Event('input', { bubbles: true }));
}

function _stopDictation(btn) {
  if (!isDictating) return;
  try { recognition && recognition.stop(); } catch (_) { /* already stopped */ }
  isDictating = false;
  if (btn) {
    btn.innerHTML = icon('mic', { size: '18px' });
    btn.title = 'Voice dictation';
    btn.classList.remove('recording');
  }
  dictationInputEl = null;
}

function startSpeechDictation(btn) {
  const input = document.getElementById('chat-input');
  if (!input) return;

  // Second tap = stop.
  if (isDictating) { _stopDictation(btn); return; }

  const rec = _ensureRecognition();
  if (!rec) {
    alert('Voice dictation is not supported in this browser. Try Chrome, Edge, or Safari.');
    return;
  }

  dictationInputEl = input;
  // Snapshot the textarea so the user can keep typed text and we only append
  // newly-dictated phrases. Pad with a space if the prior content didn't end
  // in one, so "hello" + dictated "world" doesn't smash into "helloworld".
  dictationBaseText = input.value;
  if (dictationBaseText && !/\s$/.test(dictationBaseText)) dictationBaseText += ' ';

  rec.onresult = (event) => {
    if (!dictationInputEl) return;
    let finalText = '';
    let interimText = '';
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const r = event.results[i];
      if (r.isFinal) finalText += r[0].transcript;
      else           interimText += r[0].transcript;
    }
    if (finalText) {
      // Commit final phrase into the base so subsequent interim results don't
      // overwrite it. Trim leading whitespace the API sometimes prepends.
      dictationBaseText += finalText.replace(/^\s+/, '');
      if (!/\s$/.test(dictationBaseText)) dictationBaseText += ' ';
    }
    dictationInputEl.value = dictationBaseText + interimText;
    _fireInput(dictationInputEl);
  };
  rec.onerror = (e) => {
    // 'no-speech', 'aborted', 'audio-capture' — fall through to stop UI.
    if (e && e.error === 'not-allowed') {
      alert('Microphone access denied. Allow it in the browser settings to use voice dictation.');
    }
    _stopDictation(btn);
  };
  rec.onend = () => { _stopDictation(btn); };

  try {
    rec.start();
    isDictating = true;
    btn.innerHTML = icon('circle-stop', { size: '18px' });
    btn.title = 'Stop dictation';
    btn.classList.add('recording');
    input.focus();
  } catch (err) {
    // InvalidStateError — the previous session is still finalizing. Surface
    // it and reset so the next tap works.
    isDictating = false;
    btn.classList.remove('recording');
  }
}

// ── Patch the send flow to include attachment_ids ──────────────────────────

function patchSendMessage() {
  // Hook into the existing sendMessage flow by overriding the WS message
  const originalSend = app.agentWs?.send;
  if (!originalSend) {
    // agent WS not yet connected — we'll intercept when sendMessage fires
    // Add a listener via MutationObserver or just patch the global send
    return;
  }
}

// Called by chat.js before sending — add attachment_ids to the JSON
export function addAttachmentsToMessage(msgObj) {
  if (pendingAttachments.length > 0) {
    msgObj.attachment_ids = pendingAttachments.map(a => a.attachment_id);
  }
  return msgObj;
}

// ── Render attachments in chat bubbles ─────────────────────────────────────

function _isBrowserStored(att) {
  if (att.storage_provider === 'browser') return true;
  const p = att.storage_path || '';
  return p.startsWith('indexeddb://');
}

function _resolveAttachmentUrl(att) {
  // Returns a thenable; null indicates "no resolvable URL". For browser-stored
  // attachments we mint an object URL from IndexedDB; otherwise we use the
  // server-provided URL or fall back to the local /uploads route.
  if (_isBrowserStored(att) && att.attachment_id) {
    return getObjectUrl(att.attachment_id);
  }
  return Promise.resolve(att.url || (att.storage_path ? `/uploads/${att.storage_path}` : ''));
}

function _setSrcAsync(el, attrName, att, missingClass) {
  // For browser-stored bytes the URL is async; render with an empty src and
  // swap it in when IDB resolves. For server-stored bytes we still go through
  // the same path for consistency.
  _resolveAttachmentUrl(att).then(url => {
    if (url) {
      el[attrName] = url;
    } else if (missingClass) {
      el.classList.add(missingClass);
      el.alt = `(missing) ${att.original_name || ''}`;
    }
  }).catch(() => {
    if (missingClass) el.classList.add(missingClass);
  });
}

export function renderAttachmentElement(att) {
  const mime = att.mime_type || '';

  // Image
  if (mime.startsWith('image/')) {
    const img = document.createElement('img');
    img.alt = att.original_name || 'attachment';
    img.className = 'chat-attachment-img';
    img.loading = 'lazy';
    _setSrcAsync(img, 'src', att, 'chat-attachment-missing');
    return img;
  }

  // Audio
  if (mime.startsWith('audio/')) {
    const wrapper = document.createElement('div');
    wrapper.className = 'chat-audio-wrapper';
    const label = document.createElement('div');
    label.className = 'chat-attachment-label';
    label.innerHTML = `${icon('mic', { size: '12px' })} ${att.original_name || 'Voice recording'}`;
    wrapper.appendChild(label);
    const audio = document.createElement('audio');
    audio.controls = true;
    audio.className = 'chat-audio-player';
    audio.preload = 'metadata';
    _setSrcAsync(audio, 'src', att);
    wrapper.appendChild(audio);
    return wrapper;
  }

  // Video
  if (mime.startsWith('video/')) {
    const video = document.createElement('video');
    video.controls = true;
    video.className = 'chat-attachment-video';
    video.preload = 'metadata';
    _setSrcAsync(video, 'src', att);
    return video;
  }

  // Default: download link
  const link = document.createElement('a');
  link.innerHTML = `${icon('paperclip', { size: '12px' })} ${att.original_name || 'Download attachment'}`;
  link.className = 'chat-attachment-link';
  link.target = '_blank';
  link.rel = 'noopener';
  _setSrcAsync(link, 'href', att);
  return link;
}

// ── Handle attachment event from WebSocket ─────────────────────────────────

export function handleAttachmentEvent(event) {
  if (event.type === 'attachment' && event.attachments) {
    streamAttachments = event.attachments;
    // Render in the latest agent bubble
    const bubbles = app.chatMessages.querySelectorAll('.chat-bubble.agent');
    const last = bubbles[bubbles.length - 1];
    if (last) {
      for (const att of event.attachments) {
        const el = renderAttachmentElement(att);
        if (el) last.appendChild(el);
      }
    }
  }
}

// ── Init on DOM ready (only if not already imported by chat.js) ──
// initAttachments is also called from main.js; guard against double init.
let _initDone = false;

export function ensureAttachmentsInit() {
  if (!_initDone) {
    _initDone = true;
    initAttachments();
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => ensureAttachmentsInit());
} else {
  ensureAttachmentsInit();
}
