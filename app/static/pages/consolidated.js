  // Consolidated report page. esc() is global from app.js.
  let crStart = null, crEnd = null, crCal = null, crLast = null;
  const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const crIso = d => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const crToday = () => crIso(new Date());
  const crDaysAgo = n => { const d = new Date(); d.setDate(d.getDate() - n); return crIso(d); };
  const crFmt = s => { const [y, m, d] = s.split('-').map(Number); return `${MON[m - 1]} ${d}`; };

  function onBaseReady() {
    crCal = RangeCalendar(document.getElementById('cr-cal'), {
      onChange: (s, e) => { crStart = s; crEnd = e; crUpdateLabel(); },
    });
    document.getElementById('cr-presets').addEventListener('click', e => {
      const b = e.target.closest('button'); if (!b) return;
      [...e.currentTarget.children].forEach(x => x.setAttribute('aria-pressed', 'false'));
      b.setAttribute('aria-pressed', 'true');
      const p = b.dataset.preset;
      const custom = document.getElementById('cr-custom');
      if (p === 'custom') { custom.style.display = 'block'; crCal.setRange(crStart, crEnd); return; }
      custom.style.display = 'none';
      crApplyPreset(p); crUpdateLabel();
    });
    crApplyPreset('7');
    crUpdateLabel();
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

  async function generateReport() {
    const status = document.getElementById('cr-status');
    if (!crStart || !crEnd) { status.textContent = 'Pick a date range.'; return; }

    const sources = [...document.querySelectorAll('#cr-sources input:checked')].map(c => c.value);
    const detail  = document.querySelector('input[name="cr-detail"]:checked').value;
    const prompt  = document.getElementById('cr-prompt').value.trim() || null;

    const btn = document.getElementById('cr-generate');
    btn.disabled = true;
    status.textContent = 'Generating… (this can take a few seconds)';
    try {
      const r = await fetch('/api/report/consolidated', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start: crStart, end: crEnd, sources, detail, prompt }),
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

  function renderResult(d) {
    const box = document.getElementById('cr-result');
    document.getElementById('cr-result-range').textContent = `${d.start} → ${d.end}`;

    const kpis = [`<span class="cr-kpi"><b>${d.total}</b><span>total events</span></span>`]
      .concat(Object.entries(d.by_source || {}).sort()
        .map(([k, v]) => `<span class="cr-kpi"><b>${v}</b><span>${esc(k)}</span></span>`));
    document.getElementById('cr-kpis').innerHTML = kpis.join('');

    const summary = document.getElementById('cr-summary');
    if (d.total === 0) {
      summary.innerHTML = '<span style="color:var(--text-3)">No activity in this range for the selected connectors.</span>';
    } else {
      summary.textContent = d.summary || '(no summary generated)';
      if (d.truncated) {
        summary.innerHTML += '<p style="color:var(--text-3);font-size:12px;margin-top:12px">'
          + 'Summary based on the most recent 200 events in this range.</p>';
      }
    }
    box.style.display = '';
  }

  // Download the summary as a plain-text file (no server round-trip).
  function downloadReport() {
    if (!crLast) return;
    const d = crLast;
    const bySrc = Object.entries(d.by_source || {}).sort().map(([k, v]) => `  ${k}: ${v}`).join('\n');
    const text = `Consolidated report\n${d.start} to ${d.end}\n\n`
      + `Total events: ${d.total}\n${bySrc}\n\n`
      + `Summary\n-------\n${d.summary || '(none)'}\n`;
    const blob = new Blob([text], { type: 'text/plain' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `consolidated-${d.start}_${d.end}.txt`;
    a.click();
    URL.revokeObjectURL(a.href);
  }
