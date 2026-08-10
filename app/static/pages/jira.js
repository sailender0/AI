  let chart           = null;
  let evtPeriod       = 'today';
  let _jrChartData    = null;
  let _chartRepos     = null;
  let _currentType    = 'created';
  let _panelEvents    = [];
  let _panelTab       = 'activity';

  const _ET_FILTER = {
    created:  e => /issue_created/i.test(e.event_type),
    updated:  e => /issue_updated/i.test(e.event_type),
    comments: e => /comment/i.test(e.event_type),
  };


  async function loadPanelEvents() {
    if (!_chartStartDate) return;
    const s = new Date(_chartStartDate + 'T00:00:00');
    const e = new Date(s); e.setDate(s.getDate() + 7);
    const endIso = `${e.getFullYear()}-${String(e.getMonth()+1).padStart(2,'0')}-${String(e.getDate()).padStart(2,'0')}`;
    const data = await getJSON(`/api/events/recent?source=jira&start_date=${_chartStartDate}&end_date=${endIso}&limit=200`);
    _panelEvents = data?.events || [];
    renderPanelFeed();
  }
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
    document.getElementById('range-week-label').textContent = 'Pick Week';
    document.getElementById('chart-sub').textContent = `· ${cw.label}`;
    document.getElementById('range-default').classList.add('active');
    document.getElementById('range-week').classList.remove('active');
    loadPanelEvents();
    return loadStats();
  }

  const _CT_LABELS = { created:'Created', updated:'Updated', comments:'Comments' };
  const _CT_IDS    = { created:'ct-created', updated:'ct-updated', comments:'ct-comments' };

  function setChartType(type) {
    if (!_jrChartData) return;
    _currentType = type;
    Object.entries(_CT_IDS).forEach(([t, id]) => {
      document.getElementById(id)?.classList.toggle('active', t === type);
    });
    document.getElementById('chart-title').textContent = _CT_LABELS[type];
    _renderJrChart(_jrChartData.labels, _jrChartData.datasets[type] || [], (_chartRepos || {})[type] || {});
    renderPanelFeed();
  }

  function _renderJrChart(labels, data, repos) {
    const cc = chartColors();
    const ctx = document.getElementById('main-chart').getContext('2d');
    if (chart) chart.destroy();
    chart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          data,
          backgroundColor: 'rgba(6,182,212,0.6)',
          borderColor: '#06b6d4',
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
    if (data.source !== 'jira') return;
    loadStats();
    loadPanelEvents();
    loadEvents();
    loadAssigned();
  };

  async function onBaseReady(meData) {
    const urlError = new URLSearchParams(window.location.search).get('error');
    if (urlError) {
      const el = document.getElementById('auth-error');
      el.classList.remove('hidden');
      document.getElementById('auth-error-detail').textContent = `Error: ${urlError}`;
      document.getElementById('auth-error-retry').href = meData.connect_urls.jira;
    }
    if (!meData.integrations.jira) {
      document.getElementById('not-connected').classList.remove('hidden');
      document.getElementById('connect-btn').href = meData.connect_urls.jira;
    } else {
      if (meData.integration_errors?.jira) {
        document.getElementById('webhook-error').classList.remove('hidden');
      }
      document.getElementById('main-content').classList.remove('hidden');
      initPicker();
      setEvtPeriod('today', false);
      await loadAssigned();
      await Promise.all([setRangeDefault(), loadEvents(), loadPanelEvents()]);
    }
  }

  function setEvtPeriod(p, fetch = true) {
    evtPeriod = p;
    document.getElementById('evt-today').classList.toggle('active',   p === 'today');
    document.getElementById('evt-history').classList.toggle('active', p === 'history');
    if (fetch) loadEvents();
  }

  async function loadStats() {
    let url = '/api/jira/stats';
    if (_chartStartDate) url += `?start_date=${_chartStartDate}`;
    const res = await fetch(url, { credentials: 'include' });
    const data = await res.json();

    _jrChartData = data.chart;
    _chartRepos  = data.chart.repos || null;
    setChartType('created');

    document.getElementById('top-label').textContent = data.top_label || 'Top Projects';
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
            <div class="h-1 rounded-full" style="width:${Math.round(item.count/max*100)}%;background:#06b6d4"></div>
          </div>
        </div>`).join('');
    }

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
        return `<div class="flex items-start gap-2 py-2" style="border-bottom:1px solid var(--border)">
          <div class="w-2 h-2 rounded-full mt-1.5 shrink-0" style="background:${color}"></div>
          <div class="min-w-0 flex-1">
            <span class="text-xs font-semibold truncate block" style="color:var(--text-1)">${it.workspace||'—'}</span>
            <span class="text-xs truncate block mb-0.5" style="color:var(--text-2)">${it.issue_key ? _keyLink(it.issue_key) + ' ' : ''}${it.title||'—'}</span>
            <span class="text-xs mt-1 block" style="color:var(--text-3)">${time}</span>
          </div>
        </div>`;
      }).join('')}`;
  }

  const _jrFmt = et => (et||'unknown').replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase());

  function renderConnFeed(events) {
    events = escEvents(events);
    const initials = window._actUserInitials || '??';
    const author   = window._actAuthorName   || 'you';
    if (evtPeriod === 'history') {
      return `<div class="commit-feed">${events.map(it => {
        const color = repoColor(it.workspace||'');
        const time  = new Date(it.occurred_at).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
        return `<div class="commit-item">
          <div class="commit-avatar" style="background:${color}">${initials}</div>
          <div class="commit-body">
            <div class="commit-meta">
              <span class="commit-author">${author}</span>
              ${it.workspace?`<span class="commit-repo-pill">${ICON_BRANCH} ${it.workspace}</span>`:''}
              <span class="commit-time">${ICON_CLOCK} ${time}</span>
            </div>
            <div class="commit-title">${it.issue_key ? _keyLink(it.issue_key) + ' ' : ''}${it.title||'—'}</div>
            <div class="commit-sub flex items-center gap-1.5">${_jrFmt(it.event_type)} ${_chip(it.status, '#06b6d4')}${_prioChip(it.priority)}</div>
          </div>
        </div>`;
      }).join('')}</div>`;
    }
    const groups = {};
    events.forEach(it => { const k=it.event_type||'other'; (groups[k]=groups[k]||[]).push(it); });
    return Object.entries(groups).map(([et,evts]) => `
      <div class="mb-3">
        <div class="flex items-center gap-2 mb-1 pl-2" style="border-left:2px solid #06b6d4">
          <span class="text-xs font-semibold" style="color:var(--text-2)">${_jrFmt(et)}</span>
          <span class="text-xs px-1.5 py-0.5 rounded-full" style="background:var(--border-strong);color:var(--text-3)">${evts.length}</span>
        </div>
        <div class="commit-feed">${evts.map(it => {
          const color = repoColor(it.workspace||'');
          const time  = new Date(it.occurred_at).toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'});
          return `<div class="commit-item">
            <div class="commit-avatar" style="background:${color}">${initials}</div>
            <div class="commit-body">
              <div class="commit-meta">
                <span class="commit-author">${author}</span>
                ${it.workspace?`<span class="commit-repo-pill">${ICON_BRANCH} ${it.workspace}</span>`:''}
                <span class="commit-time">${ICON_CLOCK} ${time}</span>
              </div>
              <div class="commit-title">${it.issue_key ? _keyLink(it.issue_key) + ' ' : ''}${it.title||'—'}</div>
              ${(it.status || it.priority) ? `<div class="flex items-center gap-1.5 mt-0.5">${_chip(it.status, '#06b6d4')}${_prioChip(it.priority)}</div>` : ''}
            </div>
          </div>`;
        }).join('')}</div>
      </div>`).join('');
  }

  async function loadEvents() {
    const res  = await fetch('/api/events/recent?source=jira&limit=50', { credentials: 'include' });
    const data = await res.json();
    const list = document.getElementById('events-list');
    let events = data.events || [];
    if (evtPeriod === 'today') {
      const today = new Date().toDateString();
      events = events.filter(e => new Date(e.occurred_at).toDateString() === today);
    }
    events.sort((a, b) => new Date(b.occurred_at) - new Date(a.occurred_at));
    if (!events.length) {
      list.innerHTML = `<p class="text-gray-500 text-sm">${evtPeriod==='today'?'No Jira activity today.':'No recent Jira activity.'}</p>`;
      return;
    }
    list.innerHTML = renderConnFeed(events);
  }
