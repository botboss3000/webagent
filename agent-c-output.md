# Agent C — UI: Parallel Providers Settings

## Files Changed

### `index.html` (1 edit)
Added Parallel Providers section inside the settings modal, between Model picker and Save button.

New elements:
- `#settings-parallel-section` — dark card container
- `#settings-parallel-toggle` — checkbox toggle with On/Off label
- `#settings-parallel-body` — collapsible body (hidden when toggle off)
- `#settings-parallel-rows` — container for per-provider row cards
- `#settings-parallel-add` — "+ Add Provider" button

### `ui/js/settings.js` (8 edits)
1. Added DOM refs for `PARALLEL_TOGGLE`, `PARALLEL_LABEL`, `PARALLEL_BODY`, `PARALLEL_ROWS`, `PARALLEL_ADD`
2. Added state vars: `parallelMode`, `parallelProviders[]`, `parallelUidCounter`, `parallelModelData{}`
3. Added toggle+add event wiring in `initSettings()`
4. Added `initParallelDropdownClose()` call in `initSettings()`
5. Updated `openSettings()` to call `loadMultiProviders()` + `applyParallelUIState()`
6. Added `applyParallelUIState()` function
7. Updated `closeSettings()` to reset parallel state on close
8. Updated `saveSettings()` to call `saveMultiProviders()` + reload parallel state
9. Added all parallel functions:
   - `_nextUid()` — unique ID generation
   - `loadMultiProviders()` — GET /admin/settings/multi-providers
   - `saveMultiProviders()` — POST /admin/settings/multi-providers
   - `renderParallelRows()` — builds per-row cards with provider select, URL, API key, model picker
   - `fetchModelsForRow()` — fetches models for a specific provider row
   - `addParallelRow()` — adds a new blank row
   - `removeParallelRow()` — removes row, auto-disables toggle if <2 rows
   - `initParallelDropdownClose()` — document click handler to close model dropdowns

## Integration Points

**Save flow:** Existing `saveSettings()` now calls `await saveMultiProviders()` after the single-provider save, then reloads both configs.

**Load flow:** Existing `openSettings()` calls `await loadMultiProviders()` after `loadSettings()`, then `applyParallelUIState()` to render.

**Backend API expected:**
- `GET /admin/settings/multi-providers` → `{ parallel_mode, providers: [...] }`
- `POST /admin/settings/multi-providers` ← `{ parallel_mode, providers: [...] }`

## State Lifecycle
- On modal open: fetch from server, populate state, render
- On save: persist both single + parallel config to server, re-render
- On close: full state reset, next open is clean
- Toggle: show/hide provider rows, auto-add one row if empty
- Remove last row: auto-disable toggle, hide body

## API Key Handling
- Keys stored plaintext per row (matching existing behavior)
- Keys persist across modal open/close within same session
- Server responsible for masking if needed on GET

## Risks
- Depends on Agent A implementing GET/POST `/admin/settings/multi-providers` backend endpoints
- Model fetch per row uses `/admin/settings/models` with optional `api_key` and `base_url` query params — Agent A must support these params
- If server returns 404 on `/admin/settings/multi-providers`, parallel features silently disabled (try/catch)
