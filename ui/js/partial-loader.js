// Fetches HTML partials at boot and injects them into placeholder slots in
// index.html before the main JS modules run. Two-phase load:
//   Phase 1: top-level tab pages (Pages / Agents / Admin Tools) — each
//            partial's body replaces the inner HTML of its mount.
//   Phase 2: admin sub-pages — each partial uses <template data-slot="...">
//            blocks whose content is appended into the matching selector
//            in the parent document (the slots live inside the admin-tools
//            shell loaded in phase 1).
//
// Resolves the exported `partialsReady` promise once all partials are in
// the DOM, after which the bootstrap in index.html dynamically imports the
// main JS modules so every getElementById call still resolves.

const TOP_LEVEL = [
  { url: './ui/pages.html', mount: '#tab-autoagent' },
  { url: './ui/agents.html', mount: '#tab-agents' },
  { url: './ui/web.html', mount: '#tab-web' },
  { url: './ui/wiki.html', mount: '#tab-wiki' },
  { url: './ui/admin-tools.html', mount: '#tab-admin-tools' },
  { url: './ui/chat.html', mount: '#chat-panel' },
];

const ADMIN_SUB_PAGES = [
  './ui/admin-tools/admin-configuration.html',
  './ui/admin-tools/database.html',
  './ui/admin-tools/file-manager.html',
  './ui/admin-tools/terminal.html',
  './ui/admin-tools/source-control.html',
  './ui/admin-tools/interactions.html',
  './ui/admin-tools/runtime.html',
  './ui/admin-tools/diagnostics.html',
];

async function fetchHtml(url) {
  const resp = await fetch(url, { cache: 'no-store' });
  if (!resp.ok) throw new Error(`Failed to fetch ${url}: ${resp.status}`);
  return resp.text();
}

function injectIntoMount(html, mountSelector) {
  const mount = document.querySelector(mountSelector);
  if (!mount) throw new Error(`Mount not found: ${mountSelector}`);
  mount.innerHTML = html;
}

function injectSlotsFromHtml(html, sourceUrl) {
  // Parse as a document fragment to access top-level <template data-slot>.
  const wrapper = document.createElement('div');
  wrapper.innerHTML = html;
  const templates = wrapper.querySelectorAll('template[data-slot]');
  if (!templates.length) {
    throw new Error(`No <template data-slot> blocks found in ${sourceUrl}`);
  }
  for (const tpl of templates) {
    const sel = tpl.getAttribute('data-slot');
    const target = document.querySelector(sel);
    if (!target) {
      throw new Error(`Slot target "${sel}" not found (from ${sourceUrl})`);
    }
    target.appendChild(tpl.content.cloneNode(true));
  }
}

export const partialsReady = (async () => {
  // Phase 1: load top-level pages in parallel.
  const topHtml = await Promise.all(TOP_LEVEL.map(p => fetchHtml(p.url)));
  TOP_LEVEL.forEach((p, i) => injectIntoMount(topHtml[i], p.mount));

  // Phase 2: load admin sub-pages in parallel (slots now exist).
  const subHtml = await Promise.all(ADMIN_SUB_PAGES.map(fetchHtml));
  ADMIN_SUB_PAGES.forEach((url, i) => injectSlotsFromHtml(subHtml[i], url));

  // Render lucide icons for the freshly injected DOM.
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    window.lucide.createIcons();
  }
})();
