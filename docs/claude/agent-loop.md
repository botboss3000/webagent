# Agent loop guidance (read before changing loop nodes)

Read this before adding, removing, or renaming any node in the agent loop pipeline or its diagram.

## Agent loop diagram — keep both views in sync

The agent loop diagram (`ui/js/loop-diagram.js`) has **two completely independent, hardcoded layouts**. Adding or changing any node requires updating **both**:

| Layout | Location in file | Description |
|--------|-----------------|-------------|
| Horizontal | `LOOP_NODES` array + `LOOP_EDGES` | Used when the diagram is zoomed out |
| Vertical | Inline array inside `buildVerticalLayout()` | Used when the diagram is zoomed in |

Neither layout is derived from the other. A node missing from one view simply won't render there.

**Full checklist for adding a new loop node:**

1. `LOOP_NODES` array — add node with correct `cx`, `cy`, `hw`, `hh`
2. `LOOP_EDGES` — wire in/out edges
3. `_H_GROUPS` — add node ID to the correct stage group (controls horizontal compression)
4. `buildVerticalLayout()` inline array — add node with correct `cy`, `hw`, `hh`; shift all subsequent `cy` values down; extend stage band `y2`; update `canvasH`
5. Update the comment at the top of `buildVerticalLayout()` with the new node count
6. `ui/js/loop-logic.js` `eventToNodeId()` — add a `case` mapping the pipeline event name to the node ID
7. `ui/loop-nodes.json` — add entries to `NODE_STATIC_ITEMS` and `NODE_PANEL_INFO`
8. `ui/js/agents.js` `_INFO_NODES` set — add node ID if it should show info-only panel (no edit bar)
9. `app/agent/loop_executor.py` `DEFAULT_NODE_ORDER` list — add node in the correct position
10. All agent JSON templates in `data/agents/` — add node ID to each `loop_logic` array
11. Any existing agents' `loop_logic` DB field — update via migration or seed script

## The `attachment_describe` node is a capability-driven type-router

`attachment_describe` (in `app/api/chat.py` `_maybe_describe_images`) is no longer
image-specific glue — it is the **attachment type-router**. It maps each attached
file to the ability that handles its mime-type (today: images → Image Vision; future
document/video abilities drop in the same way) via `app/agent/attachment_router.py`,
then picks a path from the agent's *actual* model capability
(`media_routing` / `model_sees_images` in `app/admin/settings.py`):

- **inline** — the turn model can see the media → leave it inlined;
- **describe** — it can't, but the handling ability + a capable worker model exist →
  one-shot describe (synchronous), folded into the user turn with guidance;
- **unreadable** — the ability is off or no capable model is configured → DO NOT
  inline; the "you can't read this; tell the user, don't hallucinate" directive is
  given to the model for that turn **and surfaced as a foldable `route_attachment`
  tool row** (carrying the reason: `ability_disabled` vs `no_vision_model`), but it
  is **not** written into the user's message row — so it never shows in the chat
  bubble. The directive's wording attributes the gap to the *ability being off / no
  vision model*, not to a model limitation. Same rule for the **describe** path's
  guidance *note*: the image description is persisted into the turn (kept across
  turns), but the agent-directive note is delivered to the model only — not the bubble.

A new file-type ability adds itself by shipping a companion `<ability>.json`
(`handles` + `worker_system` + `guidance`) next to its `.py` — **no edit to this
node**. The node id and its pipeline events (`attachment_describe_start/end`) are
unchanged, so the loop diagram needs no update.
