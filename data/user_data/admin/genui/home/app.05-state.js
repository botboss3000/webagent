/* ========================================================================
 * PART 05/14 - app.05-state.js            (grep: GENUI-SPLIT)
 * ROLE:  In-memory STATE (projects, activeId, openItem, itemQ, sessions)
 *        + localStorage persistence via GenUIState (key genui:home:state),
 *        recalcProject() and friends.
 * ORDER: Part 5 - renderers (08), interactions (09), QA (10) and live (06)
 *        all read/write STATE.
 * EDIT:  State shape is load-bearing: loadPageState/savePageState are also
 *        called by boot (14) and live (06). Keep the key genui:home:state.
 * VERIFY: syntax-check, then screenshot_genui('home','both').
 * ======================================================================== */

/* ── LOCAL STATE — genui:<slug>:state (standard) ──
 * Persists the page's semantic state (which project is open, which item is
 * expanded, the item search) so a reopen restores exactly where the user
 * left off. Page scroll is persisted by the genui host automatically; we
 * also save it here so the page is self-sufficient anywhere it runs.
 * Canonical snippet + spec:
 *   ui/main-panel/genui/genui-state.standard.js
 *   docs/claude/genui-state-standard.md
 */
var SLUG='home';
var GenUIState=(function(){
  function key(slug){return'genui:'+slug+':state'}
  function load(slug){try{var o=JSON.parse(localStorage.getItem(key(slug))||'null');return o&&typeof o==='object'?o:{}}catch(e){return{}}}
  function save(slug,state){try{var o={};for(var k in state){var v=state[k];if(v!==null&&v!==undefined)o[k]=v}if(Object.keys(o).length)localStorage.setItem(key(slug),JSON.stringify(o));else localStorage.removeItem(key(slug))}catch(e){}}
  function clear(slug){try{localStorage.removeItem(key(slug))}catch(e){}}
  function hostEl(){try{return document.getElementById('genui-host')}catch(e){return null}}
  function scrollY(){var h=hostEl();try{return h?(h.scrollTop||0):(window.pageYOffset||document.documentElement.scrollTop||0)}catch(e){return 0}}
  function scrollX(){var h=hostEl();try{return h?(h.scrollLeft||0):(window.pageXOffset||document.documentElement.scrollLeft||0)}catch(e){return 0}}
  function restoreScroll(y,sl){if(!y&&!sl)return;function go(){try{var h=hostEl();if(h){if(sl&&Math.abs(h.scrollLeft-sl)>2)h.scrollLeft=sl;if(y&&Math.abs(h.scrollTop-y)>2)h.scrollTop=y}else{window.scrollTo(sl||0,y)}}catch(e){}}go();[80,200,400,800].forEach(function(d){setTimeout(go,d)})}
  return{key:key,load:load,save:save,clear:clear,scrollY:scrollY,scrollX:scrollX,restoreScroll:restoreScroll};
})();
/* One-time migration from the pre-standard keys to the genui:home:state blob. */
(function(){try{
  var oldP=localStorage.getItem('pd-active-project'),oldI=localStorage.getItem('pd-active-item');
  if((oldP||oldI)&&!localStorage.getItem(GenUIState.key(SLUG))){
    var o={};if(oldP)o.activeId=oldP;
    if(oldI){try{var it=JSON.parse(oldI);if(it&&it.p!==undefined&&it.i!==undefined)o.openItem={p:it.p,i:it.i}}catch(_){}}
    GenUIState.save(SLUG,o);
  }
  localStorage.removeItem('pd-active-project');localStorage.removeItem('pd-active-item');
}catch(e){}})();
function savePageState(){
  var o={};
  if(STATE.activeId)o.activeId=STATE.activeId;
  if(STATE.openItem)o.openItem=STATE.openItem;
  if(STATE.itemQ)o.itemQ=STATE.itemQ;
  if(STATE.itemSearchOpen)o.itemSearchOpen=STATE.itemSearchOpen;
  if(STATE.view==='feature')o.view='feature';
  o.scroll=GenUIState.scrollY();o.scrollLeft=GenUIState.scrollX();
  GenUIState.save(SLUG,o);
}
function loadPageState(){
  var s=GenUIState.load(SLUG);
  STATE.activeId=s.activeId||null;
  STATE.openItem=s.openItem||null;
  STATE.itemQ=s.itemQ||'';
  STATE.itemSearchOpen=!!s.itemSearchOpen;
  STATE.view=s.view==='feature'?'feature':'project';
  return s;
}

