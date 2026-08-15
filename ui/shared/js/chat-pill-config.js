'use strict';

// Canonical chat-pill schema and renderer.
//
// ⚠️ JSON DRIVES SIZING — all button sizes (element_size/container_size),
// textarea properties (font_size, min_height, max_height, padding), and
// zone padding are authored in data/config/chat_ui.json. This file applies
// them as inline styles. Change the JSON, not the CSS or JS defaults.
//
// The public schema is always two rows with left/center/right zones. Older
// row/column configs are normalized here so stored per-agent overrides keep
// working while every surface consumes one layout shape.

const ZONES = ['left', 'center', 'right'];
const ALIGN_VALUES = { top: 'flex-start', center: 'center', bottom: 'flex-end' };
const CONTROL_ALIASES = { voice: 'mic' };
const MODE_CLASSES = [
  'chat-pill-mic-send',
  'chat-pill-distinct-buttons',
  'chat-pill-mic-only',
  'chat-pill-send-only',
  'chat-pill-no-action',
];

function mergeObjects(base, overrides) {
  return { ...(base || {}), ...(overrides || {}) };
}

function canonicalName(name) {
  return CONTROL_ALIASES[name] || name;
}

function emptyZoneMeta() {
  return { controls: [], carousel: null, align: null, padding: null };
}

function emptyRows() {
  return [
    { left: emptyZoneMeta(), center: emptyZoneMeta(), right: emptyZoneMeta() },
    { left: emptyZoneMeta(), center: emptyZoneMeta(), right: emptyZoneMeta() },
  ];
}

function normalizeZone(value) {
  // Legacy: bare array of control names → default align/padding
  if (Array.isArray(value)) return { controls: value.map(canonicalName), carousel: null, align: null, padding: null };
  // Object form: { controls, carousel, align, padding }
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return {
      controls: Array.isArray(value.controls) ? value.controls.map(canonicalName) : [],
      carousel: Array.isArray(value.carousel) ? value.carousel.map(canonicalName) : null,
      align: ALIGN_VALUES[value.align] || null,
      padding: value.padding || null,
    };
  }
  return emptyZoneMeta();
}

function normalizeRows(rows) {
  const out = emptyRows();
  for (let index = 0; index < Math.min(2, rows?.length || 0); index += 1) {
    for (const zone of ZONES) {
      out[index][zone] = normalizeZone(rows[index]?.[zone]);
    }
  }
  return out;
}

function legacyButtonName(buttons) {
  const micEnabled = buttons?.voice?.enabled !== false;
  const sendEnabled = buttons?.send?.enabled !== false;
  if (micEnabled && sendEnabled) return 'mic_send';
  if (micEnabled) return 'mic';
  if (sendEnabled) return 'send';
  return null;
}

function placeLegacyControl(rows, cell, name) {
  if (!cell || !name) return;
  const [rowRaw, colRaw] = String(cell).split(',');
  const row = Number.parseInt(rowRaw, 10) - 1;
  const col = Number.parseInt(colRaw, 10);
  if (row < 0 || row > 1) return;
  const zone = col === 1 ? 'left' : col === 3 ? 'right' : 'center';
  rows[row][zone].controls.push(name);
}

function finalizeRows(rows) {
  const hasCombined = rows.some(row => ZONES.some(
    zone => row[zone].controls.includes('mic_send') || (row[zone].carousel || []).includes('mic_send'),
  ));
  const seen = new Set();
  for (const row of rows) {
    for (const zone of ZONES) {
      row[zone].controls = row[zone].controls.filter(name => {
        if (hasCombined && (name === 'mic' || name === 'send')) return false;
        if (seen.has(name)) return false;
        seen.add(name);
        return true;
      });
      if (row[zone].carousel) {
        row[zone].carousel = row[zone].carousel.filter(name => {
          if (hasCombined && (name === 'mic' || name === 'send')) return false;
          if (seen.has(name)) return false;
          seen.add(name);
          return true;
        });
      }
    }
  }
  return rows;
}

export function normalizeChatPill(pill = {}) {
  const legacyButtons = pill.buttons || {};
  const canonicalControls = pill.controls || {};
  const hasLegacyShape = Boolean(
    pill.layout || pill.buttons || pill.textarea || pill.stats || pill.attach,
  );
  const micCfg = mergeObjects(
    canonicalControls.mic || canonicalControls.voice,
    legacyButtons.voice,
  );
  const sendCfg = mergeObjects(canonicalControls.send, legacyButtons.send);
  const combinedCfg = mergeObjects(micCfg, sendCfg);
  const controls = {
    ...canonicalControls,
    textarea: mergeObjects(canonicalControls.textarea, pill.textarea),
    stats: mergeObjects(canonicalControls.stats, pill.stats),
    attach: mergeObjects(canonicalControls.attach, pill.attach),
    mic: micCfg,
    send: sendCfg,
    // A switching control uses one common box. Preserve old configurations
    // by preferring the send dimensions when the two legacy buttons differ.
    mic_send: hasLegacyShape
      ? mergeObjects(canonicalControls.mic_send, combinedCfg)
      : mergeObjects(combinedCfg, canonicalControls.mic_send),
  };
  delete controls.voice;

  let rows;
  if (hasLegacyShape) {
    rows = emptyRows();
    const layout = pill.layout || {};
    placeLegacyControl(rows, layout.textarea || '1,2', 'textarea');
    placeLegacyControl(rows, layout.stats || '2,2', 'stats');
    placeLegacyControl(rows, layout.buttons || '1,3', legacyButtonName(legacyButtons));
    if (pill.attach?.enabled !== false) {
      placeLegacyControl(rows, layout.attach || '2,3', 'attach');
    }
  } else if (Array.isArray(pill.rows)) {
    rows = normalizeRows(pill.rows);
  } else {
    rows = normalizeRows([]);
  }

  return { ...pill, rows: finalizeRows(rows), controls };
}

export function configuredRowControls(config) {
  const names = [];
  for (const row of config?.rows || []) {
    for (const zone of ZONES) {
      for (const name of row?.[zone]?.controls || []) {
        if (config?.controls?.[name]?.enabled !== false) names.push(name);
      }
      for (const name of row?.[zone]?.carousel || []) {
        if (config?.controls?.[name]?.enabled !== false) names.push(name);
      }
    }
  }
  return names;
}

function showElement(el) {
  if (!el) return;
  el.hidden = false;
  el.style.removeProperty('display');
}

function hideElement(el) {
  if (!el) return;
  el.hidden = true;
  el.style.setProperty('display', 'none', 'important');
}

function sizeControl(el, cfg = {}) {
  if (!el) return;
  const containerSize = cfg.container_size || cfg.box_size;
  const elementSize = cfg.element_size || cfg.size;
  if (containerSize && containerSize !== 'auto') {
    el.style.setProperty('width', containerSize, 'important');
    el.style.setProperty('height', containerSize, 'important');
  }
  if (elementSize) {
    const icon = el.querySelector('svg') || el.querySelector('i');
    if (icon) {
      icon.style.setProperty('width', elementSize, 'important');
      icon.style.setProperty('height', elementSize, 'important');
    }
  }
}

function setButtonMode(pillEl, names) {
  pillEl.classList.remove(...MODE_CLASSES);
  const hasCombined = names.includes('mic_send');
  const hasMic = names.includes('mic');
  const hasSend = names.includes('send');
  if (hasCombined) pillEl.classList.add('chat-pill-mic-send');
  else if (hasMic && hasSend) pillEl.classList.add('chat-pill-distinct-buttons');
  else if (hasMic) pillEl.classList.add('chat-pill-mic-only');
  else if (hasSend) pillEl.classList.add('chat-pill-send-only');
  else pillEl.classList.add('chat-pill-no-action');
}

function applyStatsConfig(statsEl, cfg = {}) {
  if (!statsEl || !Array.isArray(cfg.visible)) return;
  const wanted = new Set(cfg.visible);
  const items = {
    'token-bar': statsEl.querySelector('#chat-token-bar, .chat-token-bar'),
    ctx: statsEl.querySelector('#chat-model-ctx, .chat-model-ctx'),
    cost: statsEl.querySelector('#chat-cost, .chat-cost'),
  };
  for (const [name, el] of Object.entries(items)) {
    if (el) el.hidden = !wanted.has(name);
  }
}

export function applyChatPillLayout(els, rawPill = {}) {
  const pill = normalizeChatPill(rawPill);
  const pillEl = els?.pill;
  if (!pillEl) return pill;

  const known = [
    els.input,
    els.stats,
    els.pillButtons,
    els.mic,
    els.send,
    els.attach,
    els.stop,
    els.continue,
  ].filter(Boolean);

  // Rescue stable nodes before removing the old generated rows. Moving the
  // nodes preserves all listeners registered by chat-send/attachments.
  for (const el of known) {
    if (pillEl.contains(el)) pillEl.appendChild(el);
  }
  pillEl.querySelector(':scope > .chat-pill-layout-rows')?.remove();

  if (els.pillButtons) {
    if (els.mic) els.pillButtons.appendChild(els.mic);
    if (els.send) els.pillButtons.appendChild(els.send);
  }

  const controls = {
    textarea: els.input,
    stats: els.stats,
    mic_send: els.pillButtons,
    mic: els.mic,
    send: els.send,
    attach: els.attach,
    stop: els.stop,
    continue: els.continue,
  };
  for (const el of Object.values(controls)) hideElement(el);

  const names = configuredRowControls(pill);
  setButtonMode(pillEl, names);
  pillEl.classList.add('chat-pill-row-layout');

  const rowsEl = document.createElement('div');
  rowsEl.className = 'chat-pill-layout-rows';

  for (let rowIndex = 0; rowIndex < 2; rowIndex += 1) {
    const rowCfg = pill.rows[rowIndex];
    const rowEl = document.createElement('div');
    rowEl.className = 'chat-pill-layout-row';
    rowEl.setAttribute('data-pill-row', String(rowIndex));

    for (const zone of ZONES) {
      const zoneCfg = rowCfg[zone];
      const zoneEl = document.createElement('div');
      zoneEl.className = `chat-pill-layout-zone chat-pill-layout-zone-${zone}`;
      // Apply per-zone alignment from config (overrides CSS default of flex-end)
      if (zoneCfg.align) zoneEl.style.alignItems = zoneCfg.align;
      // Apply per-zone padding from config
      if (zoneCfg.padding) zoneEl.style.padding = zoneCfg.padding;

      if (zoneCfg.carousel) {
        // ── Zone-level carousel (scrollable strip with chevrons) ──
        zoneEl.classList.add('chat-pill-zone-carousel');
        const chevLeft = document.createElement('button');
        chevLeft.type = 'button';
        chevLeft.className = 'chat-pill-zone-chev left';
        chevLeft.setAttribute('aria-label', 'Scroll left');
        chevLeft.innerHTML = '&#10094;';

        const strip = document.createElement('div');
        strip.className = 'chat-pill-zone-carousel-strip';

        const chevRight = document.createElement('button');
        chevRight.type = 'button';
        chevRight.className = 'chat-pill-zone-chev right';
        chevRight.setAttribute('aria-label', 'Scroll right');
        chevRight.innerHTML = '&#10095;';

        zoneEl.appendChild(chevLeft);
        zoneEl.appendChild(strip);
        zoneEl.appendChild(chevRight);

        for (const name of zoneCfg.carousel) {
          if (!names.includes(name)) continue;
          const el = controls[name];
          if (!el) continue;
          showElement(el);
          strip.appendChild(el);
        }

        // Wire chevrons
        const update = () => {
          const overflow = strip.scrollWidth - strip.clientWidth > 1;
          zoneEl.classList.toggle('has-overflow', overflow);
          chevLeft.classList.toggle('visible', overflow && strip.scrollLeft > 1);
          chevRight.classList.toggle('visible', overflow && strip.scrollLeft < strip.scrollWidth - strip.clientWidth - 1);
        };
        const scrollStep = () => Math.max(60, Math.floor(strip.clientWidth * 0.6));
        chevLeft.addEventListener('click', () => { strip.scrollBy({ left: -scrollStep(), behavior: 'smooth' }); });
        chevRight.addEventListener('click', () => { strip.scrollBy({ left: scrollStep(), behavior: 'smooth' }); });
        strip.addEventListener('scroll', update, { passive: true });
        requestAnimationFrame(update);
        if (typeof ResizeObserver !== 'undefined') {
          new ResizeObserver(update).observe(strip);
        }
      } else {
        // ── Standard inline controls ──
        for (const name of zoneCfg.controls) {
          if (!names.includes(name)) continue;
          const el = controls[name];
          if (!el || zoneEl.contains(el)) continue;
          showElement(el);
          if (name === 'mic_send') {
            showElement(els.mic);
            showElement(els.send);
          }
          zoneEl.appendChild(el);
        }
      }

      rowEl.appendChild(zoneEl);
    }
    rowsEl.appendChild(rowEl);
  }

  const preview = pillEl.querySelector(':scope > #chat-preview-bar');
  if (preview?.nextSibling) pillEl.insertBefore(rowsEl, preview.nextSibling);
  else pillEl.prepend(rowsEl);

  const textareaCfg = pill.controls.textarea || {};
  if (els.input) {
    if (textareaCfg.min_height) {
      els.input.style.minHeight = textareaCfg.min_height;
      els.input.removeAttribute('rows');
    }
    if (textareaCfg.max_height) els.input.style.maxHeight = textareaCfg.max_height;
    if (textareaCfg.font_size) {
      els.input.style.setProperty('--chat-pill-font-size', textareaCfg.font_size);
    }
    // padding is applied as inline-style so it overrides the CSS default
    // (8px 8px 2px 0). Authored in data/config/chat_ui.json.
    if (textareaCfg.padding) els.input.style.padding = textareaCfg.padding;
  }
  applyStatsConfig(els.stats, pill.controls.stats);
  sizeControl(els.attach, pill.controls.attach);
  sizeControl(els.stop, pill.controls.stop);
  sizeControl(els.continue, pill.controls.continue);
  sizeControl(els.mic, names.includes('mic_send') ? pill.controls.mic_send : pill.controls.mic);
  sizeControl(els.send, names.includes('mic_send') ? pill.controls.mic_send : pill.controls.send);

  // Reveal the pill — it starts hidden (style="display:none" in the HTML)
  // so the old 3×2 grid / 20px-radius shell never flashes before config loads.
  pillEl.style.removeProperty('display');

  return pill;
}
