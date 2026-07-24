  let chart           = null;
  let statsPeriod     = 'week';
  let evtPeriod       = 'today';
  // ── Week picker — shared weekPicker() from app.js (Monday-start) ───────────
  let _chartStartDate = currentWeek().start;
  let _picker         = null;

  function initPicker() {
    _picker = weekPicker({
      mount: '#week-picker',
      button: '#range-week',
      onSelect(mondayIso, label) {
        _chartStartDate = mondayIso;
        document.getElementById('range-week-label').textContent = label;
        document.getElementById('chart-sub').textContent = `· ${label}`;
        document.getElementById('range-default').classList.remove('active');
        document.getElementById('range-week').classList.add('active');
        loadStats();   // teams has no panel feed — stats only
      },
    });
    _picker?.setSelected(_chartStartDate);
  }

  function setRangeDefault() {
    const cw = currentWeek();
    _chartStartDate = cw.start;
    _picker?.setSelected(cw.start);
    _picker?.close();
    document.getElementById('range-week-label').textContent = 'Pick Week';
    document.getElementById('chart-sub').textContent = `· ${cw.label}`;
    document.getElementById('range-default').classList.add('active');
    document.getElementById('range-week').classList.remove('active');
    return loadStats();
  }

  window.onWSEvent = function(data) {
    if (data.source !== 'teams_subscription') return;
    loadStats();
    loadEvents();
  };

  async function onBaseReady(meData) {
    if (!meData.integrations.teams_subscription) {
      document.getElementById('not-connected').classList.remove('hidden');
    } else if (meData.integration_errors?.teams_subscription) {
      document.getElementById('subscription-error').classList.remove('hidden');
    }
    initPicker();
    setPeriod('week', false);
    setEvtPeriod('today', false);
    await Promise.all([setRangeDefault(), loadEvents()]);
  }

  function setEvtPeriod(p, fetch = true) {
    evtPeriod = p;
    document.getElementById('evt-today').classList.toggle('active',   p === 'today');
    document.getElementById('evt-history').classList.toggle('active', p === 'history');
    if (fetch) loadEvents();
  }

  function setPeriod(p, fetch = true) {
    statsPeriod = p;
    document.getElementById('period-today').classList.toggle('active', p === 'today');
    document.getElementById('period-week').classList.toggle('active',  p === 'week');
    if (fetch) loadStats();
  }

  async function loadStats() {
    let url = `/api/teams/stats?period=${statsPeriod}`;
    if (_chartStartDate) url += `&start_date=${_chartStartDate}`;
    const res = await fetch(url, { credentials: 'include' });
    const data = await res.json();

    const grid = document.getElementById('metric-cards');
    grid.innerHTML = (data.metrics || []).map(m => {
      const changeHtml = m.change !== undefined
        ? `<p class="text-xs mt-1 ${m.change >= 0 ? 'text-green-400' : 'text-red-400'}">${m.change >= 0 ? '↑' : '↓'} ${Math.abs(m.change)}% vs last week</p>`
        : '';
      return `<div class="card kpi-tilt-card" style="overflow:hidden;padding:0">
        <div style="height:2px;background:linear-gradient(90deg,#8b5cf6,#a78bfa)"></div>
        <div class="p-4">
          <p class="kpi-label mb-2">${m.label}</p>
          <p class="kpi-number kpi-num-teams">${m.value}</p>
          ${changeHtml}
        </div>
      </div>`;
    }).join('');
    initKpiTilt();

    const cc      = chartColors();
    const msgRepos = (data.chart.repos || {}).messages || {};
    const ctx = document.getElementById('main-chart').getContext('2d');
    if (chart) chart.destroy();
    chart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: data.chart.labels,
        datasets: [{
          data: data.chart.datasets.messages,
          backgroundColor: 'rgba(139,92,246,0.6)',
          borderColor: '#8b5cf6',
          borderWidth: 1,
          borderRadius: 4,
        }]
      },
      options: {
        responsive: true,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: items => items[0].label,
              label: item => {
                const lines = [`Messages: ${item.raw}`];
                const dayRepos = msgRepos[item.label] || {};
                Object.entries(dayRepos).forEach(([ws, cnt]) => lines.push(`  · ${ws}: ${cnt}`));
                return lines;
              },
            },
          },
        },
        scales: {
          x: { grid: { color: cc.grid }, ticks: { color: cc.tick, font: { size: 10 } } },
          y: { grid: { color: cc.grid }, ticks: { color: cc.tick, font: { size: 10 } }, beginAtZero: true },
        },
      }
    });
  }

  const _TM_COLORS = ['#3b82f6','#22c55e','#f59e0b','#8b5cf6','#ec4899','#06b6d4','#84cc16','#64748b'];
  function _tmRepoColor(s) { let h=0; for(const c of(s||''))h=(h*31+c.charCodeAt(0))&0xffff; return _TM_COLORS[h%_TM_COLORS.length]; }
  const _TM_BRANCH = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>`;
  const _TM_CLOCK  = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>`;
  const _tmFmt = et => (et||'unknown').replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase());

  function renderConnFeed(events) {
    events = escEvents(events);
    const initials = window._actUserInitials || '??';
    const author   = window._actAuthorName   || 'you';
    if (evtPeriod === 'history') {
      return `<div class="commit-feed">${events.map(it => {
        const color = _tmRepoColor(it.workspace||'');
        const time  = new Date(it.occurred_at).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
        return `<div class="commit-item">
          <div class="commit-avatar" style="background:${color}">${initials}</div>
          <div class="commit-body">
            <div class="commit-meta">
              <span class="commit-author">${author}</span>
              ${it.workspace?`<span class="commit-repo-pill">${_TM_BRANCH} ${it.workspace}</span>`:''}
              <span class="commit-time">${_TM_CLOCK} ${time}</span>
            </div>
            <div class="commit-title">${it.title||'—'}</div>
            <div class="commit-sub">${_tmFmt(it.event_type)}</div>
          </div>
        </div>`;
      }).join('')}</div>`;
    }
    const groups = {};
    events.forEach(it => { const k=it.event_type||'other'; (groups[k]=groups[k]||[]).push(it); });
    return Object.entries(groups).map(([et,evts]) => `
      <div class="mb-3">
        <div class="flex items-center gap-2 mb-1 pl-2" style="border-left:2px solid #8b5cf6">
          <span class="text-xs font-semibold" style="color:var(--text-2)">${_tmFmt(et)}</span>
          <span class="text-xs px-1.5 py-0.5 rounded-full" style="background:var(--border-strong);color:var(--text-3)">${evts.length}</span>
        </div>
        <div class="commit-feed">${evts.map(it => {
          const color = _tmRepoColor(it.workspace||'');
          const time  = new Date(it.occurred_at).toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'});
          return `<div class="commit-item">
            <div class="commit-avatar" style="background:${color}">${initials}</div>
            <div class="commit-body">
              <div class="commit-meta">
                <span class="commit-author">${author}</span>
                ${it.workspace?`<span class="commit-repo-pill">${_TM_BRANCH} ${it.workspace}</span>`:''}
                <span class="commit-time">${_TM_CLOCK} ${time}</span>
              </div>
              <div class="commit-title">${it.title||'—'}</div>
            </div>
          </div>`;
        }).join('')}</div>
      </div>`).join('');
  }

  async function loadEvents() {
    const res  = await fetch('/api/events/recent?limit=50', { credentials: 'include' });
    const data = await res.json();
    const list = document.getElementById('events-list');
    let events = (data.events || []).filter(e => e.source === 'teams_subscription');
    if (evtPeriod === 'today') {
      const today = new Date().toDateString();
      events = events.filter(e => new Date(e.occurred_at).toDateString() === today);
    }
    events.sort((a, b) => new Date(b.occurred_at) - new Date(a.occurred_at));
    if (!events.length) {
      list.innerHTML = `<p class="text-gray-500 text-sm">${evtPeriod==='today'?'No Teams activity today.':'No recent Teams activity.'}</p>`;
      return;
    }
    list.innerHTML = renderConnFeed(events);
  }
