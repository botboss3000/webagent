/* ============================================================
   Stargaze background + per-element spotlight tracking
   Extracted from index.html for maintainability.
   ============================================================ */
(function () {
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  /* —— Canvas animation —— */
  var canvas = document.getElementById('stargaze-bg');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var DPR = Math.min(window.devicePixelRatio || 1, 2);
  var W = 0, H = 0;
  var stars = [];
  var shootingStars = [];
  var dust = [];
  var nextShootAt = 0;
  var pointerX = 0.5, pointerY = 0.5;
  var targetX = 0.5, targetY = 0.5;
  var scrollY = 0;
  var lastT = 0;
  var lastPX = 0, lastPY = 0;

  function isLight() { return document.body && document.body.classList.contains('light-mode'); }

  function resize() {
    W = window.innerWidth;
    H = window.innerHeight;
    canvas.width = W * DPR;
    canvas.height = H * DPR;
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    initStars();
  }

  function initStars() {
    stars = [];
    var count = Math.round((W * H) / 8000);
    for (var i = 0; i < count; i++) {
      var layer = Math.random() < 0.55 ? 0 : (Math.random() < 0.85 ? 1 : 2);
      stars.push({
        x: Math.random(),
        y: Math.random(),
        r: layer === 0 ? Math.random() * 0.7 + 0.3
           : layer === 1 ? Math.random() * 0.9 + 0.5
           : Math.random() * 1.4 + 0.8,
        layer: layer,
        tw: Math.random() * Math.PI * 2,
        twSpeed: 0.4 + Math.random() * 0.8,
        drift: 0.01 + Math.random() * 0.04,
        hueDark: Math.random() < 0.85 ? 220 : (Math.random() < 0.5 ? 260 : 200),
        hueLight: Math.random() < 0.85 ? 22 : (Math.random() < 0.5 ? 12 : 35)
      });
    }
  }

  function drawStars(t) {
    var light = isLight();
    var px = pointerX - 0.5, py = pointerY - 0.5;

    if (light) {
      var g = ctx.createRadialGradient(W * 0.5, H * 0.28, 40, W * 0.5, H * 0.28, Math.max(W, H) * 0.9);
      g.addColorStop(0, 'rgba(255, 200, 150, 0.22)');
      g.addColorStop(0.5, 'rgba(220, 160, 110, 0.08)');
      g.addColorStop(1, 'rgba(245, 220, 195, 0)');
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, W, H);

      var g2 = ctx.createRadialGradient(
        W * (0.82 + px * 0.05), H * (0.78 + py * 0.05), 30,
        W * (0.82 + px * 0.05), H * (0.78 + py * 0.05), Math.max(W, H) * 0.6
      );
      g2.addColorStop(0, 'rgba(200, 120, 70, 0.16)');
      g2.addColorStop(0.5, 'rgba(180, 100, 60, 0.05)');
      g2.addColorStop(1, 'rgba(245, 220, 195, 0)');
      ctx.fillStyle = g2;
      ctx.fillRect(0, 0, W, H);
    } else {
      var g3 = ctx.createRadialGradient(W * 0.5, H * 0.3, 50, W * 0.5, H * 0.3, Math.max(W, H) * 0.9);
      g3.addColorStop(0, 'rgba(60, 70, 130, 0.18)');
      g3.addColorStop(0.45, 'rgba(40, 30, 80, 0.08)');
      g3.addColorStop(1, 'rgba(8, 8, 16, 0)');
      ctx.fillStyle = g3;
      ctx.fillRect(0, 0, W, H);

      var g4 = ctx.createRadialGradient(
        W * (0.82 + px * 0.05), H * (0.78 + py * 0.05), 30,
        W * (0.82 + px * 0.05), H * (0.78 + py * 0.05), Math.max(W, H) * 0.6
      );
      g4.addColorStop(0, 'rgba(125, 90, 200, 0.16)');
      g4.addColorStop(0.5, 'rgba(80, 60, 160, 0.05)');
      g4.addColorStop(1, 'rgba(8, 8, 16, 0)');
      ctx.fillStyle = g4;
      ctx.fillRect(0, 0, W, H);
    }

    for (var i = 0; i < stars.length; i++) {
      var s = stars[i];
      var parallax = (s.layer + 1) * 10;
      var x = (s.x * W) - px * parallax;
      var y = (s.y * H) - py * parallax - (scrollY * 0.04 * (s.layer + 1));
      var tw = (Math.sin(t * 0.001 * s.twSpeed + s.tw) + 1) * 0.5;
      var r = s.r * (0.9 + tw * 0.25);

      if (light) {
        var hue = s.hueLight, alpha = 0.45 + tw * 0.45, lightness = 18 + tw * 8, sat = 55;
        if (s.layer >= 1) {
          var halo = ctx.createRadialGradient(x, y, 0, x, y, r * 6);
          halo.addColorStop(0, 'hsla(' + hue + ',' + sat + '%,' + lightness + '%,' + (alpha * 0.35) + ')');
          halo.addColorStop(1, 'hsla(' + hue + ',' + sat + '%,' + lightness + '%,0)');
          ctx.fillStyle = halo;
          ctx.beginPath();
          ctx.arc(x, y, r * 6, 0, Math.PI * 2);
          ctx.fill();
        }
        ctx.fillStyle = 'hsla(' + hue + ',' + sat + '%,' + lightness + '%,' + alpha + ')';
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fill();
      } else {
        var hueD = s.hueDark, alphaD = 0.35 + tw * 0.55;
        if (s.layer >= 1) {
          var haloD = ctx.createRadialGradient(x, y, 0, x, y, r * 6);
          haloD.addColorStop(0, 'hsla(' + hueD + ',90%,85%,' + (alphaD * 0.5) + ')');
          haloD.addColorStop(1, 'hsla(' + hueD + ',90%,85%,0)');
          ctx.fillStyle = haloD;
          ctx.beginPath();
          ctx.arc(x, y, r * 6, 0, Math.PI * 2);
          ctx.fill();
        }
        ctx.fillStyle = 'hsla(' + hueD + ',80%,' + (85 + tw * 10) + '%,' + alphaD + ')';
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fill();
      }

      s.x += s.drift * 0.0006;
      if (s.x > 1.05) s.x = -0.05;
    }
  }

  function drawConstellation(light) {
    var cx = pointerX * W, cy = pointerY * H;
    var candidates = [];
    for (var i = 0; i < stars.length; i++) {
      var s = stars[i];
      if (s.layer < 1) continue;
      var sx = s.x * W - (pointerX - 0.5) * (s.layer + 1) * 10;
      var sy = s.y * H - (pointerY - 0.5) * (s.layer + 1) * 10 - scrollY * 0.04 * (s.layer + 1);
      var d = Math.hypot(sx - cx, sy - cy);
      if (d < 220) candidates.push({ x: sx, y: sy, d: d });
    }
    candidates.sort(function (a, b) { return a.d - b.d; });
    var pick = candidates.slice(0, 5);
    if (pick.length < 2) return;
    ctx.lineWidth = 0.6;
    ctx.lineCap = 'round';
    for (var j = 0; j < pick.length - 1; j++) {
      var a = pick[j], b = pick[j + 1];
      var dist = Math.hypot(a.x - b.x, a.y - b.y);
      if (dist > 200) continue;
      var alpha = (1 - dist / 200) * 0.45 * (1 - a.d / 220);
      ctx.strokeStyle = light
        ? 'rgba(80, 40, 20, ' + alpha + ')'
        : 'rgba(180, 200, 255, ' + alpha + ')';
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }
  }

  function spawnShooting(t) {
    var fromLeft = Math.random() < 0.5;
    var startX = fromLeft ? -50 + Math.random() * 100 : W + 50 - Math.random() * 100;
    var startY = Math.random() * H * 0.5;
    var speed = 0.8 + Math.random() * 0.6;
    var angle = (0.18 + Math.random() * 0.12) * Math.PI;
    shootingStars.push({
      x: startX, y: startY,
      vx: Math.cos(angle) * speed * (fromLeft ? 1 : -1),
      vy: Math.sin(angle) * speed + 0.3,
      maxLife: 900 + Math.random() * 500,
      born: t,
      trail: []
    });
  }

  function drawShootingStars(t, light) {
    if (t > nextShootAt) {
      spawnShooting(t);
      nextShootAt = t + 12000 + Math.random() * 20000;
    }
    for (var i = shootingStars.length - 1; i >= 0; i--) {
      var s = shootingStars[i];
      var age = t - s.born;
      if (age > s.maxLife) { shootingStars.splice(i, 1); continue; }
      s.x += s.vx * 16;
      s.y += s.vy * 16;
      s.trail.unshift({ x: s.x, y: s.y });
      if (s.trail.length > 22) s.trail.length = 22;

      var fade = age < 150 ? age / 150 : (age > s.maxLife - 250 ? (s.maxLife - age) / 250 : 1);
      for (var j = 0; j < s.trail.length - 1; j++) {
        var p1 = s.trail[j], p2 = s.trail[j + 1];
        var segAlpha = (1 - j / s.trail.length) * 0.9 * fade;
        ctx.strokeStyle = light
          ? 'rgba(110, 50, 20, ' + segAlpha + ')'
          : 'rgba(220, 230, 255, ' + segAlpha + ')';
        ctx.lineWidth = (1 - j / s.trail.length) * 2 + 0.3;
        ctx.lineCap = 'round';
        ctx.beginPath();
        ctx.moveTo(p1.x, p1.y);
        ctx.lineTo(p2.x, p2.y);
        ctx.stroke();
      }
      var head = ctx.createRadialGradient(s.x, s.y, 0, s.x, s.y, 8);
      if (light) {
        head.addColorStop(0, 'rgba(110, 50, 20, ' + (0.9 * fade) + ')');
        head.addColorStop(1, 'rgba(110, 50, 20, 0)');
      } else {
        head.addColorStop(0, 'rgba(255, 255, 255, ' + (0.95 * fade) + ')');
        head.addColorStop(1, 'rgba(180, 200, 255, 0)');
      }
      ctx.fillStyle = head;
      ctx.beginPath();
      ctx.arc(s.x, s.y, 8, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function drawDust(dtMs) {
    for (var i = dust.length - 1; i >= 0; i--) {
      var p = dust[i];
      p.life += dtMs;
      if (p.life > p.maxLife) { dust.splice(i, 1); continue; }
      p.x += p.vx * dtMs * 0.06;
      p.y += p.vy * dtMs * 0.06;
      var a = (1 - p.life / p.maxLife);
      ctx.fillStyle = p.isLight
        ? 'rgba(110, 60, 30, ' + (a * 0.55) + ')'
        : 'rgba(220, 230, 255, ' + (a * 0.75) + ')';
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r * a, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function frame(t) {
    t = t || performance.now();
    var dtMs = lastT ? Math.min(50, t - lastT) : 16;
    lastT = t;
    pointerX += (targetX - pointerX) * 0.06;
    pointerY += (targetY - pointerY) * 0.06;
    ctx.clearRect(0, 0, W, H);
    var light = isLight();
    drawStars(t);
    drawConstellation(light);
    drawShootingStars(t, light);
    drawDust(dtMs);
    if (document.hidden) setTimeout(function () { frame(performance.now()); }, 80);
    else requestAnimationFrame(frame);
  }

  var glowEl = document.getElementById('cursor-glow');
  var glowOn = false;
  window.addEventListener('pointermove', function (e) {
    targetX = e.clientX / window.innerWidth;
    targetY = e.clientY / window.innerHeight;
    // Drive the full-viewport cursor glow in viewport pixels.
    if (glowEl) {
      glowEl.style.setProperty('--gx', e.clientX + 'px');
      glowEl.style.setProperty('--gy', e.clientY + 'px');
      if (!glowOn) { glowEl.classList.add('is-on'); glowOn = true; }
    }
    var dx = e.clientX - lastPX, dy = e.clientY - lastPY;
    var speed = Math.hypot(dx, dy);
    if (speed > 4 && Math.random() < Math.min(0.5, speed / 60)) {
      var light = isLight();
      for (var k = 0; k < 2; k++) {
        dust.push({
          x: e.clientX + (Math.random() - 0.5) * 6,
          y: e.clientY + (Math.random() - 0.5) * 6,
          vx: (Math.random() - 0.5) * 0.3,
          vy: (Math.random() - 0.5) * 0.3 - 0.15,
          r: 0.4 + Math.random() * 1.2,
          life: 0,
          maxLife: 600 + Math.random() * 400,
          isLight: light
        });
      }
    }
    lastPX = e.clientX;
    lastPY = e.clientY;
  });
  // Fade the cursor glow out when the pointer leaves the window.
  document.addEventListener('mouseleave', function () {
    if (glowEl && glowOn) { glowEl.classList.remove('is-on'); glowOn = false; }
  });
  window.addEventListener('scroll', function () { scrollY = window.scrollY || 0; }, { passive: true });
  window.addEventListener('resize', function () {
    DPR = Math.min(window.devicePixelRatio || 1, 2);
    resize();
  });

  resize();
  frame(performance.now());

  /* —— Spotlight tracking for cards / bubbles / dropdowns / panels —— */
  // Selectors to apply spotlight to
  var SPOT_SELECTORS = [
    '#main-panel',
    '.chat-bubble',
    '.tab-select-popup',
    '.tab-option',
    '.tab-select-pill',
    '.gh-restricted-card',
    '.files-restricted-card',
    '.ac-card',
    '.agent-card',
    '.tutorial-card',
    '.card'
  ];
  var SPOT_SELECTOR_STR = SPOT_SELECTORS.join(', ');

  function decorate(root) {
    var nodes = root && root.querySelectorAll ? root.querySelectorAll(SPOT_SELECTOR_STR) : [];
    for (var i = 0; i < nodes.length; i++) nodes[i].classList.add('spot');
    if (root && root.matches && root.matches(SPOT_SELECTOR_STR)) root.classList.add('spot');
  }
  decorate(document);

  // Watch for dynamically added elements (new chat bubbles, popups, etc.)
  var mo = new MutationObserver(function (mutations) {
    for (var m = 0; m < mutations.length; m++) {
      var added = mutations[m].addedNodes;
      for (var n = 0; n < added.length; n++) {
        var node = added[n];
        if (node.nodeType === 1) decorate(node);
      }
    }
  });
  mo.observe(document.body, { childList: true, subtree: true });

  // Single delegated pointermove to update --mx/--my on the nearest .spot
  document.addEventListener('pointermove', function (e) {
    var el = e.target && e.target.closest ? e.target.closest('.spot') : null;
    if (!el) return;
    var r = el.getBoundingClientRect();
    var mx = ((e.clientX - r.left) / r.width) * 100;
    var my = ((e.clientY - r.top) / r.height) * 100;
    el.style.setProperty('--mx', mx + '%');
    el.style.setProperty('--my', my + '%');
    if (!el.classList.contains('spot-active')) el.classList.add('spot-active');
  }, true);

  // Clear .spot-active when pointer leaves a tracked element
  document.addEventListener('pointerleave', function (e) {
    if (e.target && e.target.classList && e.target.classList.contains('spot')) {
      e.target.classList.remove('spot-active');
    }
  }, true);
  // Also clear on pointerout (more reliable for some elements)
  document.addEventListener('pointerout', function (e) {
    var from = e.target;
    var to = e.relatedTarget;
    if (from && from.classList && from.classList.contains('spot')) {
      // Only clear if the new target is outside this spot
      if (!to || !from.contains(to)) from.classList.remove('spot-active');
    }
  }, true);
})();