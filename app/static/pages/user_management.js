  // CAN_EDIT is declared by the inline shim in user_management.html (server-rendered).
  let USERS = [], ALL_PERMS = [], ROLES = [], MANAGERS = [], ASSIGNABLE = [];
  let ACTOR_ID = null;
  let expandedId = null;

  // Role → accent (violet / amber / slate), tuned for the near-black surface.
  const ROLE_META = {
    admin:   { color:'#a78bfa', bg:'rgba(167,139,250,0.13)' },
    manager: { color:'#fbbf24', bg:'rgba(251,191,36,0.13)' },
    user:    { color:'#8b8b9e', bg:'rgba(255,255,255,0.06)' },
  };
  const meta = r => ROLE_META[r] || ROLE_META.user;

  // Human labels for permission keys (fallback to the raw key if unmapped).
  const PERM_LABELS = {
    email_report:     'Email Report page',
    export_my_day:    'Download My Day report',
    export_analytics: 'Download Analytics report',
    email_ai_answer:  'Email AI answers',
    consolidated_report: 'Consolidated report',
    attendance_report: 'Attendance report',
  };
  const permLabel = p => PERM_LABELS[p] || p;

  // esc() is a global from app.js.
  function initials(email) {
    return (email || '?').replace(/[^a-zA-Z]/g, '').slice(0, 2).toUpperCase() || '?';
  }

  async function loadUsers() {
    const st = document.getElementById('users-status');
    st.textContent = 'Loading…';
    try {
      const r = await fetch('/api/user-management/users', { credentials: 'include' });
      if (!r.ok) { st.textContent = '✕ ' + r.status; return; }
      const d = await r.json();
      USERS = d.users; ALL_PERMS = d.all_permissions; ROLES = d.roles;
      ASSIGNABLE = d.assignable || d.all_permissions;
      MANAGERS = d.managers || []; ACTOR_ID = d.actor_id || null;
      renderStats();
      renderBulkPanel();
      renderMgrPermsPanel();
      renderRows();
      st.textContent = '';
    } catch (e) { st.textContent = '✕ Error'; }
  }

  function renderStats() {
    document.getElementById('stat-total').textContent = USERS.length;
    document.getElementById('stat-sup').textContent   = USERS.filter(u => u.role === 'manager').length;
    document.getElementById('stat-admin').textContent = USERS.filter(u => u.role === 'admin').length;
  }

  function rowHtml(u) {
    const m = meta(u.role);
    const elevated = u.role !== 'user';
    const isManager = u.role === 'manager';
    const count = ALL_PERMS.filter(p => u.permissions.includes(p)).length;

    const roleCtl = CAN_EDIT
      ? `<select class="rep-select" style="width:auto;padding:4px 8px;font-size:12px"
           onchange="setRole('${u.id}', this.value)" onclick="event.stopPropagation()">
           ${ROLES.map(r => `<option value="${r}" ${r === u.role ? 'selected' : ''}>${r}</option>`).join('')}
         </select>`
      : `<span class="u-badge">${esc(u.role)}</span>`;

    // Toggles bind to OWN permissions (for a manager row, the team template). When the
    // actor is editing a report, permissions they can't assign are HIDDEN entirely —
    // only an admin (ASSIGNABLE = all) sees the full list. Read-only rows show all.
    const visible = ALL_PERMS.filter(p => !(u.can_edit_perms && !ASSIGNABLE.includes(p)));
    const switches = visible.map(p => {
      const editable = u.can_edit_perms && ASSIGNABLE.includes(p);
      return `
      <div class="perm-row">
        <span class="perm-name">${permLabel(p)}</span>
        <label class="sw">
          <input type="checkbox" data-perm="${p}" ${u.own_permissions.includes(p) ? 'checked' : ''} ${editable ? '' : 'disabled'}
            onchange="markPermsDirty('${u.id}')">
          <i></i>
        </label>
      </div>`;
    }).join('') || '<p style="font-size:12px;color:var(--text-3)">No permissions you can assign — ask an admin to enable some under Manager permissions.</p>';

    // Manager assignment — admin only, for plain-user rows.
    const managerCtl = (CAN_EDIT && u.role === 'user') ? `
      <div class="rep-field mt-3">
        <label>Reports to</label>
        <select class="rep-select" onchange="setManager('${u.id}', this.value)">
          <option value="">— No manager —</option>
          ${MANAGERS.map(mg => `<option value="${mg.id}" ${mg.id === u.manager_id ? 'selected' : ''}>${esc(mg.email)}</option>`).join('')}
        </select>
      </div>` : '';

    // Team — the manager's direct reports, with add/remove (admin only, manager rows).
    const teamCtl = (CAN_EDIT && isManager) ? `
      <div class="mt-4 pt-3" style="border-top:1px solid var(--border)">
        <div class="u-sec-label">Team — people reporting to this manager</div>
        <div id="team-${u.id}" class="flex flex-wrap gap-2 mt-1">${teamMembersHtml(u.id)}</div>
        <div style="position:relative;max-width:340px;margin-top:8px">
          <input id="team-search-${u.id}" class="rep-select" type="text" placeholder="Search a user to add…"
                 autocomplete="off" oninput="teamSearch('${u.id}', this.value)" onfocus="teamSearch('${u.id}', this.value)">
          <div id="team-drop-${u.id}" style="position:absolute;z-index:20;top:calc(100% + 4px);left:0;right:0;max-height:200px;
               overflow:auto;padding:5px;display:none;background:var(--surface);border:1px solid var(--border-strong);border-radius:9px"></div>
        </div>
        <p style="font-size:11px;color:var(--text-3);margin:10px 0 6px">Apply the permission toggles above to this whole team (grant keeps their extras; revoke strips them).</p>
        <div class="flex items-center gap-2">
          <button class="btn btn-solid" onclick="applyTeam('${u.id}','grant')">Apply to team</button>
          <button class="btn btn-ghost" onclick="applyTeam('${u.id}','revoke')">Revoke from team</button>
          <span class="team-status text-xs" style="color:var(--text-3)"></span>
        </div>
      </div>` : '';

    // Can-assign-to-team allow-list — admin only, on a manager row (same list as the
    // Manager permissions tab; both write assignable_perms).
    const assignableCtl = (CAN_EDIT && isManager) ? `
      <div class="mt-4 pt-3" style="border-top:1px solid var(--border)">
        <div class="u-sec-label">Can assign to team — permissions this manager may grant their reports</div>
        <div id="asg-${u.id}">${assignableSwitches(u)}</div>
        <div class="flex items-center gap-2 mt-2">
          <button class="btn btn-solid" onclick="saveAssignable('${u.id}','asg-${u.id}','asgst-${u.id}')">Save</button>
          <span class="text-xs" id="asgst-${u.id}" style="color:var(--text-3)"></span>
        </div>
      </div>` : '';

    return `
      <div class="urow" id="row-${u.id}" data-id="${u.id}" data-email="${esc(u.email).toLowerCase()}" data-role="${u.role}"
           style="--rail:${m.color};--rail-bg:${m.bg}">
        <button type="button" class="urow-head" aria-expanded="false" aria-controls="insp-${u.id}"
                onclick="toggleRow('${u.id}')">
          <span class="u-avatar">${initials(u.email)}</span>
          <span class="u-email">${esc(u.email)}</span>
          <span class="u-badge">${esc(u.role)}</span>
          <span class="u-count" title="permissions granted">${u.role === 'admin' ? 'all' : count + '/' + ALL_PERMS.length}</span>
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
              ${isManager ? `<p class="mt-2" style="font-size:11px;color:var(--text-3)">These are this manager's <b>own permissions</b> — and the <b>team template</b> copied onto their reports. An admin can restrict a manager here; team powers (seeing/editing their reports) stay regardless.</p>`
                : (u.role === 'admin' ? `<p class="mt-2" style="font-size:11px;color:var(--text-3)">Admins hold every permission.</p>` : '')}
              ${u.can_edit_perms ? `
                <div class="flex items-center gap-2 mt-3">
                  <button class="btn btn-solid perm-save" onclick="savePerms('${u.id}')" disabled>Save permissions</button>
                  <span class="perm-status text-xs" style="color:var(--text-3)"></span>
                </div>` : ''}
              ${managerCtl}
              ${assignableCtl}
              ${teamCtl}
              ${(CAN_EDIT && u.id !== window._profileId) ? `
                <div class="mt-4 pt-3" style="border-top:1px solid var(--border)">
                  <div class="u-sec-label" style="color:var(--negative)">Danger zone</div>
                  <button class="btn btn-danger" onclick='deleteUser(${JSON.stringify(u.id)}, ${JSON.stringify(u.email)})'>Delete user</button>
                  <p class="mt-2" style="font-size:11px;color:var(--text-3)">Removes the account, integrations, chats, devices and all activity data. Cannot be undone.</p>
                </div>` : ''}
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
    openRow(id);
  }
  function openRow(id) {
    const r = row(id); if (!r) return;
    r.classList.add('open');
    r.querySelector('.urow-head').setAttribute('aria-expanded', 'true');
  }
  function collapse(id) {
    const r = row(id); if (!r) return;
    r.classList.remove('open');
    r.querySelector('.urow-head').setAttribute('aria-expanded', 'false');
    if (expandedId === id) expandedId = null;
  }

  // ── Role & permissions ────────────────────────────────────────────────────────
  async function setRole(id, role) {
    const r = await fetch(`/api/user-management/users/${id}/role`, {
      method: 'PATCH', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { alert(d.error || d.detail || 'Failed'); return; }
    const u = USERS.find(x => x.id === id); if (u) u.role = role;
    renderStats();
    // Rebuild this row so switch availability, the elevated note and the Save
    // button match the new role; re-open it to keep the admin in place.
    const wasOpen = expandedId === id;
    row(id).outerHTML = rowHtml(u);
    applyFilter();
    if (wasOpen) openRow(id);
  }

  // Toggling a switch no longer saves — it enables Save and flags unsaved changes,
  // so the admin gets explicit confirmation that a change was (or wasn't) applied.
  function markPermsDirty(id) {
    const box = insp(id);
    const btn = box.querySelector('.perm-save');
    if (btn) btn.disabled = false;
    const st = box.querySelector('.perm-status');
    if (st) { st.textContent = 'Unsaved changes'; st.style.color = 'var(--text-2)'; }
  }

  async function savePerms(id) {
    const box = insp(id);
    const boxes = box.querySelectorAll(`#perms-${id} input[data-perm]`);
    const perms = [];
    boxes.forEach(b => { if (b.checked) perms.push(b.dataset.perm); });
    const btn = box.querySelector('.perm-save');
    const st = box.querySelector('.perm-status');
    btn.disabled = true;
    st.textContent = 'Saving…'; st.style.color = 'var(--text-3)';
    try {
      const r = await fetch(`/api/user-management/users/${id}/permissions`, {
        method: 'PATCH', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ permissions: perms }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        st.textContent = '✕ ' + (d.error || d.detail || 'Save failed');
        st.style.color = 'var(--negative)'; btn.disabled = false; return;
      }
      const saved = d.permissions || perms;
      const u = USERS.find(x => x.id === id);
      if (u) {
        u.own_permissions = saved;
        if (u.role !== 'admin') u.permissions = saved;   // only admin stays effective-all
      }
      const cnt = row(id).querySelector('.u-count');
      if (cnt && u && u.role !== 'admin') cnt.textContent = `${saved.length}/${ALL_PERMS.length}`;
      st.textContent = '✓ Saved'; st.style.color = 'var(--positive)';
    } catch (e) {
      st.textContent = '✕ Error'; st.style.color = 'var(--negative)'; btn.disabled = false;
    }
  }

  // ── Team management inside a manager's row (admin only) ────────────────────────
  const chip = (email, onx) => `<span class="u-badge" style="display:inline-flex;align-items:center;gap:6px;background:var(--surface-2);color:var(--text-1)">${esc(email)}<button onclick="${onx}" title="Remove" style="border:none;background:none;color:var(--text-3);cursor:pointer;font-size:14px;line-height:1">×</button></span>`;

  function teamMembersHtml(managerId) {
    const members = USERS.filter(u => u.manager_id === managerId);
    if (!members.length) return '<span style="font-size:12px;color:var(--text-3)">No one reports to this manager yet.</span>';
    return members.map(u => chip(u.email, `removeFromTeam('${u.id}','${managerId}')`)).join('');
  }
  function teamSearch(managerId, q) {
    const drop = document.getElementById('team-drop-' + managerId);
    q = q.trim().toLowerCase();
    const matches = USERS.filter(u => u.role === 'user' && u.manager_id !== managerId
      && u.id !== managerId && u.email.toLowerCase().includes(q)).slice(0, 15);
    drop.innerHTML = matches.length
      ? matches.map(u => `<div style="padding:6px 9px;border-radius:6px;cursor:pointer;font-size:13px;display:flex;justify-content:space-between"
           onmouseover="this.style.background='var(--surface-2)'" onmouseout="this.style.background=''"
           onclick="addTeamMember('${u.id}','${managerId}')"><span>${esc(u.email)}</span>${u.manager_id ? '<span style="color:var(--text-3);font-size:11px">reassign</span>' : ''}</div>`).join('')
      : '<div style="padding:8px;color:var(--text-3);font-size:12px">No matching users.</div>';
    drop.style.display = 'block';
  }
  async function addTeamMember(userId, managerId) {
    document.getElementById('team-drop-' + managerId).style.display = 'none';
    const s = document.getElementById('team-search-' + managerId); if (s) s.value = '';
    await setManager(userId, managerId);
    document.getElementById('team-' + managerId).innerHTML = teamMembersHtml(managerId);
  }
  async function removeFromTeam(userId, managerId) {
    await setManager(userId, null);
    document.getElementById('team-' + managerId).innerHTML = teamMembersHtml(managerId);
  }
  document.addEventListener('click', e => {
    if (e.target.closest('[id^="team-drop-"]') || e.target.closest('[id^="team-search-"]')) return;
    document.querySelectorAll('[id^="team-drop-"]').forEach(d => { d.style.display = 'none'; });
  });

  // ── Tabs ───────────────────────────────────────────────────────────────────────
  function showTab(name) {
    ['users', 'bulk', 'mgrperms'].forEach(n => {
      const el = document.getElementById('tab-' + n); if (el) el.style.display = n === name ? '' : 'none';
    });
    document.querySelectorAll('.ac-tabs .ac-tab').forEach(t => t.setAttribute('aria-selected', t.dataset.tab === name));
    if (name === 'bulk' && bulkTargetMode === 'role') renderBulkRoleCount();
    if (name === 'mgrperms') renderMgrPerms();
  }

  // ── Manager permissions: what each manager may ASSIGN to their team ─────────────
  // The toggle list bound to a manager's assignable_perms — reused by the tab and
  // by each manager's row.
  function assignableSwitches(mgr) {
    return ALL_PERMS.map(p => `
      <div class="perm-row"><span class="perm-name">${permLabel(p)}</span>
        <label class="sw"><input type="checkbox" data-perm="${p}" ${mgr.assignable_perms.includes(p) ? 'checked' : ''}><i></i></label>
      </div>`).join('');
  }
  function renderMgrPermsPanel() {
    const sel = document.getElementById('mgrperms-select');
    if (!sel) return;                     // non-admin: tab not rendered
    const cur = sel.value;
    sel.innerHTML = '<option value="">Choose a manager…</option>'
      + MANAGERS.map(m => `<option value="${m.id}">${esc(m.email)}</option>`).join('');
    if (cur && MANAGERS.some(m => m.id === cur)) sel.value = cur;
  }
  function renderMgrPerms() {
    const sel = document.getElementById('mgrperms-select'); if (!sel) return;
    const mgr = USERS.find(u => u.id === sel.value);
    const list = document.getElementById('mgrperms-list');
    const actions = document.getElementById('mgrperms-actions');
    if (!mgr) { list.innerHTML = '<p class="text-sm" style="color:var(--text-3)">Pick a manager to configure.</p>'; actions.style.display = 'none'; return; }
    list.innerHTML = assignableSwitches(mgr);
    actions.style.display = '';
  }
  function saveMgrPerms() {
    const id = document.getElementById('mgrperms-select').value;
    if (id) saveAssignable(id, 'mgrperms-list', 'mgrperms-status');
  }
  async function saveAssignable(managerId, listId, statusId) {
    const perms = [...document.querySelectorAll(`#${listId} input[data-perm]:checked`)].map(c => c.dataset.perm);
    const st = document.getElementById(statusId);
    st.textContent = 'Saving…'; st.style.color = 'var(--text-3)';
    try {
      const r = await fetch(`/api/user-management/managers/${managerId}/assignable`, {
        method: 'PATCH', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ permissions: perms }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { st.textContent = '✕ ' + (d.error || d.detail || 'Failed'); st.style.color = 'var(--negative)'; return; }
      const u = USERS.find(x => x.id === managerId);
      if (u) u.assignable_perms = d.assignable_perms || perms;
      st.textContent = '✓ Saved'; st.style.color = 'var(--positive)';
    } catch (e) { st.textContent = '✕ Error'; st.style.color = 'var(--negative)'; }
  }

  // ── Bulk assign: select permissions + (users | whole role) + grant/revoke ──────
  const bulkSelected = new Map();   // id -> email
  let bulkTargetMode = 'users';     // users | role

  function bulkTarget(mode) {
    bulkTargetMode = mode;
    document.getElementById('bulk-users-target').style.display = mode === 'users' ? '' : 'none';
    document.getElementById('bulk-role-target').style.display = mode === 'role' ? '' : 'none';
    const segs = document.querySelectorAll('#bulk-target-seg .seg-btn');
    segs[0].setAttribute('aria-selected', mode === 'users');
    segs[1].setAttribute('aria-selected', mode === 'role');
    if (mode === 'role') renderBulkRoleCount();
  }
  function renderBulkRoleCount() {
    const role = document.getElementById('bulk-role').value;
    const n = USERS.filter(u => u.role === role).length;
    const el = document.getElementById('bulk-role-count');
    if (el) el.textContent = `${n} ${role}${n === 1 ? '' : 's'} in your scope will be affected.`;
  }

  function renderBulkPanel() {
    const pc = document.getElementById('bulk-perms');
    if (!pc) return;
    // Only show permissions the actor can actually assign (admin sees all).
    pc.innerHTML = ALL_PERMS.filter(p => ASSIGNABLE.includes(p)).map(p => `
      <label class="perm-row" style="justify-content:flex-start;gap:8px;padding:3px 0;border:none;cursor:pointer">
        <input type="checkbox" value="${p}" style="accent-color:var(--chrome)">
        <span class="perm-name">${permLabel(p)}</span>
      </label>`).join('')
      || '<p style="font-size:12px;color:var(--text-3);max-width:220px">No permissions you can assign — ask an admin to enable some under Manager permissions.</p>';
    renderBulkSelected();
  }
  function bulkSearch(q) {
    const drop = document.getElementById('bulk-drop');
    q = q.trim().toLowerCase();
    const matches = USERS.filter(u => u.email.toLowerCase().includes(q) && !bulkSelected.has(u.id)).slice(0, 15);
    drop.innerHTML = matches.length
      ? matches.map(u => `<div style="padding:6px 9px;border-radius:6px;cursor:pointer;font-size:13px;display:flex;justify-content:space-between"
           onmouseover="this.style.background='var(--surface-2)'" onmouseout="this.style.background=''"
           onclick="bulkPick('${u.id}')"><span>${esc(u.email)}</span><span style="color:var(--text-3);font-size:11px">${esc(u.role)}</span></div>`).join('')
      : '<div style="padding:8px;color:var(--text-3);font-size:12px">No matching users.</div>';
    drop.style.display = 'block';
  }
  function bulkPick(id) {
    const u = USERS.find(x => x.id === id); if (!u) return;
    bulkSelected.set(id, u.email); renderBulkSelected();
    const s = document.getElementById('bulk-search'); s.value = ''; s.focus();
    document.getElementById('bulk-drop').style.display = 'none';
  }
  function bulkUnpick(id) { bulkSelected.delete(id); renderBulkSelected(); }
  function renderBulkSelected() {
    document.getElementById('bulk-selected').innerHTML =
      [...bulkSelected.entries()].map(([id, email]) => chip(email, `bulkUnpick('${id}')`)).join('');
  }
  async function bulkApply(mode) {
    const perms = [...document.querySelectorAll('#bulk-perms input:checked')].map(c => c.value);
    const ids = bulkTargetMode === 'role'
      ? USERS.filter(u => u.role === document.getElementById('bulk-role').value).map(u => u.id)
      : [...bulkSelected.keys()];
    const st = document.getElementById('bulk-status');
    if (!perms.length || !ids.length) { st.textContent = 'Pick at least one permission and a target.'; st.style.color = 'var(--text-2)'; return; }
    st.textContent = mode === 'grant' ? 'Granting…' : 'Revoking…'; st.style.color = 'var(--text-3)';
    try {
      const r = await fetch('/api/user-management/bulk-permissions', {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_ids: ids, permissions: perms, mode }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { st.textContent = '✕ ' + (d.error || d.detail || 'Failed'); st.style.color = 'var(--negative)'; return; }
      st.textContent = `✓ ${mode === 'grant' ? 'Granted to' : 'Revoked from'} ${d.changed}${d.skipped ? ` (${d.skipped} skipped)` : ''}`;
      st.style.color = 'var(--positive)';
      bulkSelected.clear();
      loadUsers();   // refresh badges/toggles
    } catch (e) { st.textContent = '✕ Error'; st.style.color = 'var(--negative)'; }
  }
  document.addEventListener('click', e => {
    if (!e.target.closest('#bulk-search') && !e.target.closest('#bulk-drop')) {
      const d = document.getElementById('bulk-drop'); if (d) d.style.display = 'none';
    }
  });

  // ── Manager assignment & team template (admin only) ────────────────────────────
  async function setManager(id, managerId) {
    const r = await fetch(`/api/user-management/users/${id}/manager`, {
      method: 'PATCH', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ manager_id: managerId || null }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { alert(d.error || d.detail || 'Failed'); loadUsers(); return; }
    const u = USERS.find(x => x.id === id);
    if (u) { u.manager_id = managerId || null; if (d.permissions) { u.own_permissions = d.permissions; u.permissions = d.permissions; } }
  }

  async function applyTeam(managerId, mode) {
    const box = insp(managerId);
    const st = box.querySelector('.team-status');
    // Use the manager's currently-checked template (its own_permissions toggles).
    const boxes = box.querySelectorAll(`#perms-${managerId} input[data-perm]`);
    const perms = [];
    boxes.forEach(b => { if (b.checked) perms.push(b.dataset.perm); });
    if (!perms.length) { st.textContent = 'Set at least one template permission first.'; st.style.color = 'var(--text-2)'; return; }
    st.textContent = mode === 'grant' ? 'Applying…' : 'Revoking…'; st.style.color = 'var(--text-3)';
    try {
      const r = await fetch(`/api/user-management/managers/${managerId}/team-permissions`, {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ permissions: perms, mode }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { st.textContent = '✕ ' + (d.error || d.detail || 'Failed'); st.style.color = 'var(--negative)'; return; }
      st.textContent = `✓ ${mode === 'grant' ? 'Applied to' : 'Revoked from'} ${d.count} report${d.count === 1 ? '' : 's'}`;
      st.style.color = 'var(--positive)';
      loadUsers();   // refresh reports' effective permissions
    } catch (e) { st.textContent = '✕ Error'; st.style.color = 'var(--negative)'; }
  }

  async function deleteUser(id, email) {
    // Irreversible: native confirm is the guard, the server re-checks admin + self.
    if (!confirm(`Delete ${email}?\n\nThis permanently removes their account, integrations, `
      + `chats, devices and all activity data. This cannot be undone.`)) return;
    const r = await fetch(`/api/user-management/users/${id}`, { method: 'DELETE', credentials: 'include' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { alert(d.error || d.detail || 'Delete failed'); return; }
    if (expandedId === id) expandedId = null;
    USERS = USERS.filter(u => u.id !== id);
    renderStats();
    renderRows();
  }

  // ── Audit log (admin only) ────────────────────────────────────────────────────
  const ACTION_LABELS = {
    preview:        'Viewed',
    download:       'Downloaded',
    email:          'Emailed',
    email_delivery: 'Sent',
    delete_user:    'Deleted user',
  };

  async function loadAudit() {
    const list = document.getElementById('audit-list');
    if (!list) return;
    const role   = document.getElementById('audit-role').value;
    const action = document.getElementById('audit-action').value;
    list.innerHTML = '<p class="text-sm" style="color:var(--text-3)">Loading…</p>';
    try {
      const qs = new URLSearchParams();
      if (role) qs.set('role', role);
      if (action) qs.set('action', action);
      const r = await fetch('/api/user-management/access-log?' + qs.toString(), { credentials: 'include' });
      if (!r.ok) { list.innerHTML = `<p class="text-sm" style="color:var(--negative)">✕ ${r.status}</p>`; return; }
      const d = await r.json();
      renderAudit(d.entries || []);
    } catch (e) { list.innerHTML = '<p class="text-sm" style="color:var(--negative)">✕ Error</p>'; }
  }

  function renderAudit(entries) {
    const list = document.getElementById('audit-list');
    if (!entries.length) {
      list.innerHTML = '<p class="text-sm" style="color:var(--text-3)">No matching activity.</p>';
      return;
    }
    const rows = entries.map(e => {
      const m = meta(e.actor_role);
      const when = e.at ? new Date(e.at).toLocaleString() : '';
      const kind = e.kind ? ` <span style="color:var(--text-3)">(${esc(e.kind)})</span>` : '';
      const rolePill = e.actor_role
        ? `<span class="role-pill" style="color:${m.color};background:${m.bg}">${esc(e.actor_role)}</span>` : '';
      // "sent to" only makes sense for a delivery; otherwise show the report owner.
      const recipient = e.recipient_email
        ? `<span style="color:var(--text-1)">${esc(e.recipient_email)}</span>`
        : '<span style="color:var(--text-3)">—</span>';
      return `<tr>
        <td class="audit-when">${esc(when)}</td>
        <td><div style="color:var(--text-1)">${esc(e.actor_email || '?')}</div>${rolePill}</td>
        <td><span class="act-pill">${esc(ACTION_LABELS[e.action] || e.action || '?')}</span>${kind}</td>
        <td>${esc(e.target_email || '—')}</td>
        <td>${recipient}</td>
      </tr>`;
    }).join('');
    list.innerHTML = `<table class="audit-tbl">
      <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Report of</th><th>Sent to</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  }

  function onBaseReady() {
    loadUsers();
    loadAudit();   // no-op when the audit card isn't rendered (non-admin)
  }
