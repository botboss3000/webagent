/* ========================================================================
 * PART 12/14 - app.12-chevrons.js         (grep: GENUI-SPLIT)
 * ROLE:  Carousel chevrons: wireCarousel(), left/right scroll, edge-fade
 *        mask visibility (--car-fade).
 * ORDER: Part 12 - operates on #car-track; boot (14) calls wireCarousel.
 * VERIFY: syntax-check, then screenshot_genui('home','both').
 * ======================================================================== */

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

