# UI guidance (read before any `ui/` change)

Covers theming, edge-fade masks, and Lucide icons. Read this before touching anything under `ui/` or markup in `index.html`.

## UI features must work in dark AND light mode

Every new UI feature in `ui/` (and any markup in `index.html`) **must be tested and visually correct in both dark mode and light mode** before it is considered done. The app toggles between themes via `body.light-mode`; all theme-sensitive colors are exposed as CSS variables in `ui/css/design-system.css`.

**Rules:**

1. **Never hard-code hex colors for backgrounds, borders, or text** in inline styles, HTML attributes, or JS string templates. Use the design-system variables instead:
   - Backgrounds: `var(--bg-0)`, `var(--bg-1)`, `var(--bg-2)`, `var(--bg-elev)`, `var(--bg-elev-2)`, `var(--bg-tint)`
   - Borders: `var(--border)`, `var(--border-soft)`, `var(--border-strong)`
   - Text: `var(--fg-1)`, `var(--fg-2)`, `var(--fg-3)`, `var(--fg-4)`
   - Semantic: `var(--success)`, `var(--warning)`, `var(--danger)`, `var(--accent)`
2. **Never write `var(--foo, #darkhex)` fallbacks.** Either the variable exists in `design-system.css` (no fallback needed) or it doesn't (don't invent one — add it to the design system). A literal dark hex as a fallback silently breaks light mode.
3. **If a literal hex is unavoidable** (e.g. a brand-coloured icon stripe), pick a hue with adequate contrast against **both** `--bg-0: #0d0d1a` (dark) and `--bg-0: #fffaf5` (light). Test by toggling `body.light-mode` in DevTools.
4. **Verify before pushing:** open the new feature, toggle `body.light-mode`, confirm every panel/text/border is still legible and visually consistent with neighbouring controls.

If you introduce a new theme-sensitive surface that isn't covered by the existing variables, **add a new variable to both the default and `body.light-mode` blocks of `design-system.css`** in the same change — don't sprinkle hex codes through feature code.

## Edge fades over a dynamic/themed background — mask, never a colour

When a scrolling strip (a horizontal tab carousel, a content list, etc.) needs its edges to **fade out** so the content doesn't cut off abruptly, **fade the content with a CSS alpha mask — never paint a coloured gradient to fake it.**

**Why this is a hard rule here:** several surfaces in this app sit over a background that is **not a static colour** — most notably the **live stargaze canvas** (`ui/js/stargaze.js`), which continuously redraws a warm peach (light) / nebula (dark) glow that shifts with theme and pointer position. The page base colour (`#faf5ee` light / `#07070c` dark, set in `index.css`) is *not* what you actually see there. So any edge fade implemented as `linear-gradient(..., <some colour>, transparent)` will be a guessed static colour that can never match the moving background — it shows up as a lighter/whiter rectangle. A **mask** paints no colour at all (alpha only): it fades the *content* to transparent and lets the real background — glow and all — show straight through, matching automatically in both themes and even while the canvas animates.

**The pattern (two ingredients):**

1. **Transparent buttons/affordances** (e.g. carousel chevrons) positioned over the edge — `background: none`, glyph colour from a design-system var so it tracks the theme.
2. **A `-webkit-mask-image` / `mask-image` linear-gradient on the *scroller itself*** that goes `transparent → #000` over the fade width at the edge, applied **only on the side(s) that can still scroll**, toggled by the scroll-state classes the carousel JS already maintains.

**Canonical implementations to copy:**

| Location | Selector |
|----------|----------|
| Agent card carousel (origin of the technique) | `.agent-card-tabs` / `.agent-card-tabs-chev` in `ui/css/agents.css` |
| Main header tab strip | `#main-tabs` "Masked edge-fade carousel" block in `ui/css/design-system.css` |

Both blocks carry a full explanatory comment. The `:has()` per-side selectors used for the header need a modern browser (Chrome 105+, which the app targets); if you ever need older support, toggle the mask via a JS class on the scroller instead.

## Chat pills — one shared design, and the panel layout around them

A **chat pill** is the rounded input row with an attach button, a text area, and a mic/send button (the "Chat with the agent" box, the agent-builder bar, etc.). Every pill in the app shares **one** design; individual panels only own the layout *around* the pill.

### The pill itself is a single source of truth — opt in, never restyle per-instance

All pill **geometry and behaviour** live in **one block**: the `CHAT-PILL-SYNC` comment + `.chat-pill*` rules in `ui/css/app1.css`. To add or change a pill, opt in with these classes — don't re-implement the look:

- `.chat-pill` — the outer rounded container (a CSS grid: text fills the left column and spans all rows; the mic/send and attach buttons stack in the right column, bottom-anchored).
- `.chat-pill-input` — the textarea/input (shared sizing: ~96px resting min-height, ~124px max before it scrolls internally; the native scrollbar is hidden).
- `.chat-pill-attach` / `.chat-pill-voice` / `.chat-pill-send` — the left attach button and the right mic/send buttons; all three share one transparent-circle icon-button style, only the glyph differs.

**State classes** (toggled by JS, styled centrally — don't hand-roll equivalents):

- `.has-text` — input has content → swaps **mic → send**.
- `.no-voice` — force send-only (e.g. the signed-out web-chat gate).
- `.thinking` — the agent is working → glow (set by `chat-activity.js`).
- `.drag-over` — file drag hovering the pill → dashed accent border.

**Geometry vs. skin — two files, on purpose.** The base block in `app1.css` defines an **opaque dark pill** plus all geometry/behaviour. The two **floating-glass** pills (the web-chat pill `#chat-panel .chat-pill` and the agents builder bar `#agent-builder-bar > .chat-pill`) are re-skinned transparent in `ui/css/index.css` (loaded last). So: **change the floating-glass look in `index.css`; change geometry/behaviour/buttons in `app1.css`.** Never fork either into a panel's own CSS.

**Known pills (all wired to the shared classes):** `#chat-input-row` (web chat), `#ac-int-admin-chat-input-row` (integration admin), `#agent-builder-bar-row` (agents page), `#autoagent-prompt-row` (Pages tab). Change the shared block → every one updates together.

### Panel layout — full-height content, the pill floats over it

Any panel that *hosts* a pill follows one layout: the **content scroller fills the panel's entire height**, and the **pill floats over its bottom edge** as an overlay. The scroller's own scrollbar must run all the way to the bottom of the panel; content scrolls *behind* the translucent pill. This is the look on the right-hand **chat panel**, and every other pill panel must match it.

**Why it's a hard rule:** the natural mistake is to put the pill — or any control near it (a toggle, a footer) — in **normal flow below the scroller**. A flow sibling under the scroller *reserves vertical space*, which shrinks the scroller so its content and scrollbar stop short, leaving a dead gap above the pill (the page's content visibly ends near the pill's top instead of running to the bottom). That's the exact bug that made the **agents page** look wrong next to the chat panel.

**The pattern (mirror the chat panel):**

1. **One scroller fills the panel.** The panel is a flex column (`position: relative`); the scroller is `flex: 1; overflow-y: auto` with a **bottom padding** that clears the pill's height so the last items aren't hidden under it. (Chat uses ~40px and lets content scroll *behind* the glass; that translucent-overlap is intended, not a clearance bug.)
2. **The pill is an absolute overlay**, not a flow child: `position: absolute; left/right: 0; bottom: <n>; z-index` above the scroller. It does **not** occupy layout space, so the scroller stays full-height.
3. **Auxiliary controls go *inside* the scroller** as trailing content (so they scroll with everything else and disappear behind the pill), **never** as a flow sibling between the scroller and the pill. On the agents page the "Show system agents" toggle is mounted into the squares scroller by `agents.js` for exactly this reason — earlier it sat below the grid and shrank it.

**Canonical implementations to copy:**

| Panel | Full-height content | Floating pill |
|-------|---------------------|---------------|
| Chat panel (origin) | `#chat-messages` / `#chat-messages-inner` in `ui/css/app1.css` | `#chat-input-area` (`position: absolute`) |
| Agents page | `#agents-grid` / `.agents-squares` in `ui/css/agents.css` | `#agent-builder-bar` (`position: absolute`) |
| Pages page | `#autoagent-viewport` (the iframe) in `ui/css/autoagent.css` | `#autoagent-footer` (`position: absolute`, page-nav + `#autoagent-prompt-row`) |

Each panel's CSS owns **only** this wrapper layout (the scroller's height/padding + the pill's absolute positioning and bottom alignment). The pill's appearance is not the panel's concern — that comes from the shared block described above.

**Pills in side-by-side panels must visually line up — top AND bottom.** When two pill panels sit next to each other (the agents page on the left, the chat panel on the right), the two pills read as one aligned row. Tops align automatically because every pill shares the same resting height (the `.chat-pill-input` min-height). **Bottoms do NOT align for free**, because the panels don't bottom out at the same place: the **chat pill has a footer/token row beneath it** (the `IN ◇ OUT` counter + mode button), which lifts the chat pill's bottom edge up off the panel floor, whereas the **agents builder bar has no footer row**. So you must *account for that spacing difference* on the side that lacks the footer: the agents bar adds a **bottom margin on `#agent-builder-bar > .chat-pill` that is COMPUTED from the chat pill's stack, not a hand-tuned number.** In the CSS it's written as a `calc()` of named terms so it's self-documenting: `--chat-area-bottom-inset (8px, the chat area's bottom) + --chat-pill-footer-gap (6px, its gap) + --chat-footer-row-h (≈18px, the token/mode row) = 32px`. If the two panels ever stop bottoming out on the same line — a **page or panel border is added, or a container margin changes** (e.g. `#main-panel`'s) — add that floor delta as a further term. **This pair is commonized:** grep the tag **`PILL-PANEL-ALIGN`** to find both ends (the chat side at `#chat-input-area` in `app1.css`, the agents side at `#agent-builder-bar` in `agents.css`). Rule of thumb: change the chat pill's footer, gap, bottom inset, or either panel's border/margin → update the `--chat-*` terms on the agents bar so the pair stays aligned. Never assume same-height pills bottom-align.

**Verify with geometry, not a screenshot** (screenshots of these panels hang on the stargaze canvas): assert the scroller's bottom equals the panel's bottom (e.g. `#agents-grid`'s `getBoundingClientRect().bottom === #tab-agents`'s) so you know it's truly full-height and the pill is overlaying rather than stacked. To check the cross-panel pill alignment, compare the two pills' `getBoundingClientRect()` `top` and `bottom` (e.g. `#agent-builder-bar > .chat-pill` vs `#chat-input-row`) — but the chat panel is hidden below ~800px width, so widen the viewport first (note `preview_resize` re-inits the starfield, so prefer a fresh start at a wide size).

## Toggle-list (category + rows of options) — one shared design

Whenever a surface shows **a category/section heading followed by a vertical list of options, each option being a row with an icon, a title, a one-line description, and a control (usually a toggle) on the right**, it must use the app's single shared "compact row list" look. This is the design on the **Admin Tools → Agent Settings → Agent Tools** panel: one bordered, rounded container holding flush rows, soft tinted background (peach in light mode / elevated dark in dark mode), 1px hairline dividers between rows, a coloured icon at left, bold title + muted 2-line description, and the toggle pinned to the right. Rows sit **directly inside the one container** — never each option wrapped in its own separate card ("card-within-a-card"). That separate-cards look (individual bordered boxes per option) is the **wrong** pattern and is what currently makes the agent card's Abilities panel look out of place.

**Why this is a hard rule:** these lists appear in many places. If each one carries its own styling, a single design tweak (spacing, divider, hover, light-mode tint) has to be hand-applied in every copy and they drift apart. The whole point is that *one* set of classes drives all of them, so a change made once cascades everywhere.

**Two separate patterns, don't conflate them.** There's (a) the **collapsible section header** — chevron + icon + title that opens to reveal a group — which *is* already shared across the admin pages (`.ac-category-group`), and (b) the **compact option-row list** *inside* such a section (`.ac-abilities-compact`). A page can have the right headers but still have the **wrong content** inside them. Right now **only the Agent Tools section** (in Admin → Agent Settings) actually uses the compact option-row list. Every other section — including the rest of Agent Settings (Models, Channels, Productivity, Social Media, Marketplaces) and most of App Settings — opens to **individual cards** (the compactified `.ac-int-row` cards, one box per option) rather than a single flush row-list. So the whole admin surface still needs converting, not just the agent card.

### Canonical implementation — copy this, don't re-invent

The reference lives in **`ui/css/app3.css`** as the `.ac-abilities-compact` block (container) + `.ac-ability-row` (each row), with sub-parts `.ac-ability-icon`, `.ac-ability-label` → `.ac-ability-name` + `.ac-ability-desc`, `.ac-ability-status`, and `.ac-ability-toggle-wrap`. Light-mode overrides (the peach tint) sit in the matching `body.light-mode .ac-ability-*` rules right below. The container that *holds* a list of these (the collapsible section header: chevron + icon + title that opens to reveal the list) is the `.ac-category-group` / `.ac-category-summary` block in the same file — see its boxed "SHARED COLLAPSIBLE SECTION PATTERN" comment. The matching markup contract is documented in `ui/admin-tools/admin-configuration.html` (search **"COLLAPSIBLE SECTION PATTERN"**). All of these use **design-system CSS variables** for colour (no hard-coded hex except the light-mode peach overrides) — keep it that way.

> **Restyle the toggle-list look in `app3.css` only.** Don't fork a per-page or per-panel copy. Every conformant area below reads from this single block so the whole app stays coherent.

### Every area that uses (or should use) this pattern

When the toggle-list design changes, check **all** of these so the change lands everywhere at once. **As of this writing only one area is conformant** — the rest are listed as work still to do.

- ✅ **Conformant** = already on the shared `.ac-abilities-compact` / `.ac-ability-row` row-list.
- ❌ **Divergent** = currently individual-cards or a bespoke look; should be migrated onto the row-list.

| # | Area | Where | Classes / source | State |
|---|------|-------|------------------|-------|
| 1 | **Admin → Agent Settings → Agent Tools** | `ui/admin-tools/admin-configuration.html` + `app-config.js` (`_renderAbilitiesFromMeta`) | `.ac-abilities-compact` + `.ac-ability-row` | ✅ **Canonical reference (the only conformant area)** |
| 2 | **Admin → Agent Settings — all other sections** (Models, Channels, Productivity, Social Media, Marketplaces) | `admin-configuration.html` + `app-config.js` (`_compactifyCard`) | `.ac-category-group` headers → `.ac-int-row` / `.ac-card` (one card per option) | ❌ Divergent — headers OK, but contents are individual cards; convert to the row-list |
| 3 | **Admin → App Settings** | `admin-configuration.html` + `ui/js/app-config.js` | `.ac-category-group` sections → `.ac-card` blocks | ❌ Divergent — section headers OK; option contents still card-based, audit & convert |
| 4 | **Admin → Users** | `admin-configuration.html` (Access Mode options) | `.ac-card` + `.ac-um-radio*` (radio rows, not toggles) | ❌ Divergent — bespoke radio rows; bring onto the shared row look (radio variant) |
| 5 | **Agent card → Configuration tab** | `ui/js/agents.js` + `ui/css/agents.css` | `.agents-field-group` form layout (not a toggle list) | Form fields, not this pattern — only its toggle rows (if any) should match |
| 6 | **Agent card → Tools tab** | `ui/js/agents.js` + `agents.css` | guardrail/execution checkboxes + per-tool rows | ❌ Divergent — toggle rows should adopt the shared row look |
| 7 | **Agent card → Abilities tab** | `ui/js/agents.js` (`_renderConnectionsTab` / `_buildConnectionCard` / `_renderAbilitiesBlock`) + `agents.css` | `.conn-section` + `.conn-card` (separate-card-per-option) + `.conn-ability-row` (inline-styled) | ❌ **Divergent — the wrong "card-within-a-card" look** (also uses hard-coded hex). Target for migration onto `.ac-abilities-compact` / `.ac-ability-row`. |

**Migration note (agent card, rows 6–7):** these live in `ui/css/agents.css` (the `.conn-*` family, ~lines 1681–1896) and are rendered by `agents.js`. They render each option as its own bordered `.conn-card` and use inline styles + literal hex (`#11112a`, `#7aa2f7`, …), which both breaks the shared look and violates the design-system-variables rule above. To unify, render ability rows inside a single `.ac-abilities-compact` container using `.ac-ability-row` and friends instead of one `.conn-card` per option, and drop the inline hex. When you do this, update this table's State column.

**Migration note (admin pages, rows 2–4):** the section *headers* already use the shared `.ac-category-group` look — leave those. The work is the *contents*: each section currently opens to one `.ac-int-row` / `.ac-card` box per option (see `_compactifyCard` in `app-config.js`), which should become a single `.ac-abilities-compact` row-list like the Agent Tools section. Users' Access-Mode is a radio variant of the same row.

## Lucide icons & icon buttons — the only correct pattern

The app pins **Lucide 0.469.0** (loaded as a global in `index.html`). This version has two traps you must design around: `createIcons({ nodes: [...] })` **ignores the `nodes` filter and rescans the whole document**, and every `<svg>` it generates **keeps its `data-lucide` attribute** (it just also gains the class `lucide`). So any `createIcons()` call re-builds *every* icon on the page into a brand-new DOM node. A central auto-renderer in `ui/js/icons.js` already manages this safely — getting it wrong reintroduces the infinite re-render loop that silently broke the chat **+** button (a button whose whole hit area is an icon never fires `click` if its node is swapped between press and release).

**Rules — follow these everywhere you use a Lucide icon:**

1. **Emit a placeholder, let the central renderer convert it.** In string/innerHTML contexts use the `icon('name', { size })` helper from `ui/js/icons.js`; when building DOM, create an `<i>` and `setAttribute('data-lucide', name)`. Insert it into the DOM and the `MutationObserver` in `icons.js` renders it on the next frame. That observer renders **only** unprocessed placeholders (`[data-lucide]:not(.lucide)`).

2. **Never run a bare `lucide.createIcons()`**, and never call it from your own `MutationObserver` / on every DOM mutation / on an interval. A bare call rebuilds all 150+ icons and, if fired from an observer, self-feeds into a once-per-frame loop. If you must render a specific subtree explicitly, scope it: `lucide.createIcons({ nodes: Array.from(host.querySelectorAll('[data-lucide]:not(.lucide)')) })` (the `:not(.lucide)` guard is mandatory and is the convention used throughout `files.js`).

3. **To change an already-rendered icon's glyph in place, strip the `.lucide` class first**, then re-render that node — otherwise it's treated as "already done" and nothing happens. Plain `el.setAttribute('data-lucide', 'newname')` on a rendered `<svg>` is a **no-op**. Copy the existing pattern: `_setBadgeIcon` in `sessions.js` or `_setActionIcon` in `chat.js` (set attr → `classList.remove('lucide')` → `createIcons({ nodes: [el] })`). Prefer instead to **replace the whole icon by re-inserting a fresh `<i data-lucide>` placeholder** when the surrounding element is rebuilt anyway.

4. **Bind click handlers to the button element, never to the inner icon node.** Icon `<svg>`/`<path>` nodes can be replaced by a render pass, so a listener on them (or a cached reference to them) is unreliable. Attach to the `<button>` directly (like the chevron/delete buttons) or delegate with `e.target.closest('#the-button-id')` on a document capture listener (like `#session-new`). The button element itself persists across icon swaps.

5. **Use valid Lucide 0.469 icon names.** An unknown name (e.g. `users-cog` instead of `user-cog`) leaves an unrendered `<i>` and spams the console with `icon name was not found`. When unsure, verify the name exists in this version before shipping.
