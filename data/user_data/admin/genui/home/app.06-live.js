/* ========================================================================
 * PART 06/14 - app.06-live.js             (grep: GENUI-SPLIT)
 * ROLE:  Live-data module (_liveData: polls /api/v1/genui/home/data?
 *        user_id=admin, merges agent-written updates, surgical re-render,
 *        session-tail watcher) + restoreOpenItem() + chart helpers
 *        (donut, barChart, mini, itemRank, sortItems).
 * ORDER: Part 6 - uses STATE/BAG (02/05); init() is called from boot (14).
 * NOTE:  _poll() must call the local XHR _fetch(cb) - the native global
 *        fetch() does NOT accept a callback and silently kills polling.
 * VERIFY: syntax-check, then screenshot_genui('home','both').
 * ======================================================================== */

/* ── LIVE DATA: genui-live.standard (inlined) ──
 * Polls GET /api/v1/genui/home/data for agent-written updates so items
 * advance their status/thread/plan/execution-log without a page refresh.
 * Compact standalone so the page stays self-contained; the canonical
 * reusable module lives at ui/main-panel/genui/genui-live.standard.js. */
var _liveData=null;
(function(){
  var IDLE=15000,FAST=3000,COAL=1000;
  function authH(){var t=null;try{t=localStorage.getItem('auth_token')}catch(_){}return t?{'Authorization':'Bearer '+t}:{}}
  function uidF(){try{var u=localStorage.getItem('auth_user_id')||'';if(u&&u.indexOf('anon_')!==0)return u}catch(_){}return'admin'}
  function stable(o){try{return JSON.stringify(o,function(k,v){if(v&&typeof v==='object'&&!Array.isArray(v)){var ks=Object.keys(v).sort(),r={};for(var i=0;i<ks.length;i++)r[ks[i]]=v[ks[i]];return r}return v})}catch(_){return''}}
  function _fetch(cb){
    try{var x=new XMLHttpRequest();x.open('GET','/api/v1/genui/home/data?user_id=admin',true);
    var h=authH();for(var k in h)if(h.hasOwnProperty(k))x.setRequestHeader(k,h[k]);
    x.timeout=10000;x.onload=function(){if(x.status===200)try{var b=JSON.parse(x.responseText);cb(b&&b.status==='ok'&&b.data?b.data:null)}catch(e){cb(null)}else cb(null)};
    x.onerror=function(){cb(null)};x.ontimeout=function(){cb(null)};x.send()}catch(e){cb(null)}}
  function _apply(fresh){
    if(!fresh||!fresh.projects)return;
    var pi=-1,oi=STATE.openItem;
    // Preserve user-entered text before re-render
    var savedInputs=_captureInputs();
    var changedItems=[];
    // Merge fresh projects into STATE
    for(var i=0;i<fresh.projects.length;i++){
      var fp=fresh.projects[i],ep=null;
      for(var j=0;j<STATE.projects.length;j++){if(STATE.projects[j].id===fp.id){ep=STATE.projects[j];break}}
      if(!ep){STATE.projects.push(fp);continue}
      // Find changed items (compare qa blobs)
      var fi=fp.items||[],ei=ep.items||[];
      for(var k=0;k<fi.length;k++){
        var fi2=fi[k],ei2=ei[k]||null;
        if(!ei2||stable(fi2)!==stable(ei2)){
          if(ei2){for(var mk in fi2)if(fi2.hasOwnProperty(mk))ei2[mk]=fi2[mk]}
          changedItems.push({project:fp,pi:i,ii:k,item:fi2});
        }
      }
      // Overwrite project-level fields that the agent may have changed
      ep.name=fp.name;ep.status=fp.status;ep.statusLabel=fp.statusLabel;
      ep.progress=fp.progress;ep.metrics=fp.metrics;ep.chart=fp.chart;
      ep.items=fi;ep.info=fp.info;recalcProject(ep);
    }
    STATE.sessions=fresh.sessions||STATE.sessions||{};
    BAG.config=fresh.config||BAG.config||{};
    BAG.projects=STATE.projects;
    // Surgical re-render: only changed items + their owning cards
    renderCards();
    if(oi)renderDetail();
    _restoreInputs(savedInputs);
    savePageState();
  }
  function _captureInputs(){
    var map={};
    try{
      var ins=document.querySelectorAll('input[id],textarea[id]');
      for(var i=0;i<ins.length;i++){
        var el=ins[i];if(!el.id)continue;
        if(el.tagName==='TEXTAREA'||el.type==='text'||el.type==='search')
          map[el.id]=el.value;
      }
    }catch(e){}
    return map;
  }
  function _restoreInputs(map){
    if(!map)return;
    try{
      for(var id in map)if(map.hasOwnProperty(id)){
        var el=document.getElementById(id);
        if(el&&(el.tagName==='TEXTAREA'||el.type==='text'||el.type==='search')&&el.value!==map[id])
          el.value=map[id];
      }
    }catch(e){}
  }
  // Core live loop
  var _dtimer=0,_dlast='',_dlastMs=0,_dhid=false,_wapi=null,_wgetSids=null,_wonRun=null,_wactSess={};
  function _poll(){_fetch(function(data){if(!data){_sched();return};if(stable(data)!==_dlast){_dlast=stable(data);var n=Date.now();if(n-_dlastMs>=COAL){_dlastMs=n;_apply(data)}}_sched()})}
  function _sched(){clearTimeout(_dtimer);var has=false;for(var k in _wactSess){if(_wactSess[k].running){has=true;break}}_dtimer=setTimeout(_poll,_dhid?IDLE:has?FAST:IDLE)}
  function _wake(){clearTimeout(_dtimer);_dtimer=setTimeout(_poll,150)}
  function _onVis(){_dhid=document.visibilityState==='hidden';if(!_dhid){clearTimeout(_dtimer);_dtimer=setTimeout(_poll,200)}}
  _liveData={init:function(){
    document.addEventListener('visibilitychange',_onVis);
    _poll();
    return{watchSessions:function(o){
      var o2=o||{};_wapi=o2.api||null;_wgetSids=o2.getSessions||null;_wonRun=o2.onRun||null;
      if(_wapi&&typeof _wapi.onSessionActivity==='function')_wapi.onSessionActivity(function(evt){
        _wake();if(evt&&evt.session_id){
          var e=_wactSess[evt.session_id];if(e){e.lastSeen=Date.now();if(evt.type==='tool_call'||evt.type==='pipeline'||evt.type==='response')e.running=true}
        }
      });
      setInterval(function(){if(!_wgetSids)return;var sids;try{sids=_wgetSids()}catch(_){return}
        if(!sids||!sids.length)return;for(var i=0;i<sids.length;i++){(function(sid){
        if(!sid)return;if(!_wactSess[sid])_wactSess[sid]={lastSeen:0,running:false};
        try{var x=new XMLHttpRequest();x.open('GET','/api/v1/db/session-tail?session_id='+encodeURIComponent(sid)+'&after_session_seq=0',true);
        var h=authH();for(var k in h)if(h.hasOwnProperty(k))x.setRequestHeader(k,h[k]);
        x.timeout=8000;x.onload=function(){if(x.status===200)try{var b=JSON.parse(x.responseText);var r=b.run||null;
          _wactSess[sid].running=r?!!r.active:false;if(_wonRun)try{_wonRun(sid,r)}catch(_){}}catch(e){}};
        x.onerror=function(){};x.ontimeout=function(){};x.send()}catch(e){}})(sids[i])}},5000);
    },refresh:function(){_dlastMs=0;_wake()},destroy:function(){clearTimeout(_dtimer);_dtimer=0;
      try{document.removeEventListener('visibilitychange',_onVis)}catch(_){}}
  };}};
})();

function restoreOpenItem(){
  var ai=STATE.openItem;if(!ai)return;
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

