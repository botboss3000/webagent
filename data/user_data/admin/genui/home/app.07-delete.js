/* ========================================================================
 * PART 07/14 - app.07-delete.js           (grep: GENUI-SPLIT)
 * ROLE:  Two-click delete confirm (trash -> hazard -> delete) for items
 *        and projects, via data-del-pi/data-del-ii/data-state attributes.
 * ORDER: Part 7 - handlers bind onto DOM produced by app.08 renderers.
 * EDIT:  Keep the trash->hazard->delete affordance; no one-click deletes.
 * VERIFY: syntax-check, then screenshot_genui('home','both').
 * ======================================================================== */

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
  if(STATE.openItem&&STATE.openItem.p===p.id&&STATE.openItem.i===ii)STATE.openItem=null;
  p.items.splice(ii,1);recalcProject(p);
  saveBag();savePageState();renderCards();renderDetail();
  toast('Item deleted');
}
function deleteProject(pi){
  var p=STATE.projects[pi];if(!p)return;
  var name=p.name;
  STATE.projects.splice(pi,1);
  if(STATE.activeId===p.id){STATE.activeId=null;STATE.openItem=null;}
  saveBag();savePageState();renderCards();renderDetail();
  toast('Project "'+name+'" deleted');
}
function saveProjectInfo(pi,val){
  var p=STATE.projects[pi];if(!p)return;
  p.info=String(val||'');saveBag();
}

