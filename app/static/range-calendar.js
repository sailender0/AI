// Shared flight-booking style range picker. Builds its own DOM inside `container`
// and calls opts.onChange(startISO, endISO) each time a full range is (re)selected.
// Future days are disabled. Styling lives in app.css under the .rc- prefix.
(function () {
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const iso = d => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const today = () => iso(new Date());
  const firstOfMonth = s => { const [y, m] = s.split('-').map(Number); return new Date(y, m - 1, 1); };

  window.RangeCalendar = function (container, opts) {
    opts = opts || {};
    let start = opts.start || null, end = opts.end || null;
    let base = firstOfMonth(start || today());
    container.classList.add('rc');
    container.innerHTML = `
      <div class="rc-head">
        <button type="button" class="rc-arrow" data-dir="-1" aria-label="Previous month">‹</button>
        <div class="rc-titles"><span class="rc-t0"></span><span class="rc-t1"></span></div>
        <button type="button" class="rc-arrow" data-dir="1" aria-label="Next month">›</button>
      </div>
      <div class="rc-months"><div class="rc-m rc-m0"></div><div class="rc-m rc-m1"></div></div>`;

    container.addEventListener('click', e => {
      const arrow = e.target.closest('.rc-arrow');
      if (arrow) { base.setMonth(base.getMonth() + (+arrow.dataset.dir)); render(); return; }
      const day = e.target.closest('.rc-day[data-iso]');
      if (day) onDay(day.dataset.iso);
    });

    function render() {
      renderMonth(container.querySelector('.rc-m0'), container.querySelector('.rc-t0'), base);
      renderMonth(container.querySelector('.rc-m1'), container.querySelector('.rc-t1'),
                  new Date(base.getFullYear(), base.getMonth() + 1, 1));
    }
    function renderMonth(grid, title, monthDate) {
      const y = monthDate.getFullYear(), m = monthDate.getMonth();
      title.textContent = `${MON[m]} ${y}`;
      const firstDow = new Date(y, m, 1).getDay(), days = new Date(y, m + 1, 0).getDate(), t = today();
      let html = DOW.map(d => `<span class="rc-dow">${d[0]}</span>`).join('');
      for (let i = 0; i < firstDow; i++) html += '<span class="rc-day rc-empty"></span>';
      for (let dd = 1; dd <= days; dd++) {
        const s = `${y}-${String(m + 1).padStart(2, '0')}-${String(dd).padStart(2, '0')}`;
        const future = s > t;
        html += `<button type="button" class="rc-day ${dayClass(s)} ${future ? 'rc-disabled' : ''}" ${future ? '' : `data-iso="${s}"`}>${dd}</button>`;
      }
      grid.innerHTML = html;
    }
    function dayClass(s) {
      if (!start) return '';
      if (s === start) return 'rc-start' + (end ? '' : ' rc-only');
      if (end) { if (s === end) return 'rc-end'; if (s > start && s < end) return 'rc-range'; }
      return '';
    }
    function onDay(s) {
      if (!start || end) { start = s; end = null; }      // begin a new range
      else if (s < start) { start = s; }                 // clicked before start → move start
      else { end = s; }                                  // complete the range
      render();
      if (start && end && opts.onChange) opts.onChange(start, end);
    }

    render();
    return {
      getRange: () => ({ start, end }),
      setRange: (s, e) => { start = s; end = e; base = firstOfMonth(s || today()); render(); },
    };
  };
})();
