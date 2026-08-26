/* ========================================================================
 * PART 11/14 - app.11-stop.js             (grep: GENUI-SPLIT)
 * ROLE:  Stop Operation: qa-stop-btn on planning/executing/questions
 *        items - stops the item's agent session run.
 * ORDER: Part 11 - buttons are rendered by app.08 itemHTML.
 * VERIFY: syntax-check, then screenshot_genui('home','both').
 * ======================================================================== */

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

/* ── Stop a FEATURE test run (project-level, featureTest.testQA) ── */
function requestStopFeature(pi){
  var p=STATE.projects[pi],ft=p&&p.featureTest,tq=ft&&ft.testQA||{};if(!tq.session_id)return;
  var sid=tq.session_id,btn=document.getElementById('ft-stop-'+pi);
  if(btn){btn.disabled=true;btn.textContent='Stopping...';btn.style.opacity='0.6';}
  fetch('/api/v1/chat/interrupt',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify({session_id:sid})})
  .then(function(r){return r.json()}).then(function(data){
    if(data.was_running)startStopPollFeature(pi,sid);else confirmFeatureStopped(pi,'Session was not running');
  }).catch(function(e){confirmFeatureStopped(pi,'Interrupt failed: '+e.message)});
}
function startStopPollFeature(pi,sid){
  var attempts=0,key='f'+pi;if(STOP_POLLERS[key])clearInterval(STOP_POLLERS[key]);
  STOP_POLLERS[key]=setInterval(function(){
    attempts++;
    fetch('/api/v1/db/session-tail?session_id='+encodeURIComponent(sid)+'&after_session_seq=0',{method:'GET',credentials:'include'})
    .then(function(r){return r.json()}).then(function(data){
      var run=data.run||{};if(!run.active){clearInterval(STOP_POLLERS[key]);delete STOP_POLLERS[key];confirmFeatureStopped(pi,null);}
      else if(attempts>=15){clearInterval(STOP_POLLERS[key]);delete STOP_POLLERS[key];confirmFeatureStopped(pi,'Stop requested — may still finalize');}
    }).catch(function(){if(attempts>=15){clearInterval(STOP_POLLERS[key]);delete STOP_POLLERS[key];confirmFeatureStopped(pi,'Unable to verify');}});
  },2000);
}
function confirmFeatureStopped(pi,msg){
  var btn=document.getElementById('ft-stop-'+pi);
  if(btn){btn.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>Stopped';btn.style.background='var(--success)';btn.style.opacity='1';btn.disabled=false;if(msg)btn.title=msg;setTimeout(function(){if(btn&&btn.isConnected)btn.style.display='none'},4000);}
  var p=STATE.projects[pi],ft=p&&p.featureTest,tq=ft&&ft.testQA;
  if(tq&&(tq.status==='planning'||tq.status==='executing'||tq.status==='questions')){tq.status='idle';saveBag();renderCards();renderDetail();toast('Operation stopped');}
}

