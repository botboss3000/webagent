# Computer Control

Use this ability to work through the host computer's native desktop UI. Treat
the screen as sensitive: screenshots can contain passwords, private messages,
notifications, customer data, or authentication codes.

## Current capability

Phase 3 exposes screenshots plus Windows pointer movement, clicking, scrolling,
dragging, literal Unicode text entry, and key/chord input. macOS and Linux can
capture screenshots, but their input tools return `unsupported_platform` until
native backends are added.

### `computer_screenshot(question="", analyze=true)`

Capture the entire virtual desktop, including every connected display, and save
the PNG as a conversation attachment.

- Set `question` to the exact visual fact needed, such as `"Which dialog is
  open and what buttons does it show?"`. Leave it empty for a complete screen
  description.
- Keep `analyze=true` when you need the configured vision model to inspect the
  image. Set it to `false` only when capture proof is enough or another
  image-capable tool/model will inspect the attachment.
- Read `dimensions` and `coordinate_space` from the result. Screenshot
  coordinates start at `(0, 0)` in the PNG's top-left, even when the operating
  system has a monitor positioned to the left of its primary display.
- A successful capture can include a `warning` instead of `description` when no
  vision model is configured. The screenshot is still valid; do not invent what
  it shows.
- On Windows, this uses the interactive desktop. A locked session, secure
  desktop/UAC prompt, service session, or DRM-protected surface may be blank or
  unavailable. Report that limitation plainly.
- Every successful screenshot authorizes exactly one input action. It expires
  after five minutes and is invalidated by any
  Computer Control action from another session.

## Operating loop

Always use this loop:

1. Call `computer_screenshot` and inspect the current state.
2. Choose one small action whose target is unambiguous.
3. Perform exactly one action.
4. Call `computer_screenshot` again and verify the expected state change.
5. Stop if the screen differs from what you expected. Re-observe instead of
   sending blind follow-up input.

The runtime enforces steps 1 and 4 for pointer and keyboard input: it rejects
action-before-screenshot, stale observations, changed display layouts,
out-of-bounds coordinates, and a second action before verification.

Ask before actions that submit forms, send messages, purchase, publish, delete,
install system-wide, change security settings, expose secrets, or otherwise
create an external or hard-to-reverse effect. Never type passwords, API keys,
recovery codes, or payment details unless the user explicitly supplies and
authorizes that exact use.

## Pointer tools

All coordinates are integer pixels in the most recent screenshot, measured from
its top-left `(0, 0)`. Never reuse coordinates after a screenshot reports a new
size, origin, or monitor arrangement.

### `computer_move(x, y, duration_ms=0)`

Move without clicking. Use immediate movement for ordinary targets or a bounded
duration for hover-sensitive interfaces. A move can open a hover menu, so
re-screenshot before any click.

### `computer_click(x, y, button="left", clicks=1, interval_ms=100)`

Move to the coordinate and click. Use `clicks=2` for a real double-click; never
simulate it with two separate calls. Use right or middle click only when the
observed UI makes that intent unambiguous. Clicking is confirmation-gated in
Ask/Plan modes because it can cause external effects.

### `computer_scroll(delta, x=null, y=null)`

Scroll by wheel notches. Positive values scroll up and negative values scroll
down; keep the value between `-20` and `20`. Supply both `x` and `y` to place the
pointer over a specific pane first, or omit both to use its current position.
Prefer small deltas and verify what moved.

### `computer_drag(start_x, start_y, end_x, end_y, duration_ms=600, button="left")`

Move to the start, hold the selected button, travel to the end, and release.
Observe both endpoints first. Use it for sliders, selection, rearrangement, or
drag-and-drop only when the destination is visible. Dragging is
confirmation-gated in Ask/Plan modes.

## Keyboard tools

Keyboard input goes to the control that currently owns focus. Before typing or
pressing a key, click the intended control, take a new screenshot, and verify
its focus indicator or caret. Never assume focus survived a dialog, window
switch, notification, or prior shortcut.

### `computer_type(text, interval_ms=0)`

Enter literal Unicode text without using the clipboard. Use it for ordinary
text only, not shortcuts or special keys.

- `text` must contain 1–4000 Unicode characters. Newline, tab, escape, delete,
  and all other control characters are rejected; send those deliberately with
  `computer_key`.
- Use `interval_ms=0` normally. Use a small delay only for an application that
  visibly drops fast input; the maximum is 1000 ms per character.
- The result reports character counts but never repeats the entered text.
- Treat all text entry as consequential. Re-read the user's exact requested
  text before calling, then screenshot and verify it afterward.
- Never enter a password, API key, recovery code, authentication code, payment
  detail, or other secret unless the user explicitly supplied it and authorized
  that exact destination.

### `computer_key(keys)`

Send one key or one chord. Examples: `["ENTER"]`, `["CTRL", "L"]`,
`["ALT", "F4"]`, or `["CTRL", "SHIFT", "S"]`.

- Supply an array of 1–4 unique names. A chord may contain modifiers plus one
  ordinary key. Modifiers are normalized and pressed before the ordinary key,
  then released safely in reverse order.
- Supported modifiers are `CTRL`, `SHIFT`, `ALT`, and `WIN`. Supported ordinary
  keys include `A`–`Z`, `0`–`9`, `F1`–`F24`, `ENTER`, `TAB`, `ESCAPE`,
  `BACKSPACE`, `DELETE`, `INSERT`, `SPACE`, arrows, `HOME`, `END`, `PAGEUP`,
  `PAGEDOWN`, `PRINTSCREEN`, lock keys, and `SEMICOLON`, `EQUALS`, `COMMA`,
  `MINUS`, `PERIOD`, `SLASH`, `BACKTICK`, `BRACKET_LEFT`, `BACKSLASH`,
  `BRACKET_RIGHT`, and `QUOTE`.
- Common aliases such as `CONTROL`, `RETURN`, `ESC`, `DEL`, `PGUP`, `PGDN`,
  `WINDOWS`, and `META` are normalized. Use Windows names while Windows is the
  only input backend.
- Use exactly one chord per call. Never encode a multi-step macro as one call.
- Treat `ENTER`, `DELETE`, `ALT+F4`, `CTRL+W`, `CTRL+S`, `CTRL+P`, and similar
  keys as potentially consequential. Verify the focused UI and obtain approval
  when they could submit, close, save, print, delete, publish, or send.

### Later tools

- Clipboard, file-drop, accessibility-tree, and application/window-selection
  tools should remain separate capabilities rather than being simulated with
  undocumented key sequences.

## Platform notes

- Prefer the modifier exposed by the platform: `CTRL` on Windows/Linux and
  `CMD` on macOS.
- UI scale, Retina/HiDPI, mixed-DPI monitors, display rotation, and monitor
  rearrangement can invalidate old coordinates. Take a fresh screenshot after
  any display change and before every consequential action.
- Use semantic browser tools for web pages when available; use Computer Control
  when the task depends on native applications, OS dialogs, or desktop state.
