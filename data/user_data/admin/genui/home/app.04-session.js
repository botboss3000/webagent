/* ========================================================================
 * PART 04/14 - app.04-session.js          (grep: GENUI-SPLIT)
 * ROLE:  Session routing: every chat surface (quick note, card chat, item
 *        QA, plan decisions) gets its OWN agent session; builds the chat/
 *        action payloads and sends them via WebagentGenui.api.
 * ORDER: Part 4 - uses BAG.config.chatConfig + CFG.prompts (02/03).
 * EDIT:  Session targets/reuse keys mirror the page's session_config
 *        (page.json / widget.json). Keep them consistent.
 * VERIFY: syntax-check, then screenshot_genui('home','both').
 * ======================================================================== */

/* ── SESSION ROUTING — each chat surface gets its OWN independent session ── */
function newId(){try{if(window.crypto&&crypto.randomUUID)return crypto.randomUUID()}catch(e){}return'genui-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,10)}
function ensureSession(key,title){
  var s=STATE.sessions||(STATE.sessions={});
  if(s[key]){
    // Heal a dead session: if the stored id was recycled, burn it
    // so the page self-corrects without a server restart.
    checkSessionAlive(s[key],key);
  } else {s[key]=newId();saveBag()}
  return s[key];
}
function checkSessionAlive(sid,key){
  // Lightweight probe — a 404 means dead. Fire-and-forget.
  try{
    fetch('/api/v1/db/sessions/'+encodeURIComponent(sid)+'?db=local.db',{
      headers:{'Authorization':'Bearer '+(localStorage.getItem('auth_token')||'')}
    }).then(function(r){
      if(!r.ok){STATE.sessions[key]=null;saveBag();console.warn('Session '+sid.slice(0,8)+' is dead — cleared')}
    }).catch(function(){});
  }catch(_){}
}
function qaSession(pi,ii){
  var p=STATE.projects[pi],item=p.items[ii];if(!p||!item)return'';
  if(!item.qa)item.qa={status:'idle',session_id:null,thread:[],plan:null,plan_status:null,plan_history:[],execution_log:[],research_notes:null,effort:null,impact:null,related_items:[]};
  if(!item.qa.session_id){item.qa.session_id=newId();saveBag()}
  return item.qa.session_id;
}
function chatToSession(sid,title,text,silent,raw,forceNew,label){
  try{
    var api=window.WebagentGenui&&window.WebagentGenui.api;if(!api||!api.chat||!sid)return false;
    try{window.__genuiSessionOverride=sid}catch(e){}
    // Friendly label for this dispatch: the chat panel shows a green notice
    // instead of the raw prompt as a "You" bubble. Consumed once by the genui
    // bridge (mirrors __genuiSessionOverride).
    if(label)try{window.__genuiLabelOverride=label}catch(e){}
    var opts={silent:!!silent};
    if(raw)opts.raw=true;
    if(forceNew)opts.forceNewSession=true;
    if(title&&api.nameSession)api.nameSession(title);
    api.chat(text,opts);return true;
  }catch(e){return false}
}
function directChat(msg,label){
  // Bypass the genui dispatch pipeline entirely — no buildTaggedGenuiPrompt,
  // no agent_context injection. The message sent is exactly what the caller
  // builds. Used for quick notes where the prompt in data.json is complete.
  // Reads identity from localStorage (auth_token / auth_user_id) because the
  // genui page's scripts cannot see the app module's `app` object. Mirrors
  // ui/shared/js/config.js apiPath() so it works under a subdirectory base.
  try{
    var sid=newId();
    var tok=localStorage.getItem('auth_token')||'';
    var uid=localStorage.getItem('auth_user_id')||'';
    if(!uid||!tok)return '';
    var p=window.location.pathname||'/';
    p=p.replace(/\/index\.html?$/i,'');
    if(p.length>1&&p.endsWith('/'))p=p.slice(0,-1);
    if(/^\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(p))p='';
    else if(['/app','/setup','/setup.html'].indexOf(p.toLowerCase())>=0)p='';
    else if(p==='/')p='';
    var body={user_id:uid,session_id:sid,message:msg,execution_mode:'auto'};
    // Friendly label so the chat panel shows a green notice instead of the raw
    // prompt as a "You" bubble (the raw prompt still reaches the agent).
    if(label)body.genui_label=label;
    fetch(p+'/api/v1/chat/send',{method:'POST',
      headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},
      body:JSON.stringify(body)
    }).catch(function(){});
    return sid;
  }catch(e){return ''}
}
function tplt(s,map){return s.replace(/\$\{(\w+)\}/g,function(_,k){return map[k]!==undefined?map[k]:'${'+k+'}'})}
function cardChatPrompt(p,text){var cfg=CFG.cardChat&&CFG.cardChat.template?CFG.cardChat.template:'';if(!cfg)return'['+p.name+'] '+text;var oi=p.items?p.items.filter(function(i){return !i.done}):[],items=oi.map(function(i){return i.text+' ('+(i.tag||'task')+')'}).join('; ')||'none',done=p.items?p.items.filter(function(i){return i.done}).length:0,m=p.metrics||{},agent=p.chatAgentId||(CFG.cardChat&&CFG.cardChat.agentId)||'',map={name:p.name,status:p.statusLabel||p.status||'Active',progress:p.progress,open:p.open,tasks:p.tasks,daysLeft:m.daysLeft,commits:m.commits,prs:m.prs,reviewers:m.reviewers,items:items,done:done,chart:(p.chart||[]).join(', ')||'none',notes:(p.chatNotes||'')+((CFG.cardChat&&CFG.cardChat.detailNote)||''),agentId:agent,text:text};window.__genuiCardAgentId=agent||null;return'['+p.name+'] '+tplt(cfg,map)}

var STATE={projects:BAG.projects||[],q:'',activeId:null,openItem:null,itemQ:'',itemSearchOpen:false,view:'project',sessions:(BAG.sessions||{})};
STATE.projects.forEach(recalcProject); // Fix 2: derive tasks/open from items on load (no stored counters)
var toastTimer=null,carUpd=null;

function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function slug(s){return String(s).toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'')||'project'}
function toast(msg){var t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(function(){t.classList.remove('show')},CFG.toast.duration)}

