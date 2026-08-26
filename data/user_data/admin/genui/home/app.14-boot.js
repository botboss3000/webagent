/* ========================================================================
 * PART 14/14 - app.14-boot.js             (grep: GENUI-SPLIT)
 * ROLE:  boot(): fillIcons, brand icon, loadPageState restore, bind* all,
 *        renderCards/renderDetail, GenUIState.restoreScroll, live-data
 *        init + watchSessions. Runs on DOMContentLoaded.
 * ORDER: MUST be last - boot executes the whole page, so everything it
 *        calls (renderers 08, binders 09, QA 10, chevrons 12, sticky 13,
 *        live 06) must already be defined.
 * EDIT:  Add page startup wiring here; keep delegate listeners bound once.
 * VERIFY: syntax-check, then screenshot_genui('home','both').
 * ======================================================================== */

/* ── Boot ── */
var _scrollSaveT=null;
function onPageScroll(){clearTimeout(_scrollSaveT);_scrollSaveT=setTimeout(savePageState,250)}
function boot(){
  fillIcons(document);
  document.getElementById('brandIcon').innerHTML=ICONS[CFG.header.brandIcon]||ICONS.layers;
  /* Restore the saved page state BEFORE the first render: which project is
     open, which item is expanded, the item search query, and the scroll
     position. Fresh visits (no saved state) start on the all-projects
     overview. */
  var saved=loadPageState();
  bindHeader();bindComposer();bindCards();wireCarousel();bindDetailCheckboxes();bindFeatureDetail();bindSticky();bindKbCollapse();
  document.addEventListener('scroll',onPageScroll,true);
  renderCards();renderDetail();  // renderDetail → restoreOpenItem re-expands the saved item
  GenUIState.restoreScroll(saved.scroll,saved.scrollLeft);

  /* ── Live data polling: keep the page in sync with agent-written updates ── */
  if(_liveData&&_liveData.init){
    var live=_liveData.init();
    // Watch the API's session-activity channel for instant wake on WS events.
    var api=window.WebagentGenui&&window.WebagentGenui.api;
    if(api){
      live.watchSessions({
        api:api,
        getSessions:function(){
          // Return every session id for items currently in planning/executing/questions
          // plus feature test runs (testQA) in flight.
          var sids=[];
          for(var i=0;i<STATE.projects.length;i++){
            var items=STATE.projects[i].items||[];
            for(var j=0;j<items.length;j++){
              var qa=items[j].qa||{};
              if(qa.session_id&&(qa.status==='planning'||qa.status==='executing'||qa.status==='questions'))
                sids.push(qa.session_id);
            }
            var tq=(STATE.projects[i].featureTest||{}).testQA||{};
            if(tq.session_id&&(tq.status==='planning'||tq.status==='executing'||tq.status==='questions'))
              sids.push(tq.session_id);
          }
          return sids;
        },
        onRun:function(sid,run){
          // When a session run finishes (active → false) and no new run starts,
          // force an immediate data poll so the next step renders fast.
          if(!run||!run.active) live.refresh();
        }
      });
    }
  }
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot);else boot();
