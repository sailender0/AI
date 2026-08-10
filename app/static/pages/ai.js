  let currentConvId = null;
  let conversations  = [];
  let isSending      = false;

  async function onBaseReady(data) {
    await loadConversations();
    document.getElementById('chat-input').focus();
  }

  async function loadConversations() {
    const res  = await fetch('/api/chat/conversations', { credentials: 'include' });
    const data = await res.json();
    conversations = Array.isArray(data) ? data : [];
    renderConvList();
  }

  function renderConvList() {
    const list  = document.getElementById('conv-list');
    const empty = document.getElementById('conv-empty');
    if (!conversations.length) {
      list.innerHTML = '';
      list.appendChild(empty);
      empty.classList.remove('hidden');
      return;
    }
    list.innerHTML = conversations.map(c => `
      <div class="conv-item ${c.id === currentConvId ? 'active' : ''}"
           onclick="selectConversation('${c.id}')">
        <div style="flex:1;min-width:0">
          <p class="text-xs font-medium" style="color:var(--text-1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.title)}</p>
          <p class="text-xs" style="color:var(--text-3)">${timeAgo(c.updated_at)}</p>
        </div>
        <button class="conv-del w-5 h-5 flex items-center justify-center rounded transition"
          style="color:var(--text-3)" onmouseover="this.style.color='#ef4444'" onmouseout="this.style.color='var(--text-3)'"
          onclick="deleteConversation('${c.id}',event)" title="Delete">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>
          </svg>
        </button>
      </div>`).join('');
  }

  async function selectConversation(id) {
    currentConvId = id;
    renderConvList();
    showChatState();
    const res  = await fetch(`/api/chat/conversations/${id}/messages`, { credentials: 'include' });
    const msgs = await res.json();
    document.getElementById('messages-area').innerHTML = '';
    (Array.isArray(msgs) ? msgs : []).forEach(m => appendBubble(m.role, m.content, m.id));
    scrollBottom();
  }

  function newChat() {
    currentConvId = null;
    renderConvList();
    showWelcomeState();
    document.getElementById('chat-input').focus();
  }

  async function deleteConversation(id, e) {
    e.stopPropagation();
    await fetch(`/api/chat/conversations/${id}`, { method: 'DELETE', credentials: 'include' });
    if (currentConvId === id) { currentConvId = null; showWelcomeState(); }
    conversations = conversations.filter(c => c.id !== id);
    renderConvList();
  }

  async function sendMessage() {
    if (isSending) return;
    const input    = document.getElementById('chat-input');
    const question = input.value.trim();
    if (!question) return;

    isSending = true;
    input.value = '';
    autoResizeTextarea(input);
    document.getElementById('send-btn').disabled = true;

    showChatState();
    appendBubble('user', question);
    showTyping(true);
    scrollBottom();

    try {
      if (!currentConvId) {
        const r = await fetch('/api/chat/conversations', { method: 'POST', credentials: 'include' });
        const d = await r.json();
        currentConvId = d.id;
        conversations.unshift(d);
        renderConvList();
      }

      const res = await fetch(`/api/chat/conversations/${currentConvId}/ask/stream`, {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, tz: Intl.DateTimeFormat().resolvedOptions().timeZone }),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        showTyping(false);
        appendBubble('assistant', '⚠ ' + (data.error || `Error ${res.status}`));
        return;
      }

      const reader  = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer    = '';
      let bubbleEl  = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          let msg;
          try { msg = JSON.parse(line.slice(6)); } catch { continue; }

          if (msg.token) {
            if (!bubbleEl) { showTyping(false); bubbleEl = _createStreamBubble(); }
            bubbleEl.textContent += msg.token;
            scrollBottom();
          } else if (msg.done) {
            if (msg.chart_link && bubbleEl) _appendChartChip(bubbleEl, msg.chart_link);
            if (msg.ai_message && bubbleEl) _appendEmailAction(bubbleEl, msg.ai_message.id);
            const conv = conversations.find(c => c.id === currentConvId);
            if (conv && msg.conversation_title) {
              conv.title      = msg.conversation_title;
              conv.updated_at = (msg.ai_message || {}).created_at || conv.updated_at;
              conversations   = [conv, ...conversations.filter(c => c.id !== currentConvId)];
              renderConvList();
            }
          } else if (msg.error) {
            showTyping(false);
            if (!bubbleEl) bubbleEl = _createStreamBubble();
            bubbleEl.textContent = '⚠ ' + msg.error;
          }
        }
      }

      if (!bubbleEl) { showTyping(false); appendBubble('assistant', '⚠ No response received.'); }

    } catch {
      showTyping(false);
      appendBubble('assistant', '⚠ Request failed. Please try again.');
    } finally {
      isSending = false;
      document.getElementById('send-btn').disabled = false;
      scrollBottom();
      input.focus();
    }
  }

  function _createStreamBubble() {
    const area = document.getElementById('messages-area');
    const el   = document.createElement('div');
    el.className = 'msg-ai';
    el.innerHTML = `<div class="msg-ai-avatar">AI</div><div class="msg-ai-bubble"></div>`;
    area.appendChild(el);
    return el.querySelector('.msg-ai-bubble');
  }

  function _appendChartChip(bubbleEl, link) {
    const a = document.createElement('a');
    a.href = link.href;
    a.textContent = `📊 ${link.label} →`;
    a.style.cssText = 'display:flex;width:fit-content;align-items:center;gap:4px;'
      + 'margin-top:8px;font-size:11px;padding:3px 9px;border-radius:999px;'
      + 'background:var(--surface);border:1px solid var(--border);'
      + 'color:var(--chrome);text-decoration:none';
    bubbleEl.appendChild(a);
  }

  function _appendEmailPreview(html, messageId) {
    const area = document.getElementById('messages-area');
    const el   = document.createElement('div');
    el.className = 'msg-ai';
    el.innerHTML = `<div class="msg-ai-avatar">AI</div>
      <div class="msg-ai-bubble" style="max-width:100%">
        <div style="font-size:12px;color:var(--chrome);margin-bottom:6px">Preview — this is what will be emailed to you:</div>
        <iframe sandbox style="width:100%;height:340px;border:1px solid var(--border);border-radius:8px;background:#fff"></iframe>
        <div style="display:flex;gap:8px;margin-top:8px">
          <button class="email-send-btn" style="font-size:12px;padding:6px 14px;border:none;border-radius:8px;background:#4f46e5;color:#fff;cursor:pointer">✉ Send to my inbox</button>
          <button class="email-cancel-btn" style="font-size:12px;padding:6px 14px;border-radius:8px;background:var(--surface);border:1px solid var(--border);color:var(--chrome);cursor:pointer">Cancel</button>
        </div>
      </div>`;
    area.appendChild(el);
    el.querySelector('iframe').srcdoc = html;
    const send = el.querySelector('.email-send-btn');
    send.dataset.msgId = messageId || '';
    send.addEventListener('click', (e) => emailAnswer(e.target));
    el.querySelector('.email-cancel-btn').addEventListener('click', () => el.remove());
    scrollBottom();
    return el;
  }

  async function emailAnswer(btn) {
    btn.disabled = true; btn.textContent = 'Sending…';
    const wrap = btn.closest('.msg-ai-bubble');
    try {
      const r = await fetch(`/api/chat/conversations/${currentConvId}/email`, {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message_id: btn.dataset.msgId || null }),
      });
      const d = await r.json().catch(() => ({}));
      wrap.textContent = d.sent ? '✉ Sent to your inbox.'
        : "⚠ Couldn't send — sign out and back in to grant Mail.Send.";
    } catch {
      btn.disabled = false; btn.textContent = '✉ Send to my inbox';
    }
  }

  function _appendEmailAction(bubbleEl, id) {
    if (!hasPerm('email_ai_answer')) return;
    const b = document.createElement('button');
    b.textContent = '✉ Email this';
    b.style.cssText = 'display:block;margin-top:8px;font-size:11px;padding:3px 9px;'
      + 'border-radius:999px;background:var(--surface);border:1px solid var(--border);'
      + 'color:var(--chrome);cursor:pointer';
    b.addEventListener('click', () => previewEmail(id, b));
    bubbleEl.appendChild(b);
  }

  async function previewEmail(messageId, btn) {
    if (btn) btn.disabled = true;
    try {
      const r = await fetch(`/api/chat/conversations/${currentConvId}/email/preview`, {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message_id: messageId }),
      });
      const d = await r.json().catch(() => ({}));
      if (d.html) _appendEmailPreview(d.html, messageId);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function appendBubble(role, content, id) {
    const area = document.getElementById('messages-area');
    const el   = document.createElement('div');
    if (role === 'user') {
      el.className = 'msg-user';
      el.innerHTML = `<div class="msg-user-bubble">${esc(content)}</div>`;
    } else {
      el.className = 'msg-ai';
      el.innerHTML = `<div class="msg-ai-avatar">AI</div><div class="msg-ai-bubble">${esc(content)}</div>`;
    }
    area.appendChild(el);
    if (role === 'assistant' && id) _appendEmailAction(el.querySelector('.msg-ai-bubble'), id);
  }

  function showTyping(on) {
    const existing = document.getElementById('typing-bubble');
    if (existing) existing.remove();
    if (!on) return;
    const area = document.createElement('div');
    area.id        = 'typing-bubble';
    area.className = 'msg-ai';
    area.innerHTML = `<div class="msg-ai-avatar">AI</div>
      <div class="msg-ai-bubble" style="padding:12px 16px">
        <span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span>
      </div>`;
    document.getElementById('messages-area').appendChild(area);
  }

  function scrollBottom() {
    const a = document.getElementById('messages-area');
    if (a) a.scrollTop = a.scrollHeight;
  }

  function setQuestion(q) {
    const inp = document.getElementById('chat-input');
    inp.value = q;
    autoResizeTextarea(inp);
    inp.focus();
  }

  function showChatState() {
    document.getElementById('welcome-state').classList.add('hidden');
    document.getElementById('chat-state').classList.remove('hidden');
  }
  function showWelcomeState() {
    document.getElementById('welcome-state').classList.remove('hidden');
    document.getElementById('chat-state').classList.add('hidden');
    document.getElementById('messages-area').innerHTML = '';
  }
  function autoResizeTextarea(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
  }
  function handleInputKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  }
  function timeAgo(iso) {
    const d = Math.floor((Date.now() - new Date(iso)) / 60000);
    if (d < 1)   return 'just now';
    if (d < 60)  return `${d}m ago`;
    const h = Math.floor(d / 60);
    if (h < 24)  return `${h}h ago`;
    const dy = Math.floor(h / 24);
    if (dy < 7)  return `${dy}d ago`;
    return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }
