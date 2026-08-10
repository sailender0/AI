  let USERS = [], ALL_PERMS = [], ROLES = [], MANAGERS = [], ASSIGNABLE = [], ADMIN_ONLY = [], NON_DELEGABLE = [];
  let ACTOR_ID = null;
  let expandedId = null;

  const ROLE_META = {
    admin:   { color:'#a78bfa', bg:'rgba(167,139,250,0.13)' },
    manager: { color:'#fbbf24', bg:'rgba(251,191,36,0.13)' },
    user:    { color:'#8b8b9e', bg:'rgba(255,255,255,0.06)' },
  };
  const meta = r => ROLE_META[r] || ROLE_META.user;

  const PERM_LABELS = {
    email_report:     'Email Report page',
    export_my_day:    'Download My Day report',
    export_analytics: 'Download Analytics report',
    email_ai_answer:  'Email AI answers',
    consolidated_report: 'Consolidated report',
    attendance_report: 'Attendance report',
    teams_activity:   'Teams activity',
    outlook_activity: 'Outlook activity',
    activity_detail:  'See event names',
    device_activity:  'Device activity',
  };
  const permLabel = p => PERM_LABELS[p] || p;

  // The two report keys. Admin-only ones carry their own tag; these get theirs because
  // a manager can never hand either down, whoever holds it.
  const REPORT_KEYS = ['consolidated_report', 'attendance_report'];
  // Shown as the item's tooltip — the report keys are the only ones that need explaining.
  const PERM_DESC = {
    consolidated_report: 'GitHub, GitLab and Jira — how many events, per day. No names, no other connectors.',
    teams_activity:   'Adds Teams chats and calls. Stored as who they spoke to, never what was said.',
    outlook_activity: 'Adds mail and meetings. Meeting titles are the real subject lines.',
    activity_detail:  'Turns counts into named events and unlocks the AI summary. Off means numbers only.',
    device_activity:  'Adds focus time and AI tool usage from the desktop agent, in hours.',
  };

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
      ADMIN_ONLY = d.admin_only || [];
      NON_DELEGABLE = d.non_delegable || d.admin_only || [];
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

  // ── Permission panes ──────────────────────────────────────────────────────
  // Left pane = what this profile holds, right pane = the whole catalog, searchable.
  // Dragging right→left copies (the catalog never shrinks); dragging back, or the ×,
  // confirms and then stages a removal. Save writes the left pane as it stands.
  // One renderer for all three places permissions are edited — a user's own list, a
  // manager's can-assign list in their row, and the same list in the Manager tab — so
  // the eligibility rules can't drift between them.

  const permTag = k => ADMIN_ONLY.includes(k) ? '<span class="perm-tag">Admin only</span>'
                     : REPORT_KEYS.includes(k) ? '<span class="perm-tag report">Report</span>' : '';

  // Why a catalog key can't be dropped on this profile, or '' when it can. The server
  // clamps all three cases anyway (sanitize_permissions / assignable_permissions) —
  // showing the key greyed with its reason beats hiding it or offering a dead control.
  function blockReason(kind, u, k) {
    if (kind === 'assignable') return NON_DELEGABLE.includes(k) ? 'Never delegable' : '';
    if (!ASSIGNABLE.includes(k)) return 'Not in your can-assign list';
    if (u.role === 'user' && ADMIN_ONLY.includes(k)) return 'User role can’t hold this';
    return '';
  }

  function ppItem(k, side, blocked) {
    const name = esc(permLabel(k));
    const desc = PERM_DESC[k] ? ` title="${esc(PERM_DESC[k])}"` : '';
    if (side === 'right') {
      return `<li class="pp-item${blocked ? ' blocked' : ''}" data-k="${k}" data-side="right"
        ${blocked ? `aria-disabled="true" title="${esc(blocked)}"` : `draggable="true" tabindex="0"${desc}`}>
        <span class="pp-grip" aria-hidden="true">⣿</span>
        <span class="pp-name">${name}</span>${permTag(k)}
        ${blocked ? `<span class="perm-tag why">${esc(blocked)}</span>` : ''}
      </li>`;
    }
    return `<li class="pp-item" data-k="${k}" data-side="left" draggable="true" tabindex="0"${desc}>
      <span class="pp-grip" aria-hidden="true">⣿</span>
      <span class="pp-name">${name}</span>${permTag(k)}
      <button class="pp-x" type="button" data-rm="${k}" aria-label="Remove ${name}">×</button>
    </li>`;
  }

  // `kind` is 'perms' (a profile's own list) or 'assignable' (what a manager may hand
  // down). Both endpoints take the full list, which is exactly what the left pane is.
  function permPanesHtml(paneId, kind, u, assigned, saveLabel) {
    return `
      <div class="pp" id="${paneId}" data-kind="${kind}" data-uid="${u.id}"
           data-init="${(assigned || []).join(' ')}">
        <div class="pp-grid">
          <div class="pp-pane">
            <div class="pp-head">
              <span class="pp-title">${kind === 'assignable' ? 'May assign to their team' : 'Granted'}</span>
              <span class="pp-count"></span>
            </div>
            <ul class="pp-list pp-left" data-side="left"></ul>
            <p class="pp-warn"></p>
          </div>
          <div class="pp-pane">
            <div class="pp-head">
              <span class="pp-title">All permissions</span>
              <span class="pp-count">${ALL_PERMS.length} total</span>
              <input class="ac-input pp-q" type="search" placeholder="Search permissions…"
                     aria-label="Search permissions">
            </div>
            <ul class="pp-list pp-right" data-side="right"></ul>
          </div>
        </div>
        <p class="pp-hint">Drag right → left to grant · drag back or × to remove · click an item to move it without a mouse.</p>
        <div class="flex items-center gap-2 mt-2">
          <button class="btn btn-solid pp-save" type="button" disabled>${saveLabel}</button>
          <span class="pp-status text-xs" style="color:var(--text-3)"></span>
        </div>
      </div>`;
  }

  const ppLeftKeys = pp => [...pp.querySelectorAll('.pp-left li[data-k]')].map(li => li.dataset.k);

  // The markup ships with empty lists; this fills them from data-init. Called after
  // every innerHTML that can contain a pane.
  function mountPanes(root) {
    (root || document).querySelectorAll('.pp').forEach(pp =>
      renderPane(pp, pp.dataset.init ? pp.dataset.init.split(' ') : []));
  }

  function renderPane(pp, keys) {
    const u = USERS.find(x => x.id === pp.dataset.uid);
    if (!u) return;
    const kind = pp.dataset.kind;
    // Canonical order, and unknown keys drop out here the same way the server's
    // sanitize_permissions drops them.
    const held = ALL_PERMS.filter(k => (keys || ppLeftKeys(pp)).includes(k));
    pp.querySelector('.pp-left').innerHTML = held.map(k => ppItem(k, 'left')).join('')
      || '<li class="pp-empty">Nothing here yet — drag a permission across.</li>';

    const q = (pp.querySelector('.pp-q').value || '').trim().toLowerCase();
    const shown = ALL_PERMS.filter(k => !q || permLabel(k).toLowerCase().includes(q) || k.includes(q));
    pp.querySelector('.pp-right').innerHTML =
      shown.map(k => ppItem(k, 'right', blockReason(kind, u, k))).join('')
      || '<li class="pp-empty">No permission matches that search.</li>';

    pp.querySelector('.pp-count').textContent =
      `${held.length} ${kind === 'assignable' ? 'selected' : 'granted'}`;
  }

  function ppWarn(pp, msg) {
    const el = pp.querySelector('.pp-warn');
    el.textContent = msg || '';
    clearTimeout(el._t);
    if (msg) el._t = setTimeout(() => { el.textContent = ''; }, 3200);
  }

  function ppDirty(pp) {
    pp.querySelector('.pp-save').disabled = false;
    const st = pp.querySelector('.pp-status');
    st.textContent = 'Unsaved changes'; st.style.color = 'var(--text-2)';
  }

  function ppAdd(pp, k) {
    const u = USERS.find(x => x.id === pp.dataset.uid); if (!u) return;
    const why = blockReason(pp.dataset.kind, u, k);
    if (why) { ppWarn(pp, `“${permLabel(k)}” can’t be granted here — ${why.toLowerCase()}.`); return; }
    const keys = ppLeftKeys(pp);
    if (keys.includes(k)) {
      ppWarn(pp, `“${permLabel(k)}” is already in the list.`);
      const el = pp.querySelector(`.pp-left li[data-k="${k}"]`);
      if (el) { el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
                el.scrollIntoView({ block: 'nearest' }); }
      return;
    }
    renderPane(pp, [...keys, k]);
    ppDirty(pp);
  }

  // A drag is easy to trigger by accident, so removal asks — once per row unless the
  // actor ticks "don't ask again", which `collapse()` resets along with the row.
  const skipRemoveConfirm = new Set();

  async function ppRemove(pp, k) {
    const u = USERS.find(x => x.id === pp.dataset.uid);
    const who = pp.dataset.kind === 'assignable' ? `${u ? u.email : 'this manager'}'s can-assign list`
                                                 : (u ? u.email : 'this user');
    if (!skipRemoveConfirm.has(pp.id)) {
      const ok = await confirmDialog(`Remove “${permLabel(k)}” from ${who}?`,
        'It comes off the list when you save. Until then nothing has changed on the server.',
        'Remove', true, true);
      if (!ok) return;
      if (document.getElementById('cdlg-skip').checked) skipRemoveConfirm.add(pp.id);
    }
    renderPane(pp, ppLeftKeys(pp).filter(x => x !== k));
    ppDirty(pp);
  }

  // Both panes PATCH a full list, so the left pane is the payload.
  async function savePane(pp) {
    const id = pp.dataset.uid, kind = pp.dataset.kind;
    const perms = ppLeftKeys(pp);
    const btn = pp.querySelector('.pp-save'), st = pp.querySelector('.pp-status');
    btn.disabled = true;
    st.textContent = 'Saving…'; st.style.color = 'var(--text-3)';
    const url = kind === 'assignable'
      ? `/api/user-management/managers/${id}/assignable`
      : `/api/user-management/users/${id}/permissions`;
    try {
      const r = await fetch(url, {
        method: 'PATCH', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ permissions: perms }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        st.textContent = '✕ ' + (d.error || d.detail || 'Save failed');
        st.style.color = 'var(--negative)'; btn.disabled = false; return;
      }
      const u = USERS.find(x => x.id === id);
      if (kind === 'assignable') {
        const saved = d.assignable_perms || perms;
        if (u) u.assignable_perms = saved;
        renderPane(pp, saved);
      } else {
        const saved = d.permissions || perms;
        if (u) {
          u.own_permissions = saved;
          if (u.role !== 'admin') u.permissions = saved;
          const cnt = row(id) && row(id).querySelector('.u-count');
          if (cnt && u.role !== 'admin') cnt.textContent = `${saved.length}/${ALL_PERMS.length}`;
        }
        renderPane(pp, saved);
      }
      st.textContent = '✓ Saved'; st.style.color = 'var(--positive)';
    } catch (e) {
      st.textContent = '✕ Error'; st.style.color = 'var(--negative)'; btn.disabled = false;
    }
  }

  // A row the actor can't edit (their own, another manager's) lists what is granted
  // instead of rendering panes — nothing there is theirs to move.
  function readonlyPerms(u) {
    if (u.role === 'admin') return '<p style="font-size:12px;color:var(--text-3)">Holds every permission.</p>';
    const held = ALL_PERMS.filter(p => u.permissions.includes(p));
    if (!held.length) return '<p style="font-size:12px;color:var(--text-3)">No permissions granted.</p>';
    return `<div class="flex flex-wrap gap-2">${held.map(p => `<span class="u-badge">${permLabel(p)}</span>`).join('')}</div>`;
  }

  function rowHtml(u) {
    const m = meta(u.role);
    const isManager = u.role === 'manager';
    const count = ALL_PERMS.filter(p => u.permissions.includes(p)).length;

    const roleCtl = CAN_EDIT
      ? `<select class="rep-select" style="width:auto;padding:4px 8px;font-size:12px"
           onchange="setRole('${u.id}', this.value, this)" onclick="event.stopPropagation()">
           ${ROLES.map(r => `<option value="${r}" ${r === u.role ? 'selected' : ''}>${r}</option>`).join('')}
         </select>`
      : `<span class="u-badge">${esc(u.role)}</span>`;

    const managerCtl = (CAN_EDIT && u.role === 'user') ? `
      <div class="rep-field mt-3">
        <label>Reports to</label>
        <select class="rep-select" onchange="setManager('${u.id}', this.value, this)">
          <option value="">— No manager —</option>
          ${MANAGERS.map(mg => `<option value="${mg.id}" ${mg.id === u.manager_id ? 'selected' : ''}>${esc(mg.email)}</option>`).join('')}
        </select>
      </div>` : '';

    const teamCtl = (CAN_EDIT && isManager) ? `
      <div class="mt-4 pt-3" style="border-top:1px solid var(--border)">
        <div class="u-sec-label">Team — people reporting to this manager</div>
        <ul id="team-${u.id}" class="tlist mt-1">${teamMembersHtml(u.id)}</ul>
        <div style="position:relative;max-width:340px;margin-top:8px">
          <input id="team-search-${u.id}" class="rep-select" type="text" placeholder="Search a user to add…"
                 autocomplete="off" oninput="teamSearch('${u.id}', this.value)" onfocus="teamSearch('${u.id}', this.value)">
          <div id="team-drop-${u.id}" style="position:absolute;z-index:20;top:calc(100% + 4px);left:0;right:0;max-height:200px;
               overflow:auto;padding:5px;display:none;background:var(--surface);border:1px solid var(--border-strong);border-radius:9px"></div>
        </div>
        <p style="font-size:11px;color:var(--text-3);margin:10px 0 6px">Apply the permissions listed above to this whole team (grant keeps their extras; revoke strips them).</p>
        <div class="flex items-center gap-2">
          <button class="btn btn-solid" onclick="applyTeam('${u.id}','grant')">Apply to team</button>
          <button class="btn btn-ghost" onclick="applyTeam('${u.id}','revoke')">Revoke from team</button>
          <span class="team-status text-xs" style="color:var(--text-3)"></span>
        </div>
      </div>` : '';

    const assignableCtl = (CAN_EDIT && isManager) ? `
      <div class="mt-4 pt-3" style="border-top:1px solid var(--border)">
        <div class="u-sec-label">Can assign to team — permissions this manager may grant their reports</div>
        ${permPanesHtml(`asg-${u.id}`, 'assignable', u, u.assignable_perms, 'Save can-assign list')}
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
              ${u.can_edit_perms
                ? permPanesHtml(`perms-${u.id}`, 'perms', u, u.own_permissions, 'Save permissions')
                : `<div id="perms-${u.id}">${readonlyPerms(u)}</div>`}
              ${isManager ? `<p class="mt-2" style="font-size:11px;color:var(--text-3)">These are this manager's <b>own permissions</b> — nobody inherits them. An admin can restrict a manager here; team powers (seeing/editing their reports) stay regardless.</p>`
                : (u.role === 'admin' ? `<p class="mt-2" style="font-size:11px;color:var(--text-3)">Admins hold every permission.</p>` : '')}
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
    mountPanes(el);
    applyFilter();
  }

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
    skipRemoveConfirm.clear();   // "don't ask again" lasts only while the row is open
    if (expandedId === id) expandedId = null;
  }

  // Resolves true only on the Confirm button — Esc and the backdrop leave
  // returnValue empty, which is exactly the cancel we want. `skippable` shows the
  // "don't ask again" box; the caller reads it, since only the caller knows the scope.
  function confirmDialog(title, body, okLabel, danger, skippable) {
    const dlg = document.getElementById('confirm-dlg');
    const ok = document.getElementById('cdlg-ok');
    const skip = document.getElementById('cdlg-skip');
    document.getElementById('cdlg-title').textContent = title;
    document.getElementById('cdlg-body').textContent = body;
    skip.checked = false;
    skip.parentElement.style.display = skippable ? '' : 'none';
    ok.textContent = okLabel;
    ok.className = 'btn ' + (danger ? 'btn-danger' : 'btn-solid');
    dlg.returnValue = '';
    dlg.showModal();
    return new Promise(resolve => dlg.addEventListener(
      'close', () => resolve(dlg.returnValue === 'ok'), { once: true }));
  }

  const ROLE_NOTE = {
    admin:   'Admins can see and report on every user\'s activity, change any role, and delete accounts.',
    manager: 'Managers can see, report on and edit permissions for the people assigned to them. Nobody reports to them until you build their team.',
    user:    'They keep their own permissions but lose all access to other people\'s data.',
  };

  async function setRole(id, role, el) {
    const u = USERS.find(x => x.id === id);
    if (!u || role === u.role) return;
    // Demotion detaches the team — the one consequence of a role change that
    // touches OTHER rows, so it gets spelled out before anything is written.
    const team = (u.role === 'manager' && role !== 'manager')
      ? USERS.filter(x => x.manager_id === id).length : 0;
    const body = (ROLE_NOTE[role] || '')
      + (team ? ` ${team} ${team === 1 ? 'person' : 'people'} currently report to them and will be left without a manager.` : '');
    if (!await confirmDialog(`Change ${u.email} from ${u.role} to ${role}?`, body, 'Change role')) {
      if (el) el.value = u.role;
      return;
    }
    const r = await fetch(`/api/user-management/users/${id}/role`, {
      method: 'PATCH', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { alert(d.error || d.detail || 'Failed'); if (el) el.value = u.role; return; }
    u.role = role;
    // The server clears manager_id on every report of a demoted manager; mirror it
    // locally so the team chips and the Reports-to dropdowns don't show a stale tie.
    if (team) USERS.forEach(x => { if (x.manager_id === id) x.manager_id = null; });
    renderStats();
    const wasOpen = expandedId === id;
    row(id).outerHTML = rowHtml(u);
    mountPanes(row(id));
    applyFilter();
    if (wasOpen) openRow(id);
  }

  // Every pane behaves the same, so the listeners live on the document once rather
  // than being re-bound each time a row repaints.
  document.addEventListener('dragstart', e => {
    const li = e.target.closest('.pp-item');
    if (!li || li.classList.contains('blocked')) return;
    e.dataTransfer.setData('text/plain', JSON.stringify({
      pane: li.closest('.pp').id, k: li.dataset.k, from: li.dataset.side,
    }));
    e.dataTransfer.effectAllowed = 'copy';
    li.classList.add('dragging');
  });
  document.addEventListener('dragend', e => {
    const li = e.target.closest('.pp-item'); if (li) li.classList.remove('dragging');
  });
  document.addEventListener('dragover', e => {
    const list = e.target.closest('.pp-list'); if (!list) return;
    e.preventDefault(); list.classList.add('drag-over');
  });
  document.addEventListener('dragleave', e => {
    const list = e.target.closest('.pp-list'); if (list) list.classList.remove('drag-over');
  });
  document.addEventListener('drop', e => {
    const list = e.target.closest('.pp-list'); if (!list) return;
    e.preventDefault(); list.classList.remove('drag-over');
    let d; try { d = JSON.parse(e.dataTransfer.getData('text/plain')); } catch (err) { return; }
    const pp = list.closest('.pp');
    if (!d || !pp || d.pane !== pp.id) return;   // never across panes: two profiles, two lists
    const to = list.dataset.side;
    if (to === 'left'  && d.from === 'right') ppAdd(pp, d.k);
    if (to === 'right' && d.from === 'left')  ppRemove(pp, d.k);
  });

  // Mouse-free path: click or Enter does exactly what a drop does, confirm included.
  document.addEventListener('click', e => {
    const pp = e.target.closest('.pp'); if (!pp) return;
    if (e.target.closest('.pp-save')) { savePane(pp); return; }
    const rm = e.target.closest('[data-rm]');
    if (rm) { ppRemove(pp, rm.dataset.rm); return; }
    const li = e.target.closest('.pp-item');
    if (!li || li.classList.contains('blocked')) return;
    li.dataset.side === 'right' ? ppAdd(pp, li.dataset.k) : ppRemove(pp, li.dataset.k);
  });
  document.addEventListener('keydown', e => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const li = e.target.closest('.pp-item');
    if (!li || li.classList.contains('blocked')) return;
    e.preventDefault();
    const pp = li.closest('.pp');
    li.dataset.side === 'right' ? ppAdd(pp, li.dataset.k) : ppRemove(pp, li.dataset.k);
  });
  document.addEventListener('input', e => {
    if (e.target.classList.contains('pp-q')) renderPane(e.target.closest('.pp'));
  });

  const chip = (email, onx) => `<span class="u-badge" style="display:inline-flex;align-items:center;gap:6px;background:var(--surface-2);color:var(--text-1)">${esc(email)}<button onclick="${onx}" title="Remove" style="border:none;background:none;color:var(--text-3);cursor:pointer;font-size:14px;line-height:1">×</button></span>`;

  // One row per person in a height-capped list: nine people used to wrap into four
  // ragged rows of chips, and the row grew every time somebody joined.
  function teamMembersHtml(managerId) {
    const members = USERS.filter(u => u.manager_id === managerId);
    if (!members.length) return '<li class="pp-empty">No one reports to this manager yet.</li>';
    return members.map(u => `
      <li class="trow">
        <span class="u-avatar" style="width:26px;height:26px;margin-left:0;font-size:10px">${initials(u.email)}</span>
        <span class="pp-name">${esc(u.email)}</span>
        <button class="pp-x" type="button" onclick="removeFromTeam('${u.id}','${managerId}')"
                aria-label="Remove ${esc(u.email)} from team">×</button>
      </li>`).join('');
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

  function showTab(name) {
    ['users', 'bulk', 'mgrperms'].forEach(n => {
      const el = document.getElementById('tab-' + n); if (el) el.style.display = n === name ? '' : 'none';
    });
    document.querySelectorAll('.ac-tabs .ac-tab').forEach(t => t.setAttribute('aria-selected', t.dataset.tab === name));
    if (name === 'bulk' && bulkTargetMode === 'role') renderBulkRoleCount();
    if (name === 'mgrperms') renderMgrPerms();
  }

  function renderMgrPermsPanel() {
    const sel = document.getElementById('mgrperms-select');
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = '<option value="">Choose a manager…</option>'
      + MANAGERS.map(m => `<option value="${m.id}">${esc(m.email)}</option>`).join('');
    if (cur && MANAGERS.some(m => m.id === cur)) sel.value = cur;
  }
  function renderMgrPerms() {
    const sel = document.getElementById('mgrperms-select'); if (!sel) return;
    const mgr = USERS.find(u => u.id === sel.value);
    const list = document.getElementById('mgrperms-list');
    skipRemoveConfirm.delete('mgrperms-pane');
    if (!mgr) { list.innerHTML = '<p class="text-sm" style="color:var(--text-3)">Pick a manager to configure.</p>'; return; }
    list.innerHTML = permPanesHtml('mgrperms-pane', 'assignable', mgr, mgr.assignable_perms, 'Save can-assign list');
    mountPanes(list);
  }

  const bulkSelected = new Map();
  let bulkTargetMode = 'users';

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
      loadUsers();
    } catch (e) { st.textContent = '✕ Error'; st.style.color = 'var(--negative)'; }
  }
  document.addEventListener('click', e => {
    if (!e.target.closest('#bulk-search') && !e.target.closest('#bulk-drop')) {
      const d = document.getElementById('bulk-drop'); if (d) d.style.display = 'none';
    }
  });

  // Every path that moves someone between teams lands here — the Reports-to
  // dropdown, the team chips' ×, and the search-to-add — so the confirmation lives
  // here too rather than at three call sites. `el` is the dropdown, if one triggered
  // it: on cancel it has already repainted the new choice and has to be put back.
  async function setManager(id, managerId, el) {
    const u = USERS.find(x => x.id === id);
    const who = u ? u.email : 'this user';
    const mgr = MANAGERS.find(m => m.id === managerId);
    const [title, body] = managerId
      ? [`Add ${who} to ${mgr ? mgr.email : 'this manager'}'s team?`,
         `This only changes who they report to. ${who} keeps exactly the permissions `
         + `they have now — the manager's are not copied across.`]
      : [`Remove ${who} from their manager's team?`,
         'They keep their permissions, but the manager loses access to their reports.'];
    if (!await confirmDialog(title, body, managerId ? 'Add to team' : 'Remove')) {
      if (el) el.value = (u && u.manager_id) || '';
      return false;
    }
    const r = await fetch(`/api/user-management/users/${id}/manager`, {
      method: 'PATCH', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ manager_id: managerId || null }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { alert(d.error || d.detail || 'Failed'); loadUsers(); return false; }
    if (u) { u.manager_id = managerId || null; if (d.permissions) { u.own_permissions = d.permissions; u.permissions = d.permissions; } }
    return true;
  }

  async function applyTeam(managerId, mode) {
    const box = insp(managerId);
    const st = box.querySelector('.team-status');
    const pane = document.getElementById(`perms-${managerId}`);
    const perms = pane ? ppLeftKeys(pane) : [];
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
      loadUsers();
    } catch (e) { st.textContent = '✕ Error'; st.style.color = 'var(--negative)'; }
  }

  async function deleteUser(id, email) {
    if (!await confirmDialog(`Delete ${email}?`,
      'This permanently removes their account, integrations, chats, devices and all '
      + 'activity data. This cannot be undone.', 'Delete user', true)) return;
    const r = await fetch(`/api/user-management/users/${id}`, { method: 'DELETE', credentials: 'include' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { alert(d.error || d.detail || 'Delete failed'); return; }
    if (expandedId === id) expandedId = null;
    USERS = USERS.filter(u => u.id !== id);
    renderStats();
    renderRows();
  }

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
    loadAudit();
  }
