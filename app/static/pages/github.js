  let chart           = null;
  let statsPeriod     = 'week';
  let evtPeriod       = 'today';
  let _panelEvents    = [];
  let _panelTab       = 'activity';

  const _ET_FILTER = {
    commits:       e => /^commit/i.test(e.event_type),
    pull_requests: e => /^pr_(?!review$)/i.test(e.event_type),
    issues:        e => /^issue/i.test(e.event_type),
    reviews:       e => /^pr_review$/i.test(e.event_type),
  };

  async function loadPanelEvents() {
    if (!_chartStartDate) return;
    const s = new Date(_chartStartDate + 'T00:00:00');
    const e = new Date(s); e.setDate(s.getDate() + 7);
    const endIso = `${e.getFullYear()}-${String(e.getMonth()+1).padStart(2,'0')}-${String(e.getDate()).padStart(2,'0')}`;
    const data = await getJSON(`/api/events/recent?source=github&start_date=${_chartStartDate}&end_date=${endIso}&limit=200`);
    _panelEvents = data?.events || [];
    renderPanelFeed();
  }
  let _ghChartData    = null;
  let _chartRepos     = null;
  let _currentType    = 'commits';
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
        loadPanelEvents();
        loadStats();
      },
    });
    _picker?.setSelected(_chartStartDate);
  }

  function setRangeDefault() {
    const cw = currentWeek();
    _chartStartDate = cw.start;
    _picker?.setSelected(cw.start);
    _picker?.close();
    document.getElementById('range-default').classList.add('active');
    document.getElementById('range-week').classList.remove('active');
    document.getElementById('range-week-label').textContent = 'Pick Week';
    document.getElementById('chart-sub').textContent = `· ${cw.label}`;
    loadPanelEvents();
    return loadStats();
  }

  function setPanelTab(tab) {
    _panelTab = tab;
    document.getElementById('panel-tab-activity').classList.toggle('active', tab === 'activity');
    document.getElementById('panel-tab-repos').classList.toggle('active',    tab === 'repos');
    document.getElementById('panel-feed').classList.toggle('hidden',         tab !== 'activity');
    document.getElementById('panel-top-repos').classList.toggle('hidden',    tab !== 'repos');
  }

  function renderPanelFeed() {
    const el = document.getElementById('panel-feed');
    if (!el || _panelTab !== 'activity') return;
    const filter = _ET_FILTER[_currentType] || (() => true);
    const events = (_panelEvents || []).filter(filter).slice(0, 30);
    const typeName = _CT_LABELS[_currentType] || 'Activity';

    if (!events.length) {
      el.innerHTML = `
        <p class="text-xs font-semibold mb-2" style="color:var(--text-1)">${typeName}</p>
        <p class="text-xs py-2" style="color:var(--text-3)">No ${typeName.toLowerCase()} this week.</p>`;
      return;
    }
    el.innerHTML = `
      <div class="flex items-center gap-2 mb-3">
        <span class="text-xs font-semibold" style="color:var(--text-1)">${typeName}</span>
        <span class="text-xs px-1.5 py-0.5 rounded-full" style="background:var(--surface-2);color:var(--text-3)">${events.length}</span>
      </div>
      ${events.map(it => {
        const color = repoColor(it.workspace || '');
        const time  = new Date(it.occurred_at).toLocaleString(undefined, {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'});
        const filesHtml = it.files?.length
          ? `<div class="flex flex-wrap gap-1 mt-1">${it.files.slice(0,4).map(f=>`<span style="font-size:10px;font-family:monospace;background:var(--surface-2);color:var(--text-3);padding:1px 5px;border-radius:4px">${f.split('/').pop()}</span>`).join('')}${it.files.length>4?`<span style="font-size:10px;color:var(--text-3)">+${it.files.length-4}</span>`:''}</div>` : '';
        return `<div class="flex items-start gap-2 py-2" style="border-bottom:1px solid var(--border)">
          <div class="w-2 h-2 rounded-full mt-1.5 shrink-0" style="background:${color}"></div>
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-1.5 mb-0.5">
              <span class="text-xs font-semibold truncate" style="color:var(--text-1)">${it.workspace||'—'}</span>
              ${/^pr_/i.test(it.event_type)?`<span style="font-size:10px;font-weight:600;background:var(--surface-2);color:var(--text-2);padding:1px 6px;border-radius:4px">${_prLabel(it.event_type)}</span>`:''}
              ${it.sha?`<code style="font-size:10px;font-family:monospace;background:var(--surface-2);color:var(--text-3);padding:1px 5px;border-radius:4px">${it.sha}</code>`:''}
            </div>
            <span class="text-xs truncate block mb-0.5" style="color:var(--text-2)">${it.title||'—'}</span>
            ${filesHtml}
            <span class="text-xs mt-1 block" style="color:var(--text-3)">${time}</span>
          </div>
        </div>`;
      }).join('')}`;
  }

  const _CT_LABELS = { commits:'Commits', pull_requests:'Pull Requests', issues:'Issues', reviews:'Reviews' };
  const _CT_IDS    = { commits:'ct-commits', pull_requests:'ct-prs', issues:'ct-issues', reviews:'ct-reviews' };

  function setChartType(type) {
    if (!_ghChartData) return;
    _currentType = type;
    Object.entries(_CT_IDS).forEach(([t, id]) => {
      document.getElementById(id)?.classList.toggle('active', t === type);
    });
    document.getElementById('chart-title').textContent = _CT_LABELS[type];
    _renderGhChart(_ghChartData.labels, _ghChartData.datasets[type] || [], (_chartRepos || {})[type] || {});
    renderPanelFeed();
  }

  function _renderGhChart(labels, data, repos) {
    const cc = chartColors();
    const ctx = document.getElementById('main-chart').getContext('2d');
    if (chart) chart.destroy();
    chart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          data,
          borderColor: '#f97316',
          backgroundColor: 'rgba(249,115,22,0.1)',
          borderWidth: 2,
          tension: 0.4,
          fill: true,
          pointBackgroundColor: '#f97316',
          pointRadius: 3,
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
                const lines = [`${_CT_LABELS[_currentType]}: ${item.raw}`];
                const dayRepos = (repos || {})[item.label] || {};
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

  window.onWSEvent = function(data) {
    if (data.source !== 'github') return;
    loadStats();
    loadPanelEvents();
    loadEvents();
  };

  async function onBaseReady(meData) {
    if (!meData.integrations.github) {
      document.getElementById('not-connected').classList.remove('hidden');
      document.getElementById('connect-btn').href = meData.connect_urls.github;
    } else {
      document.getElementById('main-content').classList.remove('hidden');
      initPicker();
      setPeriod('week', false);
      setEvtPeriod('today', false);
      await Promise.all([setRangeDefault(), loadEvents(), loadPanelEvents()]);
    }
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
    let url = `/api/github/stats?period=${statsPeriod}`;
    if (_chartStartDate) url += `&start_date=${_chartStartDate}`;
    const res = await fetch(url, { credentials: 'include' });
    const data = await res.json();

    const grid = document.getElementById('metric-cards');
    grid.innerHTML = (data.metrics || []).map(m => {
      const changeHtml = m.change !== undefined
        ? `<p class="text-xs mt-1 ${m.change >= 0 ? 'text-green-400' : 'text-red-400'}">${m.change >= 0 ? '↑' : '↓'} ${Math.abs(m.change)}% vs last week</p>`
        : '';
      return `<div class="card kpi-tilt-card" style="overflow:hidden;padding:0">
        <div style="height:2px;background:linear-gradient(90deg,#f97316,#fb923c)"></div>
        <div class="p-4">
          <p class="kpi-label mb-2">${m.label}</p>
          <p class="kpi-number kpi-num-github">${m.value}</p>
          ${changeHtml}
        </div>
      </div>`;
    }).join('');
    initKpiTilt();

    _ghChartData = data.chart;
    _chartRepos  = data.chart.repos || null;
    setChartType('commits');

    document.getElementById('top-label').textContent = data.top_label || 'Top Repositories';
    const topEl = document.getElementById('top-items');
    if (!data.top_items?.length) {
      topEl.innerHTML = '<p class="text-gray-500 text-xs">No data yet.</p>';
    } else {
      const max = data.top_items[0].count;
      topEl.innerHTML = data.top_items.map(item => `
        <div>
          <div class="flex justify-between text-xs mb-1">
            <span class="text-gray-300 truncate">${esc(item.name)}</span>
            <span class="text-gray-500 ml-2 shrink-0">${item.count}</span>
          </div>
          <div class="h-1 bg-gray-800 rounded-full">
            <div class="h-1 rounded-full" style="width:${Math.round(item.count/max*100)}%;background:#f97316"></div>
          </div>
        </div>`).join('');
    }

  }

  const _ghFmt = et => (et||'unknown').replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase());
  const _prLabel = et => (et||'').replace(/^pr_/i,'').replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase()) || 'PR';

  function _ghCommitExtra(it) {
    const shaHtml = it.sha
      ? `<code style="font-size:10px;font-family:monospace;background:var(--surface-2);color:var(--text-3);padding:1px 5px;border-radius:4px;margin-left:4px">${it.sha}</code>`
      : '';
    const filesHtml = it.files?.length
      ? `<div class="flex flex-wrap gap-1 mt-1">${it.files.slice(0,4).map(f=>`<span style="font-size:10px;font-family:monospace;background:var(--surface-2);color:var(--text-3);padding:1px 5px;border-radius:4px">${f.split('/').pop()}</span>`).join('')}${it.files.length>4?`<span style="font-size:10px;color:var(--text-3)"> +${it.files.length-4} more</span>`:''}</div>`
      : '';
    return { shaHtml, filesHtml };
  }

  function renderConnFeed(events) {
    events = escEvents(events);
    const initials = window._actUserInitials || '??';
    const author   = window._actAuthorName   || 'you';
    if (evtPeriod === 'history') {
      return `<div class="commit-feed">${events.map(it => {
        const color = repoColor(it.workspace||'');
        const time  = new Date(it.occurred_at).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
        const {shaHtml, filesHtml} = _ghCommitExtra(it);
        return `<div class="commit-item">
          <div class="commit-avatar" style="background:${color}">${initials}</div>
          <div class="commit-body">
            <div class="commit-meta">
              <span class="commit-author">${author}</span>
              ${it.workspace?`<span class="commit-repo-pill">${ICON_BRANCH} ${it.workspace}</span>`:''}
              <span class="commit-time">${ICON_CLOCK} ${time}</span>
            </div>
            <div class="commit-title">${it.title||'—'}${shaHtml}</div>
            <div class="commit-sub">${_ghFmt(it.event_type)}</div>
            ${filesHtml}
          </div>
        </div>`;
      }).join('')}</div>`;
    }
    const groups = {};
    events.forEach(it => { const k=it.event_type||'other'; (groups[k]=groups[k]||[]).push(it); });
    return Object.entries(groups).map(([et,evts]) => `
      <div class="mb-3">
        <div class="flex items-center gap-2 mb-1 pl-2" style="border-left:2px solid #f97316">
          <span class="text-xs font-semibold" style="color:var(--text-2)">${_ghFmt(et)}</span>
          <span class="text-xs px-1.5 py-0.5 rounded-full" style="background:var(--border-strong);color:var(--text-3)">${evts.length}</span>
        </div>
        <div class="commit-feed">${evts.map(it => {
          const color = repoColor(it.workspace||'');
          const time  = new Date(it.occurred_at).toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'});
          const {shaHtml, filesHtml} = _ghCommitExtra(it);
          return `<div class="commit-item">
            <div class="commit-avatar" style="background:${color}">${initials}</div>
            <div class="commit-body">
              <div class="commit-meta">
                <span class="commit-author">${author}</span>
                ${it.workspace?`<span class="commit-repo-pill">${ICON_BRANCH} ${it.workspace}</span>`:''}
                <span class="commit-time">${ICON_CLOCK} ${time}</span>
              </div>
              <div class="commit-title">${it.title||'—'}${shaHtml}</div>
              ${filesHtml}
            </div>
          </div>`;
        }).join('')}</div>
      </div>`).join('');
  }

  async function loadEvents() {
    const res  = await fetch('/api/events/recent?source=github&limit=50', { credentials: 'include' });
    const data = await res.json();
    const list = document.getElementById('events-list');
    let events = data.events || [];
    if (evtPeriod === 'today') {
      const today = new Date().toDateString();
      events = events.filter(e => new Date(e.occurred_at).toDateString() === today);
    }
    events.sort((a, b) => new Date(b.occurred_at) - new Date(a.occurred_at));
    if (!events.length) {
      list.innerHTML = `<p class="text-gray-500 text-sm">${evtPeriod==='today'?'No GitHub activity today.':'No recent GitHub activity.'}</p>`;
      return;
    }
    list.innerHTML = renderConnFeed(events);
  }
