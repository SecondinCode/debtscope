/* DebtScope crawler page: findings panel, crawl orchestration, bond tracing.
 * Depends on spider.js (window.Spider) and dashboard.js (window.Dash). */
(function () {
  const D = JSON.parse(document.getElementById('ds-data').textContent);
  const $ = s => document.querySelector(s);
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const all = Array.from(document.querySelectorAll('mark.ds'));
  const colors = Object.fromEntries(D.categories.map(c => [c.id, c.color]));
  const active = new Set(D.categories.filter(c => c.count).map(c => c.id));
  let query = '', list = [], cursor = -1, shown = 200, crawling = false, runId = 0;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const fmt = (v, d = 0) => v == null ? 'n/a' : v.toLocaleString('en-US', { maximumFractionDigits: d, minimumFractionDigits: d });

  // ---------------------------------------------------------------- header
  $('#ds-co').textContent = D.ticker ? `${D.company} (${D.ticker})` : D.company;
  $('#ds-sub').innerHTML = `${D.form}, period ended ${D.period}. <a href="${D.url}" target="_blank" rel="noopener">Original on EDGAR</a>`;

  // -------------------------------------------------------------- geometry
  function centre(el) {
    const r = el.getBoundingClientRect();
    return { x: r.left + scrollX + r.width / 2, y: r.top + scrollY + r.height / 2 };
  }
  const visible = el => el.getClientRects().length > 0;
  const contentRight = () => document.documentElement.clientWidth -
    (document.body.classList.contains('ds-collapsed') ? 30 : $('#ds-panel').offsetWidth + 20);
  function perch(p) {
    // a spot near the finding, inside the text column, slightly above it
    const side = p.x > contentRight() / 2 ? -1 : 1;
    const x = Math.max(40, Math.min(contentRight() - 40, p.x + side * (70 + Math.random() * 50)));
    return { x, y: p.y - 48 - Math.random() * 30 };
  }
  function scrollToY(y) {
    window.scrollTo({ top: Math.max(0, y - innerHeight * 0.42), behavior: reduce ? 'auto' : 'smooth' });
  }

  // ------------------------------------------------------------ categories
  function renderCats() {
    const box = $('#ds-cats');
    box.innerHTML = '';
    D.categories.forEach(c => {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'ds-cat'; b.style.setProperty('--c', c.color);
      b.setAttribute('aria-pressed', active.has(c.id));
      b.innerHTML = `<span class="dot"></span>${c.label}<span class="n">${fmt(c.count)}</span>`;
      b.onclick = () => { active.has(c.id) ? active.delete(c.id) : active.add(c.id); renderCats(); refresh(); };
      box.appendChild(b);
    });
  }
  function context(m) {
    if (m._ctx != null) return m._ctx;
    let blk = m.parentElement;
    while (blk && blk.textContent.length < 60 && blk.parentElement && blk.parentElement !== document.body) blk = blk.parentElement;
    const t = (blk ? (blk.innerText || blk.textContent) : m.textContent).replace(/\s+/g, ' ').trim();
    const i = t.indexOf(m.textContent);
    m._ctx = i < 0 ? t.slice(0, 110) : (i > 45 ? '…' : '') + t.slice(Math.max(0, i - 45), i + m.textContent.length + 60);
    return m._ctx;
  }
  function refresh() {
    all.forEach(m => m.classList.toggle('off', !active.has(m.dataset.c)));
    const q = query.toLowerCase();
    list = all.filter(m => active.has(m.dataset.c) && visible(m) &&
      (!q || m.textContent.toLowerCase().includes(q) || (m.dataset.x || '').toLowerCase().includes(q)
          || context(m).toLowerCase().includes(q)));
    cursor = Math.min(cursor, list.length - 1);
    shown = 200; renderList(); renderCount();
  }
  function renderCount(found) {
    const n = found != null ? found : cursor + 1;
    $('#ds-count').innerHTML = `${fmt(Math.max(0, n))} <span>of</span> ${fmt(list.length)} <span>found</span>`;
  }
  function renderList() {
    const ul = $('#ds-list'); ul.innerHTML = '';
    list.slice(0, shown).forEach((m, i) => {
      const li = document.createElement('li'), b = document.createElement('button');
      b.type = 'button';
      const h = document.createElement('span'); h.className = 'hit'; h.style.setProperty('--c', colors[m.dataset.c]);
      h.textContent = m.textContent;
      const c = document.createElement('span'); c.className = 'ctx';
      c.textContent = m.dataset.x ? `${m.dataset.x.split(':').pop()}: ${context(m)}` : context(m);
      b.append(h, c); b.onclick = () => go(i); li.appendChild(b); ul.appendChild(li);
    });
    if (list.length > shown) {
      const more = document.createElement('button'); more.className = 'ds-more'; more.type = 'button';
      more.textContent = `Show ${Math.min(200, list.length - shown)} more`;
      more.onclick = () => { shown += 200; renderList(); };
      ul.appendChild(more);
    }
  }
  function flash(m) { m.classList.remove('flash'); void m.offsetWidth; m.classList.add('flash'); }

  // ------------------------------------------------- single-step navigation
  async function go(i) {
    if (!list.length) return;
    stopCrawl();
    const id = ++runId;
    cursor = (i + list.length) % list.length;
    const m = list[cursor], p = centre(m);
    renderCount(); flash(m);
    scrollToY(p.y);
    Spider.setFollow(false);
    const spot = perch(p);
    await Spider.walkTo(spot.x, spot.y);
    if (id === runId) Spider.spin(p.x, p.y, colors[m.dataset.c]);
  }
  $('#ds-prev').onclick = () => go(cursor - 1);
  $('#ds-next').onclick = () => go(cursor + 1);
  $('#ds-q').addEventListener('input', e => { query = e.target.value.trim(); refresh(); });
  $('#ds-speed').addEventListener('input', e => Spider.setSpeed(+e.target.value));

  // ----------------------------------------------------------------- crawl
  async function crawl() {
    const id = ++runId;
    crawling = true;
    $('#ds-go').textContent = 'Stop'; $('#ds-go').setAttribute('aria-pressed', 'true');
    Spider.clear(); Spider.setFollow(true);
    all.forEach(m => m.classList.remove('hit'));
    const start = Math.max(0, cursor);
    Dash.reset(list.length - start);
    let found = 0;
    for (let i = start; i < list.length && crawling && id === runId; i++) {
      const m = list[i], p = centre(m), s = Spider.pos;
      // walk only when the next finding is out of reach; nearby ones get a thread from where we stand
      // threads can be long (the spider casts silk across the page); walk only when the
      // finding has left the reading band or is really far away
      const onScreen = p.y > scrollY + 40 && p.y < scrollY + innerHeight - 220;
      if (!onScreen || Math.hypot(p.x - s.x, p.y - s.y) > 560) {
        const spot = perch(p);
        await Spider.walkTo(spot.x, spot.y);
        if (!crawling || id !== runId) break;
      }
      await Spider.spin(p.x, p.y, colors[m.dataset.c]);
      m.classList.add('hit');
      Dash.onHit(m);
      cursor = i; found++; renderCount(found);
      await sleep(reduce ? 10 : 140 / (+$('#ds-speed').value));
    }
    if (id === runId) stopCrawl();
  }
  function stopCrawl() {
    crawling = false;
    Spider.stop(); Spider.setFollow(false);
    $('#ds-go').textContent = 'Start crawl'; $('#ds-go').setAttribute('aria-pressed', 'false');
    renderCount();
  }
  $('#ds-go').onclick = () => crawling ? (runId++, stopCrawl()) : crawl();

  // ------------------------------------------------------------ debt stack
  function renderDebt() {
    const M = D.metrics, ccy = D.ccy;
    const k = [
      [fmt(M.xbrl_total_debt && M.xbrl_total_debt / 1e6), `Term debt, ${ccy} mm`],
      [fmt(M.net_debt && M.net_debt / 1e6), `Net debt, ${ccy} mm`],
      [M.net_debt_to_ebitda == null ? 'n/a' : fmt(M.net_debt_to_ebitda, 1) + 'x', 'Net debt / EBITDA'],
      [M.interest_coverage == null ? 'n/a' : fmt(M.interest_coverage, 1) + 'x', 'Interest coverage'],
      [M.coverage_ratio == null ? 'n/a' : fmt(M.coverage_ratio * 100) + '%', 'Debt explained bond-level'],
      [M.due_within_24m_pct == null ? 'n/a' : fmt(M.due_within_24m_pct, 1) + '%', 'Due within 24 months'],
    ];
    $('#ds-kpis').innerHTML = k.map(([v, l]) => `<div class="ds-kpi"><b>${v}</b><small>${l}</small></div>`).join('');
    $('#ds-warns').innerHTML = '';
    D.warnings.forEach(w => { const el = document.createElement('div'); el.className = 'ds-warn'; el.textContent = w; $('#ds-warns').appendChild(el); });
    const box = $('#ds-bonds'); box.innerHTML = '';
    D.instruments.forEach(b => {
      const n = b.key ? all.filter(m => m.dataset.k === b.key).length : 0;
      const btn = document.createElement('button'); btn.type = 'button';
      const left = document.createElement('span'); left.textContent = b.label;
      const st = document.createElement('span'); st.className = 'st';
      st.textContent = `${b.status}${n ? `, ${n} mention${n > 1 ? 's' : ''}` : ''}`;
      left.appendChild(st);
      const amt = document.createElement('span'); amt.className = 'amt';
      amt.textContent = b.mm == null ? '—' : fmt(b.mm);
      btn.append(left, amt);
      btn.onclick = () => trace(b, btn);
      box.appendChild(btn);
    });
  }
  async function trace(b, btn) {
    document.querySelectorAll('.ds-bonds button').forEach(x => x.classList.toggle('sel', x === btn));
    const hits = all.filter(m => b.key && m.dataset.k === b.key && visible(m));
    if (!hits.length) return;
    stopCrawl();
    const id = ++runId;
    Spider.clear();
    const first = centre(hits[0]);
    scrollToY(first.y);
    Spider.setFollow(true);
    const spot = perch(first);
    await Spider.walkTo(spot.x, spot.y);
    Spider.setFollow(false);
    for (const m of hits) {
      if (id !== runId) return;
      const p = centre(m);
      flash(m);
      await Spider.spin(p.x, p.y, colors.debt);
      Dash.onHit(m, { count: false });
    }
  }

  // ------------------------------------------------------- tabs and toggles
  document.querySelectorAll('.ds-tab').forEach(t => t.onclick = () => {
    document.querySelectorAll('.ds-tab').forEach(x => x.setAttribute('aria-selected', x === t));
    document.querySelectorAll('.ds-pane').forEach(p => p.hidden = p.dataset.pane !== t.dataset.tab);
  });
  $('.ds-toggle').onclick = () => { document.body.classList.toggle('ds-collapsed'); Spider.clear(); };

  renderCats(); refresh(); renderDebt();
  Spider.init($('#ds-canvas'), all);
  Spider.setSpeed(+$('#ds-speed').value);
  Dash.init(D, colors);
  Dash.reset(list.length);
})();
