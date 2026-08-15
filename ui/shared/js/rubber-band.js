/**
 * rubber-band.js — attach a spring-back overscroll effect to any scrollable
 * element. Same cubic-bezier as the chat footer drag handle.
 *
 * Usage:
 *   import { applyRubberBand } from '../../shared/js/rubber-band.js';
 *   applyRubberBand(scrollerEl);                       // horizontal default
 *   applyRubberBand(scrollerEl, { axis: 'y' });        // vertical
 *   applyRubberBand(scrollerEl, { axis: 'both' });     // both axes
 *   applyRubberBand(scrollerEl, { axis: 'y', pullThreshold: 40, maxPull: 50, onTriggerTop: fn });
 *
 * Adds wheel + touch handlers that at scroll boundaries apply a diminishing
 * translate to the element, then snap back with an overshoot spring curve.
 *
 * Options:
 *   axis             'x' | 'y' | 'both'   scroll axis (default 'x')
 *   pullThreshold    number               px of pull before onTriggerTop fires on release (default 30)
 *   maxPull          number               max px the element can be pulled (default 150)
 *   onTriggerTop     () => void           called when user pulls past threshold at the top and releases
 *   onPullTop        (ratio: 0..1)        called continuously while pulling at the top
 *   onTriggerBottom  () => void           called when user pulls past threshold at the bottom and releases
 *   onPullBottom     (ratio: 0..1)        called continuously while pulling at the bottom
 */
'use strict';

var SPRING = 'cubic-bezier(0.34, 1.56, 0.64, 1)';
var RESIST  = 0.35;

function makeAxis(axis) {
  var isX = axis === 'x';
  return {
    isX: isX,
    deltaWheel:  isX ? 'deltaX' : 'deltaY',
    deltaTouch:  isX ? 0 : 1,
    scrollSize:  isX ? function (e) { return e.scrollWidth  - e.clientWidth;  } : function (e) { return e.scrollHeight - e.clientHeight; },
    scrollPos:   isX ? function (e) { return e.scrollLeft; } : function (e) { return e.scrollTop; }
  };
}

/**
 * True when the gesture target sits inside a scrollable descendant of `el`
 * (a nested scroller that should own the gesture). Walks from the target up
 * to `el`, matching any overflow-y auto/scroll/overlay box that can actually
 * scroll. Lets nested scrollables (e.g. the bubble tool-call accordion inside
 * a chat bubble) keep their swipes instead of the rubber band stealing them
 * at `el`'s scroll boundaries with preventDefault.
 */
function _isInsideNestedScroller(el, target) {
  var node = target && target.nodeType === 1 ? target : null;
  while (node && node !== el && node !== document.body && node !== document.documentElement) {
    var cs = window.getComputedStyle(node);
    if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll' || cs.overflowY === 'overlay')
        && node.scrollHeight > node.clientHeight + 1) {
      return true;
    }
    node = node.parentElement;
  }
  return false;
}

export function applyRubberBand(el, opts) {
  if (!el || el.dataset.rbApplied === '1') return;

  opts = opts || {};
  var axis = opts.axis === 'y' ? 'y' : opts.axis === 'both' ? 'both' : 'x';
  var pullThreshold = opts.pullThreshold || 30;
  var maxPull = opts.maxPull != null ? opts.maxPull : 150;
  var onTriggerTop = opts.onTriggerTop || null;
  var onPullTop    = opts.onPullTop    || null;
  var onTriggerBottom = opts.onTriggerBottom || null;
  var onPullBottom    = opts.onPullBottom    || null;

  // ── Per-axis state ──
  var axes = axis === 'both' ? [makeAxis('x'), makeAxis('y')] : [makeAxis(axis)];
  var rbPos     = axes.map(function() { return 0; });
  var triggeredTop    = axes.map(function() { return false; });
  var triggeredBottom = axes.map(function() { return false; });
  var rbTimer   = axes.map(function() { return null; });
  var touchLastX = 0, touchLastY = 0, touchActive = false, touchInsideNested = false;

  function notifyPull(i) {
    if (!onPullTop && !onPullBottom) return;
    var ratio = Math.min(Math.abs(rbPos[i]) / pullThreshold, 1);
    var absPull = Math.abs(rbPos[i]);
    if (rbPos[i] > 0 && onPullTop) onPullTop(ratio, absPull);
    else if (rbPos[i] < 0 && onPullBottom) onPullBottom(ratio, absPull);
  }

  function snap(i) {
    if (Math.abs(rbPos[i]) < 0.5) {
      triggeredTop[i] = false;
      triggeredBottom[i] = false;
      _applyTransform();
      return;
    }
    var wasTriggeredTop = triggeredTop[i];
    var wasTriggeredBottom = triggeredBottom[i];
    var wasAbsPull = Math.abs(rbPos[i]);
    el.style.transition = 'transform 0.45s ' + SPRING;
    rbPos[i] = 0;
    triggeredTop[i] = false;
    triggeredBottom[i] = false;
    _applyTransform();
    clearTimeout(rbTimer[i]);
    if (wasTriggeredTop && onTriggerTop) onTriggerTop(wasAbsPull);
    if (wasTriggeredBottom && onTriggerBottom) onTriggerBottom(wasAbsPull);
    if (onPullTop) onPullTop(0, 0);
    if (onPullBottom) onPullBottom(0, 0);
    rbTimer[i] = setTimeout(function() { el.style.transition = ''; }, 500);
  }

  function _applyTransform() {
    var x = rbPos[0] || 0, y = axes.length > 1 ? (rbPos[1] || 0) : 0;
    if (Math.abs(x) < 0.5 && Math.abs(y) < 0.5) {
      el.style.transform = '';
    } else if (axes.length > 1) {
      el.style.transform = 'translate(' + x + 'px, ' + y + 'px)';
    } else {
      el.style.transform = (axes[0].isX ? 'translateX(' : 'translateY(') + (x || y) + 'px)';
    }
  }

  function pull(axisIdx, delta) {
    var a = axes[axisIdx];
    var i = axisIdx;
    clearTimeout(rbTimer[i]);
    el.style.transition = 'none';
    var maxS = a.scrollSize(el);
    var atStart = a.scrollPos(el) <= 0 && delta < 0;
    var atEnd   = a.scrollPos(el) >= maxS - 0.5 && delta > 0;
    if (!atStart && !atEnd) {
      if (Math.abs(rbPos[i]) > 0.5) snap(i);
      return false;
    }
    rbPos[i] -= delta * RESIST;
    rbPos[i] = Math.max(-maxPull, Math.min(maxPull, rbPos[i]));
    _applyTransform();
    if (atStart && Math.abs(rbPos[i]) >= pullThreshold) triggeredTop[i] = true;
    if (atEnd   && Math.abs(rbPos[i]) >= pullThreshold) triggeredBottom[i] = true;
    notifyPull(i);
    rbTimer[i] = setTimeout(function() { snap(i); }, 120);
    return true;
  }

  el.addEventListener('wheel', function (e) {
    // A wheel gesture over a nested scrollable belongs to THAT scroller —
    // never steal it for the rubber band.
    if (_isInsideNestedScroller(el, e.target)) return;
    var handled = false;
    for (var i = 0; i < axes.length; i++) {
      var a = axes[i];
      var d = e[a.deltaWheel];
      // Normalise line / page deltaMode so mouse-wheel ticks produce
      // meaningful pull distances comparable to trackpad gestures.
      if (e.deltaMode === 1) d *= 20;
      else if (e.deltaMode === 2) d *= el.clientHeight;

      if (pull(i, d)) {
        handled = true;
      }
    }
    if (handled) e.preventDefault();
  }, { passive: false });

  el.addEventListener('touchstart', function (e) {
    var t = e.touches[0];
    if (!t) return;
    touchLastX = t.clientX;
    touchLastY = t.clientY;
    touchActive = true;
    // Remember whether this touch began inside a nested scrollable so the
    // touchmove handler leaves the gesture to it instead of rubber-banding.
    touchInsideNested = _isInsideNestedScroller(el, e.target);
  }, { passive: true });

  el.addEventListener('touchmove', function (e) {
    if (!touchActive || touchInsideNested) return;
    var t = e.touches[0];
    if (!t) return;
    var dx = touchLastX - t.clientX;
    var dy = touchLastY - t.clientY;
    touchLastX = t.clientX;
    touchLastY = t.clientY;

    var handled = false;
    for (var i = 0; i < axes.length; i++) {
      var a = axes[i];
      var d = a.isX ? dx : dy;
      var maxS = a.scrollSize(el);
      var atStart = a.scrollPos(el) <= 0 && d < 0;
      var atEnd   = a.scrollPos(el) >= maxS - 0.5 && d > 0;
      if (atStart || atEnd) {
        clearTimeout(rbTimer[i]);
        el.style.transition = 'none';
        rbPos[i] -= d * RESIST;
        rbPos[i] = Math.max(-maxPull, Math.min(maxPull, rbPos[i]));
        _applyTransform();
        if (atStart && Math.abs(rbPos[i]) >= pullThreshold) triggeredTop[i] = true;
        if (atEnd   && Math.abs(rbPos[i]) >= pullThreshold) triggeredBottom[i] = true;
        notifyPull(i);
        rbTimer[i] = setTimeout(function(idx) { return function() { snap(idx); }; }(i), 120);
        handled = true;
      } else if (Math.abs(rbPos[i]) > 0.5) {
        snap(i);
      }
    }
    if (handled) e.preventDefault();
  }, { passive: false });

  function touchEnd() {
    touchActive = false;
    for (var i = 0; i < axes.length; i++) {
      if (Math.abs(rbPos[i]) > 0.5) snap(i);
      else { triggeredTop[i] = false; triggeredBottom[i] = false; }
    }
  }
  el.addEventListener('touchend',   touchEnd, { passive: true });
  el.addEventListener('touchcancel', touchEnd, { passive: true });

  el.dataset.rbApplied = '1';
}
