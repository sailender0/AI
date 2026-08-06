  let activityChart    = null;
  let chartRawLabels   = [];
  let _chartEventTypes = null;
  let _chartStartDate  = null;
  let _picker          = null;
  let statsPeriod      = 'week';
  let timelinePeriod   = 'today';

  window.onWSEvent = function(data) {
    loadStats();
    loadChartForWeek(_chartStartDate);
    loadTimeline();
    if (data?.source === 'jira') loadAssigned();
  };

  async function onBaseReady(data) {
    document.getElementById('main-content').classList.remove('hidden');

    document.getElementById('chart-legend').innerHTML = SOURCE_ORDER.map(k => `
      <div class="flex items-center gap-1.5">
        <span class="w-2 h-2 rounded-sm" style="background:${SOURCES[k].color}"></span>
        <span class="text-xs" style="color:var(--text-2)">${SOURCES[k].label}</span>
      </div>`).join('');

    _picker = weekPicker({ mount: '#week-picker', button: '#range-week', onSelect: selectWeek });

    setStatsPeriod('week', false);
    setTimelinePeriod('today', false);
    const jobs = [loadStats(), setRangeDefault(), loadTimeline()];
    if (data?.integrations?.jira) {
      document.getElementById('jira-assigned-card').classList.remove('hidden');
      jobs.push(loadAssigned());
    }
    await Promise.all(jobs);
  }

  function setRangeDefault() {
    const cw = currentWeek();
    _chartStartDate = cw.start;
    _picker?.setSelected(cw.start);
    _picker?.close();
    document.getElementById('range-default').classList.add('active');
    const rw = document.getElementById('range-week');
    rw.classList.remove('active');
    rw.textContent = 'Pick Week ▾';
    document.getElementById('chart-sub').textContent = `· ${cw.label}`;
    return loadChartForWeek(_chartStartDate);
  }

  function selectWeek(mondayIso, label) {
    _chartStartDate = mondayIso;
    document.getElementById('range-default').classList.remove('active');
    const rw = document.getElementById('range-week');
    rw.classList.add('active');
    rw.textContent = label + ' ▾';
    document.getElementById('chart-sub').textContent = `· ${label}`;
    loadChartForWeek(mondayIso);
  }

  async function loadChartForWeek(startDate) {
    if (!startDate) return;
    const res  = await fetch(`/api/analytics/trend?days=7&group_by=day&start_date=${startDate}`, { credentials: 'include' });
    const data = await res.json();
    chartRawLabels   = data.raw_labels  || [];
    _chartEventTypes = data.event_types || null;
    _renderActivityChart(data);
  }

  function setTimelinePeriod(period, fetch = true) {
    timelinePeriod = period;
    document.getElementById('tl-today').classList.toggle('active',   period === 'today');
    document.getElementById('tl-history').classList.toggle('active', period === 'history');
    if (fetch) loadTimeline();
  }

  function setStatsPeriod(period, fetch = true) {
    statsPeriod = period;
    document.getElementById('period-today').classList.toggle('active', period === 'today');
    document.getElementById('period-week').classList.toggle('active',  period === 'week');
    if (fetch) loadStats();
  }

  document.addEventListener('DOMContentLoaded', () => {
    fetch('/api/me', { credentials: 'include' }).then(r => r.json()).then(d => {
      if (!d.authenticated) document.getElementById('not-logged-in').classList.remove('hidden');
    });
  });

  function _kpiSrc(label) {
    const l = (label || '').toLowerCase();
    if (l.includes('jira')   || l.includes('issue')   || l.includes('sprint')) return SOURCES.jira;
    if (l.includes('meet')   || l.includes('message') || l.includes('team'))   return SOURCES.teams_subscription;
    if (l.includes('gitlab') || l.includes('merge'))                           return SOURCES.gitlab;
    return SOURCES.github;
  }

  async function loadStats() {
    const res  = await fetch(`/api/stats?period=${statsPeriod}`, { credentials: 'include' });
    const data = await res.json();
    if (!data.metrics) return;

    const grid = document.getElementById('metric-cards');
    grid.innerHTML = '';
    data.metrics.forEach(m => {
      const src = _kpiSrc(m.label);
      grid.innerHTML += `
        <div class="card kpi-tilt-card" style="overflow:hidden;padding:0">
          <div style="height:2px;background:linear-gradient(90deg,${src.grad})"></div>
          <div class="p-5">
            <p class="kpi-label mb-3">${m.label}</p>
            <p class="kpi-number ${src.cls}">${m.value}</p>
          </div>
        </div>`;
    });
    initKpiTilt();
  }

  function _renderActivityChart(data) {
    const ctx = document.getElementById('activity-chart').getContext('2d');
    if (activityChart) activityChart.destroy();

    activityChart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: data.labels,
        datasets: SOURCE_ORDER.map(k => ({
          data: data.sources[k] || [],
          backgroundColor:      SOURCES[k].color,
          hoverBackgroundColor: SOURCES[k].hover,
          borderWidth: 0,
          borderRadius: 3,
          borderSkipped: true,
          barPercentage: 0.55,
          categoryPercentage: 0.78,
          maxBarThickness: 44,
        })),
      },
      options: chartBase({
        stacked: true,
        onBarClick: (i) => {
          const date = chartRawLabels[i];
          if (date) window.location.href = `/my-day?date=${date}`;
        },
        tooltip: {
          filter: item => item.raw > 0,
          callbacks: {
            title: items => items[0].label,
            label: item => {
              const src   = SOURCE_ORDER[item.datasetIndex];
              const lines = [` ${SOURCES[src].label}: ${item.raw}`];
              if (_chartEventTypes) {
                const day       = chartRawLabels[item.dataIndex];
                const breakdown = (_chartEventTypes[src] || {})[day] || {};
                Object.entries(breakdown).sort((a, b) => b[1] - a[1])
                  .forEach(([et, cnt]) => lines.push(`   · ${etLabel(et)}: ${cnt}`));
              }
              return lines;
            },
            footer: () => ['↓ Click to open My Day'],
          },
        },
      }),
    });
  }

  function renderTimelineFeed(events) {
    events = escEvents(events);
    const initials = window._actUserInitials || '??';
    const author   = window._actAuthorName   || 'you';

    const bySource = events.reduce((acc, e) => {
      (acc[e.source] = acc[e.source] || []).push(e);
      return acc;
    }, {});
    const sources = [...SOURCE_ORDER.filter(s => bySource[s]),
                     ...Object.keys(bySource).filter(s => !SOURCE_ORDER.includes(s))];

    return sources.map(src => {
      const meta  = srcOf(src);
      const items = bySource[src];
      const groups = {};
      items.forEach(it => { const k = it.event_type || 'other'; (groups[k] = groups[k] || []).push(it); });

      const groupHtml = Object.entries(groups).map(([et, evts]) => `
        <div class="mb-3">
          <div class="flex items-center gap-2 mb-1">
            <span class="text-xs font-semibold" style="color:var(--text-2)">${etLabel(et)}</span>
            <span class="text-xs px-1.5 py-0.5 rounded-full" style="background:var(--border-strong);color:var(--text-3)">${evts.length}</span>
          </div>
          <div>${evts.map(it => commitRow(it, { initials, author, when: fmtTime(it.occurred_at) })).join('')}</div>
        </div>`).join('');

      return `<div class="mb-4">
        <div class="flex items-center gap-2 mb-2 pl-2.5" style="border-left:2px solid ${meta.color}">
          <span class="text-xs font-semibold" style="color:${meta.color}">${meta.label}</span>
          <span class="text-xs ml-auto" style="color:var(--text-3)">${items.length} event${items.length !== 1 ? 's' : ''}</span>
        </div>
        <div class="pl-3" style="border-left:1px solid var(--border)">${groupHtml}</div>
      </div>`;
    }).join('');
  }

  function renderHistoryFeed(events) {
    events = escEvents(events);
    const initials = window._actUserInitials || '??';
    const author   = window._actAuthorName   || 'you';
    return events.map(it => {
      const meta  = srcOf(it.source);
      const badge = `<span class="src-badge" style="background:${meta.color}22;color:${meta.color}">${meta.label}</span>`;
      return commitRow(it, { initials, author, when: fmtDateTime(it.occurred_at), badge, sub: etLabel(it.event_type) });
    }).join('');
  }

  async function loadTimeline() {
    const icon = document.getElementById('refresh-icon');
    if (icon) icon.style.animation = 'spin 0.8s linear infinite';
    const res  = await fetch('/api/events/recent?limit=50', { credentials: 'include' });
    const data = await res.json();
    const list = document.getElementById('timeline-list');
    if (icon) icon.style.animation = '';

    let events = data.events || [];
    if (timelinePeriod === 'today') {
      const today = new Date().toDateString();
      events = events.filter(e => new Date(e.occurred_at).toDateString() === today);
    }
    events.sort((a, b) => new Date(b.occurred_at) - new Date(a.occurred_at));

    if (!events.length) {
      list.innerHTML = `<p class="text-xs px-2 py-1" style="color:var(--text-3)">${
        timelinePeriod === 'today' ? 'No activity yet today.' : 'No recent activity.'
      }</p>`;
      return;
    }
    list.innerHTML = timelinePeriod === 'history'
      ? renderHistoryFeed(events)
      : renderTimelineFeed(events);
  }

  function standupCard() {
    return {
      expanded: false,
      loading: false,
      text: '',
      error: '',
      periodLabel: '',
      generatedAt: null,
      copied: false,

      get today() { return new Date().toISOString().slice(0, 10); },

      get stale() {
        if (!this.generatedAt) return false;
        const age = (Date.now() - new Date(this.generatedAt)) / 60000;
        return age > 120 && new Date().getHours() < 13;
      },

      get generatedAtLabel() {
        if (!this.generatedAt) return '';
        return new Date(this.generatedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      },

      _draftKey()     { return `standup_draft_${this.today}`; },
      _collapsedKey() { return `standup_collapsed_${this.today}`; },

      saveDraft() { localStorage.setItem(this._draftKey(), this.text); },

      init() {
        const hour = new Date().getHours();
        const collapsed = localStorage.getItem(this._collapsedKey()) === '1';
        this.expanded = hour < 11 && !collapsed;
        if (this.expanded) this.load();

        document.addEventListener('visibilitychange', () => {
          if (!document.hidden && !this.text && this.expanded) this.load();
        });
      },

      async load() {
        const draft = localStorage.getItem(this._draftKey());
        if (draft) this.text = draft;

        this.loading = true;
        this.error = '';
        try {
          const r = await fetch('/api/standup/today', { credentials: 'include' });
          const d = await r.json();
          if (d.error) { this.error = d.error; return; }
          if (!draft) this.text = d.standup;
          this.periodLabel = d.period || '';
          this.generatedAt = d.generated_at || null;
        } catch (e) {
          this.error = 'Failed to load standup.';
        } finally {
          this.loading = false;
        }
      },

      async regenerate() {
        this.loading = true;
        this.error = '';
        this.text = '';
        localStorage.removeItem(this._draftKey());
        try {
          const r = await fetch('/api/standup/regenerate', { method: 'POST', credentials: 'include' });
          const d = await r.json();
          if (d.error) { this.error = d.error; return; }
          this.text = d.standup;
          this.periodLabel = d.period || '';
          this.generatedAt = d.generated_at || null;
        } catch (e) {
          this.error = 'Failed to regenerate.';
        } finally {
          this.loading = false;
        }
      },

      async copy() {
        await navigator.clipboard.writeText(this.text);
        this.copied = true;
        setTimeout(() => this.copied = false, 2000);
      },

      expand() {
        this.expanded = true;
        localStorage.removeItem(this._collapsedKey());
        if (!this.text) this.load();
      },

      collapse() {
        this.expanded = false;
        localStorage.setItem(this._collapsedKey(), '1');
      },
    };
  }
