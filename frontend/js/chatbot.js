// In-app help chatbot — a floating widget mounted once by Shell.render(),
// so it shows up on every authenticated page without per-page wiring.
// Answers are scoped server-side to the signed-in account's role
// (guest/operator/admin — see backend/chatbot.py); this file only handles
// the UI and talking to /api/chat.

const Chatbot = {
  _mounted: false,
  _history: [], // [{role: 'user'|'assistant', text}], oldest first — sent back each turn for context
  _open: false,

  mount() {
    // Guard against double-mounting — some pages could plausibly call
    // Shell.render() more than once in a dev-reload scenario, and a
    // second widget stacked on the first would be a confusing bug to
    // chase down later.
    if (this._mounted) return;
    this._mounted = true;

    const wrap = document.createElement('div');
    wrap.id = 'chatbotWidget';
    wrap.innerHTML = `
      <button id="chatbotToggle" class="chatbot-toggle" aria-label="Open help chat" aria-expanded="false">
        <i class="fas fa-comment-dots" aria-hidden="true"></i>
      </button>
      <div id="chatbotPanel" class="chatbot-panel" hidden>
        <div class="chatbot-head">
          <span><i class="fas fa-comment-dots" aria-hidden="true"></i> Help</span>
          <button id="chatbotClose" class="chatbot-close" aria-label="Close help chat">
            <i class="fas fa-xmark" aria-hidden="true"></i>
          </button>
        </div>
        <div id="chatbotLog" class="chatbot-log"></div>
        <form id="chatbotForm" class="chatbot-form">
          <input id="chatbotInput" class="chatbot-input" type="text" placeholder="Ask a question…" autocomplete="off" maxlength="800">
          <button type="submit" class="chatbot-send" aria-label="Send">
            <i class="fas fa-paper-plane" aria-hidden="true"></i>
          </button>
        </form>
      </div>`;
    document.body.appendChild(wrap);

    const toggle = document.getElementById('chatbotToggle');
    const panel = document.getElementById('chatbotPanel');
    const close = document.getElementById('chatbotClose');
    const form = document.getElementById('chatbotForm');
    const input = document.getElementById('chatbotInput');
    const log = document.getElementById('chatbotLog');

    const setOpen = (open) => {
      this._open = open;
      panel.hidden = !open;
      toggle.setAttribute('aria-expanded', String(open));
      toggle.classList.toggle('is-active', open);
      if (open) {
        input.focus();
        if (!log.children.length) this._greet(log);
      }
    };

    toggle.addEventListener('click', () => setOpen(!this._open));
    close.addEventListener('click', () => setOpen(false));
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && this._open) setOpen(false);
    });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      input.disabled = true;

      this._append(log, 'user', text);
      this._history.push({ role: 'user', text });
      const typing = this._append(log, 'assistant', '…', true);

      try {
        const res = await Auth.fetch('/api/chat', {
          method: 'POST',
          body: JSON.stringify({ message: text, history: this._history.slice(0, -1) }),
        });
        const d = await res.json();
        typing.remove();
        if (!res.ok || !d.success) {
          this._append(log, 'assistant', d.message || 'Something went wrong — try again in a moment.', false, true);
        } else {
          this._append(log, 'assistant', d.reply);
          this._history.push({ role: 'assistant', text: d.reply });
        }
      } catch (err) {
        typing.remove();
        this._append(log, 'assistant', 'Could not reach the help assistant.', false, true);
      } finally {
        input.disabled = false;
        input.focus();
      }
    });
  },

  _greet(log) {
    const user = Auth.getUser() || {};
    const role = user.is_admin ? 'admin' : user.is_guest ? 'guest' : 'operator';
    const lines = {
      admin: "Hi — ask me anything about running the console: settings, alerts, thresholds, reports, whatever you're trying to find.",
      operator: "Hi — ask me about your own history, how the checkpoint works, or how badges and verdicts work.",
      guest: "Hi — you're browsing as a guest. Ask me about trying the demo, or how to sign up for a real account.",
    };
    this._append(log, 'assistant', lines[role]);
  },

  // Returns the created row element so the caller can remove it (used for
  // the transient "…" typing indicator).
  _append(log, role, text, isTyping, isError) {
    const row = document.createElement('div');
    row.className = `chatbot-msg chatbot-msg-${role}${isTyping ? ' is-typing' : ''}${isError ? ' is-error' : ''}`;
    // Only the assistant's own text is ever markdown-rendered — the
    // user's typed input and the typing/error placeholders stay as plain
    // escaped text via textContent, both because there's no formatting to
    // render there and to keep the injection surface as small as
    // possible.
    if (role === 'assistant' && !isTyping && !isError) {
      row.innerHTML = this._renderMarkdownLite(text);
    } else {
      row.textContent = text;
    }
    log.appendChild(row);
    log.scrollTop = log.scrollHeight;
    return row;
  },

  // A small, deliberately limited Markdown-ish renderer — bold and bullet
  // lists only, which covers what the system prompt actually asks the
  // model for ("a few sentences or a short list"). HTML-escapes first and
  // only then re-introduces the few tags this builds itself, so nothing
  // in the model's output (or, indirectly, in what a user typed earlier
  // in the conversation and the model echoed back) can inject real HTML.
  _renderMarkdownLite(text) {
    const escape = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const bold = (s) => s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

    const lines = escape(text).split('\n');
    let html = '';
    let inList = false;
    for (const raw of lines) {
      const line = raw.trim();
      const bullet = /^[*-]\s+(.*)/.exec(line);
      if (bullet) {
        if (!inList) { html += '<ul>'; inList = true; }
        html += `<li>${bold(bullet[1])}</li>`;
      } else {
        if (inList) { html += '</ul>'; inList = false; }
        if (line) html += `<p>${bold(line)}</p>`;
      }
    }
    if (inList) html += '</ul>';
    return html;
  },
};
