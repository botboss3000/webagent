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

  // Voice recording
  if (voiceBtn && navigator.mediaDevices) {
    voiceBtn.addEventListener('click', () => startVoiceRecording(voiceBtn));
  } else if (voiceBtn) {
    const row = document.getElementById('chat-input-row');
    if (row) row.classList.add('no-voice');
  }

  // Drag & drop — set up a drop zone on a container
  function setupDropZone(el, overlayClass) {
    if (!el) return;
    let counter = 0;
    el.addEventListener('dragenter', (e) => {
      e.preventDefault();
      e.stopPropagation();
      counter++;
      if (counter === 1) el.classList.add('drag-over');
    });
    el.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.stopPropagation();
    });
    el.addEventListener('dragleave', (e) => {
      e.preventDefault();
      e.stopPropagation();
      counter--;
      if (counter <= 0) { counter = 0; el.classList.remove('drag-over'); }
    });
    el.addEventListener('drop', async (e) => {
      e.preventDefault();
      e.stopPropagation();
      counter = 0;
      el.classList.remove('drag-over');
      const files = Array.from(e.dataTransfer.files);
      for (const file of files) {
        await uploadAndPreview(file);
      }
    });
  }

  // Drop zone 1: chat messages area
  setupDropZone(document.getElementById('chat-messages'));
  // Drop zone 2: chat footer
  setupDropZone(document.getElementById('chat-input-area'));

  // Expose attachment_ids to chat send flow
  app.getPendingAttachmentIds = () => pendingAttachments.map(a => a.attachment_id);
  app.clearPendingAttachments = clearPendingAttachments;

  // Override send flow to include attachment_ids
  patchSendMessage();
}

// ── Upload ─────────────────────────────────────────────────────────────────

async function uploadAndPreview(file) {
  if (!file) return;

  const maxSize = 25 * 1024 * 1024; // 25MB
  if (file.size > maxSize) {
    alert(`File too large: ${file.name} (${(file.size / 1024 / 1024).toFixed(1)}MB). Max 25MB.`);
    return;
  }

  const previewBar = document.getElementById('chat-preview-bar');
  const chip = document.createElement('span');
  chip.className = 'chat-attachment-pill uploading';
  chip.innerHTML = `${icon('upload', { size: '12px' })} ${file.name}`;
  previewBar.appendChild(chip);
  previewBar.style.display = 'flex';

  try {
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

    const data = await res.json();

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
      const idx = pendingAttachments.findIndex(a => a.attachment_id === data.attachment_id);
      if (idx >= 0) pendingAttachments.splice(idx, 1);
      if (pendingAttachments.length === 0) previewBar.style.display = 'none';
    });
    chip.appendChild(removeBtn);

    // Store
    pendingAttachments.push(data);
  } catch (err) {
    chip.className = 'chat-attachment-pill error';
    chip.textContent = `${file.name}: ${err.message}`;
    setTimeout(() => { chip.remove(); if (pendingAttachments.length === 0) previewBar.style.display = 'none'; }, 3000);
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

// ── Voice Recording ────────────────────────────────────────────────────────

let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;

async function startVoiceRecording(btn) {
  if (isRecording) {
    // Stop recording
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
      mediaRecorder.stop();
    }
    btn.innerHTML = icon('mic', { size: '16px' });
    btn.classList.remove('recording');
    isRecording = false;
    return;
  }

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioChunks = [];
    mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });

    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) audioChunks.push(e.data);
    };

    mediaRecorder.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      const blob = new Blob(audioChunks, { type: 'audio/webm' });
      btn.innerHTML = icon('mic', { size: '18px' });
      btn.title = 'Record voice';
      btn.classList.remove('recording');
      isRecording = false;

      // Upload the recording
      const file = new File([blob], `voice-${Date.now()}.webm`, { type: 'audio/webm' });
      await uploadAndPreview(file);
    };

    mediaRecorder.start();
    btn.innerHTML = icon('circle-stop', { size: '18px' });
    btn.title = 'Stop recording';
    btn.classList.add('recording');
    isRecording = true;
  } catch (err) {
    alert('Microphone access denied: ' + err.message);
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

export function renderAttachmentElement(att) {
  const mime = att.mime_type || '';
  const url = att.url || `/uploads/${att.storage_path}`;

  // Image
  if (mime.startsWith('image/')) {
    const img = document.createElement('img');
    img.src = url;
    img.alt = att.original_name || 'attachment';
    img.className = 'chat-attachment-img';
    img.loading = 'lazy';
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
    audio.src = url;
    audio.controls = true;
    audio.className = 'chat-audio-player';
    audio.preload = 'metadata';
    wrapper.appendChild(audio);
    return wrapper;
  }

  // Video
  if (mime.startsWith('video/')) {
    const video = document.createElement('video');
    video.src = url;
    video.controls = true;
    video.className = 'chat-attachment-video';
    video.preload = 'metadata';
    return video;
  }

  // Default: download link
  const link = document.createElement('a');
  link.href = url;
  link.innerHTML = `${icon('paperclip', { size: '12px' })} ${att.original_name || 'Download attachment'}`;
  link.className = 'chat-attachment-link';
  link.target = '_blank';
  link.rel = 'noopener';
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
