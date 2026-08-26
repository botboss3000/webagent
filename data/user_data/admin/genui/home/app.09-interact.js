/* ========================================================================
 * PART 09/14 - app.09-interact.js         (grep: GENUI-SPLIT)
 * ROLE:  Card interactions: activate(), bindCards(), card click/chat
 *        buttons, keyboard activation, checkbox/input bindings.
 * ORDER: Part 9 - binds against DOM from app.08; boot (14) calls bind*.
 * EDIT:  Add NEW listeners here or in boot, never inside renderers.
 * VERIFY: syntax-check, then screenshot_genui('home','both').
 * ======================================================================== */

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
  chatToSession(ensureSession('card:'+id,'Chat: '+nm),'Chat: '+nm,cardChatPrompt(pr,''),false,false,false,tplt((CFG.cardChat&&CFG.cardChat.userMessage)||'Chat opened with the ${name} card on the Project Development Tracker GenUI page',{name:nm}));
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
    var ctx=buildProjectContext();
    var msg=tplt(CFG.prompts.quickNote||DEF_PROMPTS.quickNote,{project:tn||'general (no card selected)',text:t,projectContext:ctx});
    // fresh session per note + raw prompt via direct POST (no genui tag/context wrap)
    directChat(msg,CFG.prompts.quickNote_userMessage||DEF_PROMPTS.quickNote_userMessage);
    ta.value='';document.getElementById('quickNotePill').classList.remove('has-text');
    toast('Quick note sent \u2713');
  }
  ta.addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendQuickNote();}});
  document.getElementById('qnSend').addEventListener('click',sendQuickNote);
}
function buildProjectContext(){
  var out='',projs=STATE.projects,i,p;
  for(i=0;i<projs.length;i++){
    p=projs[i];
    out+=p.name+' ['+(p.status||'active')+'] — '+(p.open||0)+' open, '+(p.progress||0)+'%';
    if(p.info&&p.info.trim())out+=' | '+p.info.trim().slice(0,120);
    out+='\n';
  }
  return out;
}
function bindComposer(){
  var composer=document.getElementById('composer');
  document.getElementById('projNew').addEventListener('click',function(){composer.hidden=false;var n=document.getElementById('projName');if(n)try{n.focus()}catch(_){}});
  document.getElementById('projCancel').addEventListener('click',function(){composer.hidden=true});
  document.getElementById('projCreate').addEventListener('click',function(){
    var name=document.getElementById('projName').value.trim();
    if(!name){toast(S2.toastNameRequired);return}
    var status=document.getElementById('projStatus').value,id=slug(name)+'-'+String(Date.now()).slice(-5);
    STATE.projects.push({id:id,name:name,icon:'folder',status:status,statusLabel:{active:'Active','at-risk':'At Risk','on-track':'On Track',blocked:'Blocked',done:'Done'}[status],progress:0,tasks:3,open:3,metrics:{commits:0,prs:0,daysLeft:0,reviewers:0},chart:[0,0,0,0,0,0,0,0],info:'',items:[{text:'Define scope',done:false,tag:'feat'},{text:'Draft project plan',done:false,tag:'chore'},{text:'Set up team',done:false,tag:'chore'}]});
    composer.hidden=true;document.getElementById('projName').value='';renderCards();activate(id);
    toast(tplt(S2.toastProjectAdded,{name:name}));
  });
  document.addEventListener('keydown',function(e){if(e.key==='Escape'&&!composer.hidden)composer.hidden=true});
}

/* ── Helpers ── */
function closest(el,sel){while(el&&el!==document){if(el.matches&&el.matches(sel))return el;el=el.parentElement}return null}
function formatTS(ts){try{var d=new Date(ts);return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}catch(e){return''}}

/* ── FEATURE DETAIL INTERACTIONS — bound once at boot, delegated on #detail-zone ── */
function bindFeatureDetail(){
  var zone=document.getElementById('detail-zone');if(!zone)return;
  zone.addEventListener('click',function(e){
    var btn=closest(e.target,'.ft-run');if(!btn)return;
    e.stopPropagation();var pi=parseInt(btn.dataset.ftPi);if(isNaN(pi))return;startFeatureTest(pi);
  });
  zone.addEventListener('click',function(e){
    var btn=closest(e.target,'.ft-entry');if(!btn)return;
    e.stopPropagation();var pi=parseInt(btn.dataset.ftPi);if(isNaN(pi))return;
    var p=STATE.projects[pi];if(p)ftObj(p);
    STATE.view='feature';savePageState();renderDetail();
  });
  zone.addEventListener('click',function(e){
    var btn=closest(e.target,'.ft-back');if(!btn)return;
    e.stopPropagation();var pi=parseInt(btn.dataset.ftPi);if(isNaN(pi))return;
    STATE.view='project';savePageState();renderDetail();
  });
  zone.addEventListener('click',function(e){
    var btn=closest(e.target,'.ft-stop');if(!btn||btn.disabled)return;
    e.stopPropagation();var pi=parseInt(btn.dataset.ftPi);if(isNaN(pi))return;requestStopFeature(pi);
  });
  zone.addEventListener('click',function(e){
    var btn=closest(e.target,'.ft-send,.ft-send-answers');if(!btn)return;
    e.stopPropagation();var pi=parseInt(btn.dataset.ftPi);if(isNaN(pi))return;sendFeatureAnswer(pi);
  });
  zone.addEventListener('click',function(e){
    var skip=closest(e.target,'.ft-skip');if(!skip)return;
    e.stopPropagation();var pi=parseInt(skip.dataset.ftPi);if(isNaN(pi))return;skipFeatureToPlan(pi);
  });
  zone.addEventListener('click',function(e){
    var row=closest(e.target,'.ft-q-row');if(!row)return;
    e.stopPropagation();var q=closest(row,'.qa-question');if(q)q.classList.toggle('open');
  });
  zone.addEventListener('click',function(e){
    var btn=closest(e.target,'.ft-plan-accept,.ft-plan-reject,.ft-plan-comment-send');if(!btn)return;
    e.stopPropagation();var pi=parseInt(btn.dataset.ftPi);if(isNaN(pi))return;
    var p=STATE.projects[pi],ft=p&&p.featureTest,tq=ft&&ft.testQA;if(!tq)return;
    if(btn.classList.contains('ft-plan-accept')){sendFeaturePlanAction(pi,'accept','')}
    else if(btn.classList.contains('ft-plan-reject')){
      var row=document.getElementById('ft-plan-comment-row-'+pi);
      if(row&&row.style.display==='flex'){sendFeaturePlanAction(pi,'reject_with_comment','')}
      else{if(row)row.style.display='flex';}
    }else{
      var inp=document.getElementById('ft-plan-comment-inp-'+pi);
      sendFeaturePlanAction(pi,'reject_with_comment',inp?inp.value.trim():'');
    }
  });
  zone.addEventListener('click',function(e){
    var link=closest(e.target,'.ft-open-chat');if(!link)return;
    e.stopPropagation();var pi=parseInt(link.dataset.ftPi);if(isNaN(pi))return;
    var p=STATE.projects[pi],ft=p&&p.featureTest,tq=ft&&ft.testQA;if(!tq||!tq.session_id)return;
    toast('Opening chat...');
    chatToSession(tq.session_id,p.name,'[Feature Readiness] Open in Chat: '+p.name,false,false,false,CFG.prompts.openChat_userMessage||DEF_PROMPTS.openChat_userMessage);
  });
  zone.addEventListener('click',function(e){
    var btn=closest(e.target,'.req-add-btn');if(!btn)return;
    e.stopPropagation();var pi=parseInt(btn.dataset.ftPi);if(isNaN(pi))return;addRequirement(pi);
  });
  zone.addEventListener('click',function(e){
    var btn=closest(e.target,'.freq-del');if(!btn)return;
    e.stopPropagation();var pi=parseInt(btn.dataset.ftPi),ri=parseInt(btn.dataset.r);
    if(isNaN(pi)||isNaN(ri))return;
    delClick(btn,function(){deleteFeatureRequirement(pi,ri)});
  });
  zone.addEventListener('change',function(e){
    var t=e.target;if(!t||t.type!=='checkbox'||t.dataset.r===undefined)return;
    var pi=parseInt(t.dataset.ftPi),ri=parseInt(t.dataset.r);
    if(isNaN(pi)||isNaN(ri))return;
    var p=STATE.projects[pi],ft=p&&p.featureTest;if(!ft||!ft.requirements[ri])return;
    ft.requirements[ri].done=t.checked;
    saveBag();renderDetail();
  });
  zone.addEventListener('keydown',function(e){
    var inp=e.target;if(!inp||!inp.id||inp.id.indexOf('req-add-')!==0)return;
    if(e.key==='Enter'){e.preventDefault();addRequirement(parseInt(inp.id.replace('req-add-',''),10));}
  });
  zone.addEventListener('change',function(e){
    var inp=e.target;if(!inp||!inp.classList||!inp.classList.contains('req-criteria'))return;
    var pi=parseInt(inp.dataset.ftPi),ri=parseInt(inp.dataset.r);
    if(isNaN(pi)||isNaN(ri))return;
    saveRequirementCriteria(pi,ri,inp.value);
  });
  zone.addEventListener('focusout',function(e){
    var t=e.target;if(!t||!t.id||t.id.indexOf('feature-desc-')!==0)return;
    var pi=parseInt(t.id.replace('feature-desc-',''),10);
    if(!isNaN(pi))saveFeatureDesc(pi,t.value);
  });
}
