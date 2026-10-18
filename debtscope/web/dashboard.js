/* DebtScope dashboard strip: four panels that react to what the spider finds.
 *   1. Live findings  - newest hits with category and source (XBRL concept or filing text)
 *   2. Crawl progress - findings read so far, split by category
 *   3. Notes in text  - one dot per note in the debt stack; lights up when the spider reaches a mention
 *   4. Maturities     - XBRL contractual schedule; a bar pulses when a note maturing in it is found
 */
window.Dash = (function () {
  const $ = s => document.querySelector(s);
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  let D, colors, total = 0, read = 0, t0 = 0;
  const perCat = {}, noteDots = new Map(), litNotes = new Set();
  const fmt = (v, d = 0) => v == null ? 'n/a' : v.toLocaleString('en-US', { maximumFractionDigits: d, minimumFractionDigits: d });

  function clock() {
    const s = Math.floor((performance.now() - t0) / 1000);
    return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  }

  function buildNotes() {
    const notes = D.instruments.filter(i => !i.aggregate && i.key);
    const box = $('#dsd-dots');
    box.innerHTML = '';
    noteDots.clear();
    notes.forEach(n => {
      const d = document.createElement('span');
      d.className = 'dsd-dot';
      d.dataset.st = n.status.replace(/\s+/g, '-');
      d.title = `${n.label} (${n.status})`;
      box.appendChild(d);
      if (!noteDots.has(n.key)) noteDots.set(n.key, []);
      noteDots.get(n.key).push(d);
    });
    $('#dsd-notes-n').textContent = `0 of ${notes.length}`;
  }

  function buildWall() {
    const max = Math.max(1, ...D.schedule.map(s => s.mm));
    $('#dsd-wall').innerHTML = D.schedule.length ? D.schedule.map(s =>
      `<div class="dsd-bar" data-b="${s.bucket}" style="--h:${Math.max(3, s.mm / max * 100)}%" title="${esc(s.label)}: ${fmt(s.mm)} mm">
         <i></i><span>${esc(s.label.replace(/^FY20/, "FY"))}</span></div>`).join('')
      : '<p class="dsd-empty">No maturity schedule tagged in XBRL.</p>';
    const due = D.metrics.due_within_24m_pct;
    $('#dsd-wall-cap').textContent = due == null ? '' : `${fmt(due, 1)}% due within 24 months`;
  }

  function buildProgress() {
    $('#dsd-seg').innerHTML = D.categories.map(c =>
      `<i data-c="${c.id}" style="background:${c.color};flex-grow:0"></i>`).join('');
    renderProgress();
  }
  function renderProgress() {
    $('#dsd-read').textContent = fmt(read);
    $('#dsd-total').textContent = fmt(total);
    D.categories.forEach(c => {
      const el = document.querySelector(`#dsd-seg i[data-c="${c.id}"]`);
      if (el) el.style.flexGrow = total ? (perCat[c.id] || 0) / total : 0;
    });
    $('#dsd-legend').innerHTML = D.categories.filter(c => perCat[c.id]).map(c =>
      `<span><b style="background:${c.color}"></b>${esc(c.label)} ${fmt(perCat[c.id])}</span>`).join('');
  }

  return {
    init(data, catColors) {
      D = data; colors = catColors;
      buildNotes(); buildWall(); buildProgress();
      $('#dsd-toggle').onclick = () => {
        const hidden = document.body.classList.toggle('dsd-hidden');
        $('#dsd-toggle').textContent = hidden ? 'Show dashboard' : 'Hide dashboard';
      };
    },
    reset(n) {
      total = n; read = 0; t0 = performance.now();
      for (const k in perCat) delete perCat[k];
      litNotes.clear();
      document.querySelectorAll('.dsd-dot.lit').forEach(d => d.classList.remove('lit'));
      $('#dsd-feed').innerHTML = '';
      $('#dsd-notes-n').textContent = `0 of ${[...noteDots.values()].reduce((a, v) => a + v.length, 0)}`;
      renderProgress();
    },
    onHit(m, opts = {}) {
      if (opts.count !== false) {          // tracing a bond does not count as crawl progress
        read++;
        perCat[m.dataset.c] = (perCat[m.dataset.c] || 0) + 1;
        renderProgress();
      }
      // live feed
      const cat = D.categories.find(c => c.id === m.dataset.c);
      const row = document.createElement('li');
      const src = m.dataset.x ? m.dataset.x : m.dataset.r ? `table: ${m.dataset.r}` : 'filing text';
      row.innerHTML = `<time>${clock()}</time><b style="--c:${cat ? cat.color : '#999'}"></b>`
        + `<span class="v">${esc(m.textContent.trim().slice(0, 42))}</span><span class="s">${esc(src)}</span>`;
      const feed = $('#dsd-feed');
      feed.prepend(row);
      while (feed.children.length > 6) feed.lastChild.remove();
      // notes grid + maturity bars react to bond mentions
      const k = m.dataset.k;
      if (k && noteDots.has(k)) {
        noteDots.get(k).forEach(d => d.classList.add('lit'));
        litNotes.add(k);
        const lit = document.querySelectorAll('.dsd-dot.lit').length;
        $('#dsd-notes-n').textContent = `${lit} of ${document.querySelectorAll('.dsd-dot').length}`;
        const year = +k.split('|')[1];
        const offset = year - D.fy;
        const bucket = offset <= 0 ? 1 : offset > 5 ? 6 : offset;
        const bar = document.querySelector(`.dsd-bar[data-b="${bucket}"]`);
        if (bar) { bar.classList.remove('pulse'); void bar.offsetWidth; bar.classList.add('pulse'); }
      }
    },
  };
})();
