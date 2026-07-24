function aiToolsPage() {
  return {
    // AI_TOOLS_INIT is declared by the inline shim in agent_ai_tools.html.
    mode:            AI_TOOLS_INIT.mode,
    selectedDate:    AI_TOOLS_INIT.date || new Date().toLocaleDateString('en-CA'),
    selectedWeek:    AI_TOOLS_INIT.week || '',
    modelFilter:     '',
    claudeUsage:     [],
    weekClaudeUsage: [],
    weekClaudeByDay: {},
    weekToolsByDay:  {},
    weekToolActiveByDay: {},
    dayExpanded:     {},
    loading:         false,

    async init() {
      if (!this.selectedWeek) this.selectedWeek = this.currentWeekStr;
      if (this.mode === 'week') {
        await this.loadWeek();
      } else {
        await this.load();
      }
    },

    async load() {
      this.loading = true;
      try {
        const tz = encodeURIComponent(Intl.DateTimeFormat().resolvedOptions().timeZone);
        const dateParam = this.selectedDate !== this.todayStr ? `&date=${this.selectedDate}` : '';
        const r = await fetch(`/api/agent/today?tz=${tz}${dateParam}`);
        if (r.ok) { const d = await r.json(); this.claudeUsage = d.claude_usage || []; }
      } finally { this.loading = false; }
    },

    async loadWeek() {
      this.loading = true;
      try {
        const tz = encodeURIComponent(Intl.DateTimeFormat().resolvedOptions().timeZone);
        const weekStart = this.weekToMonday(this.selectedWeek);
        const r = await fetch(`/api/agent/week?tz=${tz}&week_start=${weekStart}`);
        if (r.ok) {
          const d = await r.json();
          this.weekClaudeUsage = d.claude_usage  || [];
          this.weekClaudeByDay = d.claude_by_day  || {};
          this.weekToolsByDay  = d.tools_by_day   || {};
          this.weekToolActiveByDay = d.tool_active_by_day || {};
        }
      } finally { this.loading = false; }
    },

    get currentData() { return this.mode === 'day' ? this.claudeUsage : this.weekClaudeUsage; },

    get allModels() {
      const s = new Set();
      this.currentData.forEach(r => (r.models || []).forEach(m => s.add(m.model)));
      return [...s].sort();
    },

    get filteredRepos() {
      if (!this.modelFilter) return this.currentData;
      return this.currentData.map(r => ({
        ...r,
        models:        (r.models || []).filter(m => m.model === this.modelFilter),
        input_tokens:  (r.models || []).filter(m => m.model === this.modelFilter).reduce((s, m) => s + m.input_tokens, 0),
        output_tokens: (r.models || []).filter(m => m.model === this.modelFilter).reduce((s, m) => s + m.output_tokens, 0),
      })).filter(r => r.models.length > 0);
    },

    get totalIn()  { return this.filteredRepos.reduce((s, r) => s + r.input_tokens, 0); },
    get totalOut() { return this.filteredRepos.reduce((s, r) => s + r.output_tokens, 0); },

    get modelSummary() {
      const map = {};
      this.currentData.forEach(r =>
        (r.models || []).forEach(m => {
          if (!map[m.model]) map[m.model] = {model: m.model, total: 0, messages: 0};
          map[m.model].total    += m.input_tokens + m.output_tokens;
          map[m.model].messages += m.messages;
        })
      );
      return Object.values(map).sort((a, b) => b.total - a.total);
    },

    // 7-day array for week mode day-by-day display
    get weekDaysFiltered() {
      if (!this.selectedWeek) return [];
      const monday = this.weekToMonday(this.selectedWeek);
      return Array.from({length: 7}, (_, i) => {
        const d = new Date(monday + 'T12:00:00Z');
        d.setUTCDate(d.getUTCDate() + i);
        const date = d.toLocaleDateString('en-CA');
        let repos = (this.weekClaudeByDay[date] || []).slice();
        if (this.modelFilter) {
          repos = repos.map(r => ({
            ...r,
            models:        (r.models||[]).filter(m => m.model === this.modelFilter),
            input_tokens:  (r.models||[]).filter(m => m.model === this.modelFilter).reduce((s,m)=>s+m.input_tokens,0),
            output_tokens: (r.models||[]).filter(m => m.model === this.modelFilter).reduce((s,m)=>s+m.output_tokens,0),
          })).filter(r => r.models.length > 0);
        }
        const allTools  = this.weekToolsByDay[date] || [];
        const otherTools = allTools.filter(t => t !== 'claude-code');
        const activeMin = this.weekToolActiveByDay[date] || {};
        return {date, repos, totalTokens: repos.reduce((s,r)=>s+r.input_tokens+r.output_tokens,0), otherTools, activeMin};
      });
    },

    formatFullDate(iso) {
      return new Date(iso + 'T12:00:00Z').toLocaleDateString(undefined,
        {weekday:'long', month:'short', day:'numeric'});
    },

    isToday(iso) { return iso === this.todayStr; },

    // ── Navigation (same as my_activity.html) ──
    prevDay() {
      const d = new Date(this.selectedDate + 'T12:00:00Z');
      d.setUTCDate(d.getUTCDate() - 1);
      this.selectedDate = d.toLocaleDateString('en-CA');
      this.load();
    },
    nextDay() {
      const d = new Date(this.selectedDate + 'T12:00:00Z');
      d.setUTCDate(d.getUTCDate() + 1);
      const n = d.toLocaleDateString('en-CA');
      if (n <= this.todayStr) { this.selectedDate = n; this.load(); }
    },
    goToday() { this.selectedDate = this.todayStr; this.load(); },
    prevWeek() {
      this.selectedWeek = this._addWeeks(this.selectedWeek, -1); this.loadWeek();
    },
    nextWeek() {
      if (this.selectedWeek >= this.currentWeekStr) return;
      this.selectedWeek = this._addWeeks(this.selectedWeek, 1); this.loadWeek();
    },
    goThisWeek() { this.selectedWeek = this.currentWeekStr; this.loadWeek(); },

    _addWeeks(w, delta) {
      const mon = this.weekToMonday(w);
      const d = new Date(mon + 'T12:00:00Z');
      d.setUTCDate(d.getUTCDate() + delta * 7);
      const day = d.getUTCDay() || 7;
      d.setUTCDate(d.getUTCDate() + 4 - day);
      const ys = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
      const wk = Math.ceil((((d - ys) / 86400000) + 1) / 7);
      return `${d.getUTCFullYear()}-W${String(wk).padStart(2, '0')}`;
    },
    weekToMonday(ws) {
      const [y, wn] = ws.split('-W').map(Number);
      const jan4 = new Date(Date.UTC(y, 0, 4));
      const j4d  = jan4.getUTCDay() || 7;
      const mon  = new Date(jan4);
      mon.setUTCDate(jan4.getUTCDate() - (j4d - 1) + (wn - 1) * 7);
      return mon.toLocaleDateString('en-CA');
    },
    get todayStr() { return new Date().toLocaleDateString('en-CA'); },
    get currentWeekStr() {
      const d = new Date();
      const u = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
      const day = u.getUTCDay() || 7;
      u.setUTCDate(u.getUTCDate() + 4 - day);
      const ys = new Date(Date.UTC(u.getUTCFullYear(), 0, 1));
      const wk = Math.ceil((((u - ys) / 86400000) + 1) / 7);
      return `${u.getUTCFullYear()}-W${String(wk).padStart(2, '0')}`;
    },
    formatTokens(t) {
      if (!t) return '0';
      if (t >= 1_000_000) return (t / 1_000_000).toFixed(1) + 'M';
      return t >= 1000 ? (t / 1000).toFixed(1) + 'k' : String(t);
    },
    formatMinutes(min) {
      if (!min) return '0 min';
      const h = Math.floor(min / 60), m = min % 60;
      return h ? `${h}h ${m}m` : `${m} min`;
    },
  };
}
