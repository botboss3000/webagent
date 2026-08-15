/* ══ MOUNT RULES ══
 * Delegate event listeners (addEventListener on a persistent container like
 * #detail-zone or #car-track) MUST be bound ONCE at boot — never inside
 * renderCards / renderDetail / or anything called by them. Re-binding inside
 * a render function causes exponential listener growth when a render trigger
 * (like a change event from saved-view restore) itself fires the listener.
 *   Bind once in boot():
 *     bindHeader(); bindComposer(); bindCards(); bindDetailCheckboxes();
 */
(function(){'use strict';
/* Shadow-DOM compat shim: the genui markup lives in a shadow root. The headless
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
S2.tasksLabel=S2.tasksLabel||'tasks';S2.openLabel=S2.openLabel||'open';S2.commitsLabel=S2.commitsLabel||'commits';S2.prsLabel=S2.prsLabel||'PRs';S2.daysLabel=S2.daysLabel||'days';S2.revsLabel=S2.revsLabel||'revs';S2.daysLeftLabel=S2.daysLeftLabel||'days left';S2.progressLabel=S2.progressLabel||'Progress';S2.detailsLabel=S2.detailsLabel||'Details';S2.openItemsLabel=S2.openItemsLabel||'Open items';S2.completionLabel=S2.completionLabel||'Completion';S2.velocityLabel=S2.velocityLabel||'Velocity · 8 weeks';S2.doneLabel=S2.doneLabel||'Done';S2.openLabel2=S2.openLabel2||'Open';S2.noProjects=S2.noProjects||'No projects match — try a different search, or create one.';S2.noItems=S2.noItems||'No items yet';S2.toastProjectAdded=S2.toastProjectAdded||'Project "${name}" added.';S2.toastNameRequired=S2.toastNameRequired||'Give the project a name first.';S2.toastChatSent=S2.toastChatSent||'Chat sent to ${name}';S2.toastSearchChat=S2.toastSearchChat||'Search/chat: ${text}';S2.allProjectsLabel=S2.allProjectsLabel||'All Projects';S2.projectsLabel=S2.projectsLabel||'projects';S2.overallProgressLabel=S2.overallProgressLabel||'Overall progress';S2.projectsListLabel=S2.projectsListLabel||'Projects';

/* ── PROMPTS — editable in data.json → config.prompts (fallbacks inline) ── */
var DEF_PROMPTS={
  quickNote:'[QUICK NOTE] A no-response-needed note from the user.\nTarget project: ${project}\nNote: ${text}',
  qa:{template:'[Project Readiness Agent]\nProject: ${project} (${status})\nItem: ${item} [${tag}]\nQA Status: ${qaStatus}\nUser Hint: ${hint}\n--- Q&A Thread ---\n${thread}\n${action}\n\nQUESTION RULES: Before proposing a plan, ask any clarifying questions needed — each 1 to 2 sentences max (3 ok occasionally), no preamble, no research dump in the question. Several independent questions may be asked at once (set qa.questions=[{id,text},...]); if answers chain, ask one at a time. Do NOT propose a plan while questions are pending.',
      userAnswer:'--- User Answer ---\n${answer}\nACTION: User answered. If critical unknowns remain, ask the next clarifying question(s) — each 1-2 sentences; update qa.questions and keep qa.status="questions", refresh the genui, and wait. If fully specified, propose the plan now: write qa.plan (approach/files/steps/effort/impact/risks/research_notes/related_items), set qa.status="plan_ready", refresh the genui.',
      skip:'ACTION: User wants to skip questions and go straight to plan — propose the plan now using reasonable assumptions.',
      planAction:"ACTION: User's plan decision: ${decision}"}
};
CFG.prompts=CFG.prompts||{};CFG.prompts.quickNote=CFG.prompts.quickNote||DEF_PROMPTS.quickNote;
CFG.prompts.qa=CFG.prompts.qa||{};for(var _pk in DEF_PROMPTS.qa){if(CFG.prompts.qa[_pk]===undefined)CFG.prompts.qa[_pk]=DEF_PROMPTS.qa[_pk]}

/* ── SESSION ROUTING — each chat surface gets its OWN independent session ── */
function newId(){try{if(window.crypto&&crypto.randomUUID)return crypto.randomUUID()}catch(e){}return'genui-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,10)}
function ensureSession(key,title){
  var s=STATE.sessions||(STATE.sessions={});
  if(!s[key]){s[key]=newId();saveBag()}
  return s[key];
}
function qaSession(pi,ii){
  var p=STATE.projects[pi],item=p.items[ii];if(!p||!item)return'';
  if(!item.qa)item.qa={status:'idle',session_id:null,thread:[],plan:null,plan_status:null,plan_history:[],execution_log:[],research_notes:null,effort:null,impact:null,related_items:[]};
  if(!item.qa.session_id){item.qa.session_id=newId();saveBag()}
  return item.qa.session_id;
}
function chatToSession(sid,title,text){
  try{
    var api=window.WebagentGenui&&window.WebagentGenui.api;if(!api||!api.chat||!sid)return false;
    try{window.__genuiSessionOverride=sid}catch(e){}
    if(title&&api.nameSession)api.nameSession(title);
    api.chat(text);return true;
  }catch(e){return false}
}
function tplt(s,map){return s.replace(/\$\{(\w+)\}/g,function(_,k){return map[k]!==undefined?map[k]:'${'+k+'}'})}
function cardChatPrompt(p,text){var cfg=CFG.cardChat&&CFG.cardChat.template?CFG.cardChat.template:'';if(!cfg)return'['+p.name+'] '+text;var oi=p.items?p.items.filter(function(i){return !i.done}):[],items=oi.map(function(i){return i.text+' ('+(i.tag||'task')+')'}).join('; ')||'none',done=p.items?p.items.filter(function(i){return i.done}).length:0,m=p.metrics||{},agent=p.chatAgentId||(CFG.cardChat&&CFG.cardChat.agentId)||'',map={name:p.name,status:p.statusLabel||p.status||'Active',progress:p.progress,open:p.open,tasks:p.tasks,daysLeft:m.daysLeft,commits:m.commits,prs:m.prs,reviewers:m.reviewers,items:items,done:done,chart:(p.chart||[]).join(', ')||'none',notes:(p.chatNotes||'')+((CFG.cardChat&&CFG.cardChat.detailNote)||''),agentId:agent,text:text};window.__genuiCardAgentId=agent||null;return'['+p.name+'] '+tplt(cfg,map)}

var STATE={projects:BAG.projects||[],q:'',activeId:null,sessions:(BAG.sessions||{})};
var toastTimer=null,carUpd=null;

function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function slug(s){return String(s).toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'')||'project'}
function toast(msg){var t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(function(){t.classList.remove('show')},CFG.toast.duration)}

/* ── LOCAL STATE ── */
var LS_P='pd-active-project',LS_I='pd-active-item';
function loadActiveProject(){try{return localStorage.getItem(LS_P)||''}catch(e){return''}}
function saveActiveProject(id){try{id?localStorage.setItem(LS_P,id):localStorage.removeItem(LS_P)}catch(e){}}
function loadActiveItem(){try{var s=localStorage.getItem(LS_I);if(s){var o=JSON.parse(s);if(o&&o.p!==undefined&&o.i!==undefined)return o}}catch(e){}return null}
function saveActiveItem(pid,i){try{localStorage.setItem(LS_I,JSON.stringify({p:pid,i:i}))}catch(e){}}
function clearActiveItem(){try{localStorage.removeItem(LS_I)}catch(e){}}
function restoreOpenItem(){
  var ai=loadActiveItem();if(!ai)return;
  if(STATE.activeId!==ai.p)return;
  var pi=-1;for(var i=0;i<STATE.projects.length;i++){if(STATE.projects[i].id===ai.p){pi=i;break}}
  if(pi<0)return;
  var it=STATE.projects[pi].items[ai.i];if(!it)return;
  var el=document.getElementById('qa-'+pi+'-'+ai.i);if(!el)return;
  var items=el.closest('.items');if(items){var openEls=items.querySelectorAll('.item-qa.open');for(var k=0;k<openEls.length;k++){if(openEls[k]!==el)openEls[k].classList.remove('open')}}
  el.classList.add('open');renderQAContent(pi,ai.i);
  var li=el.closest('.item');if(li)li.classList.add('open');
}

function donut(done,total){var C=2*Math.PI*CFG.donut.radius,f=total?done/total:0,R=CFG.donut.radius,SZ=R*2+12;return'<div class="donut-wrap"><svg viewBox="0 0 '+SZ+' '+SZ+'" class="donut" aria-hidden="true"><circle cx="'+(SZ/2)+'" cy="'+(SZ/2)+'" r="'+R+'" class="donut-bg"/><circle cx="'+(SZ/2)+'" cy="'+(SZ/2)+'" r="'+R+'" class="donut-fg" stroke-dasharray="'+(f*C).toFixed(1)+' '+C.toFixed(1)+'"/></svg><div class="donut-legend"><div class="lg"><span class="sw" style="background:var(--success)"></span>'+S2.doneLabel+'<b>'+done+'</b></div><div class="lg"><span class="sw" style="background:var(--bg-tint-2)"></span>'+S2.openLabel2+'<b>'+(total-done)+'</b></div></div></div>'}
function barChart(vals){var max=Math.max.apply(null,vals.concat([1])),h='';for(var i=0;i<vals.length;i++){var v=Math.max(4,Math.round(vals[i]/max*100));h+='<div class="bar-col"><div class="bar-col-fill'+(i===vals.length-1?' last':'')+'" style="height:'+v+'%"></div><span class="bar-col-label">'+(i+1)+'</span></div>'}return'<div class="bars">'+h+'</div>'}
function mini(n,v,l){return'<div class="mini">'+ic(n,'m-ic')+'<div><strong>'+v+'</strong><span>'+l+'</span></div></div>'}
function itemRank(it){var qa=it.qa||{},st=qa.status||'idle';if(it.done||st==='done')return 5;if(st==='questions')return 0;if(st==='plan_ready')return 1;if(qa.thread&&qa.thread.some(function(m){return m.role==='agent'&&m.needs_help}))return 1;if(st==='executing')return 2;if(st==='planning')return 3;return 4}
function sortItems(items){items=items||[];return items.slice().sort(function(a,b){var r=itemRank(a)-itemRank(b);return r?r:0})}

/* ── DELETE — two-click confirm (trash → hazard → delete), same affordance as the session dropdown ── */
var DEL_ARM_MS=400,DEL_REVERT_MS=3000;
function resetDelBtn(btn){
  if(!btn)return;
  clearTimeout(btn._armTimer);clearTimeout(btn._revertTimer);
  var title=btn.dataset.delTitle||'Delete';
  btn.dataset.state='trash';btn.classList.remove('del-warn','del-busy');
  btn.title=title;btn.innerHTML=ic('trash');
}
function disarmOtherDels(btn){
  var all=document.querySelectorAll('.item-del,.detail-del');
  for(var i=0;i<all.length;i++){var o=all[i];if(o!==btn&&o.dataset.state!=='trash')resetDelBtn(o)}
}
function delClick(btn,onConfirm){
  var st=btn.dataset.state||'trash';
  if(st==='arming')return;
  if(st==='trash'){
    btn.dataset.state='arming';btn.title='Arming…';
    btn.innerHTML='<span class="del-spin">'+ic('loader')+'</span>';
    clearTimeout(btn._armTimer);
    btn._armTimer=setTimeout(function(){
      if(btn.dataset.state!=='arming')return;
      btn.dataset.state='warning';btn.classList.add('del-warn');
      btn.title='Click again to delete';btn.innerHTML=ic('alert-triangle');
      disarmOtherDels(btn);
      clearTimeout(btn._revertTimer);
      btn._revertTimer=setTimeout(function(){resetDelBtn(btn)},DEL_REVERT_MS);
    },DEL_ARM_MS);
  }else if(st==='warning'){
    btn.dataset.state='deleting';btn.classList.remove('del-warn');btn.classList.add('del-busy');
    btn.title='Deleting…';btn.innerHTML='<span class="del-spin">'+ic('loader')+'</span>';
    clearTimeout(btn._revertTimer);
    if(onConfirm)Promise.resolve().then(function(){onConfirm(btn)});
  }
}
function recalcProject(p){
  var items=p.items||[],done=0,i;
  for(i=0;i<items.length;i++)if(items[i].done)done++;
  p.tasks=items.length;p.open=items.length-done;
}
function deleteItem(pi,ii){
  var p=STATE.projects[pi];if(!p||!p.items[ii])return;
  p.items.splice(ii,1);recalcProject(p);
  saveBag();renderCards();renderDetail();
  toast('Item deleted');
}
function deleteProject(pi){
  var p=STATE.projects[pi];if(!p)return;
  var name=p.name;
  STATE.projects.splice(pi,1);
  if(STATE.activeId===p.id){STATE.activeId=null;saveActiveProject('');clearActiveItem();}
  saveBag();renderCards();renderDetail();
  toast('Project "'+name+'" deleted');
}

/* ── CARD — fixed size: icon, name, task counts, chat icon, progress bar ── */
function cardHTML(p,i){
  return'<article class="card" data-id="'+esc(p.id)+'" tabindex="0" role="button" aria-label="Open '+esc(p.name)+'" style="animation-delay:'+Math.min(i*40,280)+'ms"><div class="card-top"><div class="tile">'+ic(p.icon||'folder')+'</div><div class="card-title"><h3>'+esc(p.name)+'</h3><span class="sub">'+p.tasks+' '+S2.tasksLabel+' · '+p.open+' '+S2.openLabel+'</span></div><button class="card-chat" type="button" data-id="'+esc(p.id)+'" title="Chat about '+esc(p.name)+'" aria-label="Chat about '+esc(p.name)+'">'+ic('chat')+'</button></div><div class="progress"><div class="progress-head"><span>'+S2.progressLabel+'</span><span class="pct">'+p.progress+'%</span></div><div class="bar"><div class="bar-fill" style="width:'+p.progress+'%"></div></div></div></article>'
}
function itemHTML(it,pi,ii){
  var qa=it.qa||{},st=qa.status||'idle',hasQA=st!=='idle';
  var statusHTML='';
  if(hasQA){
    var labels={questions:'Questions pending',planning:'Planning...',plan_ready:'Plan ready',executing:'Executing...',done:'Done'};
    var pills={questions:'qa-s-questions',planning:'qa-s-planning',plan_ready:'qa-s-plan_ready',executing:'qa-s-executing',done:'qa-s-done'};
    statusHTML='<span class="qa-status '+pills[st]+'" data-qa-pi="'+pi+'" data-qa-ii="'+ii+'" title="'+labels[st]+'"><span class="dot"></span>'+labels[st]+'</span>';
    if(st==='planning'||st==='executing'||st==='questions'){
      statusHTML+='<span class="qa-stop-btn" id="qa-stop-btn-'+pi+'-'+ii+'" data-qa-pi="'+pi+'" data-qa-ii="'+ii+'" title="Stop this operation"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>Stop</span>';
    }
  }else{
    statusHTML='<span class="qa-status qa-s-idle" data-qa-pi="'+pi+'" data-qa-ii="'+ii+'" title="Start research">Research</span>';
  }
  var qaBlock='<div class="item-qa" id="qa-'+pi+'-'+ii+'"></div>';
  return'<li class="item'+(it.done?' done':'')+'" data-p="'+pi+'" data-i="'+ii+'"><div class="item-row"><label class="check"><input type="checkbox" data-p="'+pi+'" data-i="'+ii+'"'+(it.done?' checked':'')+'></label><div class="item-content"><span>'+esc(it.text)+'</span><div class="item-meta"><span class="tag tag-'+esc(it.tag||'chore')+'">'+esc(it.tag||'chore')+'</span>'+statusHTML+'</div></div><button class="item-del" type="button" data-del-pi="'+pi+'" data-del-ii="'+ii+'" data-state="trash" data-del-title="Delete item" title="Delete item" aria-label="Delete item">'+ic('trash')+'</button></div>'+qaBlock+'</li>'
}
function matchItem(it,q){var parts=[it.text,it.tag,it.user_hint],qa=it.qa||{},i,j;
  if(qa.thread)for(i=0;i<qa.thread.length;i++)parts.push(qa.thread[i].text);
  if(qa.research_notes)parts.push(qa.research_notes);
  if(qa.user_hint)parts.push(qa.user_hint);
  var pl=qa.plan;if(pl){if(pl.approach)parts.push(pl.approach);if(pl.files)parts=parts.concat(pl.files);if(pl.steps)parts=parts.concat(pl.steps);if(pl.risks)parts=parts.concat(pl.risks);if(pl.related_items)parts=parts.concat(pl.related_items);if(pl.research_notes)parts.push(pl.research_notes)}
  if(qa.execution_log)for(i=0;i<qa.execution_log.length;i++)parts.push(qa.execution_log[i].step);
  for(i=0;i<parts.length;i++){if(parts[i]&&String(parts[i]).toLowerCase().indexOf(q)!==-1)return true}return false}
function detailHTML(p,pi){
  var items=p.items||[],done=items.filter(function(i){return i.done}).length,open=items.length-done,q=(STATE.itemQ||'').trim().toLowerCase(),ordered=items.map(function(it,i){return{it:it,i:i}}).sort(function(a,b){var r=itemRank(a.it)-itemRank(b.it);return r?r:a.i-b.i});
  if(q)ordered=ordered.filter(function(o){return matchItem(o.it,q)});
  var vis=ordered.length,list=items.length?(q&&!vis?'<li class="empty">No items match "'+esc(STATE.itemQ)+'"</li>':ordered.map(function(o){return itemHTML(o.it,pi,o.i)}).join('')):'<li class="empty">'+S2.noItems+'</li>';
  var m=p.metrics||{};
  var chip=q?vis:open;return'<header class="detail-head"><div class="tile">'+ic(p.icon||'folder')+'</div><div><h2>'+esc(p.name)+'</h2><span class="sub">'+p.tasks+' '+S2.tasksLabel+' · '+open+' '+S2.openLabel+' · '+p.progress+'% complete</span></div><span class="pill st-'+esc(p.status||'active')+'" style="margin-left:0"><span class="dot"></span>'+(p.statusLabel||p.status||'Active')+'</span><button class="detail-search'+(STATE.itemSearchOpen?' active':'')+'" id="detail-search" title="Search items" aria-label="Search items" aria-expanded="'+STATE.itemSearchOpen+'">'+ic('search')+'</button><button class="detail-del" type="button" data-del-p="'+pi+'" data-state="trash" data-del-title="Delete project" title="Delete project" aria-label="Delete project">'+ic('trash')+'</button><button class="detail-close" title="Close" id="detail-close">'+ic('close')+'</button></header><div class="detail-search-row"'+(STATE.itemSearchOpen?'':' style="display:none"')+'><span class="ic">'+ic('search')+'</span><input type="text" id="detail-search-input" class="detail-search-input" placeholder="Search titles and content…" value="'+esc(STATE.itemQ)+'" autocomplete="off" spellcheck="false"><button class="detail-search-clear" id="detail-search-clear" title="Clear search">'+ic('close')+'</button></div><div class="detail-body"><div class="panel"><div class="panel-head"><h4>'+S2.openItemsLabel+'</h4><span class="countchip">'+chip+'</span></div><ul class="items">'+list+'</ul></div><div class="charts"><div class="panel"><div class="panel-head"><h4>'+S2.completionLabel+'</h4></div>'+donut(done,items.length)+'</div>'+(p.chart&&p.chart.length?'<div class="panel"><div class="panel-head"><h4>'+S2.velocityLabel+'</h4></div>'+barChart(p.chart)+'</div>':'')+'<div class="minimetrics">'+mini('commit',m.commits||0,S2.commitsLabel)+mini('pr',m.prs||0,S2.prsLabel)+mini('users',m.reviewers||0,S2.revsLabel)+mini('calendar',m.daysLeft||0,S2.daysLeftLabel)+'</div></div></div>'
}
function totalDetailHTML(list){
  var n=list.length,tasks=0,open=0,don=0,tot=0,pr=0,cm=0,rv=0,dl=0,prog=0,chart=[],stats={},i,j;
  for(i=0;i<n;i++){var p=list[i],items=p.items||[],done=0;for(j=0;j<items.length;j++)if(items[j].done)done++;
    tasks+=p.tasks||0;open+=p.open||0;don+=done;tot+=items.length;prog+=p.progress||0;
    var m=p.metrics||{};cm+=m.commits||0;pr+=m.prs||0;rv+=m.reviewers||0;dl+=m.daysLeft||0;
    if(p.chart&&p.chart.length)for(j=0;j<p.chart.length;j++)chart[j]=(chart[j]||0)+p.chart[j];
    var st=p.status||'active';stats[st]=(stats[st]||0)+1;}
  var avg=n?Math.round(prog/n):0,rows='';
  for(i=0;i<n;i++){var q=list[i];rows+='<div class="tproj" data-id="'+esc(q.id)+'" role="button" tabindex="0" title="Open '+esc(q.name)+'"><div class="tproj-head"><span class="tproj-name">'+esc(q.name)+'</span><span class="tproj-pct">'+(q.progress||0)+'%</span></div><div class="bar"><div class="bar-fill" style="width:'+(q.progress||0)+'%"></div></div></div>';}
  var labels={active:'Active','at-risk':'At Risk','on-track':'On Track',blocked:'Blocked',done:'Done'},order=['active','on-track','at-risk','blocked','done'],sr='';
  for(i=0;i<order.length;i++){var sk=order[i],c=stats[sk]||0;if(!c)continue;sr+='<span class="pill st-'+sk+'"><span class="dot"></span>'+c+' '+labels[sk]+'</span>';}
  return'<header class="detail-head"><div class="tile">'+ic('layers')+'</div><div><h2>'+esc(S2.allProjectsLabel)+'</h2><span class="sub">'+n+' '+S2.projectsLabel+' · '+open+' '+S2.openLabel+' · '+avg+'% complete</span></div><span class="pill st-active"><span class="dot"></span>'+n+' '+S2.projectsLabel+'</span></header><div class="detail-body"><div class="col"><div class="panel"><div class="panel-head"><h4>'+S2.overallProgressLabel+'</h4><span class="countchip">'+avg+'%</span></div><div class="bar big-bar"><div class="bar-fill" style="width:'+avg+'%"></div></div>'+(sr?'<div class="status-row">'+sr+'</div>':'')+'</div><div class="panel"><div class="panel-head"><h4>'+S2.projectsListLabel+'</h4><span class="countchip">'+n+'</span></div><div class="tproj-list">'+rows+'</div></div></div><div class="charts"><div class="panel"><div class="panel-head"><h4>'+S2.completionLabel+'</h4></div>'+donut(don,tot)+'</div>'+(chart.length?'<div class="panel"><div class="panel-head"><h4>'+S2.velocityLabel+'</h4></div>'+barChart(chart)+'</div>':'')+'<div class="minimetrics">'+mini('commit',cm,S2.commitsLabel)+mini('pr',pr,S2.prsLabel)+mini('users',rv,S2.revsLabel)+mini('calendar',dl,S2.daysLeftLabel)+'</div></div></div>'
}
function visible(){var q=STATE.q.toLowerCase();return STATE.projects.filter(function(p){if(q&&p.name.toLowerCase().indexOf(q)===-1)return false;return true})}
function renderCards(){var list=visible(),grid=document.getElementById('car-track');document.getElementById('gridCount').textContent=list.length;document.getElementById('gridSub').textContent=STATE.q?'matching "'+STATE.q+'"':'';if(!list.length){grid.innerHTML='<div class="empty-state">'+S2.noProjects+'</div>';if(carUpd)carUpd();return}grid.innerHTML=list.map(cardHTML).join('');var i,cards=grid.querySelectorAll('.card');for(i=0;i<cards.length;i++){cards[i].classList.toggle('active',cards[i].dataset.id===STATE.activeId)}if(carUpd)carUpd()}
function renderDetail(){var zone=document.getElementById('detail-zone'),sub=document.getElementById('detail-sub');sub=sub||{};sub.textContent=sub.textContent||function(){};if(!STATE.activeId){var lst=visible();if(!lst.length){zone.innerHTML='<div class="detail-placeholder">'+CFG.detail.defaultMessage+'</div>';sub.textContent('');return}sub.textContent(S2.allProjectsLabel);zone.innerHTML='<div class="detail" id="detail-active">'+totalDetailHTML(lst)+'</div>';var trs=zone.querySelectorAll('.tproj');for(var t=0;t<trs.length;t++){(function(el){el.addEventListener('click',function(){activate(el.dataset.id)});el.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();activate(el.dataset.id)}})})(trs[t])}return}var pi=-1,p=null;for(var i=0;i<STATE.projects.length;i++){if(STATE.projects[i].id===STATE.activeId){pi=i;p=STATE.projects[i];break}}if(!p){zone.innerHTML='<div class="detail-placeholder">'+CFG.detail.defaultMessage+'</div>';sub.textContent('');return}sub.textContent(p.name);zone.innerHTML='<div class="detail" id="detail-active">'+detailHTML(p,pi)+'</div>';document.getElementById('detail-close').addEventListener('click',function(){activate(null)});restoreItemSearch();restoreOpenItem()}
function activate(id){if(id!==STATE.activeId){clearActiveItem();STATE.itemQ='';STATE.itemSearchOpen=false}STATE.activeId=id;saveActiveProject(id||'');if(!id)clearActiveItem();var i,els=document.querySelectorAll('.card');for(i=0;i<els.length;i++)els[i].classList.toggle('active',els[i].dataset.id===id);renderDetail()}
function bindDetailCheckboxes(){
  var zone=document.getElementById('detail-zone');if(!zone)return;
  zone.addEventListener('change',function(e){
    var t=e.target;if(t.type!=='checkbox'||t.dataset.p===undefined)return;
    var p=STATE.projects[+t.dataset.p],item=p.items[+t.dataset.i];if(!p||!item)return;
    item.done=t.checked;p.open=p.items.filter(function(i){return!i.done}).length;renderCards();renderDetail();
  });
  zone.addEventListener('click',function(e){
    var status=closest(e.target,'.qa-status');if(!status)return;
    e.stopPropagation();var pi=parseInt(status.dataset.qaPi),ii=parseInt(status.dataset.qaIi);
    if(isNaN(pi)||isNaN(ii))return;var p=STATE.projects[pi];if(!p||!p.items[ii])return;
    var item=p.items[ii],qa=item.qa||{},st=qa.status||'idle';
    if(st==='idle')startQA(pi,ii);else toggleQA(pi,ii);
  });
  zone.addEventListener('click',function(e){
    var btn=closest(e.target,'.qa-stop-btn');if(!btn||btn.disabled)return;
    e.stopPropagation();var pi=parseInt(btn.dataset.qaPi),ii=parseInt(btn.dataset.qaIi);
    if(isNaN(pi)||isNaN(ii))return;requestStop(pi,ii);
  });
  zone.addEventListener('click',function(e){
    var btn=closest(e.target,'.item-del');if(!btn)return;
    e.stopPropagation();var pi=parseInt(btn.dataset.delPi),ii=parseInt(btn.dataset.delIi);
    if(isNaN(pi)||isNaN(ii))return;delClick(btn,function(){deleteItem(pi,ii)});
  });
  zone.addEventListener('click',function(e){
    var btn=closest(e.target,'.detail-del');if(!btn)return;
    e.stopPropagation();var pi=parseInt(btn.dataset.delP);
    if(isNaN(pi))return;delClick(btn,function(){deleteProject(pi)});
  });
  zone.addEventListener('click',function(e){
    var btn=closest(e.target,'.qa-see-chat');if(!btn)return;
    e.stopPropagation();var pi=parseInt(btn.dataset.qaPi),ii=parseInt(btn.dataset.qaIi);
    if(isNaN(pi)||isNaN(ii))return;var p=STATE.projects[pi];if(!p||!p.items[ii])return;
    var item=p.items[ii];toast('Opening chat for: '+item.text.substring(0,40)+'...');
    var sid=item.qa&&item.qa.session_id?item.qa.session_id:ensureSession('general','Project Tracker — General Chat');
    chatToSession(sid,item.qa&&item.qa.session_id?item.text:'Project Tracker — General Chat','[Project Readiness] User opened chat for item: '+item.text+' | Project: '+p.name);
  });
  zone.addEventListener('click',function(e){
    var btn=closest(e.target,'.qa-send');if(!btn)return;
    e.stopPropagation();var pi=parseInt(btn.dataset.qaPi),ii=parseInt(btn.dataset.qaIi);
    if(isNaN(pi)||isNaN(ii))return;sendAnswer(pi,ii);
  });
  zone.addEventListener('click',function(e){
    var skip=closest(e.target,'.qa-skip');if(!skip)return;
    e.stopPropagation();var pi=parseInt(skip.dataset.qaPi),ii=parseInt(skip.dataset.qaIi);
    if(isNaN(pi)||isNaN(ii))return;skipToPlan(pi,ii);
  });
  zone.addEventListener('click',function(e){
    var row=closest(e.target,'.qa-question-row');if(!row)return;
    e.stopPropagation();var q=closest(row,'.qa-question');if(q)q.classList.toggle('open');
  });
  zone.addEventListener('click',function(e){
    var btn=closest(e.target,'.qa-send-answers');if(!btn)return;
    e.stopPropagation();var pi=parseInt(btn.dataset.qaPi),ii=parseInt(btn.dataset.qaIi);
    if(isNaN(pi)||isNaN(ii))return;sendAnswer(pi,ii);
  });
  zone.addEventListener('click',function(e){
    var link=closest(e.target,'.qa-open-chat');if(!link)return;
    e.stopPropagation();var pi=parseInt(link.dataset.qaPi),ii=parseInt(link.dataset.qaIi);
    if(isNaN(pi)||isNaN(ii))return;var p=STATE.projects[pi];if(!p||!p.items[ii])return;
    var item=p.items[ii];toast('Opening chat...');
    var sid=item.qa&&item.qa.session_id?item.qa.session_id:ensureSession('general','Project Tracker — General Chat');
    chatToSession(sid,item.qa&&item.qa.session_id?item.text:'Project Tracker — General Chat','[Project Readiness] Open in Chat: '+item.text+' | Project: '+p.name);
  });
  zone.addEventListener('click',function(e){
    var btn=closest(e.target,'.plan-accept,.plan-reject,.plan-comment-send');if(!btn)return;
    e.stopPropagation();var pi=parseInt(btn.dataset.qaPi),ii=parseInt(btn.dataset.qaIi);
    if(isNaN(pi)||isNaN(ii))return;var p=STATE.projects[pi];if(!p||!p.items[ii])return;
    var item=p.items[ii],qa=item.qa;if(!qa)return;
    if(btn.classList.contains('plan-accept')){sendPlanAction(pi,ii,'accept','')}
    else if(btn.classList.contains('plan-reject')){
      var row=document.getElementById('plan-comment-row-'+pi+'-'+ii);
      if(row&&row.style.display==='flex'){sendPlanAction(pi,ii,'reject_with_comment','')}
      else{if(row)row.style.display='flex';}
    }else if(btn.classList.contains('plan-comment-send')){
      var inp=document.getElementById('plan-comment-inp-'+pi+'-'+ii);
      sendPlanAction(pi,ii,'reject_with_comment',inp?inp.value.trim():'');
    }
  });
  zone.addEventListener('click',function(e){
    var t=e.target;
    if(t.closest('.qa-status')||t.closest('.qa-stop-btn')||t.closest('.check')||t.closest('input,textarea,button,a,label'))return;
    var row=closest(t,'.item-row');if(!row)return;
    var li=closest(t,'.item');if(!li||li.dataset.p===undefined||li.dataset.i===undefined)return;
    e.stopPropagation();toggleQA(+li.dataset.p,+li.dataset.i);
  });
  zone.addEventListener('click',function(e){
    var btn=closest(e.target,'.qa-start');if(!btn)return;
    e.stopPropagation();var pi=parseInt(btn.dataset.qaPi),ii=parseInt(btn.dataset.qaIi);
    if(isNaN(pi)||isNaN(ii))return;startQA(pi,ii);
  });
  zone.addEventListener('click',function(e){
    var btn=closest(e.target,'.detail-search');if(!btn)return;
    e.stopPropagation();toggleItemSearch();
  });
  zone.addEventListener('click',function(e){
    var btn=closest(e.target,'.detail-search-clear');if(!btn)return;
    e.stopPropagation();clearItemSearch(true);
  });
  zone.addEventListener('input',function(e){
    var inp=e.target;if(!inp||inp.id!=='detail-search-input')return;
    setItemQuery(inp.value);
  });
  zone.addEventListener('keydown',function(e){
    var inp=e.target;if(!inp||inp.id!=='detail-search-input')return;
    if(e.key==='Escape'){e.preventDefault();toggleItemSearch(true)}
  });
}
function setItemQuery(v){STATE.itemQ=v;var inp=document.getElementById('detail-search-input'),pos=inp?inp.selectionStart:null;renderDetail();var n=document.getElementById('detail-search-input');if(n){try{n.focus();if(pos!==null)n.setSelectionRange(pos,pos)}catch(_){}}}
function clearItemSearch(refocus){STATE.itemQ='';renderDetail();if(refocus){var n=document.getElementById('detail-search-input');if(n)try{n.focus()}catch(_){}}}
function toggleItemSearch(forceClose){STATE.itemSearchOpen=forceClose?false:!STATE.itemSearchOpen;if(!STATE.itemSearchOpen)STATE.itemQ='';renderDetail();if(STATE.itemSearchOpen){var n=document.getElementById('detail-search-input');if(n)try{n.focus()}catch(_){}}}
function restoreItemSearch(){var row=document.getElementById('detail-search-row'),trig=document.getElementById('detail-search');if(row)row.style.display=STATE.itemSearchOpen?'flex':'none';if(trig)trig.classList.toggle('active',STATE.itemSearchOpen)}

/* ── CARD INTERACTIONS — click opens detail; chat icon opens project chat ── */
function bindCards(){
  var grid=document.getElementById('car-track');if(!grid)return;
  grid.addEventListener('click',function(e){
    var chat=closest(e.target,'.card-chat');
    if(chat){e.stopPropagation();openCardChat(chat.dataset.id);return}
    var card=closest(e.target,'.card');
    if(card)activate(card.dataset.id);
  });
  grid.addEventListener('keydown',function(e){
    if(e.key==='Enter'||e.key===' '){
      var card=closest(e.target,'.card');
      if(card&&!closest(e.target,'.card-chat')){e.preventDefault();activate(card.dataset.id)}
    }
  });
}
function openCardChat(id){
  var pr=STATE.projects.filter(function(p){return p.id===id})[0];if(!pr)return;
  var nm=pr.name;
  chatToSession(ensureSession('card:'+id,'Chat: '+nm),'Chat: '+nm,cardChatPrompt(pr,''));
  toast(tplt(S2.toastChatSent,{name:nm}));
}

/* ── HEADER / COMPOSER ── */
function bindHeader(){
  var ta=document.getElementById('qnInput');
  ta.placeholder='Quick note — add item, task, or idea...';
  ta.addEventListener('input',function(){document.getElementById('quickNotePill').classList.toggle('has-text',ta.value.trim().length>0)});
  function sendQuickNote(){
    var t=ta.value.trim();if(!t)return;
    var tid=STATE.activeId,tn='';
    if(tid){var tp=STATE.projects.filter(function(p){return p.id===tid})[0];if(tp)tn=tp.name;}
    var parts=tplt(CFG.prompts.quickNote||DEF_PROMPTS.quickNote,{project:tn||'general (no card selected)',text:t});
    chatToSession(ensureSession('general','Project Tracker — General Chat'),'Project Tracker — General Chat',parts);
    ta.value='';document.getElementById('quickNotePill').classList.remove('has-text');
    toast('Quick note sent \u2713');
  }
  ta.addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendQuickNote();}});
  document.getElementById('qnSend').addEventListener('click',sendQuickNote);
}
function bindComposer(){
  var composer=document.getElementById('composer');
  document.getElementById('projNew').addEventListener('click',function(){composer.hidden=false;var n=document.getElementById('projName');if(n)try{n.focus()}catch(_){}});
  document.getElementById('projCancel').addEventListener('click',function(){composer.hidden=true});
  document.getElementById('projCreate').addEventListener('click',function(){
    var name=document.getElementById('projName').value.trim();
    if(!name){toast(S2.toastNameRequired);return}
    var status=document.getElementById('projStatus').value,id=slug(name)+'-'+String(Date.now()).slice(-5);
    STATE.projects.push({id:id,name:name,icon:'folder',status:status,statusLabel:{active:'Active','at-risk':'At Risk','on-track':'On Track',blocked:'Blocked',done:'Done'}[status],progress:0,tasks:3,open:3,metrics:{commits:0,prs:0,daysLeft:0,reviewers:0},chart:[0,0,0,0,0,0,0,0],items:[{text:'Define scope',done:false,tag:'feat'},{text:'Draft project plan',done:false,tag:'chore'},{text:'Set up team',done:false,tag:'chore'}]});
    composer.hidden=true;document.getElementById('projName').value='';renderCards();activate(id);
    toast(tplt(S2.toastProjectAdded,{name:name}));
  });
  document.addEventListener('keydown',function(e){if(e.key==='Escape'&&!composer.hidden)composer.hidden=true});
}

/* ── Helpers ── */
function closest(el,sel){while(el&&el!==document){if(el.matches&&el.matches(sel))return el;el=el.parentElement}return null}
function formatTS(ts){try{var d=new Date(ts);return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}catch(e){return''}}
/* ── QA Core ── */
function startQA(pi,ii){
  var p=STATE.projects[pi],item=p.items[ii];
  if(!item.qa)item.qa={status:'idle',session_id:null,thread:[],plan:null,plan_status:null,plan_history:[],execution_log:[],research_notes:null,effort:null,impact:null,related_items:[]};
  item.qa.status='planning';item.qa.thread=[{role:'agent',text:'Analyzing this item and researching the codebase...',ts:new Date().toISOString()}];
  saveBag();  // persist QA state so it survives page refresh
  renderCards();renderDetail();setTimeout(function(){toggleQA(pi,ii)},100);
  // Route this task into its OWN dedicated session (research → plan → execute
  // all happen in that one session), named after the task.
  var sid=qaSession(pi,ii),msg=buildQAPrompt(pi,ii,null,null);
  chatToSession(sid,item.text,msg);
  toast('Research started: '+item.text.substring(0,40)+'...');
}
function buildQAPrompt(pi,ii,answer,action){
  var p=STATE.projects[pi],item=p.items[ii];
  if(!item.qa)item.qa={status:'idle',session_id:null,thread:[],plan:null,plan_status:null,plan_history:[],execution_log:[],research_notes:null,effort:null,impact:null,related_items:[]};
  var qa=item.qa,P=CFG.prompts.qa||{};
  var thread=qa.thread.map(function(m){return(m.role==='agent'?'Agent':'User')+': '+m.text}).join('\n');
  var act='';
  if(action==='answer'&&answer)act=tplt(P.userAnswer||DEF_PROMPTS.qa.userAnswer,{answer:answer});
  else if(action==='skip')act=P.skip||DEF_PROMPTS.qa.skip;
  else if(action==='plan_action')act=tplt(P.planAction||DEF_PROMPTS.qa.planAction,{decision:answer});
  var hint='';var hintEl=document.getElementById('qa-hint-'+pi+'-'+ii);if(hintEl&&hintEl.value.trim())hint=hintEl.value.trim();
  return tplt(P.template||DEF_PROMPTS.qa.template,{project:p.name,status:p.statusLabel||'',item:item.text,tag:item.tag||'',qaStatus:qa.status,hint:hint,thread:thread,action:act});
}
function toggleQA(pi,ii){
  var el=document.getElementById('qa-'+pi+'-'+ii);if(!el)return;
  var isOpen=el.classList.contains('open'),p=STATE.projects[pi];
  var items=el.closest('.items');
  if(items){var openEls=items.querySelectorAll('.item-qa.open');for(var k=0;k<openEls.length;k++){if(openEls[k]!==el){openEls[k].classList.remove('open');var oli=openEls[k].closest('.item');if(oli)oli.classList.remove('open');}}}
  if(isOpen){el.classList.remove('open');clearActiveItem();var li2=el.closest('.item');if(li2)li2.classList.remove('open');}
  else{el.classList.add('open');renderQAContent(pi,ii);if(p)saveActiveItem(p.id,ii);var li3=el.closest('.item');if(li3)li3.classList.add('open');}
}
function renderQAContent(pi,ii){
  var p=STATE.projects[pi],item=p.items[ii],qa=item.qa||{};
  var el=document.getElementById('qa-'+pi+'-'+ii);if(!el)return;
  var html='',needsHelp=qa.thread&&qa.thread.some(function(m){return m.role==='agent'&&m.needs_help});
  if(qa.status==='planning')html+='<div class="qa-live-status" id="qa-live-status-'+pi+'-'+ii+'"><span class="qa-live-dot"></span> Agent is working — researching and analyzing...</div>';
  else if(qa.status==='executing')html+='<div class="qa-live-status" id="qa-live-status-'+pi+'-'+ii+'"><span class="qa-live-dot"></span> Agent is executing the plan...</div>';
  else if(qa.status==='questions')html+='<div class="qa-live-status" id="qa-live-status-'+pi+'-'+ii+'"><span class="qa-live-dot" style="background:var(--warning)"></span> Waiting for your answers</div>';
  else if(!qa.status||qa.status==='idle')html+='<div class="qa-live-status" id="qa-live-status-'+pi+'-'+ii+'"><span class="qa-live-dot" style="background:var(--fg-4);animation:none"></span> Not started — nothing researched yet</div><button class="qa-start" data-qa-pi="'+pi+'" data-qa-ii="'+ii+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M7 5l12 7-12 7z"/></svg> Start research</button>';
  if(qa.session_id&&(qa.status==='planning'||qa.status==='executing'||qa.status==='questions'))
    html+='<div class="qa-stop-row"><button class="qa-stop-btn" id="qa-stop-btn-'+pi+'-'+ii+'" data-qa-pi="'+pi+'" data-qa-ii="'+ii+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>Stop</button></div>';
  html+='<div class="qa-hint"><span class="qa-hint-label">Quick hint:</span><input id="qa-hint-'+pi+'-'+ii+'" type="text" placeholder="e.g. I think this is a CSS issue..." value="'+esc((qa.research_notes||'').substring(0,80))+'"></div>';
  if(qa.status==='questions'&&qa.questions&&qa.questions.length){
    html+='<div class="qa-questions" id="qa-questions-'+pi+'-'+ii+'">';
    qa.questions.forEach(function(q,qi){
      html+='<div class="qa-question" id="qa-question-'+pi+'-'+ii+'-'+qi+'" data-qi="'+qi+'">'
        +'<button type="button" class="qa-question-row" data-qa-pi="'+pi+'" data-qa-ii="'+ii+'" data-qi="'+qi+'">'
        +'<span class="qa-q-num">Q'+(qi+1)+'</span><span class="qa-q-text">'+esc(q.text||'')+'</span>'
        +'<span class="qa-q-chev">'+ic('chevron-down')+'</span></button>'
        +'<div class="qa-q-answer-wrap"><textarea id="qa-q-answer-'+pi+'-'+ii+'-'+qi+'" rows="2" placeholder="Your answer (1–2 sentences)..."></textarea></div>'
        +'</div>';
    });
    html+='<div class="qa-q-actions"><button class="qa-send-answers" data-qa-pi="'+pi+'" data-qa-ii="'+ii+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13"/><path d="m22 2-7 20-4-9-9-4z"/></svg>Send answers</button><span class="qa-skip" data-qa-pi="'+pi+'" data-qa-ii="'+ii+'">Skip to plan →</span></div>';
    html+='</div>';
  }else if(qa.status==='questions'){
    html+='<div class="qa-answer-row"><textarea id="qa-answer-'+pi+'-'+ii+'" rows="2" placeholder="Your answer..."></textarea><button class="qa-send" data-qa-pi="'+pi+'" data-qa-ii="'+ii+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13"/><path d="m22 2-7 20-4-9-9-4z"/></svg>Send</button></div><span class="qa-skip" data-qa-pi="'+pi+'" data-qa-ii="'+ii+'">Skip to plan →</span>';
  }
  if(qa.status==='plan_ready'&&qa.plan)html+=renderPlanCard(item,qa,pi,ii);
  else if(qa.status==='executing'&&qa.execution_log&&qa.execution_log.length)html+=renderExecLog(item,qa,pi,ii);
  if(qa.thread&&qa.thread.length){html+='<details class="qa-log"><summary>Research log</summary><div class="qa-thread">';qa.thread.forEach(function(m){
    var badge=m.needs_help?'<span style="display:inline-block;font-size:9px;font-weight:700;color:var(--warning);background:color-mix(in srgb,var(--warning)14%,transparent);border-radius:4px;padding:1px 5px;margin-bottom:2px">⚠ Needs your input</span>':'';
    if(m.role==='agent')html+='<div class="qa-msg qa-agent"><div class="qa-role">Agent</div>'+badge+esc(m.text)+'<div class="qa-ts">'+formatTS(m.ts)+'</div></div>';
    else html+='<div class="qa-msg qa-user"><div class="qa-role">You</div>'+esc(m.text)+'</div>';
  });html+='</div></details>';}
  if(needsHelp)html+='<button class="qa-see-chat open" id="qa-see-chat-'+pi+'-'+ii+'" data-qa-pi="'+pi+'" data-qa-ii="'+ii+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H8l-5 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>See chat for more info</button>';
  html+='<a class="qa-open-chat" data-qa-pi="'+pi+'" data-qa-ii="'+ii+'">Open in Chat →</a>';
  el.innerHTML=html;
}

function sendAnswer(pi,ii){
  var p=STATE.projects[pi],item=p.items[ii],qa=item.qa;if(!qa)return;
  var answer='',parts=[],i,inp,val;
  if(qa.questions&&qa.questions.length){
    for(i=0;i<qa.questions.length;i++){
      inp=document.getElementById('qa-q-answer-'+pi+'-'+ii+'-'+i);
      val=inp?inp.value.trim():'';
      parts.push('Q'+(i+1)+') '+(qa.questions[i].text||'')+(val?'\n→ '+val:'\n→ (no answer)'));
    }
    answer=parts.join('\n\n');
  }else{
    inp=document.getElementById('qa-answer-'+pi+'-'+ii);
    answer=inp?inp.value.trim():'';if(!answer)return;
  }
  qa.thread.push({role:'user',text:answer,ts:new Date().toISOString()});
  qa.status='planning';qa.questions=qa.questions||[];
  saveBag();renderCards();renderDetail();
  var msg=buildQAPrompt(pi,ii,answer,'answer');
  chatToSession(qaSession(pi,ii),item.text,msg);toast('Answer sent \u2713');
}

function skipToPlan(pi,ii){
  var p=STATE.projects[pi],item=p.items[ii],qa=item.qa;if(!qa)return;
  qa.thread.push({role:'user',text:'[User chose to skip questions — proceed to plan]',ts:new Date().toISOString()});
  qa.status='planning';saveBag();renderCards();renderDetail();var msg=buildQAPrompt(pi,ii,null,'skip');
  chatToSession(qaSession(pi,ii),item.text,msg);toast('Skipping to plan...');
}
function sendPlanAction(pi,ii,action,comment){
  var p=STATE.projects[pi],item=p.items[ii],qa=item.qa;if(!qa)return;
  if(action==='accept'){qa.plan_status='accepted';qa.status='executing';qa.execution_log=[{step:'Plan accepted — starting execution',status:'running',detail:''}];}
  else if(action==='reject_with_comment'){
    if(!qa.plan_history)qa.plan_history=[];qa.plan_history.push(JSON.parse(JSON.stringify(qa.plan||{})));
    qa.plan_status='rejected';qa.status='planning';qa.thread.push({role:'user',text:'Plan rejected: '+comment,ts:new Date().toISOString()});
    var row=document.getElementById('plan-comment-row-'+pi+'-'+ii);if(row)row.style.display='none';
  }
  saveBag();renderCards();renderDetail();var msg=buildQAPrompt(pi,ii,action+(comment?': '+comment:''),'plan_action');
  chatToSession(qaSession(pi,ii),item.text,msg);
  toast(action==='accept'?'Plan accepted — executing...':'Feedback sent \u2713');
}
function renderPlanCard(item,qa,pi,ii){
  var plan=qa.plan||{},html='<div class="plan-card open" id="plan-'+pi+'-'+ii+'">';
  html+='<h4><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg> Plan Ready — Review</h4>';
  if(plan.approach)html+='<div class="plan-approach">'+esc(plan.approach)+'</div>';
  html+='<div class="plan-meta">';
  if(plan.effort)html+='<span class="plan-badge effort">Effort: '+esc(plan.effort)+'</span>';
  if(plan.impact)html+='<span class="plan-badge impact-'+(plan.impact||'').toLowerCase()+'">Impact: '+esc(plan.impact)+'</span>';
  html+='</div>';
  if(plan.files&&plan.files.length){html+='<div class="plan-files">Files: ';plan.files.forEach(function(f){html+='<span>'+esc(f)+'</span>'});html+='</div>';}
  if(plan.steps&&plan.steps.length){html+='<ol class="plan-steps">';plan.steps.forEach(function(s){html+='<li>'+esc(s)+'</li>'});html+='</ol>';}
  if(plan.risks&&plan.risks.length)html+='<div class="plan-risks">\u26a0 Risks: '+plan.risks.map(function(s){return esc(s)}).join('; ')+'</div>';
  if(plan.research_notes)html+='<div class="plan-research">\ud83d\udca1 '+esc(plan.research_notes)+'</div>';
  if(qa.related_items&&qa.related_items.length){html+='<div class="plan-related">Related: ';qa.related_items.forEach(function(r){html+='<a>'+esc(r.project||'')+': '+esc(r.text||'')+'</a> '});html+='</div>';}
  if(qa.plan_history&&qa.plan_history.length){html+='<details style="font-size:11px;color:var(--fg-3)"><summary>'+qa.plan_history.length+' previous plan(s)</summary>';qa.plan_history.forEach(function(h,i){html+='<div style="margin-top:4px;padding:4px 8px;background:var(--bg-tint);border-radius:6px">v'+(i+1)+': '+esc(h.approach||'No approach')+'</div>'});html+='</details>';}
  html+='<div class="plan-actions"><button class="btn btn-accept plan-accept" data-qa-pi="'+pi+'" data-qa-ii="'+ii+'">Accept Plan</button><button class="btn btn-reject plan-reject" data-qa-pi="'+pi+'" data-qa-ii="'+ii+'">Reject</button></div>';
  html+='<div class="plan-comment-row" id="plan-comment-row-'+pi+'-'+ii+'" style="display:none"><input id="plan-comment-inp-'+pi+'-'+ii+'" type="text" placeholder="What needs to change?"><button class="btn btn-ghost plan-comment-send" data-qa-pi="'+pi+'" data-qa-ii="'+ii+'" style="font-size:11px;padding:5px 10px">Send</button></div>';
  return html;
}
function renderExecLog(item,qa,pi,ii){
  var html='<div class="exec-log open" id="exec-'+pi+'-'+ii+'">';
  if(qa.execution_log&&qa.execution_log.length)qa.execution_log.forEach(function(e){html+='<div class="exec-entry"><span class="exec-dot '+esc(e.status||'running')+'"></span><span class="exec-step">'+esc(e.step||'')+'</span><span class="exec-detail">'+esc(e.detail||'')+'</span></div>'});
  html+='</div>';return html;
}

/* ── Stop Operation ── */
var STOP_POLLERS={};
function requestStop(pi,ii){
  var p=STATE.projects[pi],item=p.items[ii],qa=item.qa||{};if(!qa.session_id)return;
  var sid=qa.session_id,btn=document.getElementById('qa-stop-btn-'+pi+'-'+ii);
  if(btn){btn.disabled=true;btn.textContent='Stopping...';btn.style.opacity='0.6';}
  fetch('/api/v1/chat/interrupt',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify({session_id:sid})})
  .then(function(r){return r.json()}).then(function(data){
    if(data.was_running)startStopPoll(pi,ii,sid);else confirmStopped(pi,ii,'Session was not running');
  }).catch(function(e){confirmStopped(pi,ii,'Interrupt failed: '+e.message)});
}
function startStopPoll(pi,ii,sid){
  var attempts=0,key=pi+'-'+ii;if(STOP_POLLERS[key])clearInterval(STOP_POLLERS[key]);
  STOP_POLLERS[key]=setInterval(function(){
    attempts++;
    fetch('/api/v1/db/session-tail?session_id='+encodeURIComponent(sid)+'&after_session_seq=0',{method:'GET',credentials:'include'})
    .then(function(r){return r.json()}).then(function(data){
      var run=data.run||{};if(!run.active){clearInterval(STOP_POLLERS[key]);delete STOP_POLLERS[key];confirmStopped(pi,ii,null);}
      else if(attempts>=15){clearInterval(STOP_POLLERS[key]);delete STOP_POLLERS[key];confirmStopped(pi,ii,'Stop requested — may still finalize');}
    }).catch(function(){if(attempts>=15){clearInterval(STOP_POLLERS[key]);delete STOP_POLLERS[key];confirmStopped(pi,ii,'Unable to verify');}});
  },2000);
}
function confirmStopped(pi,ii,msg){
  var btn=document.getElementById('qa-stop-btn-'+pi+'-'+ii);
  if(btn){btn.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>Stopped';btn.style.background='var(--success)';btn.style.opacity='1';btn.disabled=false;if(msg)btn.title=msg;setTimeout(function(){if(btn&&btn.isConnected)btn.style.display='none'},4000);}
  var p=STATE.projects[pi],item=p.items[ii],qa=item.qa||{};
  if(qa.status==='planning'||qa.status==='executing'||qa.status==='questions'){qa.status='idle';saveBag();renderCards();renderDetail();toast('Operation stopped');}
}

/* ── CAROUSEL CHEVRONS ── */
function wireCarousel(){
  var track=document.getElementById('car-track'),wrap=document.getElementById('car-wrap');
  var chevL=document.getElementById('car-chev-left'),chevR=document.getElementById('car-chev-right');
  if(!track||!wrap)return;
  function update(){
    if(!chevL||!chevR)return;
    var over=track.scrollWidth-track.clientWidth>1;
    wrap.classList.toggle('has-overflow',over);
    chevL.classList.toggle('visible',over&&track.scrollLeft>1);
    chevR.classList.toggle('visible',over&&track.scrollLeft<track.scrollWidth-track.clientWidth-1);
  }
  carUpd=update;
  if(chevL&&chevR){
    var step=function(){return Math.max(80,Math.floor(track.clientWidth*0.6))};
    chevL.addEventListener('click',function(){track.scrollBy({left:-step(),behavior:'smooth'})});
    chevR.addEventListener('click',function(){track.scrollBy({left:step(),behavior:'smooth'})});
  }
  track.addEventListener('scroll',update,{passive:true});
  window.addEventListener('resize',update);
  update();
}

/* ── STICKY CAROUSEL + DETAIL HEAD ──
 * Pure CSS sticky: the carousel pins to the top (top:0); the detail head pins
 * just below it at the carousel's measured height (--car-h). We only measure the
 * carousel height here — the app mounts genui pages while the host is
 * display:none, so offsetHeight is 0 during boot and we must defer until real
 * measurements exist. */
function bindSticky(){
  var lockEl=document.getElementById('detail-lock');if(!lockEl)return;
  var carZone=document.getElementById('car-zone');if(!carZone)return;
  function measure(){
    var h=carZone.offsetHeight;
    if(h>0)lockEl.style.setProperty('--car-h',h+'px');
  }
  measure();
  var tries=0;
  (function retry(){
    if(!lockEl.style.getPropertyValue('--car-h')&&tries++<60){setTimeout(function(){measure();retry();},100);}
  })();
  window.addEventListener('resize',measure);
}

/* ── Boot ── */
function boot(){
  fillIcons(document);
  document.getElementById('brandIcon').innerHTML=ICONS[CFG.header.brandIcon]||ICONS.layers;
  bindHeader();bindComposer();bindCards();wireCarousel();bindDetailCheckboxes();bindSticky();
  /* Start on the all-projects overview — no auto-restore of the last-opened
     project (it hid the overview on load). Opening a tile swaps to its
     details; the close button returns to the overview. */
  renderCards();renderDetail();
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot);else boot();
})();
