/* ========================================================================
 * PART 02/14 - app.02-data.js             (grep: GENUI-SPLIT)
 * ROLE:  Data bag + config: loadBag(), BAG, saveBag() (debounced POST to
 *        /api/v1/genui/home/data?user_id=admin - the page's ONLY writer of
 *        data.json), CFG defaults + S2 label strings.
 * ORDER: Part 2 - defines BAG/CFG/S2 used by every later part.
 * EDIT:  Labels/defaults fallbacks live here; the LIVE values come from
 *        data.json config (they win over these defaults).
 * VERIFY: syntax-check, then screenshot_genui('home','both').
 * ======================================================================== */

/* ── DATA BAG ── */
function loadBag(){var d=null;try{if(window.__GENUI_DATA&&window.__GENUI_DATA.projects)d=window.__GENUI_DATA;else if(window.WebagentGenui.api&&typeof window.WebagentGenui.api.getData==='function')d=window.WebagentGenui.api.getData()}catch(e){}return d||{projects:[],config:{}}}
var BAG=loadBag();

/* ── Persist QA state to data.json so Research/plan status survives refresh ── */
var _saveTimer=0;
function saveBag(){
  clearTimeout(_saveTimer);
  _saveTimer=setTimeout(function(){
    try{
      // Build the full data bag (projects + config) the same way loadBag expects
      var bag={projects:STATE.projects,config:BAG.config||{},sessions:STATE.sessions||{}};
      fetch('/api/v1/genui/home/data?user_id=admin',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({data:bag})
      });
    }catch(e){}
  },200);
}
var CFG=JSON.parse(JSON.stringify(BAG.config||{}));
CFG.card=CFG.card||{};CFG.card.gap=CFG.card.gap||16;CFG.card.radius=CFG.card.radius||16;
CFG.donut=CFG.donut||{};CFG.donut.radius=CFG.donut.radius||30;CFG.donut.strokeWidth=CFG.donut.strokeWidth||9;CFG.bars=CFG.bars||{};CFG.bars.height=CFG.bars.height||110;CFG.bars.maxWidth=CFG.bars.maxWidth||24;
CFG.toast=CFG.toast||{};CFG.toast.duration=CFG.toast.duration||2400;
CFG.header=CFG.header||{};CFG.header.brandIcon=CFG.header.brandIcon||'layers';
CFG.detail=CFG.detail||{};CFG.detail.defaultMessage=CFG.detail.defaultMessage||'Select a project card above to see its open items and activity.';
CFG.strings=CFG.strings||{};var S2=CFG.strings;
S2.tasksLabel=S2.tasksLabel||'tasks';S2.openLabel=S2.openLabel||'open';S2.commitsLabel=S2.commitsLabel||'commits';S2.prsLabel=S2.prsLabel||'PRs';S2.daysLabel=S2.daysLabel||'days';S2.revsLabel=S2.revsLabel||'revs';S2.daysLeftLabel=S2.daysLeftLabel||'days left';S2.progressLabel=S2.progressLabel||'Progress';S2.detailsLabel=S2.detailsLabel||'Details';S2.openItemsLabel=S2.openItemsLabel||'Open items';S2.completionLabel=S2.completionLabel||'Completion';S2.velocityLabel=S2.velocityLabel||'Velocity · 8 weeks';S2.doneLabel=S2.doneLabel||'Done';S2.openLabel2=S2.openLabel2||'Open';S2.noProjects=S2.noProjects||'No projects match — try a different search, or create one.';S2.noItems=S2.noItems||'No items yet';S2.toastProjectAdded=S2.toastProjectAdded||'Project "${name}" added.';S2.toastNameRequired=S2.toastNameRequired||'Give the project a name first.';S2.toastChatSent=S2.toastChatSent||'Chat sent to ${name}';S2.toastSearchChat=S2.toastSearchChat||'Search/chat: ${text}';S2.allProjectsLabel=S2.allProjectsLabel||'All Projects';S2.projectsLabel=S2.projectsLabel||'projects';S2.overallProgressLabel=S2.overallProgressLabel||'Overall progress';S2.projectsListLabel=S2.projectsListLabel||'Projects';S2.featureLabel=S2.featureLabel||'Feature';S2.featureTestLabel=S2.featureTestLabel||'Requirements and Testing';S2.backToProjectLabel=S2.backToProjectLabel||'Project';S2.featureDescLabel=S2.featureDescLabel||'Feature description';S2.requirementsLabel=S2.requirementsLabel||'Requirements';S2.testPlanLabel=S2.testPlanLabel||'Test plan';S2.runTestsLabel=S2.runTestsLabel||'Run tests';S2.reRunTestsLabel=S2.reRunTestsLabel||'Re-run tests';S2.noTestPlan=S2.noTestPlan||'No test plan yet — run tests to draft one.';S2.addRequirement=S2.addRequirement||'Add requirement…';S2.reqCriteria=S2.reqCriteria||'Acceptance criteria…';

