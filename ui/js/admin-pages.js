// Drop-in admin-tools page loader.
//
// Makes Admin Tools sub-pages truly drop-in: a page is a folder under
// ui/admin-tools/<dir>/ carrying a page.json manifest plus its own HTML
// partial, CSS and JS. The backend (GET /api/v1/admin-tools/pages) scans those
// folders; this module fetches the list and, for each page:
//   1. injects its strip icon button into the sidebar strip,
//   2. injects its <template data-slot> blocks into the admin-tools shell,
//   3. links its stylesheet,
//   4. dynamically imports its JS module (lifecycle: start / stop /
//      renderSidebar),
// then records it in `dropinAdminPages` so files.js can switch to it and drive
// its lifecycle generically — with NO per-page edits to files.js or
// partial-loader.js. Add a folder → the page appears. Delete it → it's gone.
//
// page.json fields: { id, view, title, switch, icon, order, html, css, js,
//                     main }. `view` defaults to id; `main` (the <main> id)
// defaults to `files-<view>-main`.

import { apiPath } from './config.js';

// view -> { dir, mainId, title, switchLabel, icon, start, stop, renderSidebar }
export const dropinAdminPages = new Map();

function _injectSlots(html, sourceUrl) {
  const wrapper = document.createElement('div');
  wrapper.innerHTML = html;
  const templates = wrapper.querySelectorAll('template[data-slot]');
  for (const tpl of templates) {
    const target = document.querySelector(tpl.getAttribute('data-slot'));
    if (target) target.appendChild(tpl.content.cloneNode(true));
    else console.warn(`[admin-pages] slot ${tpl.getAttribute('data-slot')} not found (from ${sourceUrl})`);
  }
}

function _injectStripButton(p) {
  const strip = document.querySelector('.files-sidebar-strip');
  if (!strip) return;
  if (strip.querySelector(`.files-strip-view[data-view="${p.view}"]`)) return;  // already there
  const btn = document.createElement('button');
  btn.className = 'files-strip-btn files-strip-view';
  btn.dataset.view = p.view;
  btn.title = p.title || p.view;
  btn.innerHTML = `<i data-lucide="${p.icon || 'square'}" class="lucide-icon"></i>`;
  strip.appendChild(btn);
}

function _linkCss(href) {
  if (document.querySelector(`link[data-admin-page-css="${href}"]`)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = href;
  link.setAttribute('data-admin-page-css', href);
  document.head.appendChild(link);
}

export async function loadDropinAdminPages() {
  let pages = [];
  try {
    const res = await fetch(apiPath('/api/v1/admin-tools/pages'), { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    pages = await res.json();
  } catch (e) {
    console.warn('[admin-pages] discovery failed (no drop-in pages loaded):', e);
    return;
  }

  for (const p of (pages || [])) {
    try {
      const view = p.view || p.id;
      if (!view) continue;
      const dir = p.dir || p.id;
      const base = apiPath(`/ui/admin-tools/${dir}/`);

      _injectStripButton({ ...p, view });

      if (p.html) {
        const url = base + p.html;
        const html = await fetch(url, { cache: 'no-store' }).then((r) => r.ok ? r.text() : '');
        if (html) _injectSlots(html, url);
      }
      if (p.css) _linkCss(base + p.css);

      let mod = {};
      if (p.js) {
        try { mod = await import(base + p.js); }
        catch (e) { console.warn(`[admin-pages] failed to import ${dir}/${p.js}:`, e); }
      }

      dropinAdminPages.set(view, {
        dir,
        mainId: p.main || `files-${view}-main`,
        title: p.title || view,
        switchLabel: p.switch || (p.title || view).toLowerCase(),
        icon: p.icon || 'square',
        start: typeof mod.start === 'function' ? mod.start : null,
        stop: typeof mod.stop === 'function' ? mod.stop : null,
        renderSidebar: typeof mod.renderSidebar === 'function' ? mod.renderSidebar : null,
      });
    } catch (e) {
      console.warn('[admin-pages] failed to load page', p, e);
    }
  }
}
