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

**Two separate patterns, don't conflate them.** There's (a) the **collapsible section header** — chevron + icon + title that opens to reveal a group — which *is* still used elsewhere on the admin pages (`.ac-category-group`, e.g. App Settings and the Models configurator), and (b) the **compact option-row list** *inside* such a section (`.ac-abilities-compact`). A page can have the right headers but still have the **wrong content** inside them.

**Agent Settings is now one single flush table.** Its former collapsible categories (Channels, Productivity, Social Media, Marketplaces, Payments, Developer, CRM) have been folded into the *same* `#ac-abilities-compact` list as the host-ability groups — each former category is now an **expandable group row** sitting beside Administrator / Core / Web (see `_INTEGRATION_GROUPS` + `_buildIntegrationGroupShells` / `_placeIntegrationGroupCards` in `app-config.js`). The Channels category became the **Communication** group. The only thing left as its own collapsible section on that tab is **Models** — and its body now *also* renders as the shared compact-table component: a 4-row **configurator table** (`.ac-list.ac-model-config`: Provider / Base URL / API Key / Model) whose fields **auto-save on change** (each shows the shared `.agents-autosave-check` green tick, same as the agent abilities table — no Save button; the former "Save" is now an **Add** button that appends the configured model to the saved list), plus a bare **Saved Models table** (`.ac-list`) of expandable rows (three capability columns Text / In / Out; click a row to reveal token usage + cost, lazily fetched from `GET /admin/settings/model-info`). Per-model capabilities are **detected** from the model catalog and shown as read-only `.ac-cap-badge`s — they are not hand-entered checkboxes. The legacy category `<details>` are emptied and hidden at runtime, not deleted from the HTML.

### Canonical implementation — copy this, don't re-invent

The whole component lives in **`ui/css/app3.css`** under the boxed comment **"SHARED EXPANDABLE OPTION-ROW LIST"**. Markup contract:

- **`.ac-list`** — the bordered, rounded container (the flush "table"). The container draws the hairline dividers between items (`.ac-list > * + *`), so a bare row and an expandable row line up identically. `.ac-abilities-compact` is the legacy alias of `.ac-list` and shares its rules (the Agent Tools reference still uses it).
- **`.ac-ability-row`** — one always-visible row line: `.ac-ability-icon` + `.ac-ability-label` (→ `.ac-ability-name` bold + `.ac-ability-desc` muted) + optional `.ac-ability-status` + optional `.ac-ability-toggle-wrap` (the toggle) + optional `.ac-row-chevron` (only when expandable).
- **`.ac-row`** — wraps a row + its body to make it **expandable**; JS adds `.expanded` to open it. Use `.ac-row-static` for a non-expanding item.
- **`.ac-ability-body`** — the collapsible region revealed on expand (additional info and/or config fields: OAuth, scopes, tokens…). Hidden until the wrapping `.ac-row` has `.expanded`.

Light-mode overrides (the peach tint) sit in the matching `body.light-mode` rules right below. The collapsible *section header* that holds a whole list (chevron + icon + title) is the separate `.ac-category-group` / `.ac-category-summary` block (see its "SHARED COLLAPSIBLE SECTION PATTERN" comment). Everything uses **design-system CSS variables** for colour (no hard-coded hex except the light-mode peach overrides) — keep it that way.

> **Restyle the toggle-list look in `app3.css` only.** Don't fork a per-page or per-panel copy. Every conformant area below reads from this single block so the whole app stays coherent. The agent card keeps its `.conn-*` class names purely as JS/behaviour hooks (their visual rules are gutted in `agents.css`); the admin integration cards are grouped into one `.ac-list` by `_groupIntegrationLists()` in `app-config.js`.

### It's not just for simple toggle rows — the row is a flexible shell

The compact table is the right home for **any "category → list of options" surface**, not only on/off toggles. The row shell (icon + name + description on the left, with the rest of the row free) accepts whatever control or affordance the option needs. Reach for it whenever options share a category, even when each option does something richer than flip a switch:

- **Toggle rows** — the default (a switch on the right). *Agent Tools, integrations.*
- **Other controls on the right** — a row's control can be a **radio**, a **dropdown/select**, a **button**, a small **number/text input**, or a **status pill / "Coming soon" badge** instead of a toggle. Same row, different right-hand element.
- **Expandable rows** — wrap the row in `.ac-row` and give it an `.ac-ability-body`; clicking the row opens it to reveal **more detail and/or the configuration** (OAuth login, scopes, a dropdown, token fields, a longer explanation). Use this when a control or its detail is too big for the collapsed line — put the **control itself in the expanded body** (e.g. a settings dropdown with its hint) and keep the collapsed line to name + one-line summary.
- **Reorderable rows** — a row can carry a **drag handle** and be `draggable` so the list doubles as a drag-to-reorder list, while still showing each row's control (e.g. a visibility toggle). The flush list reads as one table; drag still works row-to-row. *Main Panel Pages.*
- **Locked / disabled / system rows** — a row can be non-interactive (locked, "always on", admin-only) and just render the state; keep it in the same list so the set stays visually unified.
- **Grouped rows (a group header + its members in the SAME list)** — when options fall into named groups, the group itself is an **expandable header row** (`.ac-row.ac-group` → `.ac-group-head` head + `.ac-group-body` members) *inside the one `.ac-list`* — never a nested bordered box. The members are plain `.ac-ability-row`s revealed on expand, sitting on a faintly recessed, indented surface so they read as children without becoming a second table. The group head carries a **3-position toggle** `.ac-tri` (Off · Mixed · On): clicking the left half turns every member off, the right half turns every member on, and the centre "mixed" detent is **derived** (set in code when members are partially on) — you reach it by toggling individual members, not by clicking it. The whole group/tri component lives in `app3.css` alongside the row component. *Agent Tools (Admin → Agent Settings) groups its 13 host abilities into Administrator / Core / Web (`_ABILITY_GROUPS`), **plus** the former integration categories as further group rows in the same table — Communication, Productivity, Social Media, Marketplaces, Payments, Developer, CRM (`_INTEGRATION_GROUPS`). The per-agent Abilities tab mirrors the host grouping — see `_AGENT_ABILITY_GROUPS` / `_buildAbilityGroupsGrid` in `agents.js`. Credential-backed members (Web Scraper, Browser Cookies, and every OAuth provider) keep their own config body; their group's tri-toggle only **reflects** how many are configured and bulk-**off** un-configures them — it can't bulk-enable an OAuth app that still needs its keys. Coming-soon members never count, and a group with no available members (Payments / Developer / CRM) shows no tri-toggle at all.*

> **Sister panels — keep the two ability tables MIRRORED.** The admin **Agent Settings** table and the per-agent **Abilities tab** (agent card) are two renderings of the *same* table design. They must stay mirrored: any change to one side's look, structure, grouping, or toggle behaviour must be applied to the other in the same change. Both code sites carry the embedded banner **`SISTER-PANEL: AGENT-ABILITY-TABLE`** — grep it to jump between the admin side (`app-config.js`) and the agent-card side (`agents.js`). This is also called out as an always-on essential in `CLAUDE.md`.

**When NOT to use it:** a surface that's a genuine **form** — a set of free text/number fields the user fills in, with no per-option category structure — should stay a form, not be forced into rows. Likewise a panel that's really **one complex configurator** (many interdependent fields, live status, action buttons) is not a list of options. *Example: Remote Access in App Settings stays as its own panel.* Rule of thumb: if you'd naturally describe it as "a list of things, each of which can be turned on / chosen / reordered," it's a compact table; if it's "a single thing with many fields," it isn't.

### Every area that uses (or should use) this pattern

When the toggle-list design changes, check **all** of these so the change lands everywhere at once.

- ✅ **Conformant** = on the shared `.ac-list` / `.ac-row` / `.ac-ability-*` component.
- ⏸️ **Left as-is (by decision)** = not converted on purpose — either it's not a list, or the owner chose to leave it.
- ❌ **Divergent** = still a bespoke look that *should* be migrated.

| # | Area | Where | Classes / source | State |
|---|------|-------|------------------|-------|
| 1 | **Admin → Agent Settings (the whole tab)** | `ui/admin-tools/admin-configuration.html` + `app-config.js` (`_initAbilitiesCompact`) | `.ac-abilities-compact` (= `.ac-list`) holding host groups + `_INTEGRATION_GROUPS` group rows | ✅ Conformant — canonical reference; one flush table of expandable groups (the Models section now also renders as two compact `.ac-list` tables — see row 8) |
| 8 | **Admin → Agent Settings — Models** (configurator + saved models) | `admin-configuration.html` + `app-config.js` (`_initLLM` / `_renderParallelRows` / `_loadSavedModelDetail`) | `.ac-list.ac-model-config` (4 auto-saving field rows) + `.ac-list` saved-models table of expandable `.ac-row`s | ✅ Conformant — fields auto-save with the shared green tick; detected capabilities shown as `.ac-cap-badge`s (not checkboxes); rows expand for token/cost via `/admin/settings/model-info` |
| 2 | **Admin → Agent Settings — integration groups** (Communication, Productivity, Social Media, Marketplaces, Payments, Developer, CRM) | `admin-configuration.html` + `app-config.js` (`_compactifyCard` → `_placeIntegrationGroupCards`) | `.ac-row.ac-group` rows whose bodies hold `.ac-card-compact` rows + coming-soon rows | ✅ Conformant — folded into the row-1 table as group rows (no longer separate category sections) |
| 3 | **Admin → App Settings** (Main Panel Pages, Startup & Boot) | `admin-configuration.html` + `app-config.js` (`_renderMainPanelList`, `_wireBootRow`) | Main Panel: `.ac-list` + `.ac-ability-row` rows w/ drag handle. Boot: `.ac-row` expandable rows, dropdown in `.ac-ability-body`. | ✅ Conformant — **Remote Access intentionally stays its own panel** (a complex configurator, not a list) |
| 4 | **Admin → Users** | `admin-configuration.html` (Access Mode options) | `.ac-card` + `.ac-um-radio*` | ⏸️ Left as-is by decision (could become a radio-variant list later) |
| 5 | **Agent card → Configuration tab** | `ui/js/agents.js` + `ui/css/agents.css` | `.agents-field-group` form layout | ⏸️ Form fields, not this pattern |
| 6 | **Agent card → Tools tab** | `ui/js/agents.js` + `agents.css` | guardrail/execution checkboxes + per-tool rows | ⏸️ Left as-is by decision (per-tool list could adopt the row look later) |
| 7 | **Agent card → Abilities tab** | `ui/js/agents.js` (`_renderConnectionsTab` / `_buildConnectionCard` / `_renderAbilitiesBlock`) + `agents.css` | `.conn-grid.ac-list` + `.conn-card.ac-row` heads + `.conn-fields.ac-ability-body` | ✅ Conformant — `.conn-*` kept as JS hooks; look comes from the shared component |

**The shared component** is `.ac-list` / `.ac-row` / `.ac-ability-*` in `app3.css`. Notes on how each conformant area plugs in:
- **Agent Settings (1)** — the original simple-toggle reference; the host-ability groups (Administrator / Core / Web) are built by `_initAbilitiesCompact` into `#ac-abilities-compact`.
- **Integration groups (2)** — `_compactifyCard` makes each card a compact row; `_buildIntegrationGroupShells` adds the group shells into the *same* `#ac-abilities-compact` table and `_placeIntegrationGroupCards()` relocates each card into its group body (then hides the emptied legacy category `<details>`).
- **App Settings (3)** — Main Panel Pages is a reorderable variant (each row carries a drag handle, the list is `draggable`); Startup & Boot is an expandable variant (collapsed line shows the current choice via `.ac-ability-status`, the dropdown sits in the expanded `.ac-ability-body`, wired by `_wireBootRow`).
- **Abilities tab (7)** — keeps its `.conn-*` names as JS hooks alongside the shared classes; visual `.conn-*` rules gutted in `agents.css`.

**Optional future work (rows 4, 6 — currently left as-is by decision):**
- **Users (4):** the Access-Mode radio options could become `.ac-ability-row`s in an `.ac-list` (a radio variant — bold name + description + radio on the right).
- **Tools tab (6):** the per-tool exposure list could render inside an `.ac-list` using `.ac-ability-row` (the guardrail/execution settings above it are a form and should stay one).

## Lucide icons & icon buttons — the only correct pattern

The app pins **Lucide 0.469.0** (loaded as a global in `index.html`). This version has two traps you must design around: `createIcons({ nodes: [...] })` **ignores the `nodes` filter and rescans the whole document**, and every `<svg>` it generates **keeps its `data-lucide` attribute** (it just also gains the class `lucide`). So any `createIcons()` call re-builds *every* icon on the page into a brand-new DOM node. A central auto-renderer in `ui/js/icons.js` already manages this safely — getting it wrong reintroduces the infinite re-render loop that silently broke the chat **+** button (a button whose whole hit area is an icon never fires `click` if its node is swapped between press and release).

**Rules — follow these everywhere you use a Lucide icon:**

1. **Emit a placeholder, let the central renderer convert it.** In string/innerHTML contexts use the `icon('name', { size })` helper from `ui/js/icons.js`; when building DOM, create an `<i>` and `setAttribute('data-lucide', name)`. Insert it into the DOM and the `MutationObserver` in `icons.js` renders it on the next frame. That observer renders **only** unprocessed placeholders (`[data-lucide]:not(.lucide)`).

2. **Never run a bare `lucide.createIcons()`**, and never call it from your own `MutationObserver` / on every DOM mutation / on an interval. A bare call rebuilds all 150+ icons and, if fired from an observer, self-feeds into a once-per-frame loop. If you must render a specific subtree explicitly, scope it: `lucide.createIcons({ nodes: Array.from(host.querySelectorAll('[data-lucide]:not(.lucide)')) })` (the `:not(.lucide)` guard is mandatory and is the convention used throughout `files.js`).

3. **To change an already-rendered icon's glyph in place, strip the `.lucide` class first**, then re-render that node — otherwise it's treated as "already done" and nothing happens. Plain `el.setAttribute('data-lucide', 'newname')` on a rendered `<svg>` is a **no-op**. Copy the existing pattern: `_setBadgeIcon` in `sessions.js` or `_setActionIcon` in `chat.js` (set attr → `classList.remove('lucide')` → `createIcons({ nodes: [el] })`). Prefer instead to **replace the whole icon by re-inserting a fresh `<i data-lucide>` placeholder** when the surrounding element is rebuilt anyway.

4. **Bind click handlers to the button element, never to the inner icon node.** Icon `<svg>`/`<path>` nodes can be replaced by a render pass, so a listener on them (or a cached reference to them) is unreliable. Attach to the `<button>` directly (like the chevron/delete buttons) or delegate with `e.target.closest('#the-button-id')` on a document capture listener (like `#session-new`). The button element itself persists across icon swaps.

5. **Use valid Lucide 0.469 icon names.** An unknown name (e.g. `users-cog` instead of `user-cog`) leaves an unrendered `<i>` and spams the console with `icon name was not found`. When unsure, verify the name exists in this version before shipping.
