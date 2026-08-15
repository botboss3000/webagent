/* ========================================================================
 * PART 13/14 - app.13-sticky.js           (grep: GENUI-SPLIT)
 * ROLE:  Sticky layout: bindSticky() + measure() computing --car-h so the
 *        detail head sticks right below the pinned carousel.
 * ORDER: Part 13 - measure() is also wired to window resize; runs after
 *        renderers exist (boot calls bindSticky).
 * VERIFY: syntax-check, then screenshot_genui('home','both').
 * ======================================================================== */

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

/* ── MOBILE KEYBOARD COLLAPSE ──
 * On phones the keyboard leaves almost no vertical room, and the two pinned bars
 * (carousel + detail head) eat the rest. While the keyboard is ACTUALLY UP and an
 * input/textarea/select has focus we toggle .kb-open on <main> and styles.css
 * hides both pinned bars so the field being edited gets the whole viewport.
 * The bars return the moment the keyboard is dismissed — even if the field keeps
 * focus (iOS hide-keyboard button / Android back / swipe down) — because
 * "keyboard up" is detected from the VISUAL VIEWPORT shrinking, not from focus
 * alone: window.visualViewport.height collapses when the keyboard shows and
 * restores when it hides, regardless of focus state.
 *
 * Detection: compare visualViewport.height against the LARGEST height seen so far
 * (the keyboard-hidden baseline) — NOT against window.innerHeight. On Android
 * Chrome and iOS 16+ Safari the layout viewport (innerHeight) ALSO shrinks when
 * the keyboard opens, so innerHeight and visualViewport end up equal and a naive
 * comparison never detects the keyboard. The baseline is re-learned after an
 * orientation change. No visualViewport support → fall back to focus-only (hide
 * on focus, show on blur). Pinch-zoom (scale > 1) is never treated as a keyboard.
 * Desktop is untouched (the CSS rule is media-scoped to <=800px).
 *
 * SCROLL RESTORE: hiding/showing the headers changes the document height, and
 * browsers re-anchor the scroll when the keyboard closes, so the user can land
 * in a different spot than they started. We remember the page position at the
 * moment the user engages an editable field (pointerdown — BEFORE the browser
 * scrolls the field into view / opens the keyboard) and put it back when the
 * headers reappear (keyboard dismissed OR field blurred). A few bounded retries
 * cover the browser's own scroll adjustments during the hide animation; we stop
 * after ~400ms so a deliberate user scroll is never fought. */
function bindKbCollapse(){
  var main=document.querySelector('main');
  if(!main){try{var r=window.WebagentGenui&&WebagentGenui.root;if(r)main=r.querySelector('main');}catch(e){}}
  if(!main)return;
  var timer=null;
  var vv=window.visualViewport;
  var baseline=0;    /* tallest viewport height seen = the keyboard-hidden size */
  var fieldOn=false; /* is an editable field focused? kept in sync via focus events */
  var restoreY=null,restoreX=null,restorePending=false,pendingT=null;
  function editable(t){return t&&(t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.tagName==='SELECT')}
  /* focusin/focusout/pointerdown are composed events: a listener on the document
   * sees e.target RETARGETED to the shadow host, not the focused element. Walk
   * composedPath() to the original target inside the shadow. */
  function realTarget(e){
    try{var p=e.composedPath&&e.composedPath();if(p&&p.length)return p[0];}catch(err){}
    return e.target;
  }
  /* Cross-check: the focused field may sit inside the genui's shadow root, so the
   * outer document's activeElement is the host element — resolve through its
   * shadowRoot to the real editable field. Catches focus changes that missed the
   * focus events above. */
  function fieldFocusedNow(){
    var ae=document.activeElement;
    try{if(ae&&ae.shadowRoot&&ae.shadowRoot.activeElement)ae=ae.shadowRoot.activeElement;}catch(e){}
    return editable(ae);
  }
  function currentHeight(){return vv?vv.height:(window.innerHeight||0)}
  function trackBaseline(){
    var h=currentHeight();
    if(h>baseline)baseline=h;
  }
  function keyboardShown(){
    if(!vv)return true;                 /* no visualViewport: focus-only mode */
    var h=vv.height;
    if(vv.scale>1.02)return false;      /* pinch-zoom, not a keyboard */
    return baseline>0&&h<baseline-80;   /* keyboard = viewport well below its max */
  }
  /* Read the page scroll exactly like the page's own persistence does
   * (GenUIState.scrollY/scrollX): the app's #genui-host when present, else the
   * window. */
  function scrollEl(){try{return document.getElementById('genui-host')}catch(e){return null}}
  function pageY(){var h=scrollEl();try{return h?(h.scrollTop||0):(window.pageYOffset||document.documentElement.scrollTop||0)}catch(e){return 0}}
  function pageX(){var h=scrollEl();try{return h?(h.scrollLeft||0):(window.pageXOffset||document.documentElement.scrollLeft||0)}catch(e){return 0}}
  /* Remember where the page was BEFORE the browser scrolls the field into view /
   * opens the keyboard, so the user can be put back there when the headers
   * reappear. Recorded once per keyboard episode (first editable pointerdown or
   * focusin); dropped if the keyboard never actually opens (~1s). */
  function recordPosition(){
    if(restorePending)return;
    restoreY=pageY();restoreX=pageX();
    restorePending=true;
    if(pendingT)clearTimeout(pendingT);
    pendingT=setTimeout(function(){restorePending=false;pendingT=null;restoreY=null;restoreX=null},1000);
  }
  function clearPending(){
    restorePending=false;
    if(pendingT){clearTimeout(pendingT);pendingT=null}
    restoreY=null;restoreX=null;
  }
  /* Put the scroll back to the pre-keyboard position. Bounded retries (0/110/220/
   * 330ms) survive the browser's own scroll restore during the keyboard-hide
   * animation; anything later is the user's own move and is left alone. */
  function restorePosition(){
    if(!restorePending)return;
    if(pendingT){clearTimeout(pendingT);pendingT=null}
    var y=restoreY,x=restoreX,tries=0;
    restorePending=false;
    function go(){
      if(tries++>=4)return;
      try{
        var h=scrollEl();
        if(h){
          if(x!==null&&Math.abs(h.scrollLeft-x)>2)h.scrollLeft=x;
          if(y!==null&&Math.abs(h.scrollTop-y)>2)h.scrollTop=y;
        }else{
          window.scrollTo(x!==null?x:0,y!==null?y:0);
        }
      }catch(e){}
      setTimeout(go,110);
    }
    go();
  }
  function apply(){
    var wasOpen=main.classList.contains('kb-open');
    var nowOpen=(fieldOn||fieldFocusedNow())&&keyboardShown();
    main.classList.toggle('kb-open',nowOpen);
    if(!wasOpen&&nowOpen&&pendingT){clearTimeout(pendingT);pendingT=null} /* keyboard opened: keep the recorded target */
    if(wasOpen&&!nowOpen){                                               /* headers came back */
      var zoom=vv&&vv.scale>1.02;
      if(!zoom)restorePosition();                                        /* …but not because of pinch-zoom */
    }
  }
  document.addEventListener('pointerdown',function(e){if(editable(realTarget(e)))recordPosition()},{capture:true,passive:true});
  document.addEventListener('touchstart',function(e){if(editable(realTarget(e)))recordPosition()},{capture:true,passive:true});
  document.addEventListener('focusin',function(e){
    if(timer){clearTimeout(timer);timer=null}
    fieldOn=editable(realTarget(e));
    if(fieldOn)recordPosition();
    apply();
  },true);
  document.addEventListener('focusout',function(e){
    if(editable(realTarget(e)))timer=setTimeout(function(){fieldOn=false;apply()},0);
  },true);
  /* The keyboard show/hide itself resizes the visual viewport — react immediately
     so the bars collapse as it slides up and return as it slides away, while the
     field keeps focus. */
  function onResize(){if(timer){clearTimeout(timer);timer=null}trackBaseline();apply()}
  if(vv)vv.addEventListener('resize',onResize);
  window.addEventListener('resize',onResize);
  window.addEventListener('orientationchange',function(){clearPending();baseline=0;setTimeout(function(){trackBaseline();apply()},120)});
  window.addEventListener('blur',function(){if(timer)clearTimeout(timer);fieldOn=false;apply()});
  trackBaseline();
  apply();
}

