'use strict';

// Drop-in entry for the Source Control admin view. Lazily builds the panel,
// renders the main pane, and live-polls the working tree while the view is on
// screen (unless the sidebar is collapsed to the strip). Logic lives in the
// shared files-git module; this adapter wires its lifecycle.
import { openGitPanel, renderGitMain, startGitAutoRefresh, stopGitAutoRefresh } from '../../shared/js/files-git.js';

export function startView() {
  const sb = document.getElementById('files-sidebar');
  const split = sb && sb.dataset.state !== 'strip';
  try { if (split) openGitPanel(sb); } catch (_) {}
  try { renderGitMain(); } catch (_) {}
  try { if (split) startGitAutoRefresh(sb); } catch (_) {}
}

export function stopView() {
  try { stopGitAutoRefresh(); } catch (_) {}
}
