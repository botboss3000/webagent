/* ========================================================================
 * PART 08/14 - app.08-cards.js            (grep: GENUI-SPLIT)
 * ROLE:  The renderers: cardHTML, itemHTML, matchItem, detailHTML,
 *        totalDetailHTML, visible, renderCards, renderDetail.
 * ORDER: Part 8 - biggest part; uses ic (01), BAG/CFG/S2 (02), STATE (05).
 * EDIT:  Card/detail markup lives here. Use tokens (var(--...)), never
 *        hardcoded hex. Do NOT bind event listeners inside renderers -
 *        bind ONCE in boot (14) / interactions (09) (see MOUNT RULES).
 * VERIFY: syntax-check, then screenshot_genui('home','both').
 * ======================================================================== */

/* ── CARD — fixed width: icon, name, chat icon, progress (completed/total + pct) ── */
function cardHTML(p,i){
  return'<article class="card" data-id="'+esc(p.id)+'" tabindex="0" role="button" aria-label="Open '+esc(p.name)+'" style="animation-delay:'+Math.min(i*40,280)+'ms"><div class="card-top"><div class="tile">'+ic(p.icon||'folder')+'</div><div class="card-title"><h3>'+esc(p.name)+'</h3></div><button class="card-chat" type="button" data-id="'+esc(p.id)+'" title="Chat about '+esc(p.name)+'" aria-label="Chat about '+esc(p.name)+'">'+ic('chat')+'</button></div><div class="progress"><div class="progress-head"><span>'+S2.progressLabel+' '+(p.tasks-p.open)+'/'+p.tasks+'</span><span class="pct">'+p.progress+'%</span></div><div class="bar"><div class="bar-fill" style="width:'+p.progress+'%"></div></div></div></article>'
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
    statusHTML='<span class="qa-status qa-s-idle" data-qa-pi="'+pi+'" data-qa-ii="'+ii+'" title="'+(it.tag==='test'?'Run test to validate this feature':'Start research')+'">'+(it.tag==='test'?'Test':'Research')+'</span>';
  }
  var qaBlock='<div class="item-qa" id="qa-'+pi+'-'+ii+'"></div>';
  return'<li class="item'+(it.done?' done':'')+'" data-p="'+pi+'" data-i="'+ii+'"><div class="item-row"><label class="check"><input type="checkbox" data-p="'+pi+'" data-i="'+ii+'"'+(it.done?' checked':'')+'></label><div class="item-content"><span>'+esc(it.text)+'</span><div class="item-meta"><span class="tag tag-'+esc(it.tag||'chore')+'">'+esc(it.tag||'chore')+'</span>'+statusHTML+'</div></div><button class="item-del" type="button" data-del-pi="'+pi+'" data-del-ii="'+ii+'" data-state="trash" data-del-title="Delete item" title="Delete item" aria-label="Delete item">'+ic('trash')+'</button></div>'+qaBlock+'</li>'
}
function freqHTML(r,pi,ri){
  return'<li class="item'+(r.done?' done':'')+'"><div class="item-row"><label class="check"><input type="checkbox" data-ft-pi="'+pi+'" data-r="'+ri+'"'+(r.done?' checked':'')+'></label><div class="item-content"><span>'+esc(r.text)+'</span><div class="item-meta"><span class="tag tag-req">req</span></div><input class="req-criteria" data-ft-pi="'+pi+'" data-r="'+ri+'" placeholder="'+esc(S2.reqCriteria)+'" value="'+esc(r.criteria||'')+'"></div><button class="item-del freq-del" type="button" data-ft-pi="'+pi+'" data-r="'+ri+'" data-state="trash" data-del-title="Delete requirement" title="Delete requirement" aria-label="Delete requirement">'+ic('trash')+'</button></div></li>'
}
function ftEntryHTML(p,pi){
  var ft=p.featureTest||{},ftStatus;
  if(ft.testPlan)ftStatus='Test plan v'+ft.testPlan.version+(ft.testPlan.runs&&ft.testPlan.runs.length?' · last run: '+ft.testPlan.runs[ft.testPlan.runs.length-1].result:' · not run yet');
  else if(ft.testQA&&ft.testQA.status&&ft.testQA.status!=='idle')ftStatus='Test run in progress ('+ft.testQA.status+')';
  else if(ft.requirements&&ft.requirements.length)ftStatus=ft.requirements.length+' requirements · no test plan yet';
  else ftStatus='No test plan yet — click to set up';
  return'<div class="panel ft-entry-panel"><button class="ft-entry" id="ft-entry-'+pi+'" data-ft-pi="'+pi+'" type="button"><span class="ft-entry-icon">'+ic('flask')+'</span><span class="ft-entry-body"><b>'+S2.featureTestLabel+'</b><span class="ft-entry-sub">'+esc(ftStatus)+'</span></span><span class="ft-entry-chev">'+ic('chevron-right')+'</span></button></div>';
}
function featureTestHTML(p,pi){
  var ft=ftObj(p),reqs=ft.requirements||[],done=reqs.filter(function(r){return r.done}).length,tq=ft.testQA||{},tp=ft.testPlan||null;
  var html='<header class="detail-head"><button class="ft-back" id="ft-back-'+pi+'" data-ft-pi="'+pi+'" type="button" title="Back to project detail">'+ic('chevron-left')+'<span>'+S2.backToProjectLabel+'</span></button><div class="tile">'+ic('flask')+'</div><div><h2>'+S2.featureTestLabel+'</h2><span class="sub">for '+esc(p.name)+' · '+done+' of '+reqs.length+' '+S2.requirementsLabel+' done</span></div><button class="detail-close" title="Close" id="detail-close">'+ic('close')+'</button></header><div class="detail-body"><div class="col">';
  html+='<div class="panel"><div class="panel-head"><h4>'+S2.featureDescLabel+'</h4></div><textarea class="info-field feature-desc" id="feature-desc-'+pi+'" placeholder="e.g. The app should let users register named gestures, each with trigger points..." rows="4">'+esc(ft.description||'')+'</textarea><div class="feature-desc-hint">The agent reads this description and every requirement below when building the test plan.</div></div>';
  html+='<div class="panel plan-panel"><div class="panel-head"><h4>'+S2.testPlanLabel+'</h4><span class="countchip" id="tp-ver-'+pi+'">'+(tp?'v'+tp.version:'—')+'</span></div><div id="ft-qa-'+pi+'">'+(tq.status&&tq.status!=='idle'?renderFeatureQA(p,pi):(tp?renderTestPlanDoc(tp,pi):'<div class="empty">'+S2.noTestPlan+'</div>'))+'</div><div class="ft-actions">'+((tq.status&&tq.status!=='idle'&&tq.status!=='done')?'':'<button class="btn btn-primary ft-run" id="ft-run-'+pi+'" data-ft-pi="'+pi+'">'+ic('flask')+' <span>'+(tq.status==='done'?S2.reRunTestsLabel:S2.runTestsLabel)+'</span></button>')+'</div></div>';
  html+='<div class="panel"><div class="panel-head"><h4>'+S2.requirementsLabel+'</h4><span class="countchip">'+reqs.length+'</span></div><ul class="items">'+(reqs.length?reqs.map(function(r,i){return freqHTML(r,pi,i)}).join(''):'<li class="empty">'+S2.noItems+'</li>')+'</ul><div class="req-add"><input id="req-add-'+pi+'" type="text" placeholder="'+esc(S2.addRequirement)+'" autocomplete="off"><button class="btn btn-ghost req-add-btn" data-ft-pi="'+pi+'">Add</button></div></div>';
  html+='</div></div>';
  return html;
}
function renderTestPlanDoc(tp,pi){
  var html='<div class="plan-doc">';
  html+='<div class="plan-doc-meta"><span>Updated '+formatTS(tp.updated)+'</span><span class="ver-badge">v'+(tp.version||1)+'</span></div>';
  if(tp.summary)html+='<div class="plan-doc-summary">'+esc(tp.summary)+'</div>';
  if(tp.requirements&&tp.requirements.length){
    html+='<div class="plan-doc-sec"><h5>Requirement coverage</h5>';
    tp.requirements.forEach(function(r){var cov=r.coverage||'uncovered';html+='<div class="req-cov"><span class="cov-badge cov-'+esc(cov)+'">'+esc(cov)+'</span><span>'+esc(r.text)+'</span></div>';});
    html+='</div>';
  }
  if(tp.testCases&&tp.testCases.length){
    html+='<div class="plan-doc-sec"><h5>Test cases</h5>';
    tp.testCases.forEach(function(tc){
      var st=tc.status||'untested';
      html+='<div class="tc"><div class="tc-head"><span class="tc-id">'+esc(tc.id||'TC')+'</span><b>'+esc(tc.title||'')+'</b><span class="tc-status tc-'+esc(st)+'">'+esc(st)+'</span></div>';
      if(tc.steps&&tc.steps.length)html+='<ol class="tc-steps">'+tc.steps.map(function(s){return'<li>'+esc(s)+'</li>'}).join('')+'</ol>';
      if(tc.expected)html+='<div class="tc-expected">Expected: '+esc(tc.expected)+'</div>';
      if(tc.codeRefs&&tc.codeRefs.length)html+='<div class="tc-refs">'+tc.codeRefs.map(function(r){return'<span>'+esc(r)+'</span>'}).join('')+'</div>';
      if(tc.triggers&&tc.triggers.length)html+='<div class="tc-triggers">Triggers: '+tc.triggers.map(function(t){return esc(t)}).join(' → ')+'</div>';
      html+='</div>';
    });
    html+='</div>';
  }
  if(tp.runs&&tp.runs.length){
    html+='<div class="plan-doc-sec"><h5>Run history</h5>';
    tp.runs.forEach(function(r){html+='<div class="run-row"><span>'+esc(r.date||'')+'</span><b>'+esc(r.result||'')+'</b></div>';});
    html+='</div>';
  }
  html+='</div>';
  return html;
}
function matchItem(it,q){var parts=[it.text,it.tag,it.user_hint,it.criteria],qa=it.qa||{},i,j;
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
  var chip=q?vis:open;return'<header class="detail-head"><div class="tile">'+ic(p.icon||'folder')+'</div><div><h2>'+esc(p.name)+'</h2><span class="sub">'+p.tasks+' '+S2.tasksLabel+' · '+open+' '+S2.openLabel+' · '+p.progress+'% complete</span></div><span class="pill st-'+esc(p.status||'active')+'" style="margin-left:0"><span class="dot"></span>'+(p.statusLabel||p.status||'Active')+'</span><button class="detail-search'+(STATE.itemSearchOpen?' active':'')+'" id="detail-search" title="Search items" aria-label="Search items" aria-expanded="'+STATE.itemSearchOpen+'">'+ic('search')+'</button><button class="detail-del" type="button" data-del-p="'+pi+'" data-state="trash" data-del-title="Delete project" title="Delete project" aria-label="Delete project">'+ic('trash')+'</button><button class="detail-close" title="Close" id="detail-close">'+ic('close')+'</button></header><div class="detail-search-row"'+(STATE.itemSearchOpen?'':' style="display:none"')+'><span class="ic">'+ic('search')+'</span><input type="text" id="detail-search-input" class="detail-search-input" placeholder="Search titles and content…" value="'+esc(STATE.itemQ)+'" autocomplete="off" spellcheck="false"><button class="detail-search-clear" id="detail-search-clear" title="Clear search">'+ic('close')+'</button></div><div class="detail-body"><div class="panel"><div class="panel-head"><h4>'+S2.openItemsLabel+'</h4><span class="countchip">'+chip+'</span></div><ul class="items">'+list+'</ul></div>'+ftEntryHTML(p,pi)+'<div class="charts"><div class="panel"><div class="panel-head"><h4>'+S2.completionLabel+'</h4></div>'+donut(done,items.length)+'</div>'+(p.chart&&p.chart.length?'<div class="panel"><div class="panel-head"><h4>'+S2.velocityLabel+'</h4></div>'+barChart(p.chart)+'</div>':'')+'<div class="minimetrics">'+mini('commit',m.commits||0,S2.commitsLabel)+mini('pr',m.prs||0,S2.prsLabel)+mini('users',m.reviewers||0,S2.revsLabel)+mini('calendar',m.daysLeft||0,S2.daysLeftLabel)+'</div></div></div><div class="panel info-panel"><div class="panel-head"><h4>Project info</h4></div><textarea class="info-field" id="info-'+pi+'" placeholder="e.g. key file paths, context, notes the agent should know…" rows="3">'+esc(p.info||'')+'</textarea></div>'
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
function renderDetail(){var zone=document.getElementById('detail-zone'),sub=document.getElementById('detail-sub');sub=sub||{};sub.textContent=sub.textContent||function(){};if(!STATE.activeId){var lst=visible();if(!lst.length){zone.innerHTML='<div class="detail-placeholder">'+CFG.detail.defaultMessage+'</div>';sub.textContent('');return}sub.textContent(S2.allProjectsLabel);zone.innerHTML='<div class="detail" id="detail-active">'+totalDetailHTML(lst)+'</div>';var trs=zone.querySelectorAll('.tproj');for(var t=0;t<trs.length;t++){(function(el){el.addEventListener('click',function(){activate(el.dataset.id)});el.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();activate(el.dataset.id)}})})(trs[t])}return}var pi=-1,p=null;for(var i=0;i<STATE.projects.length;i++){if(STATE.projects[i].id===STATE.activeId){pi=i;p=STATE.projects[i];break}}if(!p){zone.innerHTML='<div class="detail-placeholder">'+CFG.detail.defaultMessage+'</div>';sub.textContent('');return}sub.textContent(p.name);zone.innerHTML='<div class="detail" id="detail-active">'+((STATE.view==='feature'&&p.featureTest)?featureTestHTML(p,pi):detailHTML(p,pi))+'</div>';document.getElementById('detail-close').addEventListener('click',function(){activate(null)});restoreItemSearch();restoreOpenItem()}
function activate(id){if(id!==STATE.activeId){STATE.openItem=null;STATE.itemQ='';STATE.itemSearchOpen=false;STATE.view='project';}STATE.activeId=id;if(!id)STATE.openItem=null;savePageState();var i,els=document.querySelectorAll('.card');for(i=0;i<els.length;i++)els[i].classList.toggle('active',els[i].dataset.id===id);renderDetail()}
function bindDetailCheckboxes(){
  var zone=document.getElementById('detail-zone');if(!zone)return;
  zone.addEventListener('change',function(e){
    var t=e.target;if(t.type!=='checkbox'||t.dataset.p===undefined)return;
    var p=STATE.projects[+t.dataset.p],item=p.items[+t.dataset.i];if(!p||!item)return;
    item.done=t.checked;p.open=p.items.filter(function(i){return!i.done}).length;renderCards();renderDetail();
  });
  zone.addEventListener('focusout',function(e){
    var t=e.target;
    if(!t||!t.classList||!t.classList.contains('info-field'))return;
    var pi=parseInt((t.id||'').replace('info-',''),10);
    if(!isNaN(pi))saveProjectInfo(pi,t.value);
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
    chatToSession(sid,item.qa&&item.qa.session_id?item.text:'Project Tracker — General Chat','[Project Readiness] User opened chat for item: '+item.text+' | Project: '+p.name,false,false,false,CFG.prompts.openChat_userMessage||DEF_PROMPTS.openChat_userMessage);
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
    if(closest(e.target,'.ft-q-row'))return; // feature-test questions toggle via bindFeatureDetail
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
    chatToSession(sid,item.qa&&item.qa.session_id?item.text:'Project Tracker — General Chat','[Project Readiness] Open in Chat: '+item.text+' | Project: '+p.name,false,false,false,CFG.prompts.openChat_userMessage||DEF_PROMPTS.openChat_userMessage);
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
function setItemQuery(v){STATE.itemQ=v;var inp=document.getElementById('detail-search-input'),pos=inp?inp.selectionStart:null;renderDetail();savePageState();var n=document.getElementById('detail-search-input');if(n){try{n.focus();if(pos!==null)n.setSelectionRange(pos,pos)}catch(_){}}}
function clearItemSearch(refocus){STATE.itemQ='';savePageState();renderDetail();if(refocus){var n=document.getElementById('detail-search-input');if(n)try{n.focus()}catch(_){}}}
function toggleItemSearch(forceClose){STATE.itemSearchOpen=forceClose?false:!STATE.itemSearchOpen;if(!STATE.itemSearchOpen)STATE.itemQ='';savePageState();renderDetail();if(STATE.itemSearchOpen){var n=document.getElementById('detail-search-input');if(n)try{n.focus()}catch(_){}}}
function restoreItemSearch(){var row=document.getElementById('detail-search-row'),trig=document.getElementById('detail-search');if(row)row.style.display=STATE.itemSearchOpen?'flex':'none';if(trig)trig.classList.toggle('active',STATE.itemSearchOpen)}

