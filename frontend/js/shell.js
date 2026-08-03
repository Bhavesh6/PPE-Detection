// Renders the shared console shell (sidebar + topbar) so every authenticated
// page has identical navigation instead of each one hand-rolling its own.

const Shell = {
  nav: [
    { section: 'Operations' },
    { id: 'gate', href: 'visit-site.html', icon: 'fa-shield-halved', label: 'Gate Control' },
    { id: 'history', href: 'history.html', icon: 'fa-clock-rotate-left', label: 'My Records' },
    { section: 'Administration', adminOnly: true },
    { id: 'admin', href: 'admin.html', icon: 'fa-gauge-high', label: 'Overview', adminOnly: true },
  ],

  initials(name) {
    if (!name) return '?';
    return name.trim().split(/\s+/).slice(0, 2).map((p) => p[0].toUpperCase()).join('');
  },

  render(activeId, { title, subtitle } = {}) {
    const user = Auth.getUser() || {};
    const isAdmin = !!user.is_admin;

    const navHtml = this.nav
      .filter((item) => !item.adminOnly || isAdmin)
      .map((item) => {
        if (item.section) return `<div class="sidebar-section">${item.section}</div>`;
        const active = item.id === activeId ? ' is-active' : '';
        const current = item.id === activeId ? ' aria-current="page"' : '';
        return `<a href="${item.href}" class="nav-item${active}"${current}>
                  <i class="fas ${item.icon}" aria-hidden="true"></i>${item.label}
                </a>`;
      })
      .join('');

    let roleLabel = 'Operator';
    if (isAdmin) roleLabel = 'Administrator';
    else if (user.is_guest) roleLabel = 'Guest session';

    const sidebar = document.createElement('aside');
    sidebar.className = 'sidebar';
    sidebar.id = 'appSidebar';
    sidebar.innerHTML = `
      <a href="index.html" class="sidebar-brand">
        <span class="sidebar-mark"><i class="fas fa-hard-hat" aria-hidden="true"></i></span>
        <span>
          <span class="sidebar-name">SafetyFirst</span><br>
          <span class="sidebar-sub">Access Control</span>
        </span>
      </a>
      <nav class="sidebar-nav" aria-label="Main">${navHtml}</nav>
      <div class="sidebar-foot">
        <div class="sidebar-user">
          <span class="avatar">${this.initials(user.name)}</span>
          <span>
            <span class="sidebar-user-name">${user.name || 'Account'}</span><br>
            <span class="sidebar-user-role">${roleLabel}</span>
          </span>
        </div>
        <a href="#" data-logout class="btn btn-outline btn-sm" style="width:100%;color:#cbd5e1;border-color:rgba(255,255,255,.18)">
          <i class="fas fa-arrow-right-from-bracket" aria-hidden="true"></i>Sign out
        </a>
      </div>`;

    const main = document.querySelector('.main');
    const topbar = document.createElement('header');
    topbar.className = 'topbar';
    topbar.innerHTML = `
      <div style="display:flex;align-items:center;gap:14px;min-width:0">
        <button class="btn btn-outline btn-sm menu-toggle" id="menuToggle" aria-label="Toggle navigation" aria-expanded="false">
          <i class="fas fa-bars" aria-hidden="true"></i>
        </button>
        <div style="min-width:0">
          <h1>${title || ''}</h1>
          ${subtitle ? `<div class="topbar-sub">${subtitle}</div>` : ''}
        </div>
      </div>
      <div id="topbarSlot" style="display:flex;align-items:center;gap:10px"></div>`;

    document.body.prepend(sidebar);
    if (main) main.prepend(topbar);

    const toggle = document.getElementById('menuToggle');
    toggle.addEventListener('click', () => {
      const open = sidebar.classList.toggle('is-open');
      toggle.setAttribute('aria-expanded', String(open));
    });
    sidebar.querySelectorAll('a[href]').forEach((link) => {
      link.addEventListener('click', () => sidebar.classList.remove('is-open'));
    });

    // auth.js wires [data-logout] on DOMContentLoaded, which has already
    // fired by the time the shell is injected — bind it here instead.
    sidebar.querySelectorAll('[data-logout]').forEach((el) => {
      el.addEventListener('click', (e) => {
        e.preventDefault();
        Auth.logout();
      });
    });
  },
};

window.Shell = Shell;
