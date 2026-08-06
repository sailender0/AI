  async function onBaseReady(data) {
    await loadSummaries();
  }

  async function loadSummaries() {
    Object.keys(_breakdownCache).forEach(k => delete _breakdownCache[k]);
    Object.keys(_breakdownView).forEach(k  => delete _breakdownView[k]);
    Object.keys(_breakdownConn).forEach(k  => delete _breakdownConn[k]);

    const res      = await fetch('/api/summaries?limit=52', { credentials: 'include' });
    const data     = await res.json();
    const weeklies = (data.summaries || []).filter(s => s.period_type === 'weekly');
    const container = document.getElementById('summaries-list');

    if (!weeklies.length) {
      container.innerHTML = `
        <div class="bg-gray-900 border border-gray-800 rounded-xl p-8 text-center">
          <p class="text-gray-400 text-sm mb-1">No weekly summaries yet.</p>
          <p class="text-gray-500 text-xs">Click "Generate This Week" above to create your first one.</p>
        </div>`;
      return;
    }

    const statsCache = {};

    function fmtUtcDate(isoStr, opts) {
      if (!isoStr) return '';
      const [y, m, d] = isoStr.slice(0, 10).split('-').map(Number);
      return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString(undefined, { timeZone: 'UTC', ...opts });
    }

    container.innerHTML = weeklies.map((s, i) => {
      const startStr = s.period_start ? s.period_start.slice(0, 10) : '';
      const endStr   = s.period_end   ? s.period_end.slice(0, 10)   : '';
      const startLbl = fmtUtcDate(s.period_start, { month: 'short', day: 'numeric' });
      const endLbl   = fmtUtcDate(s.period_end,   { month: 'short', day: 'numeric', year: 'numeric' });
      return `
        <div class="week-row ${i === 0 ? 'open' : ''}" id="week-${i}"
          data-start="${startStr}" data-end="${endStr}" data-id="${s.id}">
          <div class="week-header" onclick="toggleWeek(${i})">
            <div class="flex items-center gap-3">
              <span class="text-xs font-medium px-2 py-0.5 rounded"
                style="background:var(--border-strong);color:var(--text-2)">WEEK</span>
              <span class="text-sm font-semibold" style="color:var(--text-1)">${startLbl} — ${endLbl}</span>
            </div>
            <div class="flex items-center gap-2">
              ${hasPerm('export_analytics') ? `
              <button onclick="event.stopPropagation();downloadWeeklyPdf('${startStr}')"
                class="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium transition"
                style="background:var(--surface-2);color:var(--text-2);border:1px solid var(--border)">
                <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
                </svg>
                Download
              </button>` : ''}

              <svg class="week-chevron w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7"/>
              </svg>
            </div>
          </div>
          <div class="week-body">
            <p class="text-sm leading-relaxed whitespace-pre-wrap" style="color:var(--text-1)">${s.content || '—'}</p>
            <div id="week-stats-${i}" class="int-grid"></div>

            <!-- Activity breakdown -->
            <div class="mt-5">
              <div class="flex items-center justify-between mb-3">
                <span class="text-xs font-semibold uppercase tracking-wider" style="color:var(--text-3)">Activity Breakdown</span>
                <div class="flex rounded-lg p-0.5 gap-0.5" style="background:var(--surface-2);border:1px solid var(--border)">
                  <button id="view-day-${i}"  onclick="setBreakdownView(${i},'day')"       class="bv-btn bv-active px-3 py-1 rounded-md text-xs font-medium">By Day</button>
                  <button id="view-conn-${i}" onclick="setBreakdownView(${i},'connector')" class="bv-btn px-3 py-1 rounded-md text-xs font-medium">By Connector</button>
                </div>
              </div>
              <div id="conn-selector-${i}" style="display:none" class="flex gap-2 flex-wrap mb-3">
                <button id="conn-btn-${i}-github"             onclick="setConnector(${i},'github')"             class="conn-btn">GitHub</button>
                <button id="conn-btn-${i}-jira"               onclick="setConnector(${i},'jira')"               class="conn-btn">Jira</button>
                <button id="conn-btn-${i}-teams_subscription" onclick="setConnector(${i},'teams_subscription')" class="conn-btn">Teams</button>
                <button id="conn-btn-${i}-gitlab"             onclick="setConnector(${i},'gitlab')"             class="conn-btn">GitLab</button>
              </div>
              <div id="week-breakdown-${i}">
                <p class="text-xs" style="color:var(--text-3)">Loading…</p>
              </div>
            </div>
          </div>
        </div>`;
    }).join('');

    if (weeklies.length) {
      loadWeekStats(0, statsCache);
      loadWeekBreakdown(0);
    }

    window._weekStatsCache = statsCache;
  }

  async function loadWeekStats(i, cache) {
    if (cache[i]) return;
    const row   = document.getElementById('week-' + i);
    const start = row?.dataset.start;
    const end   = row?.dataset.end;
    if (!start || !end) return;

    const el = document.getElementById('week-stats-' + i);
    if (!el) return;

    cache[i] = true;
    const res  = await fetch(`/api/week-stats?start=${start}&end=${end}`, { credentials: 'include' });
    const data = await res.json();

    const INTEGRATIONS = [
      {
        key: 'github', label: 'GitHub',
        stats: d => [
          { label: 'Commits',       val: d.commits },
          { label: 'Pull Requests', val: d.pull_requests },
          { label: 'Issues',        val: d.issues },
        ],
      },
      {
        key: 'jira', label: 'Jira',
        stats: d => [
          { label: 'Created',  val: d.created },
          { label: 'Updated',  val: d.updated },
          { label: 'Comments', val: d.comments },
        ],
      },
      {
        key: 'teams', label: 'Teams',
        stats: d => [
          { label: 'Messages', val: d.messages },
        ],
      },
      {
        key: 'gitlab', label: 'GitLab',
        stats: d => [
          { label: 'Commits',        val: d.commits },
          { label: 'Merge Requests', val: d.merge_requests },
          { label: 'Issues',         val: d.issues },
        ],
      },
    ];

    const cards = INTEGRATIONS
      .filter(intg => {
        const d = data[intg.key];
        return d && Object.values(d).some(v => v > 0);
      })
      .map(intg => {
        const src  = srcOf(intg.key);
        const rows = intg.stats(data[intg.key])
          .map(r => `<div class="int-stat"><span>${r.label}</span><span class="int-stat-val ${src.cls}">${r.val}</span></div>`)
          .join('');
        return `<div class="int-card kpi-tilt-card" style="overflow:hidden;padding:0">
          <div style="height:2px;background:linear-gradient(90deg,${src.grad})"></div>
          <div style="padding:10px 14px">
            <div class="int-card-title">${intg.label}</div>${rows}
          </div>
        </div>`;
      })
      .join('');
    setTimeout(initKpiTilt, 0);

    el.innerHTML = cards || '<p style="color:var(--text-3);font-size:12px;margin-top:8px">No integration data for this week.</p>';
  }

  function toggleWeek(i) {
    const row = document.getElementById('week-' + i);
    row.classList.toggle('open');
    if (row.classList.contains('open')) {
      loadWeekStats(i, window._weekStatsCache || {});
      loadWeekBreakdown(i);
    }
  }

  const _breakdownCache = {};
  const _breakdownView  = {};
  const _breakdownConn  = {};


  function fmtDayLabel(dateStr) {
    const [y,m,d] = dateStr.split('-').map(Number);
    return new Date(Date.UTC(y,m-1,d)).toLocaleDateString(undefined, {
      timeZone:'UTC', weekday:'long', month:'short', day:'numeric'
    });
  }

  function fmtEventType(et) {
    return (et || 'unknown').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  }


  const _CF_BRANCH = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>`;
  const _CF_CLOCK  = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>`;

  function _cfCommitExtra(it) {
    const shaHtml = it.sha
      ? `<code style="font-size:10px;font-family:monospace;background:var(--surface-2);color:var(--text-3);padding:1px 5px;border-radius:4px;margin-left:4px">${it.sha}</code>`
      : '';
    const filesHtml = it.files?.length
      ? `<div class="flex flex-wrap gap-1 mt-1">${it.files.slice(0,4).map(f=>`<span style="font-size:10px;font-family:monospace;background:var(--surface-2);color:var(--text-3);padding:1px 5px;border-radius:4px">${f.split('/').pop()}</span>`).join('')}${it.files.length>4?`<span style="font-size:10px;color:var(--text-3)"> +${it.files.length-4} more</span>`:''}</div>`
      : '';
    return { shaHtml, filesHtml };
  }

  function renderItems(items, src) {
    if (!items || !items.length) return '';
    items = escEvents(items);
    const initials = window._actUserInitials || '??';
    const author   = window._actAuthorName   || 'you';
    const groups   = {};
    items.forEach(it => { const k = it.event_type || 'other'; (groups[k] = groups[k] || []).push(it); });
    return Object.entries(groups).map(([et, evts]) => `
      <div class="mb-3">
        <div class="flex items-center gap-2 mb-1">
          <span class="text-xs font-semibold" style="color:var(--text-2)">${fmtEventType(et)}</span>
          <span class="text-xs px-1.5 py-0.5 rounded-full" style="background:var(--border-strong);color:var(--text-3)">${evts.length}</span>
        </div>
        <div class="commit-feed">${evts.map(it => {
          const color = repoColor(it.workspace || src || '');
          const time  = fmtTime(it.occurred_at);
          const {shaHtml, filesHtml} = _cfCommitExtra(it);
          return `<div class="commit-item">
            <div class="commit-avatar" style="background:${color}">${initials}</div>
            <div class="commit-body">
              <div class="commit-meta">
                <span class="commit-author">${author}</span>
                ${it.workspace ? `<span class="commit-repo-pill">${_CF_BRANCH} ${it.workspace}</span>` : ''}
                ${time ? `<span class="commit-time">${_CF_CLOCK} ${time}</span>` : ''}
              </div>
              <div class="commit-title">${it.issue_key ? _keyLink(it.issue_key) + ' ' : ''}${it.title || '—'}${shaHtml}</div>
              ${(it.status || it.priority) ? `<div class="flex items-center gap-1.5 mt-0.5">${_chip(it.status, '#06b6d4')}${_prioChip(it.priority)}</div>` : ''}
              ${filesHtml}
            </div>
          </div>`;
        }).join('')}</div>
      </div>`).join('');
  }

  async function loadWeekBreakdown(i) {
    if (_breakdownCache[i]) { renderBreakdown(i); return; }
    const row   = document.getElementById('week-' + i);
    const start = row?.dataset.start;
    const end   = row?.dataset.end;
    if (!start) return;
    const res  = await fetch(`/api/week-breakdown?start=${start}&end=${end}`, { credentials:'include' });
    const data = await res.json();
    _breakdownCache[i] = data;
    renderBreakdown(i);
  }

  function setBreakdownView(i, view) {
    _breakdownView[i] = view;
    document.getElementById('view-day-' + i)?.classList.toggle('bv-active',  view === 'day');
    document.getElementById('view-conn-' + i)?.classList.toggle('bv-active', view === 'connector');
    const sel = document.getElementById('conn-selector-' + i);
    if (sel) sel.style.display = view === 'connector' ? 'flex' : 'none';
    if (view === 'connector' && !_breakdownConn[i]) setConnector(i, 'github');
    else renderBreakdown(i);
  }

  function setConnector(i, src) {
    _breakdownConn[i] = src;
    SOURCE_ORDER.forEach(s => {
      const btn = document.getElementById('conn-btn-' + i + '-' + s);
      if (!btn) return;
      const active = s === src;
      btn.style.background  = active ? SOURCES[s].color : 'var(--surface-2)';
      btn.style.color       = active ? '#fff'           : 'var(--text-2)';
      btn.style.borderColor = active ? SOURCES[s].color : 'var(--border)';
    });
    renderBreakdown(i);
  }

  function renderBreakdown(i) {
    const data = _breakdownCache[i];
    const el   = document.getElementById('week-breakdown-' + i);
    if (!data || !el) return;
    const view = _breakdownView[i] || 'day';
    el.innerHTML = view === 'day'
      ? renderDayView(data.days || [])
      : renderConnectorView(data.days || [], _breakdownConn[i] || 'github');
  }

  function renderDayView(days) {
    if (!days.length) return '<p class="text-xs" style="color:var(--text-3)">No activity this week yet.</p>';
    return [...days].reverse().map(day => {
      const connRows = SOURCE_ORDER.map(src => {
        const c = day.connectors[src] || { count:0, items:[] };
        return `<div class="mb-3">
          <div class="flex items-center gap-2 mb-2 pl-2" style="border-left:2px solid ${SOURCES[src].color}">
            <span class="text-xs font-semibold" style="color:${SOURCES[src].color}">${SOURCES[src].label}</span>
            <span class="text-xs" style="color:var(--text-3)">${c.count > 0 ? c.count + ' event' + (c.count>1?'s':'') : 'No activity'}</span>
          </div>
          ${c.count > 0 ? renderItems(c.items, src) : ''}
        </div>`;
      }).join('');
      return `<div class="mb-5">
        <div class="text-xs font-bold mb-3" style="color:var(--text-1)">${fmtDayLabel(day.date)}</div>
        <div class="pl-3" style="border-left:2px solid var(--border)">${connRows}</div>
      </div>`;
    }).join('');
  }

  function renderConnectorView(days, src) {
    if (!days.length) return '<p class="text-xs" style="color:var(--text-3)">No activity this week yet.</p>';
    return [...days].reverse().map(day => {
      const c = day.connectors[src] || { count:0, items:[] };
      return `<div class="mb-4">
        <div class="flex items-center gap-2 mb-2">
          <div class="text-xs font-bold" style="color:var(--text-1)">${fmtDayLabel(day.date)}</div>
          ${c.count > 0 ? `<span class="text-xs" style="color:var(--text-3)">${c.count} event${c.count>1?'s':''}</span>` : ''}
        </div>
        <div class="pl-3" style="border-left:2px solid ${SOURCES[src].color}">
          ${c.count > 0 ? renderItems(c.items, src) : '<span class="text-xs" style="color:var(--text-3)">No activity</span>'}
        </div>
      </div>`;
    }).join('');
  }


  async function generateSummary(periodType) {
    const statusEl = document.getElementById('gen-status');
    statusEl.textContent = 'Generating…';
    statusEl.classList.remove('hidden');
    try {
      const res  = await fetch('/api/summaries/generate', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ period_type: periodType }),
      });
      const data = await res.json();
      if (data.ok) {
        statusEl.textContent = 'Done!';
        setTimeout(() => statusEl.classList.add('hidden'), 2000);
        await loadSummaries();
      } else {
        statusEl.textContent = `Error: ${data.error}`;
      }
    } catch {
      statusEl.textContent = 'Failed — check console';
    }
  }

  function downloadWeeklyPdf(weekStart) {
    window.location.href = `/api/export/weekly-pdf?week_start=${weekStart}`;
  }
