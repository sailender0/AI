function activityApp() {
  const TOOL_COLORS = {
    'claude-code':        '#e07b39',
    'github-copilot':     '#6e40c9',
    'cursor-ai':          '#1a73e8',
    'windsurf':           '#06b6d4',
    'tabnine':            '#22c55e',
    'codeium':            '#f59e0b',
    'supermaven':         '#ec4899',
    'continue-dev':       '#8b5cf6',
    'amazon-q':           '#f97316',
    'gemini-cli':         '#3b82f6',
    'gemini-code-assist': '#3b82f6',
    'ollama':             '#64748b',
  };

  return {
    tab:               'today',
    focusBlocks:       [],
    totalFocusMin:     0,
    activeTools:       [],
    toolActiveMin:     {},
    toolActivePeriods: {},
    totalTokens:       0,
    claudeUsage:       [],
    commits:           [],
    activeRepo:        null,
    activeBranch:      null,
    agentOnline:       false,
    lastUpdated:       '',
    refreshing:        false,
    selectedDate:      new Date().toLocaleDateString('en-CA'),

    weekDays:          [],
    weekTotalTokens:   0,
    weekCommitCount:   0,
    weekClaudeUsage:   [],
    weekClaudeByDay:   {},
    weekToolActiveMin: {},
    weekActiveTools:   [],
    weekLoadedAt:      null,
    chartReady:        false,
    selectedWeek:      '',
    toolExpanded:      {},

    cmpGran:           'week',
    cmpThis:           0,
    cmpLast:           0,
    cmpLoaded:         false,

    async refresh() {
      this.refreshing = true;
      try {
        if (this.tab === 'week') await this.loadWeek(true);
        else await this.load();
      } finally {
        this.refreshing = false;
      }
    },

    async init() {
      this.selectedWeek = this.currentWeekStr;
      await this.load();
      setTimeout(() => { this.chartReady = true; }, 120);
      setInterval(() => {
        if (this.tab === 'week') { this.loadWeek(true); }
        else if (this.selectedDate === this.todayStr) { this.load(); }
      }, 60000);
    },

    async load() {
      try {
        const tz = encodeURIComponent(Intl.DateTimeFormat().resolvedOptions().timeZone);
        const dateParam = this.selectedDate !== this.todayStr ? `&date=${this.selectedDate}` : '';
        const r = await fetch(`/api/agent/today?tz=${tz}${dateParam}`);
        if (!r.ok) return;
        const d = await r.json();
        this.focusBlocks   = d.focus_blocks    || [];
        this.totalFocusMin = d.total_focus_min || 0;
        this.activeTools   = d.active_tools    || [];
        this.toolActiveMin = d.tool_active_min || {};
        this.toolActivePeriods = d.tool_active_periods || {};
        this.claudeUsage   = d.claude_usage    || [];
        this.commits       = d.commits         || [];
        this.totalTokens   = this.claudeUsage.reduce((s, u) => s + u.input_tokens + u.output_tokens, 0);
        if (d.active_now) {
          this.activeRepo   = d.active_now.repo;
          this.activeBranch = d.active_now.branch;
          this.agentOnline  = d.active_now.last_seen
            ? (Date.now() - new Date(d.active_now.last_seen)) < 5 * 60 * 1000
            : false;
        } else {
          this.agentOnline = false;
        }
        this.lastUpdated = 'Updated ' + new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
      } catch(e) { this.agentOnline = false; }
    },

    async loadWeek(force = false) {
      const now = Date.now();
      if (!force && this.weekLoadedAt && now - this.weekLoadedAt < 5 * 60 * 1000) return;
      try {
        const tz = encodeURIComponent(Intl.DateTimeFormat().resolvedOptions().timeZone);
        const weekStart = this.selectedWeek ? this.weekToMonday(this.selectedWeek) : '';
        const r = await fetch(`/api/agent/week?tz=${tz}&week_start=${weekStart}`);
        if (!r.ok) return;
        const d = await r.json();
        this.weekDays          = d.days            || [];
        this.weekTotalTokens   = d.total_tokens    || 0;
        this.weekCommitCount   = d.commit_count    || 0;
        this.weekClaudeUsage   = d.claude_usage    || [];
        this.weekClaudeByDay   = d.claude_by_day   || {};
        this.weekToolActiveMin = d.tool_active_min || {};
        this.weekActiveTools   = d.active_tools    || [];
        this.weekLoadedAt      = now;
      } catch(e) {}
      if (!this.cmpLoaded || force) this.loadComparison();
    },

    async loadComparison() {
      try {
        const tz = encodeURIComponent(Intl.DateTimeFormat().resolvedOptions().timeZone);
        const r  = await fetch(`/api/agent/token-comparison?granularity=${this.cmpGran}&tz=${tz}`);
        if (!r.ok) return;
        const d = await r.json();
        this.cmpThis   = d.this?.total || 0;
        this.cmpLast   = d.last?.total || 0;
        this.cmpLoaded = true;
      } catch(e) {}
    },
    setCmpGran(g) { if (this.cmpGran === g) return; this.cmpGran = g; this.loadComparison(); },

    prevDay() {
      const d = new Date(this.selectedDate + 'T12:00:00Z');
      d.setUTCDate(d.getUTCDate() - 1);
      this.selectedDate = d.toLocaleDateString('en-CA');
      this.load();
    },
    nextDay() {
      const d = new Date(this.selectedDate + 'T12:00:00Z');
      d.setUTCDate(d.getUTCDate() + 1);
      const next = d.toLocaleDateString('en-CA');
      if (next <= this.todayStr) { this.selectedDate = next; this.load(); }
    },
    goToday() { this.selectedDate = this.todayStr; this.load(); },

    prevWeek() {
      this.selectedWeek = this._addWeeks(this.selectedWeek, -1);
      this.weekLoadedAt = null;
      this.loadWeek();
    },
    nextWeek() {
      if (this.selectedWeek >= this.currentWeekStr) return;
      this.selectedWeek = this._addWeeks(this.selectedWeek, 1);
      this.weekLoadedAt = null;
      this.loadWeek();
    },
    goThisWeek() {
      this.selectedWeek = this.currentWeekStr;
      this.weekLoadedAt = null;
      this.loadWeek();
    },
    jumpToDay(dateStr) {
      if (!dateStr || dateStr > this.todayStr) return;
      this.selectedDate = dateStr;
      this.tab = 'today';
      this.load();
    },

    _addWeeks(weekStr, delta) {
      const monday = this.weekToMonday(weekStr);
      const d = new Date(monday + 'T12:00:00Z');
      d.setUTCDate(d.getUTCDate() + delta * 7);
      const day = d.getUTCDay() || 7;
      d.setUTCDate(d.getUTCDate() + 4 - day);
      const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
      const week = Math.ceil((((d - yearStart) / 86400000) + 1) / 7);
      return `${d.getUTCFullYear()}-W${String(week).padStart(2, '0')}`;
    },

    weekToMonday(weekStr) {
      const [yearStr, wStr] = weekStr.split('-W');
      const year = parseInt(yearStr), week = parseInt(wStr);
      const jan4 = new Date(Date.UTC(year, 0, 4));
      const jan4Day = jan4.getUTCDay() || 7;
      const monday = new Date(jan4);
      monday.setUTCDate(jan4.getUTCDate() - (jan4Day - 1) + (week - 1) * 7);
      return monday.toLocaleDateString('en-CA');
    },

    get todayStr() { return new Date().toLocaleDateString('en-CA'); },

    get currentWeekStr() {
      const d = new Date();
      const utc = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
      const day = utc.getUTCDay() || 7;
      utc.setUTCDate(utc.getUTCDate() + 4 - day);
      const yearStart = new Date(Date.UTC(utc.getUTCFullYear(), 0, 1));
      const week = Math.ceil((((utc - yearStart) / 86400000) + 1) / 7);
      return `${utc.getUTCFullYear()}-W${String(week).padStart(2, '0')}`;
    },

    get weekAllDays() {
      if (!this.selectedWeek) return [];
      const monday = this.weekToMonday(this.selectedWeek);
      const byDate = Object.fromEntries(this.weekDays.map(d => [d.date, d]));
      return Array.from({length: 7}, (_, i) => {
        const d = new Date(monday + 'T12:00:00Z');
        d.setUTCDate(d.getUTCDate() + i);
        const date = d.toLocaleDateString('en-CA');
        return byDate[date] || {date, focus_min: 0, ai_tokens: 0, focus_blocks: 0};
      });
    },

    get weekMaxFocus() {
      return Math.max(1, ...this.weekAllDays.map(d => d.focus_min));
    },

    get weekMaxTokens() {
      return Math.max(1, ...this.weekAllDays.map(d => d.ai_tokens || 0));
    },

    get cmpMax() { return Math.max(1, this.cmpThis, this.cmpLast); },

    get cmpBars() {
      const m = this.cmpGran === 'month';
      return [
        { key: 'last', label: m ? 'Last month' : 'Last week', total: this.cmpLast, grad: '#94a3b8,#475569' },
        { key: 'this', label: m ? 'This month' : 'This week', total: this.cmpThis, grad: '#fdba74,#c2410c' },
      ];
    },

    get cmpDeltaLabel() {
      if (!this.cmpLast) {
        return this.cmpThis
          ? `<span style="color:var(--text-3)">No prior ${this.cmpGran} to compare</span>`
          : `<span style="color:var(--text-3)">No token usage recorded</span>`;
      }
      const delta = this.cmpThis - this.cmpLast;
      const pct   = Math.round(Math.abs(delta) / this.cmpLast * 100);
      const up    = delta >= 0;
      const color = up ? '#22c55e' : '#ef4444';
      return `<span style="color:${color}">${up ? '▲' : '▼'} ${pct}%</span>` +
             `<span style="color:var(--text-3)"> ${up ? 'more' : 'fewer'} tokens than last ${this.cmpGran}</span>`;
    },

    get aiToolRows() {
      return this.activeTools.map(name => {
        const repos = name === 'claude-code' ? this.claudeUsage : [];
        return {
          name,
          color:       TOOL_COLORS[name] || '#6b7280',
          active_min:  this.toolActiveMin[name] || 0,
          periods:     this.toolActivePeriods[name] || [],
          repos,
          totalTokens: repos.reduce((s, r) => s + r.input_tokens + r.output_tokens, 0),
        };
      });
    },

    get weekAiToolRows() {
      return this.weekActiveTools.map(name => {
        const repos = name === 'claude-code' ? this.weekClaudeUsage : [];
        return {
          name,
          color:       TOOL_COLORS[name] || '#6b7280',
          active_min:  this.weekToolActiveMin[name] || 0,
          repos,
          totalTokens: repos.reduce((s, r) => s + r.input_tokens + r.output_tokens, 0),
        };
      });
    },

    formatMinutes(min) {
      if (!min) return '0 min';
      const h = Math.floor(min / 60), m = min % 60;
      return h ? `${h}h ${m}m` : `${m} min`;
    },
    formatClock(iso) {
      return new Date(iso).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
    },
    formatHour(ms) {
      const h = new Date(ms).getHours();
      const hh = (h % 12) || 12;
      return hh + (h < 12 ? 'a' : 'p');
    },
    get timelineWindow() {
      const lo = new Date(this.selectedDate + 'T00:00:00').getTime();
      const span = 24 * 3600000;
      return {lo, hi: lo + span, span};
    },
    get timelineTicks() {
      const w = this.timelineWindow, ticks = [];
      for (let h = 0; h <= 24; h += 3) {
        ticks.push({left: h / 24 * 100, label: this.formatHour(w.lo + h * 3600000)});
      }
      return ticks;
    },
    get timelineGrid() {
      const lines = [];
      for (let h = 1; h < 24; h++) lines.push(h / 24 * 100);
      return lines;
    },
    segStyle(p) {
      const w = this.timelineWindow;
      if (!w) return 'display:none';
      const s = new Date(p.start).getTime(), e = new Date(p.end).getTime();
      const left  = (s - w.lo) / w.span * 100;
      const width = Math.max(0.8, (e - s) / w.span * 100);
      return `left:${left}%;width:${width}%`;
    },
    formatTokens(t) {
      if (!t) return '0';
      if (t >= 1_000_000) return (t / 1_000_000).toFixed(1) + 'M';
      return t >= 1000 ? (t / 1000).toFixed(1) + 'k' : t.toString();
    },
    formatTime(iso) {
      if (!iso) return '';
      return new Date(iso).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    },
    formatDay(dateStr) {
      return new Date(dateStr + 'T12:00:00Z').toLocaleDateString([], {weekday: 'short'});
    },
    formatFullDate(dateStr) {
      return new Date(dateStr + 'T12:00:00Z').toLocaleDateString([], {
        weekday: 'long', month: 'long', day: 'numeric',
      });
    },
    weekRangeLabel() {
      if (!this.selectedWeek) return '';
      const monday = this.weekToMonday(this.selectedWeek);
      const start  = new Date(monday + 'T12:00:00Z');
      const end    = new Date(start);
      end.setUTCDate(end.getUTCDate() + 6);
      const s = start.toLocaleDateString([], {month: 'short', day: 'numeric'});
      const e = end.toLocaleDateString([], {month: 'short', day: 'numeric'});
      return `${s} – ${e}`;
    },
    isToday(dateStr) { return dateStr === this.todayStr; },
  };
}
