  // Attendance report page. esc() is global from app.js. IS_ELEVATED from template.
  let AT_PEOPLE = [];               // {id,email,role} in scope (elevated only)
  const atSelected = new Map();     // id -> {email,role}
  let atLast = null;                // last query params for the CSV download
  let rangeStart = null, rangeEnd = null;   // ISO yyyy-mm-dd
  let atCal = null;                 // shared RangeCalendar instance
  let updTimer = null;
  const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const DOW = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];

  const iso = d => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  function atToday() { return iso(new Date()); }
  function atDaysAgo(n) { const d = new Date(); d.setDate(d.getDate() - n); return iso(d); }

  async function onBaseReady() {
    atCal = RangeCalendar(document.getElementById('at-cal'), {
      onChange: (s, e) => { rangeStart = s; rangeEnd = e; updateRangeLabel(); scheduleUpdate(); },
    });

    // period presets
    document.getElementById('at-presets').addEventListener('click', e => {
      const b = e.target.closest('button'); if (!b) return;
      [...e.currentTarget.children].forEach(x => x.setAttribute('aria-pressed', 'false'));
      b.setAttribute('aria-pressed', 'true');
      const p = b.dataset.preset;
      const custom = document.getElementById('at-custom');
      if (p === 'custom') { custom.style.display = 'block'; atCal.setRange(rangeStart, rangeEnd); return; }
      custom.style.display = 'none';
      applyPreset(p); updateRangeLabel(); scheduleUpdate();
    });

    if (IS_ELEVATED) {
      try {
        const r = await fetch('/api/user-management/users', { credentials: 'include' });
        if (r.ok) AT_PEOPLE = (await r.json()).users || [];
      } catch (e) { /* picker stays empty; server still scopes */ }
      // A manager's scope is just their reports — no role filter, only self+team.
      const isAdmin = window._role === 'admin';
      if (!isAdmin) document.getElementById('at-modebtn-role').style.display = 'none';
      document.getElementById('at-panel-all').textContent = isAdmin
        ? 'Showing everyone.' : 'Showing your whole team.';
      document.addEventListener('click', e => {
        if (!e.target.closest('.at-search')) document.getElementById('at-people-drop').classList.remove('show');
      });
    }

    applyPreset('7');          // default: last 7 days
    updateRangeLabel();
    runReport();               // initial live render
  }

  // ── period ──────────────────────────────────────────────────────────────────
  function applyPreset(p) {
    rangeEnd = atToday();
    if (p === 'thismonth') { const d = new Date(); rangeStart = iso(new Date(d.getFullYear(), d.getMonth(), 1)); }
    else rangeStart = atDaysAgo(+p - 1);
  }
  function updateRangeLabel() {
    const el = document.getElementById('at-range-label');
    if (rangeStart && rangeEnd) el.textContent = `${fmtISO(rangeStart)} → ${fmtISO(rangeEnd)}`;
    else if (rangeStart) el.textContent = `${fmtISO(rangeStart)} → pick an end date`;
    else el.textContent = '';
  }
  function fmtISO(s) { const [y, m, d] = s.split('-').map(Number); return `${MON[m - 1]} ${d}`; }

  // ── people ──────────────────────────────────────────────────────────────────
  let peopleMode = 'all';           // all | role | users
  function atMode(mode) {
    peopleMode = mode;
    document.getElementById('at-panel-all').style.display = mode === 'all' ? '' : 'none';
    document.getElementById('at-panel-role').style.display = mode === 'role' ? '' : 'none';
    document.getElementById('at-panel-users').style.display = mode === 'users' ? 'flex' : 'none';
    document.querySelectorAll('#at-people-mode .at-seg-btn')
      .forEach(b => b.setAttribute('aria-selected', b.dataset.mode === mode));
    scheduleUpdate();
  }
  function atUsers() {
    if (peopleMode === 'role') {
      const role = document.getElementById('at-role').value;
      return AT_PEOPLE.filter(p => p.role === role).map(p => p.id);
    }
    if (peopleMode === 'users') return [...atSelected.keys()];
    return [];   // 'all' → empty = everyone in scope (server clamps)
  }

  function atSearch(q) {
    const drop = document.getElementById('at-people-drop'); if (!drop) return;
    q = q.trim().toLowerCase();
    const matches = AT_PEOPLE.filter(p => p.email.toLowerCase().includes(q) && !atSelected.has(p.id));
    drop.innerHTML = matches.length
      ? matches.map(p => `<div class="at-opt" onclick="atPick('${p.id}')"><span>${esc(p.email)}</span><span class="r">${esc(p.role)}</span></div>`).join('')
      : '<div class="at-opt" style="cursor:default;color:var(--text-3)">No matches</div>';
    drop.classList.add('show');
  }
  function atPick(id) {
    const p = AT_PEOPLE.find(x => x.id === id); if (!p) return;
    atSelected.set(id, { email: p.email, role: p.role });
    renderSelected();
    const s = document.getElementById('at-people-search'); s.value = ''; s.focus();
    document.getElementById('at-people-drop').classList.remove('show');
    scheduleUpdate();
  }
  function atRemove(id) { atSelected.delete(id); renderSelected(); scheduleUpdate(); }
  function renderSelected() {
    document.getElementById('at-selected').innerHTML = [...atSelected.entries()]
      .map(([id, p]) => `<span class="at-chip">${esc(p.email)}<button onclick="atRemove('${id}')" title="Remove">×</button></span>`).join('');
  }

  // ── live update ───────────────────────────────────────────────────────────────
  function atParams() {
    return { start: rangeStart, end: rangeEnd, sources: [], users: IS_ELEVATED ? atUsers() : [] };
  }
  function scheduleUpdate() { clearTimeout(updTimer); updTimer = setTimeout(runReport, 250); }

  async function runReport() {
    const p = atParams();
    const status = document.getElementById('at-status');
    if (!p.start || !p.end) { status.textContent = 'Pick a date range.'; return; }
    status.textContent = 'Updating…';
    try {
      const r = await fetch('/api/report/attendance', {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start: p.start, end: p.end, sources: p.sources, user_ids: p.users }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.error) { status.textContent = '✕ ' + (d.error || d.detail || r.status); return; }
      atLast = p;
      renderGrid(d);
      document.getElementById('at-download').disabled = false;
      status.textContent = '';
    } catch (e) { status.textContent = '✕ Error — try a smaller range.'; }
  }

  function atDownload() {
    if (!atLast) return;
    const qs = new URLSearchParams({ start: atLast.start, end: atLast.end });
    if (atLast.sources.length) qs.set('sources', atLast.sources.join(','));
    if (atLast.users.length) qs.set('users', atLast.users.join(','));
    window.location.href = '/api/report/attendance.csv?' + qs.toString();
  }

  function dLabel(isoStr) {
    const d = new Date(isoStr + 'T00:00:00');
    return `<span class="mon">${MON[d.getMonth()]}</span>${d.getDate()}<br><span class="mon">${DOW[d.getDay()]}</span>`;
  }

  function renderGrid(d) {
    const box = document.getElementById('at-result');
    const present = d.rows.reduce((a, r) => a + r.present, 0);
    document.getElementById('at-kpis').innerHTML =
      `<span class="at-kpi"><b>${d.rows.length}</b><span>people</span></span>` +
      `<span class="at-kpi"><b>${d.days.length}</b><span>days</span></span>` +
      `<span class="at-kpi"><b>${present}</b><span>present-days</span></span>`;

    document.getElementById('at-head').innerHTML =
      `<tr><th class="at-usercol">${d.rows.length} ${d.rows.length === 1 ? 'person' : 'people'}</th>` +
      d.days.map(day => `<th>${dLabel(day)}</th>`).join('') +
      `<th class="at-present">Days<br>present</th></tr>`;

    document.getElementById('at-body').innerHTML = d.rows.map(r => {
      const cells = r.counts.map(c => {
        const p = c >= d.threshold;
        return `<td class="at-cell ${p ? 'present' : 'absent'}">${p ? 'P' : 'A'}</td>`;
      }).join('');
      const name = r.email.split('@')[0];
      return `<tr>
        <td class="at-usercol"><div class="at-uname">${esc(name)}</div><div class="at-urole">${esc(r.role)}</div></td>
        ${cells}<td class="at-present">${r.present}</td></tr>`;
    }).join('') || `<tr><td class="at-usercol" style="color:var(--text-3)">No users in scope.</td></tr>`;

    box.style.display = '';
  }
