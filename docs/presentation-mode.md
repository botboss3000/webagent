# Presentation Mode — what it is and how to remove it

Presentation mode is a **read-only demo path** through the Admin Tools area for
anonymous and registered non-admin visitors. Its purpose is to let the app
developer (i.e. the maintainer of the upstream repo) show off the admin
features to potential users without granting them edit access.

When `presentation_mode` is on in `app-settings.json`:

- The **Admin Tools** main tab appears for non-admin visitors.
- A blue banner at the top of Admin Tools says *"Presentation mode — you're
  viewing Admin Tools as a read-only demo. Sign in as an admin (or fork this
  app to your own repo) to enable full editing."*
- All mutating buttons and inputs inside Admin Tools and the Agents tab are
  disabled. Clicking one shows a floating tooltip near the cursor: *"Read-only
  — presentation demo. Fork this app to your own repo to enable editing."*
- Sub-views (file manager, database, terminal, source control, interactions,
  runtime loop) render their **normal** layout. Endpoints they call are
  unchanged: per-user-scoped reads (database, interactions, runtime, user
  profile) keep returning real data; admin-only endpoints (terminal WS,
  source control writes, integrations config, LLM keys, file content read)
  still 403 → the UI shows empty states.
- The User Management card renders a **single self row** built from the
  visitor's own profile (no leakage of other users' data) plus a notice that
  other rows are hidden.
- The Agent abilities tab is browseable; search works; the AI chat bar +
  toggles are disabled.
- The file-tree LISTING endpoint (`GET /admin/files/tree`) is loosened so the
  file manager shows the real project tree. File contents (`GET /read`) and
  all file mutations (`/write`, `/create`, `/rename`, `/delete`) **stay
  admin-only**. Non-admin demo browsers are additionally clamped to paths
  under the project root so they cannot navigate to `/etc`, `/home`, etc.

## Toggling it on/off

Sign in as admin → Admin Tools → Settings → User Management → Access Mode
card → check or uncheck **"Presentation mode (read-only admin demo)"** → Save.

## Removing it entirely (clone-time)

Every code change for presentation mode is wrapped in marker comments. To
remove it completely, `grep -rn "PRESENTATION-MODE"` from the repo root and
delete the marked blocks. Touch points:

### New files (delete outright)
- `ui/js/presentation-mode.js`
- `docs/presentation-mode.md` (this file)

### Backend
- `app/admin/settings.py` — delete the `presentation_mode: bool = False`
  field from `AppSettings` (one marked block).
- `app/auth/__init__.py` — delete the `presentation_mode` field from
  `AccessModeResponse` and revert the `/access-mode` endpoint to the
  one-line `return AccessModeResponse(access_mode=_gam())` form.
- `app/api/files.py` — delete the `_require_admin_or_presentation_read`
  helper, the path-clamping block inside `/tree`, and swap the gate on
  `GET /tree` back to `await _require_admin(request)`.

### Frontend
- `ui/js/left-login.js` — delete the `_presentationMode` cache, the
  `isPresentationMode()` export, the assignment in `fetchAccessMode`, the
  event-detail key, and the matching block in the `access-mode-changed`
  listener.
- `ui/js/main.js` — drop the `fetchAccessMode`/`isPresentationMode` imports
  and the marked changes inside `_applyAdminToolsVisibility()`; remove the
  `access-mode-loaded` listener and the `fetchAccessMode()` call inside the
  `_anonReady.then()` block.
- `ui/js/files.js` — drop the two presentation-mode imports at the top,
  the `presentationViewer` check inside `startAdminTools`, the
  `enablePresentationMode` call, and the `applyPresentationGate` call at
  the bottom of `applySidebarView`.
- `ui/js/app-config.js` — drop the `isPresentationMode` import, the
  checkbox load/save lines, the `_renderSelfOnlyUsersList` function, and
  the `presentation_mode` key from the `access-mode-changed` event detail.
- `ui/js/agents.js` — drop the two imports at the top, revert `canEdit` to
  `userRole === 'admin'`, and remove the `applyPresentationGate` call at
  the bottom of `_renderConnectionsTab`.
- `ui/admin-tools/admin-configuration.html` — delete the marked checkbox
  block (between `<!-- PRESENTATION-MODE START -->` and the matching END).
- `ui/css/files.css` — delete the marked CSS block at the bottom.

### Verification of removal

After cleanup, run:

```
grep -rn "PRESENTATION-MODE\|presentation_mode\|isPresentationMode\|presentation-mode" .
```

If the grep returns no results, the removal is complete. The app should
behave exactly as before the feature was introduced: Admin Tools tab is
hidden for non-admins, no banner, no floating tooltip, no demo affordances.
