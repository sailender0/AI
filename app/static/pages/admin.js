  // CAN_EDIT is declared by the inline shim in admin.html (server-rendered).
  let USERS = [], ALL_PERMS = [], ROLES = [];
  let expandedId = null;

  // Role → accent (violet / amber / slate), tuned for the near-black surface.
  const ROLE_META = {
    admin:      { color:'#a78bfa', bg:'rgba(167,139,250,0.13)' },
    supervisor: { color:'#fbbf24', bg:'rgba(251,191,36,0.13)' },
    user:       { color:'#8b8b9e', bg:'rgba(255,255,255,0.06)' },
  };
  const meta = r => ROLE_META[r] || ROLE_META.user;

  // esc() is a global from app.js.
  function repToday() {
    const d = new Date();
    return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  }
  function repMonday(dateStr) {
    const d = new Date(dateStr + 'T00:00:00');
    const dow = (d.getDay() + 6) % 7;            // Mon=0
    d.setDate(d.getDate() - dow);
    return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  }
  function initials(email) {
    return (email || '?').replace(/[^a-zA-Z]/g, '').slice(0, 2).toUpperCase() || '?';
  }

  async function loadUsers() {
    const st = document.getElementById('users-status');
    st.textContent = 'Loading…';
    try {
      const r = await fetch('/api/admin/users', { credentials: 'include' });
      if (!r.ok) { st.textContent = '✕ ' + r.status; return; }
      const d = await r.json();
      USERS = d.users; ALL_PERMS = d.all_permissions; ROLES = d.roles;
      renderStats();
      renderRows();
      st.textContent = '';
    } catch (e) { st.textContent = '✕ Error'; }
  }

  function renderStats() {
    document.getElementById('stat-total').textContent = USERS.length;
    document.getElementById('stat-sup').textContent   = USERS.filter(u => u.role === 'supervisor').length;
    document.getElementById('stat-admin').textContent = USERS.filter(u => u.role === 'admin').length;
  }

  function rowHtml(u) {
    const m = meta(u.role);
    const elevated = u.role !== 'user';
    const dis = (!CAN_EDIT || elevated) ? 'disabled' : '';
    const count = ALL_PERMS.filter(p => u.permissions.includes(p)).length;

    const roleCtl = CAN_EDIT
      ? `<select class="rep-select" style="width:auto;padding:4px 8px;font-size:12px"
           onchange="setRole('${u.id}', this.value)" onclick="event.stopPropagation()">
           ${ROLES.map(r => `<option value="${r}" ${r === u.role ? 'selected' : ''}>${r}</option>`).join('')}
         </select>`
      : `<span class="u-badge">${esc(u.role)}</span>`;

    // Recipient options: "Me" (admin, default) then every app user.
    const recipients = `<option value="">Me (${esc(window._email || 'admin')})</option>`
      + USERS.map(o => `<option value="${o.id}">${esc(o.email)}</option>`).join('');

    const switches = ALL_PERMS.map(p => `
      <div class="perm-row">
        <span class="perm-name">${p}</span>
        <label class="sw">
          <input type="checkbox" ${u.permissions.includes(p) ? 'checked' : ''} ${dis}
            onchange="togglePerm('${u.id}', this)">
          <i></i>
        </label>
      </div>`).join('');

    return `
      <div class="urow" id="row-${u.id}" data-id="${u.id}" data-email="${esc(u.email).toLowerCase()}" data-role="${u.role}"
           style="--rail:${m.color};--rail-bg:${m.bg}">
        <button type="button" class="urow-head" aria-expanded="false" aria-controls="insp-${u.id}"
                onclick="toggleRow('${u.id}')">
          <span class="u-avatar">${initials(u.email)}</span>
          <span class="u-email">${esc(u.email)}</span>
          <span class="u-badge">${esc(u.role)}</span>
          <span class="u-count" title="permissions granted">${elevated ? 'all' : count + '/' + ALL_PERMS.length}</span>
          <svg class="u-caret w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7"/>
          </svg>
        </button>

        <div class="u-insp" id="insp-${u.id}">
          <div class="u-insp-grid">
            <!-- ACCESS -->
            <div>
              <div class="u-sec-label">Access</div>
              <div class="rep-field">
                <label>Role</label>
                ${roleCtl}
              </div>
              <div id="perms-${u.id}">${switches}</div>
              ${elevated ? `<p class="mt-2" style="font-size:11px;color:var(--text-3)">Supervisors &amp; admins hold every permission — toggles apply to the <b>user</b> role.</p>` : ''}
              ${(CAN_EDIT && u.id !== window._profileId) ? `
                <div class="mt-4 pt-3" style="border-top:1px solid var(--border)">
                  <div class="u-sec-label" style="color:var(--negative)">Danger zone</div>
                  <button class="btn btn-danger" onclick='deleteUser(${JSON.stringify(u.id)}, ${JSON.stringify(u.email)})'>Delete user</button>
                  <p class="mt-2" style="font-size:11px;color:var(--text-3)">Removes the account, integrations, chats, devices and all activity data. Cannot be undone.</p>
                </div>` : ''}
            </div>

            <!-- REPORT -->
            <div>
              <div class="flex items-center justify-between">
                <div class="u-sec-label">Report</div>
                <span class="rep-status text-xs" style="color:var(--text-3)"></span>
              </div>
              <div class="flex gap-2">
                <div class="rep-field" style="flex:1">
                  <label>Report</label>
                  <select class="rep-select rep-kind" onchange="loadReport('${u.id}')">
                    <option value="my_day">My Day</option>
                    <option value="analytics">Analytics (week)</option>
                  </select>
                </div>
                <div class="rep-field" style="flex:1">
                  <label>Date</label>
                  <input class="rep-datein rep-date" type="date" onchange="loadReport('${u.id}')">
                </div>
              </div>
              <iframe class="rep-preview" sandbox=""></iframe>
              <div class="rep-field mt-3">
                <label>Email recipient</label>
                <select class="rep-select rep-to">${recipients}</select>
              </div>
              <div class="flex items-center gap-2 mt-1">
                <button class="btn btn-ghost rep-dl" onclick="downloadReport('${u.id}')">Download PDF</button>
                <button class="btn btn-solid rep-email" onclick="emailReport('${u.id}')">Send report</button>
              </div>
            </div>
          </div>
        </div>
      </div>`;
  }

  function renderRows() {
    const el = document.getElementById('user-list');
    el.innerHTML = USERS.map(rowHtml).join('') || '<p class="text-sm" style="color:var(--text-3)">No users.</p>';
    applyFilter();
  }

  // ── Filter (visibility only — never rebuilds, so open state survives) ──────────
  function applyFilter() {
    const q = (document.getElementById('u-search').value || '').toLowerCase().trim();
    const rf = document.getElementById('u-role-filter').value;
    let shown = 0;
    document.querySelectorAll('.urow').forEach(row => {
      const okQ = !q || row.dataset.email.includes(q);
      const okR = !rf || row.dataset.role === rf;
      const vis = okQ && okR;
      row.style.display = vis ? '' : 'none';
      if (vis) shown++;
    });
    document.getElementById('no-match').style.display = (USERS.length && !shown) ? 'block' : 'none';
  }

  // ── Expand / collapse ─────────────────────────────────────────────────────────
  const insp = id => document.getElementById('insp-' + id);
  const row  = id => document.getElementById('row-' + id);

  function toggleRow(id) {
    if (expandedId === id) { collapse(id); return; }
    if (expandedId) collapse(expandedId);
    expandedId = id;
    row(id).classList.add('open');
    row(id).querySelector('.urow-head').setAttribute('aria-expanded', 'true');
    const dt = insp(id).querySelector('.rep-date');
    dt.max = repToday();
    if (!dt.value) dt.value = repToday();
    loadReport(id);
  }
  function collapse(id) {
    const r = row(id); if (!r) return;
    r.classList.remove('open');
    r.querySelector('.urow-head').setAttribute('aria-expanded', 'false');
    if (expandedId === id) expandedId = null;
  }

  // ── Role & permissions (in-place updates, no rebuild) ─────────────────────────
  async function setRole(id, role) {
    const r = await fetch(`/api/admin/users/${id}/role`, {
      method: 'PATCH', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { alert(d.error || d.detail || 'Failed'); return; }
    const u = USERS.find(x => x.id === id); if (u) u.role = role;
    patchRow(id);
    renderStats();
  }

  // Re-skin one row after a role change: rail color, badges, and switch availability.
  function patchRow(id) {
    const u = USERS.find(x => x.id === id); if (!u) return;
    const m = meta(u.role), elevated = u.role !== 'user';
    const r = row(id);
    r.dataset.role = u.role;
    r.style.setProperty('--rail', m.color);
    r.style.setProperty('--rail-bg', m.bg);
    r.querySelectorAll('.u-badge').forEach(b => b.textContent = u.role);
    r.querySelector('.u-count').textContent = elevated ? 'all' : `${u.permissions.filter(p => ALL_PERMS.includes(p)).length}/${ALL_PERMS.length}`;
    r.querySelectorAll(`#perms-${id} input[type=checkbox]`).forEach(cb => { cb.disabled = !CAN_EDIT || elevated; });
    applyFilter();
  }

  function togglePerm(id, cb) {
    const boxes = document.querySelectorAll(`#perms-${id} input[type=checkbox]`);
    const perms = [];
    boxes.forEach((b, i) => { if (b.checked) perms.push(ALL_PERMS[i]); });
    fetch(`/api/admin/users/${id}/permissions`, {
      method: 'PATCH', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ permissions: perms }),
    }).then(async r => {
      if (!r.ok) { const d = await r.json().catch(() => ({})); alert(d.error || d.detail); cb.checked = !cb.checked; return; }
      const u = USERS.find(x => x.id === id);
      if (u) { u.permissions = perms; row(id).querySelector('.u-count').textContent = `${perms.length}/${ALL_PERMS.length}`; }
    });
  }

  async function deleteUser(id, email) {
    // Irreversible: native confirm is the guard, the server re-checks admin + self.
    if (!confirm(`Delete ${email}?\n\nThis permanently removes their account, integrations, `
      + `chats, devices and all activity data. This cannot be undone.`)) return;
    const r = await fetch(`/api/admin/users/${id}`, { method: 'DELETE', credentials: 'include' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { alert(d.error || d.detail || 'Delete failed'); return; }
    if (expandedId === id) expandedId = null;
    USERS = USERS.filter(u => u.id !== id);
    renderStats();
    renderRows();
  }

  // ── Report (scoped to the expanded row) ───────────────────────────────────────
  async function loadReport(id) {
    const box = insp(id);
    const kind = box.querySelector('.rep-kind').value;
    const date = box.querySelector('.rep-date').value || null;
    const st = box.querySelector('.rep-status');
    st.textContent = 'Generating…';
    try {
      const r = await fetch('/api/email/preview', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, date, user_id: id }),
      });
      const d = await r.json();
      if (!r.ok || d.error) { st.textContent = '✕ ' + (d.error || d.detail || r.status); return; }
      box.querySelector('.rep-preview').srcdoc = d.html || '';
      st.textContent = '';
    } catch (e) { st.textContent = '✕ Error'; }
  }

  function downloadReport(id) {
    const box = insp(id);
    const kind = box.querySelector('.rep-kind').value;
    const date = box.querySelector('.rep-date').value;
    const uid = encodeURIComponent(id);
    window.location.href = kind === 'analytics'
      ? `/api/export/weekly-pdf?week_start=${repMonday(date)}&user_id=${uid}`
      : `/api/export/daily-pdf?date=${date}&user_id=${uid}`;
  }

  async function emailReport(id) {
    const box = insp(id);
    const kind = box.querySelector('.rep-kind').value;
    const date = box.querySelector('.rep-date').value || null;
    const to_user_id = box.querySelector('.rep-to').value || null;   // "" = me (admin)
    const btn = box.querySelector('.rep-email');
    const old = btn.textContent;
    btn.textContent = 'Sending…'; btn.disabled = true;
    try {
      const r = await fetch('/api/email/send', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, date, user_id: id, to_user_id }),
      });
      const d = await r.json().catch(() => ({}));
      btn.textContent = (!r.ok || d.error) ? ('✕ ' + (d.error || d.detail)) : ('✓ Sent to ' + d.to);
    } catch (e) { btn.textContent = '✕ Error'; }
    setTimeout(() => { btn.textContent = old; btn.disabled = false; }, 3500);
  }

  function onBaseReady() { loadUsers(); }
