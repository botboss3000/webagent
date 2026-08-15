/* ========================================================================
 * PART 10/14 - app.10-qa.js               (grep: GENUI-SPLIT)
 * ROLE:  QA core: renderQAContent() (thread, clarifying questions, plan
 *        review, execution log), the research/answer/skip/plan-decision
 *        buttons and their session routing.
 * ORDER: Part 10 - uses prompts (03), session routing (04), STATE (05).
 * EDIT:  Plan/QA state shape must match what data.json stores and what
 *        the Project Readiness agent writes back.
 * VERIFY: syntax-check, then screenshot_genui('home','both').
 * ======================================================================== */

/* ── QA Core ── */
function startQA(pi,ii){
  var p=STATE.projects[pi],item=p.items[ii];
  if(!item.qa)item.qa={status:'idle',session_id:null,thread:[],plan:null,plan_status:null,plan_history:[],execution_log:[],research_notes:null,effort:null,impact:null,related_items:[]};
  // Every research run gets a BRAND-NEW session — never resume the previous
  // one for this item (it may be stopped, recycled, or stale). The old session
  // is left untouched in the session list; this run starts clean in its own
  // normal, visible session named after the item.
  item.qa.session_id=newId();
  item.qa.status='planning';
  item.qa.thread=[{role:'agent',text:'Analyzing this item and researching the codebase...',ts:new Date().toISOString()}];
  item.qa.questions=[];item.qa.plan=null;item.qa.plan_status=null;item.qa.execution_log=[];
  saveBag();  // persist QA state so it survives page refresh
  renderCards();renderDetail();setTimeout(function(){toggleQA(pi,ii)},100);
  // Route this task into its OWN dedicated session (research → plan → execute
  // all happen in that one session), named after the task.
  var sid=item.qa.session_id,msg=buildQAPrompt(pi,ii,null,null);
  chatToSession(sid,item.text,msg,false,false,false,CFG.prompts.qa.template_userMessage||DEF_PROMPTS.qa.template_userMessage);
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
  if(isOpen){el.classList.remove('open');STATE.openItem=null;savePageState();var li2=el.closest('.item');if(li2)li2.classList.remove('open');}
  else{el.classList.add('open');renderQAContent(pi,ii);if(p){STATE.openItem={p:p.id,i:ii};savePageState();}var li3=el.closest('.item');if(li3)li3.classList.add('open');}
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
  if(qa.status==='plan_ready'&&qa.plan){html+=renderPlanCard(item,qa,pi,ii);
    html+='<div class="exec-next"><span class="exec-next-label">Next:</span> Review & accept the plan above</div>';}
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
  // Answers travel into the SAME session the agent asked in — send only the
  // answer block, not the full project/thread replay (redundant).
  var P=CFG.prompts.qa||{};
  var msg=tplt(P.userAnswer||DEF_PROMPTS.qa.userAnswer,{answer:answer});
  chatToSession(qaSession(pi,ii),item.text,msg,false,false,false,P.userAnswer_userMessage||DEF_PROMPTS.qa.userAnswer_userMessage);toast('Answer sent \u2713');
}

function skipToPlan(pi,ii){
  var p=STATE.projects[pi],item=p.items[ii],qa=item.qa;if(!qa)return;
  qa.thread.push({role:'user',text:'[User chose to skip questions — proceed to plan]',ts:new Date().toISOString()});
  qa.status='planning';saveBag();renderCards();renderDetail();
  // Same-session dispatch — only the instruction travels, no context replay.
  var P=CFG.prompts.qa||{};
  chatToSession(qaSession(pi,ii),item.text,P.skip||DEF_PROMPTS.qa.skip,false,false,false,P.skip_userMessage||DEF_PROMPTS.qa.skip_userMessage);toast('Skipping to plan...');
}
function sendPlanAction(pi,ii,action,comment){
  var p=STATE.projects[pi],item=p.items[ii],qa=item.qa;if(!qa)return;
  if(action==='accept'){qa.plan_status='accepted';qa.status='executing';qa.execution_log=[{step:'Plan accepted — starting execution',status:'running',detail:''}];}
  else if(action==='reject_with_comment'){
    if(!qa.plan_history)qa.plan_history=[];qa.plan_history.push(JSON.parse(JSON.stringify(qa.plan||{})));
    qa.plan_status='rejected';qa.status='planning';qa.thread.push({role:'user',text:'Plan rejected: '+comment,ts:new Date().toISOString()});
    var row=document.getElementById('plan-comment-row-'+pi+'-'+ii);if(row)row.style.display='none';
  }
  saveBag();renderCards();renderDetail();
  // The plan decision is sent into the SAME session the agent is already
  // working in, so only the decision travels — replaying the full project
  // context + Q&A thread is redundant (already sent at research start).
  var P=CFG.prompts.qa||{};
  var decision=action==='accept'?'accept':'reject'+(comment?': '+comment:'');
  var msg=tplt(P.planAction||DEF_PROMPTS.qa.planAction,{decision:decision});
  chatToSession(qaSession(pi,ii),item.text,msg,false,false,false,P.planAction_userMessage||DEF_PROMPTS.qa.planAction_userMessage);
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
  // Execution log entries
  if(qa.execution_log&&qa.execution_log.length)qa.execution_log.forEach(function(e){
    html+='<div class="exec-entry"><span class="exec-dot '+esc(e.status||'running')+'"></span><span class="exec-step">'+esc(e.step||'')+'</span><span class="exec-detail">'+esc(e.detail||'')+'</span></div>'
  });
  // ── Next step indicator ────────────────────────────────────────────────
  // Find the first log entry still 'running' → that's the current step.
  // The NEXT unstarted plan step is the one after it. If everything is
  // done, show a completion note. If no plan yet, show a generic "Working…"
  var nextText='',plan=qa.plan||{},log=qa.execution_log||[],ps=plan.steps||[];
  var currentIdx=-1;
  for(var x=0;x<log.length;x++){if(log[x].status==='running'){currentIdx=x;break}}
  if(currentIdx>=0&&currentIdx+1<ps.length){
    nextText=ps[currentIdx+1];
  }else if(currentIdx<0&&log.length>0&&ps.length>log.length){
    // No running entry found — pick the first unlogged plan step
    nextText=ps[log.length];
  }else if(log.length>0&&log[log.length-1].status==='done'&&log.length<ps.length){
    nextText=ps[log.length];
  }
  if(nextText)html+='<div class="exec-next"><span class="exec-next-label">Next:</span> '+esc(nextText)+'</div>';
  else if(log.length>0&&log[log.length-1].status==='done'&&(!ps.length||log.length>=ps.length))
    html+='<div class="exec-next exec-next-done">'+ic('check')+' All steps complete</div>';
  else if(log.length>0&&log[log.length-1].status==='done')
    html+='<div class="exec-next exec-next-done">'+ic('check')+' Complete — awaiting next plan step</div>';
  html+='</div>';return html;
}

