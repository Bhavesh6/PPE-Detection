// In-app help chatbot — a floating widget mounted once by Shell.render(),
// so it shows up on every authenticated page without per-page wiring.
// Answers are scoped server-side to the signed-in account's role
// (guest/operator/admin — see backend/chatbot.py); this file only handles
// the UI and talking to /api/chat.

const Chatbot = {
  _mounted: false,
  _history: [], // [{role: 'user'|'assistant', text}], oldest first — sent back each turn for context
  _open: false,
  _busy: false,

  // The conversation survives navigation. The assistant's whole job is to
  // say "go to the Settings page" — following that advice used to wipe the
  // conversation that gave it, which made the widget worse the more useful
  // its answer was. sessionStorage rather than localStorage: this should
  // outlive a page load, not a browser session.
  STORE_KEY: 'ppe_chat_history',
  STORE_OPEN_KEY: 'ppe_chat_open',

  _load() {
    try {
      this._history = JSON.parse(sessionStorage.getItem(this.STORE_KEY)) || [];
      this._open = sessionStorage.getItem(this.STORE_OPEN_KEY) === '1';
    } catch (e) {
      this._history = [];
      this._open = false;
    }
    if (!Array.isArray(this._history)) this._history = [];
  },

  _save() {
    try {
      sessionStorage.setItem(this.STORE_KEY, JSON.stringify(this._history));
      sessionStorage.setItem(this.STORE_OPEN_KEY, this._open ? '1' : '0');
    } catch (e) { /* private mode, or quota — the chat still works, it just won't persist */ }
  },

  // Openers offered as clickable chips on first open. Someone who has just
  // been handed this product doesn't know what it can answer — a blank box
  // with a cursor is the least helpful thing to show them. Scoped to match
  // what each role can actually reach, same as the server-side prompts.
  SUGGESTIONS: {
    admin: [
      'How do I change which PPE is required?',
      'What does the confidence threshold do?',
      'How do I set a sensor threshold?',
    ],
    operator: [
      'Where do I see my own history?',
      'Why was I refused entry?',
      'How does the badge scan work?',
    ],
    guest: [
      'What does this system do?',
      'How do I try the demo?',
      'Why should I create an account?',
    ],
  },

  mount() {
    // Guard against double-mounting — some pages could plausibly call
    // Shell.render() more than once in a dev-reload scenario, and a
    // second widget stacked on the first would be a confusing bug to
    // chase down later.
    if (this._mounted) return;
    this._mounted = true;
    this._load();

    const user = Auth.getUser() || {};
    const role = user.is_admin ? 'admin' : user.is_guest ? 'guest' : 'operator';
    const roleLabel = { admin: 'Administrator help', operator: 'Operator help', guest: 'Visitor help' }[role];

    const wrap = document.createElement('div');
    wrap.id = 'chatbotWidget';
    wrap.innerHTML = `
      <button id="chatbotToggle" class="chatbot-toggle" aria-label="Open help assistant" aria-expanded="false">
        <span class="chatbot-toggle-icon chatbot-toggle-open" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"
               stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
            <circle cx="8.8" cy="11.8" r=".9" fill="currentColor" stroke="none"/>
            <circle cx="12" cy="11.8" r=".9" fill="currentColor" stroke="none"/>
            <circle cx="15.2" cy="11.8" r=".9" fill="currentColor" stroke="none"/>
          </svg>
        </span>
        <span class="chatbot-toggle-icon chatbot-toggle-close" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1"
               stroke-linecap="round" stroke-linejoin="round">
            <path d="M6 6l12 12M18 6L6 18"/>
          </svg>
        </span>
      </button>

      <div id="chatbotPanel" class="chatbot-panel" role="dialog" aria-label="Help assistant" hidden>
        <div class="chatbot-head">
          <span class="chatbot-head-mark" aria-hidden="true">
            <i class="fas fa-hard-hat"></i>
          </span>
          <span class="chatbot-head-text">
            <span class="chatbot-head-name">SafetyFirst Assistant</span>
            <span class="chatbot-head-role">${roleLabel}</span>
          </span>
          <button id="chatbotReset" class="chatbot-head-btn" aria-label="Start a new conversation" title="New conversation">
            <i class="fas fa-rotate-left" aria-hidden="true"></i>
          </button>
          <button id="chatbotClose" class="chatbot-head-btn" aria-label="Close help assistant" title="Close">
            <i class="fas fa-xmark" aria-hidden="true"></i>
          </button>
        </div>

        <div id="chatbotLog" class="chatbot-log" aria-live="polite"></div>

        <form id="chatbotForm" class="chatbot-form">
          <input id="chatbotInput" class="chatbot-input" type="text"
                 placeholder="Ask about this page, a setting, anything…"
                 autocomplete="off" maxlength="800">
          <button type="submit" id="chatbotSend" class="chatbot-send" aria-label="Send message" disabled>
            <i class="fas fa-arrow-up" aria-hidden="true"></i>
          </button>
        </form>
      </div>`;
    document.body.appendChild(wrap);

    const toggle = document.getElementById('chatbotToggle');
    const panel = document.getElementById('chatbotPanel');
    const closeBtn = document.getElementById('chatbotClose');
    const resetBtn = document.getElementById('chatbotReset');
    const form = document.getElementById('chatbotForm');
    const input = document.getElementById('chatbotInput');
    const send = document.getElementById('chatbotSend');
    const log = document.getElementById('chatbotLog');

    const setOpen = (open) => {
      this._open = open;
      panel.hidden = !open;
      toggle.setAttribute('aria-expanded', String(open));
      toggle.setAttribute('aria-label', open ? 'Close help assistant' : 'Open help assistant');
      toggle.classList.toggle('is-open', open);
      wrap.classList.toggle('is-open', open);
      this._save();
      if (open) {
        if (!log.children.length) this._paint(log, role);
        // Focus after the panel is actually visible, or the browser has
        // nothing focusable to move to yet.
        requestAnimationFrame(() => input.focus());
      }
    };

    toggle.addEventListener('click', () => setOpen(!this._open));
    closeBtn.addEventListener('click', () => setOpen(false));
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && this._open) setOpen(false);
    });

    resetBtn.addEventListener('click', () => {
      this._history = [];
      this._save();
      log.innerHTML = '';
      this._paint(log, role);
      input.focus();
    });

    // Reopen where they left off — a conversation that survives navigation
    // but makes you re-open the panel on every page hasn't really survived.
    if (this._open) setOpen(true);

    // A send button that looks pressable but does nothing on an empty box
    // is a small lie; disable it until there's something to send.
    input.addEventListener('input', () => {
      send.disabled = !input.value.trim() || this._busy;
    });

    form.addEventListener('submit', (e) => {
      e.preventDefault();
      this._send(input.value, { log, input, send });
    });

    // Delegated: the chips are re-rendered on reset, so binding them
    // individually at mount time would leave the new ones dead.
    log.addEventListener('click', (e) => {
      const chip = e.target.closest('.chatbot-chip');
      if (chip) this._send(chip.textContent, { log, input, send });
    });
  },

  async _send(rawText, { log, input, send }) {
    const text = (rawText || '').trim();
    if (!text || this._busy) return;

    this._busy = true;
    input.value = '';
    send.disabled = true;
    input.disabled = true;

    // Chips are openers, not a persistent menu — once the conversation
    // has started they'd just be clutter competing with the reply.
    const chips = log.querySelector('.chatbot-chips');
    if (chips) chips.remove();

    this._append(log, 'user', text);
    this._history.push({ role: 'user', text });
    const typing = this._appendTyping(log);

    try {
      const res = await Auth.fetch('/api/chat', {
        method: 'POST',
        body: JSON.stringify({
          message: text,
          history: this._history.slice(0, -1),
          // Filename only — the server maps it against a fixed table of
          // known pages, so anything else is simply ignored.
          page: window.location.pathname.split('/').pop(),
        }),
      });
      const d = await res.json();
      typing.remove();
      if (!res.ok || !d.success) {
        // Errors are shown but deliberately not pushed into _history —
        // "the assistant is busy" isn't part of the conversation and
        // shouldn't be replayed as context on the next question, or after
        // navigating to another page.
        this._append(log, 'assistant', d.message || 'Something went wrong — try again in a moment.', { isError: true });
      } else {
        this._append(log, 'assistant', d.reply);
        this._history.push({ role: 'assistant', text: d.reply });
      }
    } catch (err) {
      typing.remove();
      this._append(log, 'assistant', 'Could not reach the assistant.', { isError: true });
    } finally {
      this._save();
      this._busy = false;
      input.disabled = false;
      send.disabled = !input.value.trim();
      input.focus();
    }
  },

  // Draws whatever the panel should currently show: a replayed
  // conversation if there is one, otherwise the greeting and openers.
  _paint(log, role) {
    if (this._history.length) {
      for (const turn of this._history) this._append(log, turn.role, turn.text);
      return;
    }
    this._greet(log, role);
  },

  _greet(log, role) {
    const lines = {
      admin: "Ask me anything about running the console — settings, alerts, thresholds, reports, or whatever you're trying to find.",
      operator: 'Ask me about your own history, how the checkpoint decides, or how badges and verdicts work.',
      guest: "You're browsing as a guest. Ask me about trying the demo, or what an account gets you.",
    };
    this._append(log, 'assistant', lines[role]);

    const chips = document.createElement('div');
    chips.className = 'chatbot-chips';
    chips.innerHTML = (this.SUGGESTIONS[role] || [])
      .map((q) => `<button type="button" class="chatbot-chip">${q}</button>`)
      .join('');
    log.appendChild(chips);
  },

  _appendTyping(log) {
    const row = document.createElement('div');
    row.className = 'chatbot-msg chatbot-msg-assistant is-typing';
    row.innerHTML = '<span class="chatbot-dot"></span><span class="chatbot-dot"></span><span class="chatbot-dot"></span>';
    row.setAttribute('aria-label', 'Assistant is typing');
    log.appendChild(row);
    log.scrollTop = log.scrollHeight;
    return row;
  },

  _append(log, role, text, { isError } = {}) {
    const row = document.createElement('div');
    row.className = `chatbot-msg chatbot-msg-${role}${isError ? ' is-error' : ''}`;
    // Only the assistant's own prose is ever markdown-rendered — the
    // user's typed input and error text stay as plain escaped text via
    // textContent, both because there's no formatting to render there and
    // to keep the injection surface as small as possible.
    if (role === 'assistant' && !isError) {
      row.innerHTML = this._renderMarkdownLite(text);
    } else {
      row.textContent = text;
    }
    log.appendChild(row);
    log.scrollTop = log.scrollHeight;
    return row;
  },

  // A small, deliberately limited Markdown-ish renderer — bold, italic,
  // and both bullet and numbered lists, which covers what this model
  // actually emits in practice (it reaches for numbered steps whenever the
  // answer is a procedure, which "how do I..." questions usually are).
  // HTML-escapes first and only then re-introduces the few tags this
  // builds itself, so nothing in the model's output (or, indirectly, in
  // what a user typed earlier in the conversation and the model echoed
  // back) can inject real HTML.
  _renderMarkdownLite(text) {
    const escape = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const inline = (s) => s
      // Bold first: **x** would otherwise be eaten by the italic rule as
      // an empty emphasis wrapping a bolded nothing.
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\n]+?)\*(?!\*)/g, '$1<em>$2</em>');

    const lines = escape(text).split('\n');
    let html = '';
    let listTag = null; // 'ul' | 'ol' | null
    const closeList = () => { if (listTag) { html += `</${listTag}>`; listTag = null; } };
    const openList = (tag) => {
      if (listTag !== tag) { closeList(); html += `<${tag}>`; listTag = tag; }
    };

    for (const raw of lines) {
      const line = raw.trim();
      // Markers only at line start and only when followed by a space —
      // "*emphasis*" opening a line isn't a list item, and "3.5 volts"
      // isn't a numbered step.
      const bullet = /^[*-]\s+(.*)/.exec(line);
      const numbered = /^\d+[.)]\s+(.*)/.exec(line);
      if (bullet) {
        openList('ul');
        html += `<li>${inline(bullet[1])}</li>`;
      } else if (numbered) {
        openList('ol');
        html += `<li>${inline(numbered[1])}</li>`;
      } else {
        closeList();
        if (line) html += `<p>${inline(line)}</p>`;
      }
    }
    closeList();
    return html;
  },
};
