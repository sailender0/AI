/* ─────────────────────────────────────────────────────────────
   Shared app script.
   Base.html's script moved here verbatim, plus the modules that
   replace code previously copy-pasted across the page templates:
     SOURCES        — was 8 separate colour/label maps
     weekPicker()   — was 5 hand-rolled calendars, 3 week-starts
     chartBase()    — was 5 near-identical Chart.js option blocks
     renderCommitFeed() — was duplicated in 8 templates
   ───────────────────────────────────────────────────────────── */

/* ══ Sources ══════════════════════════════════════════════════
   Single source of truth. Replaces SOURCE_DOT, SOURCE_BADGE,
   SOURCE_CONFIG, TL_SRC_COLOR, TL_SRC_LABEL, SRC_COLOR,
   SRC_LABELS, INTG_GRAD and INTG_CLS. Keyed by the `source`
   value the backend emits. */
const SOURCES = {
  github:             { label: 'GitHub', color: '#f97316', hover: '#fb923c', grad: '#f97316,#fb923c', cls: 'kpi-num-github', href: '/github', icon: '⌥' },
  gitlab:             { label: 'GitLab', color: '#10b981', hover: '#34d399', grad: '#10b981,#34d399', cls: 'kpi-num-gitlab', href: '/gitlab', icon: '🦊' },
  jira:               { label: 'Jira',   color: '#06b6d4', hover: '#22d3ee', grad: '#06b6d4,#22d3ee', cls: 'kpi-num-jira',   href: '/jira',   icon: '◈' },
  teams_subscription: { label: 'Teams',  color: '#8b5cf6', hover: '#a78bfa', grad: '#8b5cf6,#a78bfa', cls: 'kpi-num-teams',  href: '/teams',  icon: '◉' },
};
// The analytics/agent pages key Teams as plain "teams". Alias, don't duplicate.
SOURCES.teams = SOURCES.teams_subscription;

const SOURCE_ORDER = ['github', 'jira', 'teams_subscription', 'gitlab'];
const srcOf = s => SOURCES[s] || { label: s, color: '#64748b', hover: '#94a3b8', grad: '#64748b,#94a3b8', cls: '' };

// Back-compat alias: base.html's sidebar used this name.
const CONNECTOR_META = SOURCES;

/* ══ Event-type labels ════════════════════════════════════════ */
const _ET_MAP = {
  commit: 'Commits', push: 'Pushes',
  pr_merged: 'PRs merged', pr_opened: 'PRs opened', pr_closed: 'PRs closed', pr_review: 'Reviews',
  issue_updated: 'Issues', issue_opened: 'Issues opened', issue_closed: 'Issues closed',
  comment: 'Comments', issue_comment: 'Comments',
  merge_request: 'Merge Requests', issue: 'Issues',
  'jira:issue_created': 'Created', 'jira:issue_updated': 'Updated',
  message_sent: 'Messages', meeting: 'Meetings',
};
function etLabel(et) {
  return _ET_MAP[et] || (et || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

/* ══ HTML escaping (XSS guard for innerHTML sinks) ═════════════
   Activity titles, repo/workspace names, filenames, event types and
   SHAs all originate from webhook/connector payloads and are rendered
   via innerHTML across the pages. Escape at the render boundary so
   injected markup shows as inert text instead of executing. */
function esc(s) {
  return (s == null ? '' : String(s))
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
// Returns escaped COPIES (never mutates the caller's cache) so re-renders on
// tab switches don't double-escape. Covers every user-controlled event field.
function escEvents(events) {
  return (events || []).map(it => ({
    ...it,
    title:      it.title      != null ? esc(it.title)      : it.title,
    workspace:  it.workspace  != null ? esc(it.workspace)  : it.workspace,
    sha:        it.sha        != null ? esc(it.sha)        : it.sha,
    event_type: it.event_type != null ? esc(it.event_type) : it.event_type,
    files:      Array.isArray(it.files) ? it.files.map(esc) : it.files,
  }));
}

/* ══ JSON fetch ═══════════════════════════════════════════════
   Callers used to do `(await res.json()).events || []`, which turns a failed
   response into an empty list — a 422 then renders as "no activity" instead of
   an error. That hid a broken week-activity panel indefinitely. Fail loudly. */
async function getJSON(url) {
  const res = await fetch(url, { credentials: 'include' });
  if (!res.ok) {
    console.error(`GET ${url} → HTTP ${res.status}`);
    return null;
  }
  return res.json();
}

/* ══ Backfill (ADR-0003) ══════════════════════════════════════ */
async function backfillNow(source) {
  if (!confirm('Import the last 30 days of ' + source + ' history? This runs in '
             + 'the background; new events appear as they arrive.')) return;
  try {
    const res = await fetch('/api/backfill/' + source + '?days=30',
                            { method: 'POST', credentials: 'include' });
    if (res.status === 202)      alert('Backfill started for ' + source + '.');
    else if (res.status === 429) alert('You ran a backfill recently — please wait before retrying.');
    else if (res.status === 401) alert('Please sign in again.');
    else                         alert('Could not start backfill (HTTP ' + res.status + ').');
  } catch (e) {
    alert('Could not reach the server to start backfill.');
  }
}

/* ══ Sidebar ══════════════════════════════════════════════════ */
function toggleSidebar() {
  const collapsed = document.getElementById('sidebar').classList.contains('collapsed');
  _setSidebar(collapsed ? 'open' : 'collapsed');
}

function _setSidebar(state) {
  const collapsed = state === 'collapsed';
  localStorage.setItem('sidebar', state);
  document.getElementById('sidebar').classList.toggle('collapsed', collapsed);
  const path = document.getElementById('toggle-path');
  if (path) path.setAttribute('d', collapsed ? 'M9 5l7 7-7 7' : 'M15 19l-7-7 7-7');
  // DA badge becomes the expand button when collapsed (the toggle btn hides with .sidebar-label)
  const badge = document.getElementById('da-badge');
  if (badge) {
    badge.style.cursor = collapsed ? 'pointer' : 'default';
    badge.title        = collapsed ? 'Expand sidebar' : '';
    badge.onclick      = collapsed ? toggleSidebar : null;
  }
}

/* ══ Theme ════════════════════════════════════════════════════ */
function setTheme(mode) {
  localStorage.setItem('theme', mode);
  _applyTheme(mode);
  _updateThemeBtns(mode);
  if (typeof loadStats === 'function') loadStats();
  if (typeof loadTrend === 'function') loadTrend();
}

function _applyTheme(mode) {
  const dark = mode === 'dark' || (mode === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
}

function _updateThemeBtns(mode) {
  ['light', 'system', 'dark'].forEach(m => {
    const btn = document.getElementById('btn-' + m);
    if (btn) {
      const active = m === mode;
      btn.classList.toggle('bg-indigo-600', active);
      btn.classList.toggle('text-white',    active);
      btn.classList.toggle('text-gray-400', !active);
    }
    const amBtn = document.getElementById('amenu-' + m);
    if (amBtn) {
      const active = m === mode;
      amBtn.style.background = active ? 'var(--chrome)' : 'transparent';
      amBtn.style.color      = active ? 'var(--chrome-text)' : 'var(--text-3)';
    }
  });
}

function toggleAppearanceMenu() {
  document.getElementById('appearance-menu').classList.toggle('hidden');
}

function chartColors() {
  const light = document.documentElement.getAttribute('data-theme') === 'light';
  return {
    grid: light ? 'rgba(0,0,0,0.06)' : 'rgba(255,255,255,0.05)',
    tick: light ? '#606078' : '#636378',
  };
}

/* ══ Chart.js shared options ══════════════════════════════════
   Was re-typed in dashboard/github/gitlab/jira/teams. Spread it and
   override what a page actually needs:
     new Chart(ctx, { type:'bar', data, options: chartBase({ stacked:true }) }) */
function chartBase({ stacked = false, onBarClick = null, tooltip = {} } = {}) {
  const cc    = chartColors();
  const light = document.documentElement.getAttribute('data-theme') === 'light';
  const axis  = {
    stacked,
    grid:   { color: cc.grid },
    ticks:  { color: cc.tick, font: { size: 10, family: 'Inter' } },
    border: { display: false },
  };
  return {
    responsive: true,
    maintainAspectRatio: false,
    onClick(evt, elements) { if (onBarClick && elements.length) onBarClick(elements[0].index, elements[0]); },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: light ? '#ffffff' : '#1e2535',
        borderColor:     light ? '#e2e8f0' : '#334155',
        borderWidth: 1,
        titleColor:  light ? '#0f172a' : '#f1f5f9',
        bodyColor:   light ? '#475569' : '#94a3b8',
        footerColor: light ? '#475569' : '#94a3b8',
        footerFont:  { size: 11 },
        padding: 12,
        cornerRadius: 8,
        ...tooltip,
      },
    },
    scales: { x: { ...axis }, y: { ...axis, beginAtZero: true } },
  };
}

/* ══ Week picker (Monday-start) ═══════════════════════════════
   Replaces 5 hand-rolled calendars that had drifted into 3 different
   week-start conventions. Monday matches the backend everywhere
   (activity.py, summarizer.py, query.py all use weekday()).

   Usage:
     const wp = weekPicker({
       mount:  '#week-picker',    // empty div; the panel renders into it
       button: '#range-week',     // the "Pick Week ▾" toggle
       onSelect(mondayIso, label) { ... },
     });
     wp.setSelected('2026-07-13');
*/
const _PK_MONTHS = ['January','February','March','April','May','June',
                    'July','August','September','October','November','December'];

const isoOf   = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
const mondayOf = d => { const m = new Date(d); m.setHours(0,0,0,0); const dow = m.getDay(); m.setDate(m.getDate() - (dow === 0 ? 6 : dow - 1)); return m; };

// "Jul 7–11" (Mon–Fri of that week), or "Jun 30–Jul 4" across a month boundary.
function weekLabel(monday) {
  const mon = new Date(monday), fri = new Date(monday);
  fri.setDate(mon.getDate() + 4);
  const fmt = d => d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  return mon.getMonth() === fri.getMonth()
    ? `${fmt(mon).replace(/\s\d+$/, '')} ${mon.getDate()}–${fri.getDate()}`
    : `${fmt(mon)}–${fmt(fri)}`;
}

// The current week's Monday + its label — every page needed this.
function currentWeek() {
  const mon = mondayOf(new Date());
  return { start: isoOf(mon), label: weekLabel(mon) };
}

function weekPicker({ mount, button, onSelect }) {
  const panel = typeof mount === 'string' ? document.querySelector(mount) : mount;
  const btn   = typeof button === 'string' ? document.querySelector(button) : button;
  if (!panel || !btn) return null;

  let year = new Date().getFullYear(), month = new Date().getMonth();
  let selected = null;

  panel.className = 'pk-panel hidden';
  panel.innerHTML = `
    <div class="flex items-center justify-between mb-2">
      <button type="button" data-pk="prev" style="background:none;border:none;cursor:pointer;color:var(--text-2);font-size:18px;line-height:1;padding:2px 6px">‹</button>
      <span data-pk="label" class="text-xs font-semibold" style="color:var(--text-1)"></span>
      <button type="button" data-pk="next" style="background:none;border:none;cursor:pointer;color:var(--text-2);font-size:18px;line-height:1;padding:2px 6px">›</button>
    </div>
    <div class="grid grid-cols-7 mb-1">
      ${['M','T','W','T','F','S','S'].map(d => `<div class="pk-hdr">${d}</div>`).join('')}
    </div>
    <div data-pk="grid"></div>`;

  const $ = sel => panel.querySelector(`[data-pk="${sel}"]`);

  function render() {
    $('label').textContent = `${_PK_MONTHS[month]} ${year}`;
    const today = new Date(); today.setHours(0, 0, 0, 0);

    // Every week whose Monday falls on/before the month's last day — that's
    // exactly the weeks that intersect this month (the first row may start in
    // the previous month; its out-of-month days render hidden via .pk-overflow).
    const first = new Date(year, month, 1);
    const last  = new Date(year, month + 1, 0);
    const grid  = $('grid');
    grid.innerHTML = '';

    for (const wk = mondayOf(first); wk <= last; wk.setDate(wk.getDate() + 7)) {
      const rowMonday = new Date(wk);
      const iso       = isoOf(rowMonday);
      const future    = rowMonday > today;

      const row = document.createElement('div');
      row.className = `pk-row${iso === selected ? ' pk-selected' : ''}${future ? ' pk-future' : ''}`;

      for (let i = 0; i < 7; i++) {
        const d    = new Date(rowMonday); d.setDate(rowMonday.getDate() + i);
        const cell = document.createElement('div');
        cell.className = `pk-cell${d.getMonth() !== month ? ' pk-overflow' : ''}`
                       + `${d.getTime() === today.getTime() ? ' pk-today' : ''}`;
        cell.textContent = d.getDate();
        row.appendChild(cell);
      }
      if (!future) row.onclick = () => { setSelected(iso); render(); close(); onSelect(iso, weekLabel(rowMonday)); };
      grid.appendChild(row);
    }
  }

  function setSelected(iso) { selected = iso; }
  function open()  { const d = selected ? new Date(selected + 'T00:00:00') : new Date();
                     year = d.getFullYear(); month = d.getMonth(); render(); panel.classList.remove('hidden'); }
  function close() { panel.classList.add('hidden'); }
  function toggle(){ panel.classList.contains('hidden') ? open() : close(); }

  $('prev').onclick = e => { e.stopPropagation(); if (--month < 0)  { month = 11; year--; } render(); };
  $('next').onclick = e => { e.stopPropagation(); if (++month > 11) { month = 0;  year++; } render(); };
  btn.addEventListener('click', e => { e.stopPropagation(); toggle(); });
  document.addEventListener('click', e => {
    if (!panel.classList.contains('hidden') && !panel.contains(e.target) && e.target !== btn) close();
  });

  return { setSelected, open, close, toggle };
}

/* ══ Commit feed rendering ════════════════════════════════════
   The .commit-item markup was duplicated across 8 templates. */
const _FEED_AVATAR_COLORS = ['#3b82f6','#22c55e','#f59e0b','#8b5cf6','#ec4899','#06b6d4','#84cc16','#64748b'];
function avatarColor(str) {
  let h = 0;
  for (const c of (str || '')) h = (h * 31 + c.charCodeAt(0)) & 0xffff;
  return _FEED_AVATAR_COLORS[h % _FEED_AVATAR_COLORS.length];
}

const ICON_BRANCH = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>`;
const ICON_CLOCK  = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>`;

// One commit row. `when` is a preformatted time/date string.
function commitRow(it, { initials = '??', author = 'you', when = '', badge = '', sub = '' } = {}) {
  const shaHtml = it.sha
    ? `<code style="font-size:10px;font-family:monospace;background:var(--surface-2);color:var(--text-3);padding:1px 5px;border-radius:4px;margin-left:4px">${it.sha}</code>`
    : '';
  const filesHtml = it.files?.length
    ? `<div class="flex flex-wrap gap-1 mt-1">${it.files.slice(0,4).map(f =>
        `<span style="font-size:10px;font-family:monospace;background:var(--surface-2);color:var(--text-3);padding:1px 5px;border-radius:4px">${f.split('/').pop()}</span>`).join('')}${
        it.files.length > 4 ? `<span style="font-size:10px;color:var(--text-3)"> +${it.files.length - 4} more</span>` : ''}</div>`
    : '';
  return `<div class="commit-item">
    <div class="commit-avatar" style="background:${avatarColor(it.workspace || it.source || '')}">${initials}</div>
    <div class="commit-body">
      <div class="commit-meta">
        <span class="commit-author">${author}</span>
        ${badge}
        ${it.workspace ? `<span class="commit-repo-pill">${ICON_BRANCH} ${it.workspace}</span>` : ''}
        ${when ? `<span class="commit-time">${ICON_CLOCK} ${when}</span>` : ''}
      </div>
      <div class="commit-title">${it.title || '—'}${shaHtml}</div>
      ${sub ? `<div class="commit-sub">${sub}</div>` : ''}
      ${filesHtml}
    </div>
  </div>`;
}

const fmtTime = iso => iso ? new Date(iso).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }) : '';
const fmtDateTime = iso => iso ? new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '';

/* ══ KPI tilt + pulse ═════════════════════════════════════════ */
function initKpiTilt() {
  document.querySelectorAll('.kpi-tilt-card').forEach(card => {
    card.addEventListener('mousemove', e => {
      const r = card.getBoundingClientRect();
      const x = (e.clientX - r.left) / r.width  - 0.5;
      const y = (e.clientY - r.top)  / r.height - 0.5;
      const shadow = document.documentElement.getAttribute('data-theme') === 'light'
        ? '0 8px 24px rgba(0,0,0,0.12)'
        : '0 14px 36px rgba(0,0,0,0.4)';
      card.style.transform = `perspective(600px) rotateY(${x*10}deg) rotateX(${-y*10}deg) scale(1.04)`;
      card.style.boxShadow = shadow;
    });
    card.addEventListener('mouseleave', () => {
      card.style.transform = '';
      card.style.boxShadow = '';
    });
  });
}

function _initPulseDots() {
  document.querySelectorAll('.period-btn').forEach(btn => {
    if (btn.textContent.trim() === 'Today' && !btn.querySelector('.live-dot')) {
      const dot = document.createElement('span');
      dot.className = 'live-dot';
      btn.prepend(dot);
    }
  });
}

/* ══ Greeting ═════════════════════════════════════════════════ */
function getGreeting(email) {
  const hour = new Date().getHours();
  const name = email.split('@')[0].split('.')[0];
  const capitalized = name.charAt(0).toUpperCase() + name.slice(1);
  if (hour < 12) return `Good morning, ${capitalized}`;
  if (hour < 17) return `Good afternoon, ${capitalized}`;
  return `Good evening, ${capitalized}`;
}

/* ══ Boot ═════════════════════════════════════════════════════ */
async function initBase() {
  const res = await fetch('/api/me', { credentials: 'include' });
  const data = await res.json();

  if (!data.authenticated) {
    document.getElementById('sidebar-login').classList.remove('hidden');
    document.getElementById('page-greeting').textContent = 'Developer Activity Tracker';
    return;
  }

  document.getElementById('sidebar-logout').classList.remove('hidden');
  document.getElementById('sidebar-email').textContent = data.email;
  document.getElementById('page-greeting').textContent = getGreeting(data.email);
  // Feed avatar identity, shared by every page's onBaseReady
  const _emailUser = (data.email || '').split('@')[0];
  window._actUserInitials = _emailUser.replace(/[^a-zA-Z]/g, '').slice(0, 2).toUpperCase() || '??';
  window._actAuthorName   = _emailUser.split(/[._]/)[0] || _emailUser;
  const av = document.getElementById('header-avatar');
  av.textContent = data.email[0].toUpperCase();
  av.classList.remove('hidden');
  const ae = document.getElementById('appearance-email');
  if (ae) ae.textContent = data.email;

  // Sidebar connectors
  const container = document.getElementById('sidebar-connectors');
  container.innerHTML = '';
  for (const source of SOURCE_ORDER) {
    const meta      = SOURCES[source];
    const connected = data.integrations[source];
    const broken    = (data.integration_errors || {})[source];
    const isActive  = ACTIVE_PAGE === source || (ACTIVE_PAGE === 'teams' && source === 'teams_subscription');
    container.innerHTML += `
      <a href="${meta.href}" title="${meta.label}${broken ? ' — connection problem, reconnect' : ''}" class="flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition
        ${isActive ? 'bg-indigo-600 text-white' : 'text-gray-400 hover:bg-gray-800 hover:text-gray-100'}">
        <span class="text-base leading-none shrink-0">${meta.icon}</span>
        <span class="sidebar-label flex-1 whitespace-nowrap">${meta.label}</span>
        <span class="sidebar-label w-1.5 h-1.5 rounded-full shrink-0 ${broken ? 'bg-amber-400' : connected ? 'bg-green-400' : 'bg-gray-600'}"></span>
      </a>`;
  }

  // Save browser timezone before loading any data — ensures all queries use local time
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    await fetch('/api/profile/timezone', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ timezone: tz }),
      credentials: 'include',
    });
  } catch (_) {}

  _initPulseDots();
  _connectWS();
  if (typeof onBaseReady === 'function') onBaseReady(data);
}

/* ══ WebSocket ════════════════════════════════════════════════ */
let _wsReconnectTimer = null;

function _connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  window._appWS = ws;

  const dot    = () => document.getElementById('ws-live-dot');
  const label  = () => document.getElementById('ws-label');
  const status = () => document.getElementById('ws-status');

  ws.onopen = () => {
    if (dot())   dot().style.display = '';
    if (label()) label().textContent = 'Live';
    if (status()) { status().style.color = 'var(--positive)'; status().style.opacity = '1'; }
    clearTimeout(_wsReconnectTimer);
  };

  ws.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (typeof window.onWSEvent === 'function') window.onWSEvent(data);
    } catch {}
  };

  ws.onclose = () => {
    if (dot())   dot().style.display = 'none';
    if (label()) label().textContent = '';
    if (status()) status().style.opacity = '0';
    _wsReconnectTimer = setTimeout(_connectWS, 3000);
  };

  ws.onerror = () => ws.close();
}

/* ══ Global listeners ═════════════════════════════════════════ */
document.addEventListener('click', e => {
  const menu   = document.getElementById('appearance-menu');
  const avatar = document.getElementById('header-avatar');
  if (menu && !menu.classList.contains('hidden') && !menu.contains(e.target) && e.target !== avatar) {
    menu.classList.add('hidden');
  }
});

window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if ((localStorage.getItem('theme') || 'dark') === 'system') _applyTheme('system');
});

/* ══ Chat widget (Alpine) ═════════════════════════════════════ */
// Proactive agent bubble (base.html). No longer a chat — it fetches what needs
// the user's attention. Full chat lives at /ai. Loads once on init so the bubble
// badge shows a count without being opened; the server caches the payload 10 min.
function agentWidget() {
  return {
    open: false,
    loading: false,
    loaded: false,
    digest: '',
    cards: [],

    init() { this.load(); },

    toggle() {
      this.open = !this.open;
      if (this.open && !this.loaded) this.load();
    },

    async load(fresh = false) {
      if (this.loading) return;
      this.loading = true;
      try {
        const tz  = Intl.DateTimeFormat().resolvedOptions().timeZone;  // IANA tz (ADR-0001)
        const qs  = `tz=${encodeURIComponent(tz)}${fresh ? '&fresh=1' : ''}`;  // fresh bypasses the server cache
        const r   = await fetch(`/api/agent/insights?${qs}`, { credentials: 'include' });
        const d   = await r.json();
        this.digest = d.digest || '';
        this.cards  = Array.isArray(d.cards) ? d.cards : [];
        this.loaded = true;
      } catch {
        this.digest = '';
        this.cards  = [];
      }
      this.loading = false;
    },

    refresh() { this.loaded = false; this.load(true); },
  };
}

/* ══ Jira "My Work" — shared by /jira and the dashboard ═══════
   Always renders the assigned-issues list into #assigned-list (+ count badge);
   when the host page provides them, also fills #jira-kpis (stat tiles) and the
   #jira-status-chart / #jira-prio-chart mini bars. Bar colors deliberately
   match the status/priority chips used across the app (color follows the
   entity); identity is always carried by the axis text labels, never color
   alone, so the shared-hue statuses stay readable for CVD users. */
// Deterministic per-repo/workspace accent, shared by every feed renderer
const _REPO_COLORS = ['#3b82f6','#22c55e','#f59e0b','#8b5cf6','#ec4899','#06b6d4','#84cc16','#64748b'];
function repoColor(s) { let h = 0; for (const c of (s || '')) h = (h * 31 + c.charCodeAt(0)) & 0xffff; return _REPO_COLORS[h % _REPO_COLORS.length]; }

let _jiraSiteUrl = localStorage.getItem('jiraSiteUrl') || '';  // survives across pages that never call loadAssigned
const _PRIO_COLORS = { highest:'#ef4444', high:'#f97316', medium:'#f59e0b', low:'#22c55e', lowest:'#64748b' };
const _CAT_COLORS  = { new:'#64748b', indeterminate:'#f59e0b', done:'#22c55e' };  // Jira statusCategory keys
const _PRIO_ORDER  = ['Highest', 'High', 'Medium', 'Low', 'Lowest'];
const _chip = (txt, color) => txt ? `<span class="text-xs px-1.5 py-0.5 rounded" style="background:${color}22;color:${color}">${esc(txt)}</span>` : '';
const _prioChip = p => _chip(p, _PRIO_COLORS[(p || '').toLowerCase()] || '#64748b');
function _keyLink(key) {
  if (!key) return '';
  const k = esc(key);
  return _jiraSiteUrl
    ? `<a href="${esc(_jiraSiteUrl)}/browse/${k}" target="_blank" rel="noopener" class="text-xs font-semibold hover:underline" style="color:#06b6d4">${k}</a>`
    : `<span class="text-xs font-semibold" style="color:#06b6d4">${k}</span>`;
}

// due date -> {days, color, label}; null when the issue has no due date
function _dueInfo(due) {
  if (!due) return null;
  const days = Math.round((new Date(due + 'T00:00:00') - new Date(new Date().toDateString())) / 864e5);
  if (days < 0)   return { days, color: '#ef4444', label: `overdue ${-days}d` };
  if (days === 0) return { days, color: '#f59e0b', label: 'due today' };
  if (days === 1) return { days, color: '#f59e0b', label: 'due tomorrow' };
  const d = new Date(due + 'T00:00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  return { days, color: 'var(--text-3)', label: `due ${d}` };
}

function _renderJiraKpis(issues, done7d) {
  const el = document.getElementById('jira-kpis');
  if (!el) return;
  const inProg  = issues.filter(i => i.status_category === 'indeterminate').length;
  const dues    = issues.map(i => _dueInfo(i.due_date)).filter(Boolean);
  const overdue = dues.filter(d => d.days < 0).length;
  const dueSoon = dues.filter(d => d.days >= 0 && d.days <= 7).length;
  const tiles = [
    ['Overdue', overdue, overdue ? '#ef4444' : null],
    ['Due this week', dueSoon, null],
    ['In progress', inProg, null],
    ['Done this week', done7d == null ? '—' : done7d, done7d ? '#22c55e' : null],
    ['Open total', issues.length, null],
  ];
  el.innerHTML = tiles.map(([label, value, color], i) => `
    <div class="flex-1 min-w-0 px-4 py-2.5" ${i ? 'style="border-left:1px solid var(--border)"' : ''}>
      <p class="kpi-label">${label}</p>
      <p class="text-xl font-bold mt-0.5" style="color:${color || 'var(--text-1)'}">${value}</p>
    </div>`).join('');
}

let _jiraStatusChart = null, _jiraPrioChart = null, _jiraDueChart = null, _jiraAgeChart = null;

function _miniBar(canvasId, labels, data, colors, prev, horizontal = true) {
  const el = document.getElementById(canvasId);
  if (!el || typeof Chart === 'undefined') return prev;
  if (prev) prev.destroy();
  const cc = chartColors();
  const valueAxis = { grid: { color: cc.grid }, ticks: { color: cc.tick, font: { size: 10 }, precision: 0 }, beginAtZero: true };
  const catAxis   = { grid: { display: false }, ticks: { color: cc.tick, font: { size: 10 } } };
  return new Chart(el.getContext('2d'), {
    type: 'bar',
    data: { labels, datasets: [{ data, backgroundColor: colors, borderRadius: 4, barThickness: 10 }] },
    options: {
      indexAxis: horizontal ? 'y' : 'x',
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: horizontal ? { x: valueAxis, y: catAxis } : { x: catAxis, y: valueAxis },
    },
  });
}

// Due outlook (overdue + next 7 days, vertical) and WIP aging (horizontal,
// sequential cyan ramp — darker = older). Only render where the page mounts them.
function _renderJiraExtraCharts(issues) {
  if (document.getElementById('jira-due-chart')) {
    const counts = new Array(8).fill(0);
    issues.forEach(i => {
      const d = _dueInfo(i.due_date);
      if (!d) return;
      if (d.days < 0) counts[0]++;
      else if (d.days <= 6) counts[1 + d.days]++;
    });
    const labels = ['Overdue'];
    for (let k = 0; k < 7; k++) {
      const dt = new Date(); dt.setDate(dt.getDate() + k);
      labels.push(k === 0 ? 'Today' : dt.toLocaleDateString(undefined, { weekday: 'short' }));
    }
    const colors = labels.map((_, i) => i === 0 ? '#ef4444' : '#06b6d4');
    _jiraDueChart = _miniBar('jira-due-chart', labels, counts, colors, _jiraDueChart, false);
  }

  if (document.getElementById('jira-age-chart')) {
    const now = Date.now();
    const buckets = [0, 0, 0, 0];
    issues.forEach(i => {
      if (!i.created) return;
      const weeks = (now - new Date(i.created)) / 6048e5;
      buckets[weeks < 1 ? 0 : weeks < 2 ? 1 : weeks < 4 ? 2 : 3]++;
    });
    _jiraAgeChart = _miniBar('jira-age-chart', ['< 1 week', '1–2 weeks', '2–4 weeks', '> 1 month'],
      buckets, ['#22d3ee', '#06b6d4', '#0891b2', '#155e75'], _jiraAgeChart);
  }
}

function _renderJiraCharts(issues) {
  // by status name (top 5), each bar wearing its statusCategory color
  const byStatus = {};
  issues.forEach(i => { const s = i.status || '—'; (byStatus[s] = byStatus[s] || { n: 0, cat: i.status_category }).n++; });
  const sLabels = Object.keys(byStatus).sort((a, b) => byStatus[b].n - byStatus[a].n).slice(0, 5);
  _jiraStatusChart = _miniBar('jira-status-chart', sLabels,
    sLabels.map(s => byStatus[s].n),
    sLabels.map(s => _CAT_COLORS[byStatus[s].cat] || '#06b6d4'),
    _jiraStatusChart);

  // by priority, in fixed severity order (unknown names appended)
  const byPrio = {};
  issues.forEach(i => { const p = i.priority || '—'; byPrio[p] = (byPrio[p] || 0) + 1; });
  const pLabels = _PRIO_ORDER.filter(p => byPrio[p])
    .concat(Object.keys(byPrio).filter(p => !_PRIO_ORDER.includes(p)));
  _jiraPrioChart = _miniBar('jira-prio-chart', pLabels,
    pLabels.map(p => byPrio[p]),
    pLabels.map(p => _PRIO_COLORS[p.toLowerCase()] || '#64748b'),
    _jiraPrioChart);
}

async function loadAssigned() {
  const el = document.getElementById('assigned-list');
  if (!el) return;
  const data = await getJSON('/api/jira/assigned');
  if (!data) {
    el.innerHTML = '<p class="text-xs" style="color:var(--text-3)">Could not load assigned issues — the Jira connection may have expired. '
      + '<a href="/connect/jira" class="hover:underline" style="color:#06b6d4">Reconnect Jira →</a></p>';
    return;
  }
  _jiraSiteUrl = data.site_url || _jiraSiteUrl;
  if (data.site_url) localStorage.setItem('jiraSiteUrl', data.site_url);
  // overdue first, then soonest due; no due date last (stable sort keeps the
  // server's priority ordering within each group)
  const issues = (data.issues || []).slice().sort((a, b) => {
    const da = _dueInfo(a.due_date), db = _dueInfo(b.due_date);
    return (da ? da.days : Infinity) - (db ? db.days : Infinity);
  });
  const countEl = document.getElementById('assigned-count');
  if (countEl) countEl.textContent = issues.length;
  _renderJiraKpis(issues, data.done_7d);
  _renderJiraCharts(issues);
  _renderJiraExtraCharts(issues);
  if (!issues.length) {
    el.innerHTML = '<p class="text-xs" style="color:var(--text-3)">Nothing open is assigned to you.</p>';
    return;
  }
  el.innerHTML = issues.map(it => {
    const dueI = _dueInfo(it.due_date);
    const due  = dueI ? `<span class="text-xs font-medium" style="color:${dueI.color}">${esc(dueI.label)}</span>` : '';
    const meta = [it.issue_type, it.sprint, it.story_points != null ? `${it.story_points} pts` : null]
      .filter(Boolean).map(esc).join(' · ');
    const pc = _PRIO_COLORS[(it.priority || '').toLowerCase()] || 'var(--border)';
    return `<div class="py-2 pl-3" style="border-bottom:1px solid var(--border);border-left:2px solid ${pc}">
      <div class="flex items-center gap-2 flex-wrap">
        ${_keyLink(it.key)}
        ${_chip(it.status, _CAT_COLORS[it.status_category] || '#06b6d4')}
        ${_prioChip(it.priority)}
        ${due}
      </div>
      <div class="text-xs truncate mt-1" style="color:var(--text-1)">${esc(it.summary)}</div>
      ${meta ? `<div class="text-xs mt-0.5" style="color:var(--text-3)">${meta}</div>` : ''}
    </div>`;
  }).join('');
}

/* ══ Go ═══════════════════════════════════════════════════════ */
_setSidebar(localStorage.getItem('sidebar') || 'open');
_updateThemeBtns(localStorage.getItem('theme') || 'dark');
initBase();
