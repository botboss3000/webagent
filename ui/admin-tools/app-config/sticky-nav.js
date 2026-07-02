'use strict';

/**
 * Shared sticky section navigator for the Admin → App Config pages.
 * ──────────────────────────────────────────────────────────────────────────
 * Keeps EVERY section heading on screen the whole time you scroll a config
 * page: a heading whose table you've scrolled past pins to the TOP of the
 * list, a heading whose table is still coming up pins to the BOTTOM, and the
 * one you're reading sits between them with its table. Every heading is always
 * visible and is click-to-jump to its table.
 *
 * One page opts in by:
 *   1. adding the `ac-stickynav` class to its `<section class="ac-section">`
 *      (the CSS in app3.css flattens the groups with display:contents and
 *      paints the sticky banner look — see "ADVANCED SCROLL" there), and
 *   2. calling `initStickyNav('<section-suffix>')` from its module's init(),
 *      plus registering it as a section hook so it re-measures every time the
 *      page is shown (at init() the section may be hidden → everything
 *      measures 0).
 *
 * WHY display:contents — a `position:sticky` element can only travel inside
 * its own containing block. Each heading normally lives in its own
 * `.ac-category-group`, so it could only stay stuck while THAT one group was on
 * screen. Collapsing the group box with display:contents makes the heading a
 * direct child of the whole section, so its containing block is the ENTIRE
 * page — which is what lets it stay stuck across every other section. The CSS
 * handles the flatten + banner; THIS module measures the headings and writes
 * their stacked top/bottom offsets inline, wires the jump clicks, and
 * highlights the current one.
 *
 * COLLAPSIBLE PAGES — display:contents breaks a `<details>`'s native collapse
 * AND its flow layout, so a collapsible group is authored as a plain
 * `<div class="ac-category-group ac-collapsible">` and opened/closed via an
 * `.ac-open` CLASS (CSS hides its body unless `.ac-open`). A group WITHOUT
 * `.ac-collapsible` is always-open (App Settings' page lists). This module tells
 * them apart by that class: collapsible = click toggles open and jumps when
 * opening; always-open = click only jumps.
 *
 * The scroller is the shared `#app-config-content` for every page.
 *
 * MOBILE SUB-HEADER (opt-in) — a section can pass `{ mobileCarousel: true }` to
 * initStickyNav (today only Agent Settings does). On a mobile-width viewport that
 * section drops the vertical sticky stacking and instead renders its headings as a
 * horizontal, swipeable strip of underline tabs in #app-config-subnav — a genuine
 * SUB-HEADER that sits directly beneath the app-config tab strip and OUTSIDE the
 * content scroller, so it spans the full width and mirrors the tab strip's look
 * (see the `.ac-mnav` block in app3.css). Tapping a tab jumps the scroll to that
 * section (landing its heading flush under the sub-header), and the tab for the
 * section you're reading highlights. Desktop is unchanged. See the `_syncMode` /
 * `_buildCarousel` helpers below and the `_navHost` / `_navFor` accessors.
 */

import { isMobileLayout } from '../../shared/js/layout.js';

const _SCROLLER_ID = 'app-config-content';
const _registered = new Set();   // section suffixes participating (for scroll updates)
// Sections that opted into the MOBILE heading carousel (initStickyNav(id,{mobileCarousel})).
const _carousel = new Set();
let _globalWired = false;
let _raf = 0;

function _scroller() { return document.getElementById(_SCROLLER_ID); }
function _section(id) { return document.getElementById('ac-section-' + id); }
// The mobile sub-header host — a sibling directly under #app-config-tabs-wrap and
// OUTSIDE the scroller (markup in app-config.html). The carousel `.ac-mnav` for a
// section is parked here (tagged with data-section) so it renders as a real full-
// width sub-header, not a strip clipped inside the content scroller.
function _navHost() { return document.getElementById('app-config-subnav'); }
function _navFor(id) { const h = _navHost(); return h ? h.querySelector(':scope > .ac-mnav[data-section="' + id + '"]') : null; }
// Every top-level `.ac-category-group` in the section, at any depth — so a page
// that renders its groups inside a wrapper (e.g. Features' #ac-features-list)
// still works. Nested groups (a group inside another group) are excluded so
// only the real section headings are picked up.
function _groups(id) {
  const sec = _section(id);
  if (!sec) return [];
  return Array.from(sec.querySelectorAll('.ac-category-group'))
    .filter((g) => !g.parentElement || !g.parentElement.closest('.ac-category-group'));
}
// Only VISIBLE headings take part in the stack. Some pages hide groups at
// runtime (e.g. Agent Settings empties + hides its legacy integration
// categories once their cards are relocated into the unified abilities table) —
// a hidden heading measures 0 and would otherwise pollute the stacked offsets.
// `offsetParent === null` is null exactly when the element (or an ancestor) is
// display:none, which also covers the whole section being inactive.
function _heads(id) {
  return _groups(id)
    .map((g) => g.querySelector(':scope > .ac-category-summary'))
    .filter((h) => h && h.offsetParent !== null);
}

// Measure each heading's rendered height and write the stacked offsets: a
// heading's TOP offset is the combined height of every heading above it (so
// when it pins to the top it lands flush under them); its BOTTOM offset is the
// combined height of every heading below it. A sticky element pins at the
// scroller's PADDING edge, so we subtract the scroller's own padding so the top
// stack sits flush just under the tabs and the bottom stack flush at the very
// bottom. No-op while the section is hidden (everything measures 0) — the
// section hook re-runs it once shown.
function _recalc(id) {
  const heads = _heads(id);
  if (!heads.length) return;
  const hts = heads.map((h) => Math.round(h.getBoundingClientRect().height));
  if (hts.every((h) => h === 0)) return;            // hidden → nothing to measure yet
  const scroller = _scroller();
  const cs = scroller ? getComputedStyle(scroller) : null;
  const padTop = cs ? (parseFloat(cs.paddingTop) || 0) : 0;
  const padBottom = cs ? (parseFloat(cs.paddingBottom) || 0) : 0;
  // Pull each stacked heading 1px CLOSER to the one before it. Heights are
  // Math.round-ed, so the cumulative offset can land ~1px below the previous
  // heading's true (fractional) bottom edge, leaving a 1px sliver of panel
  // showing between two pinned headings. Overlapping the stack by 1px per level
  // closes that seam; the overlap itself is invisible because every heading is
  // the same glass. (Only between headings — the first/last stay flush via the 0
  // start, so we never pull the top stack above the tabs or the bottom past the
  // floor.)
  const OVERLAP = 1;
  let topAcc = 0;
  const tops = hts.map((ht) => { const v = topAcc; topAcc += ht - OVERLAP; return v; });
  const bots = new Array(heads.length);
  let botAcc = 0;
  for (let i = heads.length - 1; i >= 0; i--) { bots[i] = botAcc; botAcc += hts[i] - OVERLAP; }
  // FLOOR PILL — if a section marks a floating element pinned INSIDE the
  // scroller at its bottom (class `.ac-stack-floor`), the bottom-stacking
  // headings must pile up RIGHT ON that element's top edge rather than behind it
  // / at the very floor — and with NO gap, so page content scrolling up vanishes
  // flush under the lowest heading instead of peeking through a gap between the
  // heading and the (opaque) element. We measure its live top edge (so it tracks
  // its real height/position) and re-base the bottom offsets to it: a sticky
  // `bottom: B` holds a heading's bottom edge at (contentBoxBottom − B), so to
  // land the lowest heading's bottom GAP px below the element top (GAP negative =
  // a small overlap that seals any sub-pixel sliver) we set
  // B = bots + (contentBoxBottom − pillTop) + GAP. With no such element, keep the
  // flush-to-floor base (bots − padBottom); padBottom cancels out of the pill
  // branch, so it's not subtracted there.
  //
  // NOTE: no current section uses `.ac-stack-floor`. App Settings' page-assistant
  // pill USED to sit inside the scroller and used this path; it now lives OUTSIDE
  // the scroller (a sibling below #app-config-content — see #ac-pa-bar in
  // app3.css), so the scroll viewport simply ends above it and the flush-to-floor
  // base is used. This branch is kept for any future in-scroller floor element.
  const sec = _section(id);
  let floorBase = null;   // null → no floor pill, use the flush-floor base
  if (sec && scroller) {
    const pill = sec.querySelector('.ac-stack-floor');
    if (pill && pill.offsetParent !== null) {
      const GAP = -2;   // overlap the pill top by 2px → no content seam
      const contentBoxBottom = scroller.getBoundingClientRect().bottom - padBottom;
      floorBase = Math.round(contentBoxBottom - pill.getBoundingClientRect().top + GAP);
    }
  }
  heads.forEach((h, i) => {
    h.style.top = (tops[i] - padTop) + 'px';
    h.style.bottom = ((floorBase === null ? (bots[i] - padBottom) : (bots[i] + floorBase))) + 'px';
  });
  _updateActive(id);
}

// Highlight the heading whose table is currently in view = the DEEPEST heading
// that's pinned to the top (its visual top has reached its own top offset).
function _updateActive(id) {
  const sec = _section(id);
  const scroller = _scroller();
  const heads = _heads(id);
  if (!sec || !sec.classList.contains('active') || !scroller || !heads.length) return;
  const sTop = scroller.getBoundingClientRect().top;
  // A heading pins at the scroller's top PADDING edge, so its pinned position is
  // padTop + its own top offset — account for the padding or the test mis-reads
  // which heading is engaged.
  const padTop = parseFloat(getComputedStyle(scroller).paddingTop) || 0;
  // MOBILE sub-header: the headings are un-stuck and the section-tab strip lives
  // OUTSIDE the scroller (in #app-config-subnav, above it), so nothing pins over
  // the content — a section is "current" once its heading reaches the scroller's
  // top edge. (Desktop stacks sticky headings, so a section engages once its
  // heading reaches its own pinned offset.)
  const mnav = sec.classList.contains('ac-mnav-on') ? _navFor(id) : null;
  let active = 0;
  heads.forEach((h, i) => {
    // Mobile: the section's `-28px` top margin cancels the scroller's 28px top
    // padding, so a heading rests flush at the scroller's border-box top — the
    // "current" line is 0 (heading reached the top edge, just under the sub-header).
    // Desktop: a heading engages at its own pinned offset (padTop + its inline top,
    // which for the first heading is padTop + (−padTop) = 0).
    const line = mnav ? 0 : (padTop + (parseFloat(h.style.top) || 0));
    const rTop = h.getBoundingClientRect().top - sTop;
    if (rTop <= line + 1) active = i;               // scrolled into/past this section
  });
  // MOBILE bottom-of-scroll fix: the top-engagement test above only marks a
  // section "current" once its heading crosses the top edge. At max scroll the
  // LAST section can fill most of the viewport while its heading still sits below
  // the top edge — so a taller earlier section (whose heading has already passed
  // the top) would stay selected even though you're looking at the last one.
  // Override with the section that occupies MORE THAN HALF the visible height, so
  // the highlighted tab matches what's actually on screen. A section spans from
  // its own heading down to the next heading (the last one runs to the viewport
  // floor); we clip each span to the viewport and pick the biggest visible slice.
  // (Desktop keeps pure top-engagement — its headings pin, so the deepest pinned
  // heading is always the right answer and no coverage test is needed.)
  if (mnav && heads.length > 1) {
    const rTops = heads.map((h) => h.getBoundingClientRect().top - sTop);
    const viewH = scroller.getBoundingClientRect().height;
    let best = -1, bestVis = 0;
    for (let i = 0; i < heads.length; i++) {
      const top = rTops[i];
      const end = (i + 1 < heads.length) ? rTops[i + 1] : viewH;
      const vis = Math.min(end, viewH) - Math.max(top, 0);   // visible height of this section
      if (vis > bestVis) { bestVis = vis; best = i; }
    }
    if (best >= 0 && bestVis > viewH * 0.5) active = best;
  }
  heads.forEach((h, i) => h.classList.toggle('ac-navh-active', i === active));
  // Reflect the current section in the carousel chips — only when it CHANGES, so
  // we don't fight the user's horizontal scroll on every tick — and keep the
  // active chip in view.
  if (mnav && mnav.dataset.activeIdx !== String(active)) {
    mnav.dataset.activeIdx = String(active);
    const chips = mnav.querySelectorAll('.ac-mnav-chip');
    chips.forEach((c, i) => c.classList.toggle('active', i === active));
    _scrollChipIntoView(mnav.querySelector('.ac-mnav-strip'), chips[active]);
  }
}

// One shared scroll handler updates whichever participating section is active
// (the others bail in _updateActive on the `.active` guard).
function _onScroll() {
  if (_raf) return;                                  // active-highlight only; rAF pause is harmless
  _raf = requestAnimationFrame(() => {
    _raf = 0;
    _registered.forEach((id) => _updateActive(id));
  });
}

function _onResize() {
  // _syncMode picks carousel (mobile, opted-in) vs sticky-stack (everything else)
  // and re-measures — so crossing the mobile/desktop breakpoint flips the mode.
  _registered.forEach((id) => _syncMode(id));
}

// ─────────────────────────────────────────────────────────────────────────
// ── MOBILE heading carousel (opt-in via initStickyNav(id,{mobileCarousel})) ─
// ─────────────────────────────────────────────────────────────────────────

function _headLabel(h) {
  const t = h.querySelector('.ac-category-title');
  return ((t ? t.textContent : h.textContent) || '').trim();
}

// Build (or refresh) the chip strip for a section and insert it at the top of the
// scroll block. Idempotent: skips the rebuild when the heading labels are
// unchanged so a resize storm / re-show doesn't thrash the DOM.
function _buildCarousel(id) {
  const sec = _section(id);
  const host = _navHost();
  const heads = _heads(id);
  if (!sec || !host || !heads.length) return null;
  const sig = heads.map(_headLabel).join('|');
  let nav = _navFor(id);
  if (nav && nav.dataset.sig === sig) return nav;
  if (!nav) {
    nav = document.createElement('div');
    nav.className = 'ac-mnav';
    nav.dataset.section = id;         // ties this sub-header strip to its section
    nav.innerHTML = '<div class="ac-mnav-strip" role="tablist"></div>';
    // Park it in the sub-header host (outside the scroller), NOT inside the section.
    host.appendChild(nav);
    const strip0 = nav.querySelector('.ac-mnav-strip');
    strip0.addEventListener('scroll', () => _updateMnavEdges(nav), { passive: true });
  }
  const strip = nav.querySelector('.ac-mnav-strip');
  strip.innerHTML = '';
  heads.forEach((h, i) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'ac-mnav-chip';
    chip.dataset.idx = String(i);
    // The section's leading Lucide icon, if it has one (always-open headings do;
    // the chevron-prefixed collapsible categories don't → label-only chip). Only
    // match real Lucide icons, never a bare chevron svg.
    const icon = h.querySelector(':scope > .lucide-icon, :scope > [data-lucide]');
    if (icon) { const c = icon.cloneNode(true); c.removeAttribute('style'); c.classList.add('ac-mnav-ico'); chip.appendChild(c); }
    const lbl = document.createElement('span');
    lbl.className = 'ac-mnav-label';
    lbl.textContent = _headLabel(h);
    chip.appendChild(lbl);
    chip.addEventListener('click', () => _scrollToMobile(id, i));
    strip.appendChild(chip);
  });
  nav.dataset.sig = sig;
  nav.dataset.activeIdx = '';
  if (window.lucide) { try { window.lucide.createIcons({ nodes: Array.from(strip.querySelectorAll('[data-lucide]:not(.lucide)')) }); } catch (_) {} }
  _updateMnavEdges(nav);
  return nav;
}

function _removeCarousel(id) {
  const nav = _navFor(id);
  if (nav) nav.remove();
}

// Toggle the edge-fade mask classes from the strip's live scroll position (mask,
// not a coloured gradient — see the `.ac-mnav` block in app3.css).
function _updateMnavEdges(nav) {
  const strip = nav.querySelector('.ac-mnav-strip');
  if (!strip) return;
  const overflow = strip.scrollWidth - strip.clientWidth > 1;
  nav.classList.toggle('can-left', overflow && strip.scrollLeft > 1);
  nav.classList.toggle('can-right', overflow && strip.scrollLeft < strip.scrollWidth - strip.clientWidth - 1);
}

// Nudge a chip into view inside the strip WITHOUT moving the page scroll.
function _scrollChipIntoView(strip, chip) {
  if (!strip || !chip) return;
  const s = strip.getBoundingClientRect(), c = chip.getBoundingClientRect();
  if (c.left < s.left + 8) strip.scrollBy({ left: c.left - s.left - 14, behavior: 'smooth' });
  else if (c.right > s.right - 8) strip.scrollBy({ left: c.right - s.right + 14, behavior: 'smooth' });
}

// Jump the page scroll so section i's heading rests flush at the scroller's top
// edge — i.e. directly under the sub-header (which sits OUTSIDE the scroller, so
// it doesn't overlap the content and needs no height offset). The mobile analogue
// of _scrollTo, which lands under the pinned heading stack.
function _scrollToMobile(id, i) {
  const groups = _groups(id);
  const scroller = _scroller();
  const g = groups[i];
  if (!g || !scroller) return;
  const head = g.querySelector(':scope > .ac-category-summary');
  const ref = head || g.querySelector(':scope > .ac-category-body') || g;
  const sRect = scroller.getBoundingClientRect();
  const rRect = ref.getBoundingClientRect();
  // flowTop = the heading's offset from the scroller's border-box top. Scrolling
  // there lands it at rTop 0 (the section's -28px margin cancels the scroller's
  // top padding, so the top edge is the visible content top).
  const flowTop = scroller.scrollTop + (rRect.top - sRect.top);
  const target = Math.max(0, Math.round(flowTop));
  scroller.scrollTo({ top: target, behavior: 'smooth' });
}

// Pick the right mode for the current viewport: mobile carousel (opted-in) vs the
// desktop sticky stack. Called on init, on every section show, and on resize.
function _syncMode(id) {
  const sec = _section(id);
  if (!sec) return;
  const carouselOn = _carousel.has(id) && isMobileLayout();
  sec.classList.toggle('ac-mnav-on', carouselOn);
  if (carouselOn) {
    if (!_buildCarousel(id)) { _recalc(id); return; }   // no heads yet → desktop fallback
    // Clear any inline sticky offsets a prior desktop _recalc wrote; under
    // `.ac-mnav-on` the headings ride in-flow (CSS sets position:static).
    _heads(id).forEach((h) => { h.style.top = ''; h.style.bottom = ''; });
    _updateActive(id);
  } else {
    _removeCarousel(id);
    _recalc(id);
  }
}

// Click-to-jump: scroll so group i's heading rests right under the stack of
// headings pinned above it. We measure off the (never-sticky) BODY so the
// target is the heading's true flow position even when it's currently pinned.
function _scrollTo(id, i) {
  const groups = _groups(id);
  const scroller = _scroller();
  const g = groups[i];
  if (!g || !scroller) return;
  const head = g.querySelector(':scope > .ac-category-summary');
  const body = g.querySelector(':scope > .ac-category-body') || head;
  const hH = head ? Math.round(head.getBoundingClientRect().height) : 0;
  const top = head ? (parseFloat(head.style.top) || 0) : 0;
  const sRect = scroller.getBoundingClientRect();
  const bRect = body.getBoundingClientRect();
  const padTop = parseFloat(getComputedStyle(scroller).paddingTop) || 0;
  const bodyFlowTop = scroller.scrollTop + (bRect.top - sRect.top);
  // Land the heading flush UNDER the stack pinned above it: its pinned position
  // is padTop + its own top offset, so subtract both here.
  const target = Math.max(0, Math.round(bodyFlowTop - hH - top - padTop));
  scroller.scrollTo({ top: target, behavior: 'smooth' });
}

// Handle a click on a heading. `.ac-collapsible` group: toggle its `.ac-open`
// class and jump to it when OPENING (per the chosen "expand + jump" behaviour).
// Always-open group: just jump.
function _onHeadClick(e, id, i, group) {
  // Never hijack a click on a control/link/badge that sits in the heading.
  if (e.target.closest('a, button, input, select')) return;
  if (group.classList.contains('ac-collapsible')) {
    const wasOpen = group.classList.contains('ac-open');
    if (wasOpen) {
      group.classList.remove('ac-open');
      _recalc(id);
    } else {
      group.classList.add('ac-open');
      // Let the body reflow before measuring its flow position, then jump.
      requestAnimationFrame(() => { _recalc(id); _scrollTo(id, i); });
    }
  } else {
    _scrollTo(id, i);
  }
}

/**
 * Install (or re-measure) the sticky navigator for one App Config section.
 * Safe to call repeatedly — head click wiring + the global scroll/resize
 * listeners are guarded so they attach once.
 *
 * @param {string} id  the section suffix, e.g. 'app-settings', 'database'.
 * @param {object} [opts]
 * @param {boolean} [opts.mobileCarousel] opt this section into the mobile heading
 *        carousel (horizontal chip strip instead of stacked sticky headings).
 */
export function initStickyNav(id, opts = {}) {
  const groups = _groups(id);
  const heads = _heads(id);
  if (!heads.length) return;
  _registered.add(id);
  if (opts.mobileCarousel) _carousel.add(id);
  groups.forEach((g, i) => {
    const h = g.querySelector(':scope > .ac-category-summary');
    if (!h) return;
    h.style.cursor = 'pointer';                      // overrides any inline cursor:default
    h.setAttribute('title', g.classList.contains('ac-collapsible') ? 'Open / jump to this section' : 'Jump to this section');
    if (!h.__navhWired) {
      h.addEventListener('click', (e) => _onHeadClick(e, id, i, g));
      h.__navhWired = true;
    }
  });
  if (!_globalWired) {
    _scroller()?.addEventListener('scroll', _onScroll, { passive: true });
    window.addEventListener('resize', _onResize);
    _globalWired = true;
  }
  _syncMode(id);   // mobile carousel (opted-in) or desktop sticky stack, then measure
}
