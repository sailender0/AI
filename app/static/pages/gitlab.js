  let chart           = null;
  let statsPeriod     = 'week';
  let evtPeriod       = 'today';
  let _glChartData    = null;
  let _chartRepos     = null;
  let _currentType    = 'commits';
  let _panelEvents    = [];
  let _panelTab       = 'activity';

  const _ET_FILTER = {
    commits:        e => /^commit/i.test(e.event_type),
    merge_requests: e => /^merge_request/i.test(e.event_type),
    issues:         e => /^issue/i.test(e.event_type),
    notes:          e => /^note/i.test(e.event_type),
    pipelines:      e => /^pipeline/i.test(e.event_type),
  };

  const _ET_COLOR = {
    commit:         '#10b981',
    merge_request:  '#6366f1',
    issue:          '#f59e0b',
    note:           '#06b6d4',
    pipeline:       '#8b5cf6',
    tag_push:       '#ec4899',
  };
  const _ET_ICON = {
    commit:        '⬡',
    merge_request: '⇄',
    issue:         '◎',
    note:          '💬',
    pipeline:      '⚙',
    tag_push:      '🏷',
  };
  function _etColor(et) { return _ET_COLOR[et] || '#64748b'; }
  function _etIcon(et)  { return _ET_ICON[et]  || '·'; }

  async function loadPanelEvents() {
    if (!_chartStartDate) return;
    const s = new Date(_chartStartDate + 'T00:00:00');
    const e = new Date(s); e.setDate(s.getDate() + 7);
    const endIso = `${e.getFullYear()}-${String(e.getMonth()+1).padStart(2,'0')}-${String(e.getDate()).padStart(2,'0')}`;
    const data = await getJSON(`/api/events/recent?source=gitlab&start_date=${_chartStartDate}&end_date=${endIso}&limit=200`);
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

  const _CT_LABELS = { commits:'Commits', merge_requests:'Merge Requests', issues:'Issues', notes:'Comments', pipelines:'Pipelines' };
  const _CT_IDS    = { commits:'ct-commits', merge_requests:'ct-mrs', issues:'ct-issues', notes:'ct-notes', pipelines:'ct-pipelines' };

  function setChartType(type) {
    if (!_glChartData) return;
    _currentType = type;
    Object.entries(_CT_IDS).forEach(([t, id]) => {
      document.getElementById(id)?.classList.toggle('active', t === type);
    });
    document.getElementById('chart-title').textContent = _CT_LABELS[type];
    _renderGlChart(_glChartData.labels, _glChartData.datasets[type] || [], (_chartRepos || {})[type] || {});
    renderPanelFeed();
  }

  function _renderGlChart(labels, data, repos) {
    const cc = chartColors();
    const ctx = document.getElementById('main-chart').getContext('2d');
    if (chart) chart.destroy();
    chart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          data,
          borderColor: '#10b981',
          backgroundColor: 'rgba(16,185,129,0.1)',
          borderWidth: 2,
          tension: 0.4,
          fill: true,
          pointBackgroundColor: '#10b981',
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
    if (data.source !== 'gitlab') return;
    loadStats();
    loadPanelEvents();
    loadEvents();
  };

  async function onBaseReady(meData) {
    const urlError = new URLSearchParams(window.location.search).get('error');
    if (urlError) {
      const el = document.getElementById('auth-error');
      el.classList.remove('hidden');
      document.getElementById('auth-error-detail').textContent = `Error: ${urlError}`;
      document.getElementById('auth-error-retry').href = meData.connect_urls.gitlab;
    }
    if (!meData.integrations.gitlab) {
      document.getElementById('not-connected').classList.remove('hidden');
      document.getElementById('connect-btn').href = meData.connect_urls.gitlab;
    } else {
      if (meData.integration_errors?.gitlab) {
        document.getElementById('webhook-error').classList.remove('hidden');
      }
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
    let url = `/api/gitlab/stats?period=${statsPeriod}`;
    if (_chartStartDate) url += `&start_date=${_chartStartDate}`;
    const res = await fetch(url, { credentials: 'include' });
    const data = await res.json();

    const grid = document.getElementById('metric-cards');
    grid.innerHTML = (data.metrics || []).map(m => {
      const changeHtml = m.change !== undefined
        ? `<p class="text-xs mt-1 ${m.change >= 0 ? 'text-green-400' : 'text-red-400'}">${m.change >= 0 ? '↑' : '↓'} ${Math.abs(m.change)}% vs last week</p>`
        : '';
      return `<div class="card kpi-tilt-card" style="overflow:hidden;padding:0">
        <div style="height:2px;background:linear-gradient(90deg,#10b981,#34d399)"></div>
        <div class="p-4">
          <p class="kpi-label mb-2">${m.label}</p>
          <p class="kpi-number kpi-num-gitlab">${m.value}</p>
          ${changeHtml}
        </div>
      </div>`;
    }).join('');
    initKpiTilt();

    _glChartData = data.chart;
    _chartRepos  = data.chart.repos || null;
    setChartType('commits');

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
            <div class="h-1 rounded-full" style="width:${Math.round(item.count/max*100)}%;background:#10b981"></div>
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
        const color = _etColor(it.event_type);
        const time  = new Date(it.occurred_at).toLocaleString(undefined, {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'});
        const filesHtml = it.files?.length
          ? `<div class="flex flex-wrap gap-1 mt-1">${it.files.slice(0,4).map(f=>`<span style="font-size:10px;font-family:monospace;background:var(--surface-2);color:var(--text-3);padding:1px 5px;border-radius:4px">${f.split('/').pop()}</span>`).join('')}${it.files.length>4?`<span style="font-size:10px;color:var(--text-3)">+${it.files.length-4}</span>`:''}</div>` : '';
        return `<div class="flex items-start gap-2 py-2" style="border-bottom:1px solid var(--border)">
          <div class="w-2 h-2 rounded-full mt-1.5 shrink-0" style="background:${color}"></div>
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-1.5 mb-0.5">
              <span class="text-xs font-semibold truncate" style="color:var(--text-1)">${it.workspace||'—'}</span>
              ${it.sha?`<code style="font-size:10px;font-family:monospace;background:var(--surface-2);color:var(--text-3);padding:1px 5px;border-radius:4px">${it.sha}</code>`:''}
            </div>
            <span class="text-xs truncate block mb-0.5" style="color:var(--text-2)">${it.title||'—'}</span>
            <div class="flex items-center gap-2">${_evtBadge(it.event_type)}</div>
            ${filesHtml}
            <span class="text-xs mt-1 block" style="color:var(--text-3)">${time}</span>
          </div>
        </div>`;
      }).join('')}`;
  }

  const _glFmt = et => (et||'unknown').replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase());

  function _glCommitExtra(it) {
    const shaHtml = it.sha
      ? `<code style="font-size:10px;font-family:monospace;background:var(--surface-2);color:var(--text-3);padding:1px 5px;border-radius:4px;margin-left:4px">${it.sha}</code>`
      : '';
    const filesHtml = it.files?.length
      ? `<div class="flex flex-wrap gap-1 mt-1">${it.files.slice(0,4).map(f=>`<span style="font-size:10px;font-family:monospace;background:var(--surface-2);color:var(--text-3);padding:1px 5px;border-radius:4px">${f.split('/').pop()}</span>`).join('')}${it.files.length>4?`<span style="font-size:10px;color:var(--text-3)"> +${it.files.length-4} more</span>`:''}</div>`
      : '';
    return { shaHtml, filesHtml };
  }

  function _evtBadge(et) {
    const color = _etColor(et);
    const icon  = _etIcon(et);
    return `<span style="font-size:10px;padding:1px 6px;border-radius:4px;font-weight:600;background:${color}22;color:${color}">${icon} ${_glFmt(et)}</span>`;
  }

  function renderConnFeed(events) {
    events = escEvents(events);
    const initials = window._actUserInitials || '??';
    const author   = window._actAuthorName   || 'you';
    if (evtPeriod === 'history') {
      return `<div class="commit-feed">${events.map(it => {
        const color = _etColor(it.event_type);
        const time  = new Date(it.occurred_at).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
        const {shaHtml, filesHtml} = _glCommitExtra(it);
        return `<div class="commit-item">
          <div class="commit-avatar" style="background:${color}">${initials}</div>
          <div class="commit-body">
            <div class="commit-meta">
              <span class="commit-author">${author}</span>
              ${it.workspace?`<span class="commit-repo-pill">${ICON_BRANCH} ${it.workspace}</span>`:''}
              <span class="commit-time">${ICON_CLOCK} ${time}</span>
            </div>
            <div class="commit-title">${it.title||'—'}${shaHtml}</div>
            <div class="commit-sub">${_evtBadge(it.event_type)}</div>
            ${filesHtml}
          </div>
        </div>`;
      }).join('')}</div>`;
    }
    const groups = {};
    events.forEach(it => { const k=it.event_type||'other'; (groups[k]=groups[k]||[]).push(it); });
    return Object.entries(groups).map(([et,evts]) => {
      const etColor = _etColor(et);
      return `
      <div class="mb-3">
        <div class="flex items-center gap-2 mb-1 pl-2" style="border-left:2px solid ${etColor}">
          <span class="text-xs font-semibold" style="color:${etColor}">${_etIcon(et)} ${_glFmt(et)}</span>
          <span class="text-xs px-1.5 py-0.5 rounded-full" style="background:var(--border-strong);color:var(--text-3)">${evts.length}</span>
        </div>
        <div class="commit-feed">${evts.map(it => {
          const time  = new Date(it.occurred_at).toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'});
          const {shaHtml, filesHtml} = _glCommitExtra(it);
          return `<div class="commit-item">
            <div class="commit-avatar" style="background:${etColor}">${initials}</div>
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
      </div>`;
    }).join('');
  }

  async function disconnectGitlab() {
    if (!confirm('Disconnect GitLab? This will remove your integration and stop tracking activity.')) return;
    const res = await fetch('/api/disconnect/gitlab', { method: 'POST', credentials: 'include' });
    if (res.ok) window.location.reload();
    else alert('Failed to disconnect — check console.');
  }

  async function reregisterWebhooks() {
    const btn = document.getElementById('rereg-btn');
    btn.textContent = 'Syncing…'; btn.disabled = true;
    try {
      const res  = await fetch('/api/gitlab/reregister', { method: 'POST', credentials: 'include' });
      const data = await res.json();
      if (data.registered !== undefined) {
        btn.textContent = `✓ ${data.registered}/${data.total} registered`;
      } else {
        btn.textContent = data.error || 'Failed';
      }
    } catch { btn.textContent = 'Error'; }
    setTimeout(() => { btn.textContent = 'Sync Webhooks'; btn.disabled = false; }, 3000);
  }

  async function loadEvents() {
    const res  = await fetch('/api/events/recent?source=gitlab&limit=50', { credentials: 'include' });
    const data = await res.json();
    const list = document.getElementById('events-list');
    let events = data.events || [];
    if (evtPeriod === 'today') {
      const today = new Date().toDateString();
      events = events.filter(e => new Date(e.occurred_at).toDateString() === today);
    }
    events.sort((a, b) => new Date(b.occurred_at) - new Date(a.occurred_at));
    if (!events.length) {
      list.innerHTML = `<p class="text-gray-500 text-sm">${evtPeriod==='today'?'No GitLab activity today.':'No recent GitLab activity.'}</p>`;
      return;
    }
    document.getElementById('not-connected')?.remove();
    list.innerHTML = renderConnFeed(events);
  }
