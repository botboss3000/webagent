# Plan: AutoAgent — Visual Rendering Tab (Stage 1 COMPLETE)

## Completed

- [x] 1. Create `app/visualizer/__init__.py`, `tool.py`, `SKILL.md`
- [x] 2. `tool.py` — `render_visual(html, title, session_id)` → writes to `visuals/<session>/render.html`
- [x] 3. `__init__.py` — `register_tools(tools, user_id)` injects `render_visual` as ToolInfo
- [x] 4. `loader.py` — import + call `register_tools` with try/except guard
- [x] 5. `main.py` — `os.makedirs("visuals", exist_ok=True)` + `app.mount("/visuals", ...)`
- [x] 6. `SKILL.md` — adapted Hermes p5js skill (creative standards, pipeline, modes)
- [x] 7. Seed p5js row in `context_templates` (local `INSERT OR IGNORE` + supabase lazy seed)
- [x] 8. Add AutoAgent tab HTML to `index.html` — option + tab-content div (prompt input + iframe)
- [x] 9. Create `ui/js/autoagent.js` — iframe manager, prompt submit via SSE, WS event listener, loading/empty/error states
- [x] 10. Wire in `main.js` + `tabs.js` + `agentWs.js` + `sessions.js`
- [x] 11. CSS for autoagent tab (`ui/css/autoagent.css`)
- [x] 12. Add `render_visual` to bootstrap tools list in `prompts.py`
- [x] 13. Update `README.md` — Features, Architecture table, directory tree, frontend, useful URLs
- [x] 14. Add `visuals/` to `.gitignore`

## Files Changed

| File | Change |
|------|--------|
| `app/visualizer/__init__.py` | **New** — registers render_visual tool |
| `app/visualizer/tool.py` | **New** — tool logic, writes HTML to disk |
| `app/visualizer/SKILL.md` | **New** — p5js creative coding skill |
| `app/tools/loader.py` | +7 lines — import + call register_tools guarded |
| `app/main.py` | +8 lines — mkdir visuals + StaticFiles mount |
| `app/db/local.py` | +18 lines — _seed_visualizer_template method |
| `app/db/supabase.py` | +27 lines — _ensure_p5js_template method |
| `app/agent/prompts.py` | +1 word — render_visual in bootstrap list |
| `index.html` | +1 option in tab select + ~20 lines tab content div |
| `ui/js/autoagent.js` | **New** — 220 lines, self-contained |
| `ui/js/agentWs.js` | +3 lines — forward events to autoAgentHandler |
| `ui/js/tabs.js` | +8 lines — activate/deactivate autoagent tab |
| `ui/js/main.js` | +2 lines — import + init |
| `ui/js/sessions.js` | +2 lines — session change handler |
| `ui/css/autoagent.css` | **New** — tab layout, prompt bar, iframe, states |
| `README.md` | Updated Features, Architecture, directory tree, frontend, URLs |
| `.gitignore` | +1 line — visuals/ |

## Stage 2 (Future)

- [ ] Data visualization + 3D/WebGL skill expansions
- [ ] Export button (postMessage to iframe → canvas.save → download PNG)
- [ ] Audio-reactive mode support
- [ ] Parameter sliders below prompt bar
