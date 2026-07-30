  // Teams / Outlook activity calendar. esc() is global from app.js.
  //
  // One month grid over both connectors. Every row carries a name (or address)
  // and a timestamp — never a subject line or message body. Chat is one row per
  // message, so two texts from the same person hours apart stay two rows.

  const CAL_TYPES = [
    { key: 'received', label: 'Mail received', row: 'Received', css: '--c-received' },
    { key: 'sent',     label: 'Mail sent',     row: 'Sent',     css: '--c-sent'     },
    { key: 'chat',     label: 'Chat',          row: 'Chat',     css: '--c-chat'     },
    { key: 'meeting',  label: 'Meetings',      row: 'Meeting',  css: '--c-meeting'  },
    { key: 'call',     label: 'Calls',         row: 'Call',     css: '--c-call'     },
  ];

  const calActive = new Set(CAL_TYPES.map(t => t.key));
  let calView = null;        // Date pinned to the 1st of the month in view
  let calSelected = null;    // YYYY-MM-DD
  let calPerson = '';        // counterparty id, '' = everyone
  let calNoMatch = false;
  let calMonthData = { days: {}, totals: {}, people: [] };

  const calPad = n => String(n).padStart(2, '0');
  const calIso = d => `${d.getFullYear()}-${calPad(d.getMonth() + 1)}-${calPad(d.getDate())}`;
  const calType = k => CAL_TYPES.find(t => t.key === k);

  function onBaseReady() {
    const today = new Date(); today.setHours(0, 0, 0, 0);
    calView = new Date(today.getFullYear(), today.getMonth(), 1);
    calSelected = calIso(today);

    document.getElementById('cal-chips').innerHTML = CAL_TYPES.map(t => `
      <button class="cal-chip" data-key="${t.key}" aria-pressed="true" style="--cc:var(${t.css})">
        <span class="sw" aria-hidden="true"></span>${t.label}
        <span class="n" data-count="${t.key}"></span>
      </button>`).join('');

    document.getElementById('cal-chips').addEventListener('click', e => {
      const btn = e.target.closest('.cal-chip');
      if (!btn) return;
      const key = btn.dataset.key;
      // Never allow an all-off state — the grid would be blank with no way to read why.
      if (calActive.has(key) && calActive.size === 1) return;
      calActive.has(key) ? calActive.delete(key) : calActive.add(key);
      btn.setAttribute('aria-pressed', String(calActive.has(key)));
      calRenderMonth(); calRenderDay();
    });

    document.getElementById('cal-grid').addEventListener('click', e => {
      const btn = e.target.closest('.cal-day[data-iso]');
      if (!btn || btn.disabled) return;
      calSelected = btn.dataset.iso;
      calRenderMonth(); calLoadDay();
    });

    document.getElementById('cal-prev').addEventListener('click', () => calShift(-1));
    document.getElementById('cal-next').addEventListener('click', () => calShift(1));
    document.getElementById('cal-today').addEventListener('click', () => {
      calView = new Date(today.getFullYear(), today.getMonth(), 1);
      calSelected = calIso(today);
      calLoad();
    });

    calInitMonthPicker(today);

    const input = document.getElementById('cal-person');
    input.addEventListener('input', () => {
      const hit = calMatchPerson(input.value);
      calNoMatch = hit === null;
      calPerson = hit || '';
      calLoad();
    });
    document.getElementById('cal-person-clear').addEventListener('click', () => {
      input.value = ''; calPerson = ''; calNoMatch = false;
      input.focus();
      calLoad();
    });

    calLoad();
  }

  function calShift(months) {
    calView.setMonth(calView.getMonth() + months);
    calLoad();
  }

  /* ── Month + year picker ─────────────────────────────────────────────────
     Jumping to March last year shouldn't mean sixteen clicks on an arrow. */
  const CAL_MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                   'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  let calPickYear = null;   // year shown in the popover, independent of the grid

  function calInitMonthPicker(today) {
    const btn = document.getElementById('cal-month-btn');
    const pop = document.getElementById('cal-month-pop');

    const close = () => { pop.hidden = true; btn.setAttribute('aria-expanded', 'false'); };
    const open = () => {
      calPickYear = calView.getFullYear();
      calRenderPicker(today);
      pop.hidden = false;
      btn.setAttribute('aria-expanded', 'true');
    };

    btn.addEventListener('click', e => {
      e.stopPropagation();
      pop.hidden ? open() : close();
    });
    pop.addEventListener('click', e => e.stopPropagation());
    // Clicking anywhere else, or Escape, dismisses it.
    document.addEventListener('click', () => { if (!pop.hidden) close(); });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && !pop.hidden) { close(); btn.focus(); }
    });

    document.getElementById('cal-year-prev').addEventListener('click', () => {
      calPickYear--; calRenderPicker(today);
    });
    document.getElementById('cal-year-next').addEventListener('click', () => {
      calPickYear++; calRenderPicker(today);
    });

    document.getElementById('cal-month-grid').addEventListener('click', e => {
      const cell = e.target.closest('button[data-m]');
      if (!cell || cell.disabled) return;
      calView = new Date(calPickYear, Number(cell.dataset.m), 1);
      close();
      calLoad();
    });
  }

  function calRenderPicker(today) {
    const thisYear = today.getFullYear(), thisMonth = today.getMonth();
    document.getElementById('cal-year-label').textContent = calPickYear;
    // No point offering a year you can't have any activity in.
    document.getElementById('cal-year-next').disabled = calPickYear >= thisYear;

    document.getElementById('cal-month-grid').innerHTML = CAL_MON.map((name, i) => {
      const future = calPickYear > thisYear || (calPickYear === thisYear && i > thisMonth);
      const current = calPickYear === calView.getFullYear() && i === calView.getMonth();
      return `<button type="button" data-m="${i}" ${future ? 'disabled' : ''}
                      ${current ? 'aria-current="true"' : ''}>${name}</button>`;
    }).join('');
  }

  // '' = everyone, an id = that person, null = typed something matching nobody.
  function calMatchPerson(q) {
    const s = q.trim().toLowerCase();
    if (!s) return '';
    const people = calMonthData.people || [];
    const exact = people.find(p => p.name.toLowerCase() === s);
    if (exact) return exact.id;
    const hits = people.filter(p => p.name.toLowerCase().includes(s));
    // Only narrow once the text points at one person — otherwise "a" would
    // silently pick whoever happens to sort first.
    return hits.length === 1 ? hits[0].id : null;
  }

  async function calLoad() {
    const month = `${calView.getFullYear()}-${calPad(calView.getMonth() + 1)}`;
    const qs = new URLSearchParams({ month });
    if (calPerson) qs.set('person', calPerson);
    try {
      const r = await fetch(`/api/calendar/month?${qs}`);
      calMonthData = r.ok ? await r.json() : { days: {}, totals: {}, people: [] };
    } catch (_) {
      calMonthData = { days: {}, totals: {}, people: [] };
    }
    // Only refresh the datalist when unfiltered — filtering to one person would
    // otherwise shrink the list of people you can switch to.
    if (!calPerson) {
      document.getElementById('cal-people').innerHTML =
        (calMonthData.people || []).map(p => `<option value="${esc(p.name)}"></option>`).join('');
    }
    calRenderMonth();
    calLoadDay();
  }

  function calRenderMonth() {
    const y = calView.getFullYear(), m = calView.getMonth();
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const todayIso = calIso(today);
    const days = calMonthData.days || {};
    const totals = calMonthData.totals || {};

    document.getElementById('cal-month-label').textContent =
      calView.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
    document.getElementById('cal-next').disabled = new Date(y, m + 1, 1) > today;

    const search = document.getElementById('cal-search');
    search.dataset.active = String(!!calPerson);
    search.dataset.nomatch = String(calNoMatch);
    document.getElementById('cal-person-clear').hidden =
      !document.getElementById('cal-person').value;

    // Monday-start offset, matching the backend's weekday() and the week picker.
    const firstDow = (new Date(y, m, 1).getDay() + 6) % 7;
    const lastDate = new Date(y, m + 1, 0).getDate();

    let html = '';
    for (let i = 0; i < firstDow; i++) html += '<div class="cal-day is-empty" aria-hidden="true"></div>';

    for (let d = 1; d <= lastDate; d++) {
      const iso = calIso(new Date(y, m, d));
      const future = iso > todayIso;
      const counts = days[iso] || {};
      const kinds = CAL_TYPES.filter(t => calActive.has(t.key) && counts[t.key]);
      const dots = kinds.map(t => `<span class="cal-dot" style="--dc:var(${t.css})"></span>`).join('');
      const n = kinds.reduce((a, t) => a + counts[t.key], 0);

      const cls = ['cal-day'];
      if (future) cls.push('is-future');
      if (iso === todayIso) cls.push('is-today');
      if (iso === calSelected) cls.push('is-sel');
      if (kinds.length) cls.push('has-data');

      const label = future ? `${iso}, no data yet`
        : `${new Date(y, m, d).toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })}, ${n} item${n === 1 ? '' : 's'}`;

      html += `<button class="${cls.join(' ')}" data-iso="${iso}" ${future ? 'disabled' : ''} aria-label="${esc(label)}">
                 <span class="dnum">${d}</span><span class="cal-dots">${dots}</span>
               </button>`;
    }
    document.getElementById('cal-grid').innerHTML = html;

    document.getElementById('cal-stats').innerHTML = CAL_TYPES.map(t => `
      <div class="card cal-stat p-3" style="--sc:var(${t.css});opacity:${calActive.has(t.key) ? 1 : 0.45}">
        <div class="text-[10.5px] font-semibold uppercase tracking-wider" style="color:var(--text-3)">${t.label}</div>
        <div class="num text-xl font-semibold" style="color:var(--text-1)">${totals[t.key] || 0}</div>
        <div class="text-[11.5px]" style="color:var(--text-3)">${calPerson ? 'with this person' : 'this month'}</div>
      </div>`).join('');

    CAL_TYPES.forEach(t => {
      const el = document.querySelector(`[data-count="${t.key}"]`);
      if (el) el.textContent = totals[t.key] || 0;
    });
  }

  async function calLoadDay() {
    const qs = new URLSearchParams({ date: calSelected });
    if (calPerson) qs.set('person', calPerson);
    try {
      const r = await fetch(`/api/calendar/day?${qs}`);
      calDayItems = r.ok ? (await r.json()).items || [] : [];
    } catch (_) {
      calDayItems = [];
    }
    calRenderDay();
  }

  let calDayItems = [];

  function calLineOf(it) {
    const who = `<b>${esc(it.title)}</b>`;
    switch (it.type) {
      case 'received': return `From ${who}`;
      case 'sent':     return `To ${who}${it.extra ? ` +${it.extra}` : ''}`;
      case 'chat':     return it.from_self ? `You replied to ${who}` : `Message from ${who}`;
      default:         return who;
    }
  }

  const CAL_RSVP = {
    accepted: 'Accepted', organizer: 'Organizer', tentativelyAccepted: 'Tentative',
    declined: 'Declined', notResponded: 'No response', none: '',
  };

  // The invite list. Names only, capped — an all-hands carries hundreds, and an
  // unbounded roster would push the rest of the day off the panel.
  function calRosterOf(it) {
    // Calls carry their duration in the sub-line already; only a group call
    // benefits from naming who else was on it.
    if (it.type === 'call') {
      return (it.roster || []).length > 1
        ? `<span class="ctx">${it.roster.slice(0, 4).map(esc).join(' · ')}</span>` : '';
    }
    if (it.type !== 'meeting' || !(it.roster || []).length) return '';
    const shown = it.roster.slice(0, 4).map(esc).join(' · ');
    const rest = it.roster.length - 4;
    return `<span class="ctx">${shown}${rest > 0 ? ` +${rest}` : ''}</span>`;
  }

  function calSubOf(it) {
    if (it.type === 'call') {
      // Duration of the call itself. Whether YOU were on for all of it needs
      // sessions($expand=segments) — participants_v2 has no per-person times.
      const others = (it.roster || []).length;
      return `<span class="ctx">${it.minutes} min${others > 1 ? ` · ${others} on the call` : ''}</span>`;
    }
    if (it.type !== 'meeting') return it.context ? `<span class="ctx">${esc(it.context)}</span>` : '';
    const n = (it.roster || []).length;
    // Scheduled duration, not attended — proving anyone joined needs the call
    // record, which is a separate (application-only) connector.
    const bits = [`${it.minutes} min`];
    if (n) bits.push(`${n} invited`);
    return `<span class="ctx">${bits.join(' · ')}</span>`;
  }

  function calRenderDay() {
    const [y, m, d] = calSelected.split('-').map(Number);
    document.getElementById('cal-day-title').textContent =
      new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' });

    const items = calDayItems.filter(it => calActive.has(it.type));
    document.getElementById('cal-day-meta').textContent =
      items.length ? `${items.length} item${items.length === 1 ? '' : 's'}` : '';

    const body = document.getElementById('cal-day');
    if (!items.length) {
      const hint = calPerson ? 'No activity with this person. Clear the With box above.'
        : calActive.size < CAL_TYPES.length ? 'Some types are filtered out — switch them back on above.'
        : 'Nothing recorded from Teams or Outlook.';
      body.innerHTML = `<div class="cal-empty">Nothing on this day<div class="hint">${hint}</div></div>`;
      return;
    }

    body.innerHTML = items.map(it => {
      const t = calType(it.type);
      const rsvp = CAL_RSVP[it.rsvp] || '';
      const declined = it.rsvp === 'declined';
      return `<div class="cal-row${it.from_self ? ' is-self' : ''}${declined ? ' is-declined' : ''}"
                   style="--rc:var(${t.css})">
        <span class="t">${esc(it.time)}</span>
        <span>
          <span class="kind">${t.row}</span>${rsvp ? `<span class="cal-rsvp" data-s="${it.rsvp}">${rsvp}</span>` : ''}
          <span class="line">${calLineOf(it)}</span>
          ${calSubOf(it)}
          ${calRosterOf(it)}
        </span>
      </div>`;
    }).join('');
  }
