'use strict';

import { app } from './state.js';
import { CONTROL_MODE_KEY, apiPath } from './config.js';

export async function fetchDbMode() {
  try {
    const res = await fetch(apiPath('/admin/db/mode'));
    if (res.ok) {
      const data = await res.json();
      return data.mode;
    }
  } catch (e) {
    /* server not up yet */
  }
  return null;
}

export async function setDbMode(mode) {
  try {
    const res = await fetch(apiPath('/admin/db/mode'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    });
    if (res.ok) {
      const data = await res.json();
      const s = data.status || {};
      const tableCount = s.tables
        ? Object.values(s.tables).reduce((a, b) => a + (b > 0 ? b : 0), 0)
        : 0;
      app.tStat.textContent = `Mode: ${data.status.mode} (${tableCount} records)`;
      return true;
    }
  } catch (e) {
    console.error('Failed to switch mode:', e);
  }
  return false;
}

export function initDbModeUi() {
  async function applyMode(mode) {
    const isLocal = mode === 'local';
    app.segLocal.classList.toggle('active', isLocal);
    app.segCloud.classList.toggle('active', !isLocal);
    app.segLocal.setAttribute('aria-pressed', isLocal ? 'true' : 'false');
    app.segCloud.setAttribute('aria-pressed', isLocal ? 'false' : 'true');
  }

  fetchDbMode().then(function (mode) {
    if (mode) {
      applyMode(mode);
      localStorage.setItem(CONTROL_MODE_KEY, mode);
    } else {
      const saved = localStorage.getItem(CONTROL_MODE_KEY);
      applyMode(saved === 'cloud' ? 'cloud' : 'local');
    }
  });

  app.segCloud.addEventListener('click', async function () {
    app.segCloud.disabled = true;
    app.segLocal.disabled = true;
    await setDbMode('cloud');
    localStorage.setItem(CONTROL_MODE_KEY, 'cloud');
    applyMode('cloud');
    app.segCloud.disabled = false;
    app.segLocal.disabled = false;
  });

  app.segLocal.addEventListener('click', async function () {
    app.segCloud.disabled = true;
    app.segLocal.disabled = true;
    await setDbMode('local');
    localStorage.setItem(CONTROL_MODE_KEY, 'local');
    applyMode('local');
    app.segCloud.disabled = false;
    app.segLocal.disabled = false;
  });
}
