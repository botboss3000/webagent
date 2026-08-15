/* ========================================================================
 * PART 01/14 - app.01-icons.js            (grep: GENUI-SPLIT)
 * ROLE:  Icon registry + helpers: ICONS map, ic(), fillIcons(). Also
 *        carries the MOUNT RULES contract (read it!) and the shadow-DOM
 *        document shim (window.__genuiDocument guard - do NOT touch).
 * ORDER: Must be part 1 - every later part calls ic()/ICONS.
 * SCOPE: Runs inside the shared IIFE (see index.html breadcrumb). All parts
 *        share ONE scope: never redeclare a top-level name another part
 *        declares; no import/export; 'use strict' comes from the wrapper.
 * EDIT:  Add icons to ICONS, Lucide style (viewBox 0 0 24, fill none,
 *        stroke currentColor, stroke-width ~1.6). No emoji as icons.
 * VERIFY: scripts/check-js-syntax.mjs on this file, then
 *        screenshot_genui('home','both').
 * ======================================================================== */

/* ══ MOUNT RULES ══
 * Delegate event listeners (addEventListener on a persistent container like
 * #detail-zone or #car-track) MUST be bound ONCE at boot — never inside
 * renderCards / renderDetail / or anything called by them. Re-binding inside
 * a render function causes exponential listener growth when a render trigger
 * (like a change event from saved-view restore) itself fires the listener.
 *   Bind once in boot():
 *     bindHeader(); bindComposer(); bindCards(); bindDetailCheckboxes();
 *//* Shadow-DOM compat shim: the genui markup lives in a shadow root. The headless
 * renderer runs this script with the REAL document, so getElementById would miss
 * the shadow root — fall back to the genui root so boot()/renderers resolve their
 * nodes. LIVE sessions must NOT install this shim: the host hands this script a
 * shadow-scoped `document` Proxy (window.__genuiDocument) whose getElementById
 * already resolves inside the page's shadow root. Wrapping it again would write
 * this wrapper onto the real document through the Proxy's set trap, and the
 * Proxy's fallback (real.getElementById) would then call right back into this
 * wrapper — infinite recursion, stack overflow, and the whole app's
 * getElementById throws (no other page on the server loads). */
(function(){
  if(window.__genuiDocument)return; // live session: host's scoped document handles it
  var _orig=document.getElementById.bind(document);
  document.getElementById=function(id){
    var el=_orig(id);
    if(!el){try{var r=window.WebagentGenui&&WebagentGenui.root;if(r&&r.getElementById)el=r.getElementById(id);}catch(e){}}
    return el;
  };
})();
var S='fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"';
var ICONS={layers:'<svg viewBox="0 0 24 24" '+S+'><path d="M12 2 2 7l10 5 10-5-10-5z"/><path d="M2 12l10 5 10-5"/><path d="M2 17l10 5 10-5"/></svg>',plus:'<svg viewBox="0 0 24 24" '+S+'><path d="M12 5v14M5 12h14"/></svg>',folder:'<svg viewBox="0 0 24 24" '+S+'><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',check:'<svg viewBox="0 0 24 24" '+S+'><path d="M20 6 9 17l-5-5"/></svg>','chevron-right':'<svg viewBox="0 0 24 24" '+S+'><path d="M9 6l6 6-6 6"/></svg>',commit:'<svg viewBox="0 0 24 24" '+S+'><circle cx="12" cy="12" r="3"/><path d="M4 12h5M15 12h5"/></svg>',pr:'<svg viewBox="0 0 24 24" '+S+'><circle cx="6" cy="6" r="2.6"/><circle cx="6" cy="18" r="2.6"/><circle cx="18" cy="6" r="2.6"/><path d="M6 8.6v6.8M18 8.6v5.8a2.6 2.6 0 0 1-2.6 2.6H13"/></svg>',calendar:'<svg viewBox="0 0 24 24" '+S+'><rect x="3" y="5" width="18" height="16" rx="2.5"/><path d="M3 10h18"/><path d="M8 3v4M16 3v4"/></svg>',users:'<svg viewBox="0 0 24 24" '+S+'><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',chat:'<svg viewBox="0 0 24 24" '+S+'><path d="M21 15a2 2 0 0 1-2 2H8l-5 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',send:'<svg viewBox="0 0 24 24" '+S+'><path d="M22 2 11 13"/><path d="m22 2-7 20-4-9-9-4z"/></svg>',close:'<svg viewBox="0 0 24 24" '+S+'><path d="M18 6 6 18M6 6l12 12"/></svg>',search:'<svg viewBox="0 0 24 24" '+S+'><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>','chevron-down':'<svg viewBox="0 0 24 24" '+S+'><path d="M6 9l6 6 6-6"/></svg>',trash:'<svg viewBox="0 0 24 24" '+S+'><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M10 11v6M14 11v6"/></svg>','alert-triangle':'<svg viewBox="0 0 24 24" '+S+'><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>',loader:'<svg viewBox="0 0 24 24" '+S+'><path d="M21 12a9 9 0 1 1-6.2-8.6"/></svg>'};
function ic(n,c){return'<span class="ic'+(c?' '+c:'')+'">'+(ICONS[n]||ICONS.folder)+'</span>'}
function fillIcons(root){var els=(root||document).querySelectorAll('[data-icon]');for(var i=0;i<els.length;i++){els[i].innerHTML=ICONS[els[i].getAttribute('data-icon')]||ICONS.folder}}

