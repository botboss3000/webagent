'use strict';

// Terminal Chat keys — on-screen key grid that REPLACES the text pill for the
// Terminal Chat panel. A keyboard button in the chat footer (#chat-term-keys-btn,
// in ui/chat-side-panel/chat-side-panel.html) swaps the pill (#chat-input-row) for
// a fixed 3-row grid (#chat-term-keypad) in the SAME spot — number pad, arrows &
// shortcut keys. Each key fires a SINGLE keystroke straight to the live PTY via
// app.terminalChatKey (defined in ui/chat-side-panel/js/chat-terminal-engine.js) —
// no Enter is appended (unlike the chat pill in chat-send.js), so you can drive TUI
// menus (Claude Code), move the cursor, pick numbered options, and interrupt.
//
// Sibling of the admin terminal's keybar (ui/shared/js/files.js → initTerminalKeybar);
// the key→byte map mirrors that file's chipBytes().
// REMOVE-WHEN: the Terminal Chat engine (chat-terminal-engine.js) is dropped.

import { app } from '../../shared/js/state.js';

// key id → raw bytes the PTY expects. Arrows / Home / End / PgUp-Dn are ANSI
// cursor escapes; Ctrl-combos are the matching control codes; digits are literal.
// Backspace is DEL (0x7f), the PTY convention.
const KEY_BYTES = {
  up: '\x1b[A', down: '\x1b[B', right: '\x1b[C', left: '\x1b[D',
  home: '\x1b[H', end: '\x1b[F', pgup: '\x1b[5~', pgdn: '\x1b[6~',
  esc: '\x1b', tab: '\t', 'shift-tab': '\x1b[Z', enter: '\r',
  backspace: '\x7f', del: '\x1b[3~',
  'ctrl-c': '\x03', 'ctrl-d': '\x04', 'ctrl-z': '\x1a',
  'ctrl-l': '\x0c', 'ctrl-r': '\x12',
};
for (let d = 0; d <= 9; d++) KEY_BYTES['num-' + d] = String(d);

// The grid, in reading order: 3 rows × 8 columns.
//   row 1 — digits 1-8
//   row 2 — digits 9-0, then Esc / Tab / ⇧Tab / Enter / Backspace / interrupt
//   row 3 — the four arrows, then Home / End / PgUp / PgDn
const LAYOUT = [
  { key: 'num-1', label: '1' }, { key: 'num-2', label: '2' }, { key: 'num-3', label: '3' }, { key: 'num-4', label: '4' },
  { key: 'num-5', label: '5' }, { key: 'num-6', label: '6' }, { key: 'num-7', label: '7' }, { key: 'num-8', label: '8' },
  { key: 'num-9', label: '9' }, { key: 'num-0', label: '0' },
  { key: 'esc', label: 'Esc' }, { key: 'tab', label: 'Tab' }, { key: 'shift-tab', label: '⇧Tab', title: 'Shift+Tab (back-tab)' },
  { key: 'enter', label: '↵', title: 'Enter' }, { key: 'backspace', label: '⌫', title: 'Backspace' },
  { key: 'ctrl-c', label: '^C', title: 'Interrupt (Ctrl+C)' },
  { key: 'left', icon: 'arrow-left', title: 'Left' }, { key: 'up', icon: 'arrow-up', title: 'Up' },
  { key: 'down', icon: 'arrow-down', title: 'Down' }, { key: 'right', icon: 'arrow-right', title: 'Right' },
  { key: 'home', label: 'Home' }, { key: 'end', label: 'End' },
  { key: 'pgup', label: 'PgUp' }, { key: 'pgdn', label: 'PgDn' },
];

let _panel = null;
let _btn = null;
let _area = null;
let _wired = false;

function _send(key) {
  if (!Object.prototype.hasOwnProperty.call(KEY_BYTES, key)) return;
  if (typeof app.terminalChatKey === 'function') app.terminalChatKey(KEY_BYTES[key]);
  // A footer/grid tap steals focus from the terminal — hand it back so mouse and
  // direct keyboard interaction keep landing where the user expects.
  if (typeof app.terminalChatFocus === 'function') app.terminalChatFocus();
}

function _buildHtml() {
  return LAYOUT.map((k) => {
    const inner = k.icon ? '<i data-lucide="' + k.icon + '"></i>' : k.label;
    const title = k.title || k.label || k.key;
    return '<button type="button" class="ctk-chip" data-key="' + k.key + '" '
      + 'title="' + title + '" aria-label="' + title + '">' + inner + '</button>';
  }).join('');
}

// Swap pill ↔ keys grid: the input area carries .term-keys-open while the grid
// shows (CSS hides #chat-input-row and reveals #chat-term-keypad in its place).
function _setOpen(open) {
  if (!_area || !_btn) return;
  _area.classList.toggle('term-keys-open', open);
  _btn.dataset.active = open ? '1' : '';
}

export function initTerminalKeys() {
  _btn = document.getElementById('chat-term-keys-btn');
  _panel = document.getElementById('chat-term-keypad');
  _area = document.getElementById('chat-input-area');
  if (!_btn || !_panel || !_area || _wired) return;
  _wired = true;

  _panel.innerHTML = _buildHtml();

  // Toggle the grid in/out of the pill's spot.
  _btn.addEventListener('click', (e) => {
    e.preventDefault();
    _setOpen(!_area.classList.contains('term-keys-open'));
  });

  // One delegated handler — closest() finds the owning button even when the tap
  // lands on an inner Lucide <svg>.
  _panel.addEventListener('click', (e) => {
    const chip = e.target.closest('[data-key]');
    if (!chip) return;
    e.preventDefault();
    _send(chip.getAttribute('data-key'));
  });

  // The engine reveals / hides the footer toggle as Terminal Chat mounts/unmounts;
  // unmount also restores the pill (closes the grid).
  app.showTerminalKeys = () => { if (_btn) _btn.hidden = false; };
  app.hideTerminalKeys = () => { if (_btn) _btn.hidden = true; _setOpen(false); };
}
