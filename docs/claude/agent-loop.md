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
