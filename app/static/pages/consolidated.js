  // Consolidated report page. esc() and srcOf() are globals from app.js.
  let crStart = null, crEnd = null, crLast = null;
  // What this caller may actually ask for — fetched, never assumed, so the controls
  // offer exactly what the API would accept.
  let crScope = { actor_id: null, chips: [], detail: false, device: false, people: [] };
  let crWho = null;                 // {id,email} being reported on; null = self
  const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const CHIP_LABEL = { github:'GitHub', gitlab:'GitLab', jira:'Jira', teams:'Teams', outlook:'Outlook' };
  const crIso = d => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const crToday = () => crIso(new Date());
  const crDaysAgo = n => { const d = new Date(); d.setDate(d.getDate() - n); return crIso(d); };
  const crFmt = s => { const [y, m, d] = s.split('-').map(Number); return `${MON[m - 1]} ${d}`; };

  async function onBaseReady() {
    const from = document.getElementById('cr-from');
    const to   = document.getElementById('cr-to');

    document.getElementById('cr-presets').addEventListener('click', e => {
      const b = e.target.closest('button'); if (!b) return;
      [...e.currentTarget.children].forEach(x => x.setAttribute('aria-pressed', 'false'));
      b.setAttribute('aria-pressed', 'true');
      const p = b.dataset.preset;
      const custom = document.getElementById('cr-custom');
      if (p === 'custom') {
        custom.style.display = 'block';
        from.value = crStart || ''; to.value = crEnd || '';
        return;
      }
      custom.style.display = 'none';
      crApplyPreset(p); crUpdateLabel();
    });

    // Native date inputs are the range picker — max="today" keeps the report on
    // dates that can actually have activity.
    from.max = to.max = crToday();
    from.addEventListener('change', () => { crStart = from.value || null; crUpdateLabel(); });
    to.addEventListener('change',   () => { crEnd   = to.value   || null; crUpdateLabel(); });
    document.addEventListener('click', e => {
      if (!e.target.closest('.cr-people')) document.getElementById('cr-person-drop').classList.remove('show');
    });

    crApplyPreset('7');
    crUpdateLabel();
    await crLoadScope();
  }

  async function crLoadScope() {
    try {
      const r = await fetch('/api/report/consolidated/scope', { credentials: 'include' });
      if (r.ok) crScope = await r.json();
    } catch (e) { /* controls fall back to the base set; the server still clamps */ }
    if (!crScope.chips || !crScope.chips.length) crScope.chips = ['github', 'gitlab', 'jira'];
    crRenderControls();
  }

  function crRenderControls() {
    document.getElementById('cr-sources').innerHTML = crScope.chips.map(c =>
      `<label class="cr-chip"><input type="checkbox" value="${c}" checked> ${esc(CHIP_LABEL[c] || c)}</label>`
    ).join('');

    const locked = ['teams', 'outlook'].filter(c => !crScope.chips.includes(c));
    document.getElementById('cr-sources-hint').textContent = locked.length
      ? `${locked.map(c => CHIP_LABEL[c]).join(' and ')} need a permission an admin turns on.`
      : 'Teams covers chats and calls, Outlook covers mail and meetings.';

    // Depth controls only mean something when the caller can see event names.
    const canNarrate = crScope.detail;
    document.getElementById('cr-depth-block').style.display  = canNarrate ? '' : 'none';
    document.getElementById('cr-prompt-block').style.display = canNarrate ? '' : 'none';

    const others = (crScope.people || []).filter(p => p.id !== crScope.actor_id);
    if (others.length) {
      document.getElementById('cr-person-block').style.display = '';
      document.getElementById('cr-person-hint').textContent = others.length === 1
        ? 'Leave blank for your own activity, or pick your one report.'
        : `Leave blank for your own activity, or pick any of your ${others.length} people.`;
    }
  }

  function crApplyPreset(p) {
    crEnd = crToday();
    if (p === 'thismonth') { const d = new Date(); crStart = crIso(new Date(d.getFullYear(), d.getMonth(), 1)); }
    else crStart = crDaysAgo(+p - 1);
  }
  function crUpdateLabel() {
    const el = document.getElementById('cr-range-label');
    if (crStart && crEnd) el.textContent = `${crFmt(crStart)} → ${crFmt(crEnd)}`;
    else if (crStart) el.textContent = `${crFmt(crStart)} → pick an end date`;
    else el.textContent = '';
  }

  // ── person picker ───────────────────────────────────────────────────────────
  function crPersonSearch(q) {
    const drop = document.getElementById('cr-person-drop'); if (!drop) return;
    q = (q || '').trim().toLowerCase();
    if (!q) crWho = null;   // cleared the box = back to your own activity
    const matches = (crScope.people || []).filter(p => p.email.toLowerCase().includes(q));
    drop.innerHTML = matches.length
      ? matches.map(p => `<div class="cr-opt" onclick="crPickPerson('${p.id}')">
           <span>${esc(p.email)}${p.id === crScope.actor_id ? ' (you)' : ''}</span>
           <span class="r">${esc(p.role)}</span></div>`).join('')
      : '<div class="cr-opt" style="cursor:default;color:var(--text-3)">No matches</div>';
    drop.classList.add('show');
  }

  function crPickPerson(id) {
    const p = (crScope.people || []).find(x => x.id === id);
    crWho = (!p || p.id === crScope.actor_id) ? null : { id: p.id, email: p.email };
    document.getElementById('cr-person').value = p ? p.email : '';
    document.getElementById('cr-person-drop').classList.remove('show');
  }

  // ── run ─────────────────────────────────────────────────────────────────────
  async function generateReport() {
    const status = document.getElementById('cr-status');
    if (!crStart || !crEnd) { status.textContent = 'Pick a date range.'; return; }

    const sources = [...document.querySelectorAll('#cr-sources input:checked')].map(c => c.value);
    const depth   = document.querySelector('input[name="cr-detail"]:checked');
    const promptEl = document.getElementById('cr-prompt');

    const btn = document.getElementById('cr-generate');
    btn.disabled = true;
    status.textContent = 'Generating… (this can take a few seconds)';
    try {
      const r = await fetch('/api/report/consolidated', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          start: crStart, end: crEnd, sources,
          user_id: crWho ? crWho.id : null,
          detail: depth ? depth.value : 'brief',
          prompt: (crScope.detail && promptEl.value.trim()) || null,
        }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.error) { status.textContent = '✕ ' + (d.error || d.detail || r.status); return; }
      crLast = d;
      renderResult(d);
      document.getElementById('cr-download').disabled = false;
      status.textContent = '';
    } catch (e) {
      status.textContent = '✕ Error — try a smaller range.';
    } finally {
      btn.disabled = false;
    }
  }

  const BUCKET_HEAD = { day: 'Day', week: 'Week', month: 'Month' };
  const fmtMins = m => m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m` : `${m}m`;

  function renderResult(d) {
    document.getElementById('cr-result-range').textContent = `${d.start} → ${d.end}`;
    document.getElementById('cr-result-who').textContent =
      d.is_self ? 'Your activity' : (crWho ? crWho.email : 'Activity');
    document.getElementById('cr-result-bucket').textContent =
      `Grouped by ${d.bucket} · ${(d.buckets || []).length} rows`;

    const kpis = [`<span class="cr-kpi"><b>${d.total}</b><span>total events</span></span>`]
      .concat(Object.entries(d.by_source || {}).sort()
        .map(([k, v]) => `<span class="cr-kpi"><b>${v}</b><span>${esc(srcOf(k).label)}</span></span>`));
    document.getElementById('cr-kpis').innerHTML = kpis.join('');

    // Counts-only is a different report, not a greyed-out one — say so plainly.
    const lock = document.getElementById('cr-lock');
    if (!d.detail) {
      lock.style.display = '';
      lock.innerHTML = '<b>Counts only</b><span>Event names and the written summary are switched off for '
        + 'your account. Ask an admin to enable "See event names".</span>';
    } else { lock.style.display = 'none'; }

    const summary = document.getElementById('cr-summary');
    if (d.total === 0) {
      summary.innerHTML = '<span style="color:var(--text-3)">No activity in this range for the selected connectors.</span>';
    } else if (!d.detail) {
      summary.textContent = '';
    } else {
      summary.textContent = d.summary || '(no summary generated)';
      if (d.truncated) {
        summary.innerHTML += '<p class="cr-note" style="margin-top:12px">'
          + 'Summary based on the most recent 200 events in this range.</p>';
      }
    }

    renderTable(d);
    document.getElementById('cr-result').style.display = '';
  }

  function renderTable(d) {
    const cols = Object.keys(d.by_source || {}).sort();
    const buckets = d.buckets || [];
    const thead = document.getElementById('cr-thead');
    const tbody = document.getElementById('cr-tbody');

    if (!buckets.length || !d.total) { thead.innerHTML = ''; tbody.innerHTML = ''; return; }

    thead.innerHTML = '<tr><th>' + BUCKET_HEAD[d.bucket] + '</th>'
      + cols.map(c => `<th class="n">${esc(srcOf(c).label)}</th>`).join('')
      + (d.device ? '<th class="n cr-sep">Focus</th>' : '')
      + '<th class="n">Total</th></tr>';

    const totals = {};
    cols.forEach(c => { totals[c] = buckets.reduce((a, b) => a + (b.counts[c] || 0), 0); });
    const totalMins = buckets.reduce((a, b) => a + (b.device_minutes || 0), 0);

    tbody.innerHTML = buckets.map(b =>
      `<tr><td>${esc(b.label)}</td>`
      + cols.map(c => {
          const v = b.counts[c] || 0;
          return `<td class="n${v ? '' : ' z'}">${v}</td>`;
        }).join('')
      + (d.device ? `<td class="n cr-sep${b.device_minutes ? '' : ' z'}">${fmtMins(b.device_minutes || 0)}</td>` : '')
      + `<td class="n b">${b.total}</td></tr>`
    ).join('')
    + `<tr class="tot"><td>Total</td>`
      + cols.map(c => `<td class="n">${totals[c]}</td>`).join('')
      + (d.device ? `<td class="n cr-sep">${fmtMins(totalMins)}</td>` : '')
      + `<td class="n">${d.total}</td></tr>`;
  }

  // Download as PDF. The server rebuilds the report rather than taking the rendered
  // page back, so the document can't disagree with what the permissions allow.
  function downloadReport() {
    if (!crLast) return;
    const depth = document.querySelector('input[name="cr-detail"]:checked');
    const promptEl = document.getElementById('cr-prompt');
    const q = new URLSearchParams({
      start: crLast.start, end: crLast.end,
      sources: [...document.querySelectorAll('#cr-sources input:checked')].map(c => c.value).join(','),
      depth: depth ? depth.value : 'brief',
    });
    if (crWho) q.set('user_id', crWho.id);
    if (crScope.detail && promptEl.value.trim()) q.set('prompt', promptEl.value.trim());
    window.location = '/api/report/consolidated.pdf?' + q.toString();
  }
