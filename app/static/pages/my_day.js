  const MONTHS       = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  const MONTHS_SHORT = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

  const _today = new Date();
  _today.setHours(0, 0, 0, 0);

  const _urlDate  = new URLSearchParams(location.search).get('date');
  const _initDate = _urlDate && /^\d{4}-\d{2}-\d{2}$/.test(_urlDate) ? _urlDate : dateFmt(_today);
  const _initObj  = new Date(_initDate + 'T00:00:00');

  let viewYear     = _initObj.getFullYear();
  let viewMonth    = _initObj.getMonth();
  let pickerYear   = viewYear;
  let selectedDate = _initDate;

  const NOW_MK = _today.getFullYear() * 12 + _today.getMonth();
  const MIN_MK = NOW_MK - 12; // allow 1 year back

  function dateFmt(d) { return d.toISOString().slice(0, 10); }

  async function onBaseReady(data) {
    updateWeekHeader(selectedDate);
    renderCalendar();
    renderMonthPicker();
    if (data.integrations?.jira) loadDueStrip();   // fire-and-forget: strip is a bonus
    await loadDayData(selectedDate);
  }

  // "What's burning today" — live from Jira; stays hidden on a clean day.
  async function loadDueStrip() {
    const el = document.getElementById('due-strip');
    const data = await getJSON('/api/jira/assigned');
    if (!el || !data) return;
    const urgent = (data.issues || [])
      .map(it => ({ it, d: _dueInfo(it.due_date) }))
      .filter(x => x.d && x.d.days <= 0);
    if (!urgent.length) return;
    const overdue = urgent.filter(x => x.d.days < 0).length;
    const today   = urgent.length - overdue;
    const head = [overdue ? `${overdue} overdue` : '', today ? `${today} due today` : '']
      .filter(Boolean).join(' · ');
    el.innerHTML = `
      <div class="flex items-center gap-3 flex-wrap">
        <span class="text-sm font-semibold" style="color:#fbbf24">⏰ ${head}</span>
        ${urgent.slice(0, 6).map(x => `<span class="flex items-center gap-1.5">
          ${_keyLink(x.it.key)}<span class="text-xs" style="color:${x.d.color}">${esc(x.d.label)}</span>
        </span>`).join('')}
        ${urgent.length > 6 ? `<span class="text-xs" style="color:var(--text-3)">+${urgent.length - 6} more</span>` : ''}
      </div>`;
    el.classList.remove('hidden');
  }

  function renderCalendar() {
    document.getElementById('month-label').textContent = `${MONTHS[viewMonth]} ${viewYear}`;

    const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();
    const startDow    = new Date(viewYear, viewMonth, 1).getDay();
    const todayStr    = dateFmt(_today);
    const selObj      = new Date(selectedDate + 'T00:00:00');

    const prevYear2  = viewMonth === 0 ? viewYear - 1 : viewYear;
    const prevMonth2 = viewMonth === 0 ? 11 : viewMonth - 1;
    const prevDays   = new Date(viewYear, viewMonth, 0).getDate();
    const nextYear2  = viewMonth === 11 ? viewYear + 1 : viewYear;
    const nextMonth2 = viewMonth === 11 ? 0 : viewMonth + 1;

    // Build cells: overflow-prev + current month + overflow-next
    const cells = [];
    for (let i = startDow - 1; i >= 0; i--)
      cells.push({ day: prevDays - i, month: prevMonth2, year: prevYear2, overflow: true });
    for (let d = 1; d <= daysInMonth; d++)
      cells.push({ day: d, month: viewMonth, year: viewYear, overflow: false });
    let nextDay = 1;
    const totalRows = Math.ceil(cells.length / 7);
    while (cells.length < totalRows * 7)
      cells.push({ day: nextDay++, month: nextMonth2, year: nextYear2, overflow: true });

    const grid = document.getElementById('cal-grid');
    grid.innerHTML = '';

    for (let row = 0; row < totalRows; row++) {
      const rowCells = cells.slice(row * 7, row * 7 + 7);
      const rowEl = document.createElement('div');
      rowEl.className = 'cal-row';

      // Highlight the row that contains the selected date
      const rowHasSel = rowCells.some(c =>
        !c.overflow &&
        c.year  === selObj.getFullYear() &&
        c.month === selObj.getMonth() &&
        c.day   === selObj.getDate()
      );
      if (rowHasSel) rowEl.classList.add('this-week');

      rowCells.forEach(c => {
        const dateObj     = new Date(c.year, c.month, c.day);
        const dateStr     = dateFmt(dateObj);
        const isToday     = dateStr === todayStr;
        const isSel       = dateStr === selectedDate;
        const isFuture    = dateObj > _today;
        const isPastLimit = c.year * 12 + c.month < MIN_MK;

        let cls = 'cal-cell';
        if (c.overflow)                       cls += ' overflow';
        else if (isFuture || isPastLimit)     cls += ' future';
        else                                  cls += ' clickable';
        if (isToday)                          cls += ' today';
        else if (isSel && !c.overflow)        cls += ' selected';

        const cell = Object.assign(document.createElement('div'), { className: cls, textContent: c.day });
        if (!c.overflow && !isFuture && !isPastLimit)
          cell.onclick = () => selectDay(dateStr);
        rowEl.appendChild(cell);
      });

      grid.appendChild(rowEl);
    }

    const mk = viewYear * 12 + viewMonth;
    document.getElementById('prev-btn').style.visibility = mk <= MIN_MK ? 'hidden' : '';
    document.getElementById('next-btn').style.visibility = mk >= NOW_MK ? 'hidden' : '';
  }

  function renderMonthPicker() {
    document.getElementById('year-label').textContent = pickerYear;
    const nowYear = _today.getFullYear();
    const minYear = nowYear - 1;

    document.getElementById('month-grid').innerHTML = MONTHS_SHORT.map((name, i) => {
      const mk     = pickerYear * 12 + i;
      const isCur  = pickerYear === viewYear && i === viewMonth;
      const isFut  = mk > NOW_MK;
      const isPast = mk < MIN_MK;
      let cls = 'month-cell';
      if (isCur)           cls += ' active';
      else if (isFut || isPast) cls += ' disabled';
      return `<div class="${cls}" ${(!isCur && !isFut && !isPast) ? `onclick="selectMonth(${pickerYear},${i})"` : ''}>${name}</div>`;
    }).join('');

    document.getElementById('prev-year-btn').style.visibility = pickerYear <= minYear ? 'hidden' : '';
    document.getElementById('next-year-btn').style.visibility = pickerYear >= nowYear ? 'hidden' : '';
  }

  function selectMonth(year, month) {
    viewYear = year; viewMonth = month;
    renderCalendar(); renderMonthPicker();
  }

  function prevYear() { pickerYear--; renderMonthPicker(); }
  function nextYear() { pickerYear++; renderMonthPicker(); }

  function goToToday() {
    viewYear  = _today.getFullYear();
    viewMonth = _today.getMonth();
    pickerYear   = viewYear;
    selectedDate = dateFmt(_today);
    updateWeekHeader(selectedDate);
    renderCalendar(); renderMonthPicker();
    loadDayData(selectedDate);
  }

  function getWeekLabel(dateStr) {
    const d   = new Date(dateStr + 'T00:00:00');
    const dow = d.getDay();
    const mon = new Date(d); mon.setDate(d.getDate() - (dow === 0 ? 6 : dow - 1));
    const fri = new Date(mon); fri.setDate(mon.getDate() + 4);
    const fmt = dt => dt.toLocaleDateString(undefined, { month: 'long', day: 'numeric' });
    const year = fri.getFullYear();
    if (mon.getMonth() === fri.getMonth())
      return `${fmt(mon).replace(/\s\d+$/, '')} ${mon.getDate()}–${fri.getDate()}, ${year}`;
    return `${fmt(mon)}–${fmt(fri)}, ${year}`;
  }

  let _calOpen = true;

  function updateWeekHeader(dateStr) {
    document.getElementById('week-label').textContent = getWeekLabel(dateStr);
  }

  function toggleCalendar() {
    _calOpen = !_calOpen;
    const card    = document.getElementById('cal-card');
    const chevron = document.getElementById('week-chevron');
    card.style.display    = _calOpen ? '' : 'none';
    chevron.style.transform = _calOpen ? '' : 'rotate(-90deg)';
  }

  function selectDay(dateStr) {
    selectedDate = dateStr;
    updateWeekHeader(dateStr);
    renderCalendar();
    loadDayData(dateStr);
  }

  function prevMonth() {
    if (viewYear * 12 + viewMonth <= MIN_MK) return;
    if (--viewMonth < 0) { viewMonth = 11; viewYear--; }
    pickerYear = viewYear;
    renderCalendar(); renderMonthPicker();
  }

  function nextMonth() {
    if (viewYear * 12 + viewMonth >= NOW_MK) return;
    if (++viewMonth > 11) { viewMonth = 0; viewYear++; }
    pickerYear = viewYear;
    renderCalendar(); renderMonthPicker();
  }

  async function loadDayData(dateStr) {
    const isToday   = dateStr === dateFmt(_today);
    const dateLabel = new Date(dateStr + 'T00:00:00').toLocaleDateString(undefined, {
      weekday: 'long', month: 'long', day: 'numeric', year: 'numeric',
    });

    document.getElementById('day-badge').textContent =
      isToday ? 'TODAY' : new Date(dateStr + 'T00:00:00')
        .toLocaleDateString(undefined, { weekday: 'short' }).toUpperCase();
    document.getElementById('day-label').textContent = dateLabel;
    document.getElementById('summary-content').textContent = 'Loading…';
    document.getElementById('events-list').innerHTML =
      '<p class="text-sm" style="color:var(--text-3)">Loading…</p>';
    document.getElementById('today-stats').innerHTML = '';

    let data;
    try {
      const res = await fetch(`/api/day-data?date=${dateStr}`, { credentials: 'include' });
      data = await res.json();
    } catch {
      document.getElementById('summary-content').textContent = 'Failed to load — check connection.';
      return;
    }

    const hasEvents = (data.events || []).length > 0;
    const hasSummary = !!data.summary;
    document.getElementById('gen-btn').style.display    = hasEvents ? '' : 'none';
    document.getElementById('dl-btn').style.display     = (hasEvents && hasPerm('export_my_day')) ? '' : 'none';

    // Summary
    const summaryEl = document.getElementById('summary-content');
    if (data.summary) {
      summaryEl.textContent = data.summary;
      document.getElementById('gen-label').textContent = 'Regenerate';
    } else {
      summaryEl.textContent = hasEvents
        ? 'No summary yet. Click "Generate" to create one.'
        : (isToday ? 'No activity yet today.' : 'No activity on this day.');
      document.getElementById('gen-label').textContent = 'Generate';
    }

    // Stats
    const sc       = data.source_counts || {};
    const evtLabel = isToday ? 'events today' : 'events that day';
    document.getElementById('today-stats').innerHTML = [
      { label: 'GitHub', key: 'github',             icon: '⌥', grad: '#f97316,#fb923c', cls: 'kpi-num-github'  },
      { label: 'Jira',   key: 'jira',               icon: '◈', grad: '#06b6d4,#22d3ee', cls: 'kpi-num-jira'    },
      { label: 'Teams',  key: 'teams_subscription', icon: '◉', grad: '#8b5cf6,#a78bfa', cls: 'kpi-num-teams'   },
      { label: 'GitLab', key: 'gitlab',             icon: '🦊', grad: '#10b981,#34d399', cls: 'kpi-num-gitlab'  },
    ].map(s => `
      <div class="card kpi-tilt-card" style="overflow:hidden;padding:0">
        <div style="height:2px;background:linear-gradient(90deg,${s.grad})"></div>
        <div class="p-4">
          <p class="kpi-label mb-2">${s.icon} ${s.label}</p>
          <p class="kpi-number ${s.cls}">${sc[s.key] || 0}</p>
          <p class="text-xs mt-1" style="color:var(--text-3)">${evtLabel}</p>
        </div>
      </div>`).join('');
    initKpiTilt();

    // Timeline
    const list   = document.getElementById('events-list');
    const events = data.events || [];
    if (!events.length) {
      list.innerHTML = `<p class="text-sm" style="color:var(--text-3)">${
        isToday ? 'No activity yet today.' : 'No activity on this day.'
      }</p>`;
      return;
    }
    events.sort((a, b) => new Date(b.occurred_at) - new Date(a.occurred_at));
    list.innerHTML = renderTimeline(events);
  }

  // Source colours/labels and repoColor() come from app.js.

  const _DY_BRANCH = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>`;
  const _DY_CLOCK  = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>`;

  function _dayFmtTime(isoStr) {
    if (!isoStr) return '';
    return new Date(isoStr).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  }

  function _dayFmtEventType(et) {
    return (et || 'unknown').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  }

  function _dayCommitExtra(it) {
    const shaHtml = it.sha
      ? `<code style="font-size:10px;font-family:monospace;background:var(--surface-2);color:var(--text-3);padding:1px 5px;border-radius:4px;margin-left:4px">${it.sha}</code>`
      : '';
    const filesHtml = it.files?.length
      ? `<div class="flex flex-wrap gap-1 mt-1">${it.files.slice(0,4).map(f=>`<span style="font-size:10px;font-family:monospace;background:var(--surface-2);color:var(--text-3);padding:1px 5px;border-radius:4px">${f.split('/').pop()}</span>`).join('')}${it.files.length>4?`<span style="font-size:10px;color:var(--text-3)"> +${it.files.length-4} more</span>`:''}</div>`
      : '';
    return { shaHtml, filesHtml };
  }

  function renderTimeline(events) {
    events = escEvents(events);
    const bySource = events.reduce((acc, e) => {
      (acc[e.source] = acc[e.source] || []).push(e);
      return acc;
    }, {});
    const order   = ['github', 'jira', 'teams_subscription', 'gitlab'];
    const sources = [...order.filter(s => bySource[s]), ...Object.keys(bySource).filter(s => !order.includes(s))];
    const initials = window._actUserInitials || '??';
    const author   = window._actAuthorName   || 'you';

    return sources.map(src => {
      const { color, label } = srcOf(src);
      const items = bySource[src];
      const groups = {};
      items.forEach(it => { const k = it.event_type || 'other'; (groups[k] = groups[k] || []).push(it); });
      const feedHtml = Object.entries(groups).map(([et, evts]) => `
        <div class="mb-3">
          <div class="flex items-center gap-2 mb-1">
            <span class="text-xs font-semibold" style="color:var(--text-2)">${_dayFmtEventType(et)}</span>
            <span class="text-xs px-1.5 py-0.5 rounded-full" style="background:var(--border-strong);color:var(--text-3)">${evts.length}</span>
          </div>
          <div class="commit-feed">${evts.map(it => {
            const avatarColor = repoColor(it.workspace || src || '');
            const time        = _dayFmtTime(it.occurred_at);
            const {shaHtml, filesHtml} = _dayCommitExtra(it);
            return `<div class="commit-item">
              <div class="commit-avatar" style="background:${avatarColor}">${initials}</div>
              <div class="commit-body">
                <div class="commit-meta">
                  <span class="commit-author">${author}</span>
                  ${it.workspace ? `<span class="commit-repo-pill">${_DY_BRANCH} ${it.workspace}</span>` : ''}
                  ${time ? `<span class="commit-time">${_DY_CLOCK} ${time}</span>` : ''}
                </div>
                <div class="commit-title">${it.issue_key ? _keyLink(it.issue_key) + ' ' : ''}${it.title || '—'}${shaHtml}</div>
                ${(it.status || it.priority) ? `<div class="flex items-center gap-1.5 mt-0.5">${_chip(it.status, '#06b6d4')}${_prioChip(it.priority)}</div>` : ''}
                ${filesHtml}
              </div>
            </div>`;
          }).join('')}</div>
        </div>`).join('');
      return `<div class="mb-4">
        <div class="flex items-center gap-2 mb-2 pl-2.5" style="border-left:2px solid ${color}">
          <span class="text-xs font-semibold" style="color:${color}">${label}</span>
          <span class="text-xs ml-auto" style="color:var(--text-3)">${items.length} event${items.length !== 1 ? 's' : ''}</span>
        </div>
        ${feedHtml}
      </div>`;
    }).join('');
  }

  window.onWSEvent = function(data) {
    if (selectedDate === dateFmt(_today)) {
      loadDayData(selectedDate);
    }
  };

  async function generateSummary() {
    const label   = document.getElementById('gen-label');
    const content = document.getElementById('summary-content');
    label.textContent   = 'Generating…';
    content.textContent = 'Generating your daily summary…';
    try {
      const res  = await fetch('/api/summaries/generate', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ period_type: 'daily', date: selectedDate }),
      });
      const data = await res.json();
      if (data.ok) {
        await loadDayData(selectedDate);
      } else {
        content.textContent = `Error: ${data.error}`;
        label.textContent   = 'Retry';
      }
    } catch {
      content.textContent = 'Failed — check logs';
      label.textContent   = 'Retry';
    }
  }

  function downloadDailyPdf() {
    window.location.href = `/api/export/daily-pdf?date=${selectedDate}`;
  }
