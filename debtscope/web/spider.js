/* DebtScope spider: a procedurally animated crawler drawn on a canvas.
 *
 * Model (top-down view):
 *   - body: position, velocity, heading; steers toward a target with arrival slowdown
 *   - 8 legs, each a 2-segment arm solved with inverse kinematics (law of cosines)
 *   - feet stay planted until they drift too far from their rest spot, then step;
 *     legs move in two alternating groups (tetrapod gait), like a real spider
 *   - a stepping foot looks for a highlighted word nearby and grips its edge
 *   - silk threads are drawn from the abdomen to each finding
 * Everything lives in document coordinates; the canvas covers the viewport only.
 */
window.Spider = (function () {
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const NAVY = '#14213D', LEG = '#2BB3D9', KNOT = '#F03E5E';
  const L1 = 22, L2 = 27;                 // femur, tibia (px)
  // per leg pair: [hip offset forward, rest angle from heading (deg), reach]
  // reach stays below L1 + L2 so the knee always shows a gentle bend
  const LAYOUT = [[7, 42, 41], [3, 74, 43], [-1, 110, 43], [-5, 148, 41]];

  let cv, ctx, dpr = 1, running = false, last = 0;
  let speedFactor = 1, follow = false, userScrolledAt = 0;
  const body = { x: 160, y: 160, vx: 0, vy: 0, h: -Math.PI / 2, phase: 0 };
  let target = null, arrive = null, lastGroup = 0;
  const legs = [];
  const threads = [];
  const visible = new Set();
  let rects = [], rectsAt = 0;

  // ---------------------------------------------------------------- helpers
  const fwd = () => [Math.cos(body.h), Math.sin(body.h)];
  const toWorld = (a, b) => { const [fx, fy] = fwd(); return [body.x + fx * a - fy * b, body.y + fy * a + fx * b]; };
  const dist = (ax, ay, bx, by) => Math.hypot(ax - bx, ay - by);
  const ease = t => t < .5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
  function angleLerp(a, b, t) {
    let d = ((b - a + Math.PI) % (2 * Math.PI) + 2 * Math.PI) % (2 * Math.PI) - Math.PI;
    return a + d * Math.min(1, t);
  }

  function buildLegs() {
    legs.length = 0;
    [-1, 1].forEach(side => LAYOUT.forEach(([hipF, deg, reach], i) => {
      const a = deg * Math.PI / 180;
      const leg = { side, i, hip: [hipF, side * 4.5], rest: [Math.cos(a) * reach, side * Math.sin(a) * reach],
                    group: (i + (side > 0 ? 1 : 0)) % 2, foot: null, step: null };
      const [x, y] = toWorld(...leg.rest);
      leg.foot = { x, y };
      legs.push(leg);
    }));
  }

  // ------------------------------------------------- feet grip highlighted words
  function refreshRects(now) {
    if (now - rectsAt < 180) return;
    rectsAt = now;
    rects = [];
    for (const el of visible) {
      for (const r of el.getClientRects()) {
        rects.push([r.left + scrollX, r.top + scrollY, r.right + scrollX, r.bottom + scrollY]);
      }
      if (rects.length > 500) break;
    }
  }
  function grip(x, y) {
    let best = null, bd = 30;
    for (const [x0, y0, x1, y1] of rects) {
      const cx = Math.max(x0, Math.min(x, x1)), cy = Math.max(y0, Math.min(y, y1));
      const d = dist(x, y, cx, cy);
      if (d < bd) {
        // land on the nearest horizontal edge of the word box
        const ey = Math.abs(y - y0) < Math.abs(y - y1) ? y0 : y1;
        best = { x: Math.max(x0 + 1, Math.min(x, x1 - 1)), y: ey };
        bd = d;
      }
    }
    return best || { x, y };
  }

  // ------------------------------------------------------------------ physics
  function update(dt, now) {
    const maxV = 300 * speedFactor;
    if (target) {
      const dx = target.x - body.x, dy = target.y - body.y, d = Math.hypot(dx, dy);
      const want = d < 1 ? 0 : maxV * Math.min(1, d / 70);
      const tx = d ? dx / d * want : 0, ty = d ? dy / d * want : 0;
      const k = Math.min(1, dt * 6);
      body.vx += (tx - body.vx) * k;
      body.vy += (ty - body.vy) * k;
      // close enough to start casting silk; the body keeps settling on its own
      if (arrive && d < 28) { const done = arrive; arrive = null; done(); }
      if (d < 3) { body.vx = body.vy = 0; target = null; }
    } else {
      const k = Math.min(1, dt * 8);
      body.vx -= body.vx * k; body.vy -= body.vy * k;
    }
    body.x += body.vx * dt;
    body.y += body.vy * dt;
    const sp = Math.hypot(body.vx, body.vy);
    if (sp > 8) body.h = angleLerp(body.h, Math.atan2(body.vy, body.vx), dt * 7);
    body.phase += dt * (2 + sp / 30);

    refreshRects(now);
    const stepDur = Math.max(0.07, 0.15 / Math.sqrt(speedFactor));
    const wants = legs.map(l => {
      const [rx, ry] = toWorld(...l.rest);
      const ix = rx + body.vx * 0.14, iy = ry + body.vy * 0.14;
      return { l, ix, iy, err: dist(l.foot.x, l.foot.y, ix, iy) };
    });
    const thresh = sp > 10 ? 15 : 5;
    // Tetrapod gait: one group of four legs swings while the other stands.
    // When nobody is swinging, the other group gets its turn (avoids starving one side).
    const swinging = legs.some(l => l.step);
    if (!swinging) {
      const needy = g => wants.some(w => w.l.group === g && w.err > thresh);
      if (needy(1 - lastGroup)) lastGroup = 1 - lastGroup;
      else if (!needy(lastGroup)) lastGroup = lastGroup;
    }
    for (const w of wants) {
      const l = w.l;
      if (l.step) continue;
      const overstretched = w.err > (L1 + L2) * 0.9;     // never let a leg trail behind
      if ((w.err > thresh && l.group === lastGroup && !legs.some(o => o.step && o.group !== l.group)) || overstretched) {
        l.step = { from: { ...l.foot }, to: grip(w.ix, w.iy), t: 0, dur: stepDur };
      }
    }
    for (const l of legs) {
      if (!l.step) continue;
      l.step.t += dt / l.step.dur;
      const t = Math.min(1, l.step.t), e = ease(t);
      l.foot.x = l.step.from.x + (l.step.to.x - l.step.from.x) * e;
      l.foot.y = l.step.from.y + (l.step.to.y - l.step.from.y) * e;
      l.lift = Math.sin(Math.PI * t);
      if (t >= 1) { l.step = null; l.lift = 0; }
    }

    // camera: keep the spider in the reading band unless the user is scrolling
    if (follow && now - userScrolledAt > 2500) {
      const sy = body.y - scrollY;
      if (sy < innerHeight * 0.22 || sy > innerHeight * 0.62) {
        const goal = body.y - innerHeight * 0.42;
        window.scrollTo(scrollX, scrollY + (goal - scrollY) * Math.min(1, dt * (reduce ? 60 : 3.5)));
      }
    }
  }

  // ---------------------------------------------------------------- drawing
  function knee(hx, hy, fx, fy, i) {
    let d = dist(hx, hy, fx, fy);
    d = Math.max(Math.abs(L1 - L2) + 0.5, Math.min(L1 + L2 - 0.05, d));
    const base = Math.atan2(fy - hy, fx - hx);
    const a = Math.acos((L1 * L1 + d * d - L2 * L2) / (2 * L1 * d));
    // seen from above, front legs arc forward and rear legs arc backward
    const k1 = [hx + Math.cos(base + a) * L1, hy + Math.sin(base + a) * L1];
    const k2 = [hx + Math.cos(base - a) * L1, hy + Math.sin(base - a) * L1];
    const [fx0, fy0] = fwd();
    const along = k => (k[0] - hx) * fx0 + (k[1] - hy) * fy0;
    const forwardKnee = along(k1) > along(k2) ? k1 : k2;
    return i < 2 ? forwardKnee : (forwardKnee === k1 ? k2 : k1);
  }

  function drawThreads(now) {
    const n = threads.length;
    threads.forEach((t, idx) => {
      const p = Math.min(1, (now - t.born) / t.dur);
      const age = n - idx;
      const alpha = age > 24 ? 0.16 : age > 10 ? 0.45 : 0.9;
      const mx = (t.x0 + t.x1) / 2, my = Math.max(t.y0, t.y1) + Math.min(60, Math.abs(t.x1 - t.x0) * 0.08 + 12);
      ctx.beginPath();
      ctx.moveTo(t.x0, t.y0);
      const steps = 18;
      for (let s = 1; s <= steps * p; s++) {
        const u = s / steps;
        const x = (1 - u) * (1 - u) * t.x0 + 2 * (1 - u) * u * mx + u * u * t.x1;
        const y = (1 - u) * (1 - u) * t.y0 + 2 * (1 - u) * u * my + u * u * t.y1;
        ctx.lineTo(x, y);
      }
      ctx.strokeStyle = t.color;
      ctx.globalAlpha = alpha;
      ctx.lineWidth = age > 10 ? 1.1 : 1.6;
      ctx.stroke();
      if (p >= 1) {
        ctx.beginPath();
        ctx.arc(t.x1, t.y1, age > 10 ? 2 : 3, 0, Math.PI * 2);
        ctx.fillStyle = KNOT;
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    });
  }

  function drawSpider() {
    const [fx, fy] = fwd();
    // shadow
    ctx.fillStyle = 'rgba(20,33,61,.13)';
    ctx.beginPath();
    ctx.ellipse(body.x - fx * 6 + 3, body.y - fy * 6 + 4, 20, 14, body.h, 0, Math.PI * 2);
    ctx.fill();
    // legs: dark underlay then cyan, so they read on white and on highlights
    for (const l of legs) {
      const [hx, hy] = toWorld(...l.hip);
      const lift = l.lift || 0;
      // a raised foot is drawn pulled in towards the hip
      const ffx = l.foot.x + (hx - l.foot.x) * 0.18 * lift, ffy = l.foot.y + (hy - l.foot.y) * 0.18 * lift - 3 * lift;
      const [kx, ky] = knee(hx, hy, ffx, ffy, l.i);
      for (const [w, c, a] of [[4, NAVY, .55], [2.2, LEG, 1]]) {
        ctx.globalAlpha = a;
        ctx.strokeStyle = c;
        ctx.lineCap = 'round'; ctx.lineJoin = 'round';
        ctx.lineWidth = w;
        ctx.beginPath(); ctx.moveTo(hx, hy); ctx.lineTo(kx, ky); ctx.stroke();
        ctx.lineWidth = w * 0.8;
        ctx.beginPath(); ctx.moveTo(kx, ky); ctx.lineTo(ffx, ffy); ctx.stroke();
      }
      ctx.globalAlpha = 1;
      ctx.fillStyle = NAVY;
      ctx.beginPath(); ctx.arc(kx, ky, 1.8, 0, Math.PI * 2); ctx.fill();
      if (!lift) { ctx.fillStyle = LEG; ctx.beginPath(); ctx.arc(ffx, ffy, 1.4, 0, Math.PI * 2); ctx.fill(); }
    }
    // abdomen (breathes a little), cephalothorax, eyes
    const breathe = 1 + Math.sin(body.phase * 1.3) * 0.035;
    ctx.fillStyle = NAVY;
    ctx.beginPath();
    ctx.ellipse(body.x - fx * 16, body.y - fy * 16, 13.5 * breathe, 10 * breathe, body.h, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = LEG; ctx.lineWidth = 1.2; ctx.globalAlpha = .55;
    ctx.beginPath();
    ctx.ellipse(body.x - fx * 18, body.y - fy * 18, 6, 3, body.h, 0, Math.PI * 2);
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillStyle = NAVY;
    ctx.beginPath();
    ctx.ellipse(body.x + fx * 2, body.y + fy * 2, 9, 7.5, body.h, 0, Math.PI * 2);
    ctx.fill();
    for (const s of [-1, 1]) {
      const [ex, ey] = toWorld(9, s * 2.8);
      ctx.fillStyle = LEG;
      ctx.beginPath(); ctx.arc(ex, ey, 1.4, 0, Math.PI * 2); ctx.fill();
    }
  }

  function frame(now) {
    const dt = Math.min(0.05, (now - last) / 1000 || 0.016);
    last = now;
    update(dt, now);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.translate(-scrollX, -scrollY);
    drawThreads(now);
    drawSpider();
    requestAnimationFrame(frame);
  }

  function resize() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    cv.width = innerWidth * dpr; cv.height = innerHeight * dpr;
    cv.style.width = innerWidth + 'px'; cv.style.height = innerHeight + 'px';
  }

  // ------------------------------------------------------------------- API
  return {
    init(canvas, marks) {
      cv = canvas; ctx = cv.getContext('2d');
      resize();
      addEventListener('resize', resize);
      for (const ev of ['wheel', 'touchmove', 'keydown']) addEventListener(ev, () => { userScrolledAt = performance.now(); }, { passive: true });
      const io = new IntersectionObserver(es => es.forEach(e => e.isIntersecting ? visible.add(e.target) : visible.delete(e.target)),
                                          { rootMargin: '120px' });
      marks.forEach(m => io.observe(m));
      body.x = Math.min(innerWidth * 0.35, 260); body.y = scrollY + 150;
      buildLegs();
      if (!running) { running = true; last = performance.now(); requestAnimationFrame(frame); }
    },
    get pos() { return { x: body.x, y: body.y }; },
    setSpeed(s) { speedFactor = 0.45 + (s - 1) * 0.28; },     // slider 1..10 -> 0.45..2.97
    setFollow(on) { follow = on; if (on) userScrolledAt = 0; },
    walkTo(x, y) {
      if (reduce) { body.x = x; body.y = y; buildLegs(); return Promise.resolve(); }
      if (arrive) arrive();                                     // cancel previous walk
      target = { x, y };
      return new Promise(res => {
        arrive = res;
        setTimeout(() => { if (arrive === res) { target = null; arrive = null; res(); } }, 7000);
      });
    },
    spin(x, y, color) {
      const [fx, fy] = fwd();
      const dur = reduce ? 1 : 300 / Math.sqrt(speedFactor);
      threads.push({ x0: body.x - fx * 26, y0: body.y - fy * 26, x1: x, y1: y, color: color || LEG, born: performance.now(), dur });
      if (threads.length > 80) threads.shift();
      return new Promise(res => setTimeout(res, dur * 0.7));
    },
    clear() { threads.length = 0; },
    stop() { if (arrive) arrive(); target = null; arrive = null; },
  };
})();
