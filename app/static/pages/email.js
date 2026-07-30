  const KINDS = {
    my_day: 'My Day', device_activity: 'Device Activity (Day)',
    device_activity_week: 'Device Activity (Week)', analytics: 'Analytics', standup: 'Standup'
  };
  const WEEK_KINDS = ['analytics', 'device_activity_week'];

  function localToday() {
    const d = new Date();
    return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  }

  function weekLabel(dateStr) {
    const d = new Date(dateStr + 'T00:00:00');
    const dow = (d.getDay() + 6) % 7;                    // Mon=0
    const mon = new Date(d); mon.setDate(d.getDate() - dow);
    const sun = new Date(mon); sun.setDate(mon.getDate() + 6);
    const fmt = x => x.toLocaleDateString([], { month: 'short', day: 'numeric' });
    return `Week of ${fmt(mon)} – ${fmt(sun)}`;
  }

  // ── Send panel ──────────────────────────────────────────────────────────────
  // Device Activity carries a Day/Weekly toggle → effective kind is device_activity
  // (day) or device_activity_week (weekly). Other reports pass through unchanged.
  function effectiveKind() {
    const kind = document.getElementById('email-kind').value;
    if (kind === 'device_activity') {
      const v = document.querySelector('input[name="device-view"]:checked');
      return v ? v.value : 'device_activity';
    }
    return kind;
  }

  function updateDateUI() {
    const isWeek = WEEK_KINDS.includes(effectiveKind());
    const label = document.getElementById('email-date-label');
    const hint = document.getElementById('email-date-hint');
    const date = document.getElementById('email-date').value;
    if (isWeek) {
      label.textContent = 'Week (pick any day)';
      hint.textContent = date ? weekLabel(date) : '';
    } else {
      label.textContent = 'Date';
      hint.textContent = '';
    }
  }

  function onKindChange() {
    const kind = document.getElementById('email-kind').value;
    document.getElementById('device-toggle').style.display = (kind === 'device_activity') ? 'flex' : 'none';
    updateDateUI();
    loadPreview();
  }

  async function loadPreview() {
    const kind = effectiveKind();
    const date = document.getElementById('email-date').value || null;
    const status = document.getElementById('email-status');
    updateDateUI();
    status.textContent = 'Generating…';
    try {
      const r = await fetch('/api/email/preview', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, date }),
      });
      const d = await r.json();
      if (d.error) { status.textContent = '✕ ' + d.error; return; }
      document.getElementById('email-preview').srcdoc = d.html || '';
      status.textContent = '';
    } catch (e) { status.textContent = '✕ Error'; }
  }

  async function sendEmail() {
    const kind = effectiveKind();
    const date = document.getElementById('email-date').value || null;
    const btn = document.getElementById('email-send-btn');
    const old = btn.textContent;
    btn.textContent = 'Sending…'; btn.disabled = true;
    try {
      const r = await fetch('/api/email/send', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, date }),
      });
      const d = await r.json();
      btn.textContent = d.error ? ('✕ ' + d.error) : ('✓ Sent to ' + d.to);
    } catch (e) { btn.textContent = '✕ Error'; }
    setTimeout(() => { btn.textContent = old; btn.disabled = false; }, 3500);
  }

  // ── Schedule panel ──────────────────────────────────────────────────────────
  async function loadSchedule() {
    const kind = document.getElementById('sched-kind').value;
    try {
      const r = await fetch('/api/email/preferences', { credentials: 'include' });
      const d = await r.json();
      const prefs = d.preferences || [];
      const p = prefs.find(x => x.kind === kind);
      document.getElementById('sched-freq').value = (p && p.enabled) ? p.frequency : 'off';
      if (p) document.getElementById('sched-hour').value = p.hour;
      renderScheduleList(prefs);
    } catch (e) { /* not signed in yet */ }
  }

  function renderScheduleList(prefs) {
    const el = document.getElementById('sched-list');
    const active = (prefs || []).filter(p => p.enabled);
    if (!active.length) { el.innerHTML = '<p class="text-sm" style="color:var(--text-3)">No active schedules.</p>'; return; }
    el.innerHTML = active.map(p =>
      `<div class="text-sm flex items-center justify-between gap-2" style="color:var(--text-2)">
         <span>• ${KINDS[p.kind] || p.kind} — ${p.frequency} at ${String(p.hour).padStart(2, '0')}:00</span>
         <button onclick="deleteSchedule('${p.kind}')" title="Delete schedule"
           class="text-xs px-2 py-0.5 rounded transition hover:opacity-80"
           style="color:var(--text-3);border:1px solid var(--border)">Delete</button>
       </div>`
    ).join('');
  }

  async function deleteSchedule(kind) {
    if (!confirm(`Delete the ${KINDS[kind] || kind} schedule?`)) return;
    const status = document.getElementById('sched-status');
    try {
      const r = await fetch('/api/email/preferences/' + encodeURIComponent(kind), {
        method: 'DELETE', credentials: 'include',
      });
      const d = await r.json();
      status.textContent = d.error ? ('✕ ' + d.error) : '✓ Deleted';
      loadSchedule();
    } catch (e) { status.textContent = '✕ Error'; }
    setTimeout(() => { status.textContent = ''; }, 2500);
  }

  async function saveSchedule() {
    const kind = document.getElementById('sched-kind').value;
    const frequency = document.getElementById('sched-freq').value;
    const hour = parseInt(document.getElementById('sched-hour').value, 10) || 0;
    const btn = document.getElementById('sched-save-btn');
    const old = btn.textContent;
    btn.textContent = 'Saving…'; btn.disabled = true;
    try {
      const r = await fetch('/api/email/preferences', {
        method: 'PUT', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, frequency, hour }),
      });
      const d = await r.json();
      btn.textContent = d.error ? ('✕ ' + d.error) : '✓ Saved';
      loadSchedule();
    } catch (e) { btn.textContent = '✕ Error'; }
    setTimeout(() => { btn.textContent = old; btn.disabled = false; }, 2500);
  }

  // base.html calls this once /api/me resolves (logged-in)
  function onBaseReady() {
    const dateEl = document.getElementById('email-date');
    const today = localToday();
    dateEl.max = today;        // native picker disables future dates
    dateEl.value = today;
    onKindChange();            // sets toggle visibility + loads preview
    loadSchedule();
  }
