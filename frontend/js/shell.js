// Renders the shared console shell (sidebar + topbar) so every authenticated
// page has identical navigation instead of each one hand-rolling its own.

const Shell = {
  // Visibility rules:
  //   adminOnly  — administrators only
  //   memberOnly — hidden from guests; a guest has no lasting record to read
  //   deviceOnly — only on the checkpoint device (see IS_DEVICE in config.js),
  //                so an office browser isn't offered gate hardware screens
  nav: [
    { section: 'Operations' },
    { id: 'gate', href: 'visit-site.html', icon: 'fa-shield-halved', label: 'Gate Control' },
    { id: 'history', href: 'history.html', icon: 'fa-clock-rotate-left', label: 'My Records', memberOnly: true },
    { section: 'Administration', adminOnly: true },
    { id: 'admin', href: 'admin.html', icon: 'fa-gauge-high', label: 'Overview', adminOnly: true },
    { id: 'alerts', href: 'alerts.html', icon: 'fa-triangle-exclamation', label: 'Alerts', adminOnly: true },
    { id: 'violations', href: 'violations.html', icon: 'fa-images', label: 'Captures', adminOnly: true },
    { id: 'reports', href: 'reports.html', icon: 'fa-file-lines', label: 'Reports', adminOnly: true },
    { id: 'analytics', href: 'analytics.html', icon: 'fa-chart-simple', label: 'Analytics', adminOnly: true },
    { id: 'settings', href: 'settings.html', icon: 'fa-sliders', label: 'Checkpoint Policy', adminOnly: true },
    { id: 'gps', href: 'gps.html', icon: 'fa-location-dot', label: 'Site Location', adminOnly: true },
    { id: 'audit', href: 'audit.html', icon: 'fa-scroll', label: 'Change Log', adminOnly: true },
    { section: 'Device', deviceOnly: true },
    { id: 'pihome', href: 'pi-home.html', icon: 'fa-tablet-screen-button', label: 'Device Home', deviceOnly: true },
    { id: 'kiosk', href: 'kiosk.html', icon: 'fa-expand', label: 'Checkpoint Display', deviceOnly: true },
  ],

  initials(name) {
    if (!name) return '?';
    return name.trim().split(/\s+/).slice(0, 2).map((p) => p[0].toUpperCase()).join('');
  },

  render(activeId, { title, subtitle } = {}) {
    const user = Auth.getUser() || {};
    const isAdmin = !!user.is_admin;

    const isGuest = !!user.is_guest;
    const isDevice = !!window.IS_DEVICE;

    const visible = this.nav.filter((item) => {
      if (item.adminOnly && !isAdmin) return false;
      if (item.memberOnly && isGuest) return false;
      if (item.deviceOnly && !isDevice) return false;
      return true;
    });

    // Drop a section heading that no longer has anything under it, otherwise
    // hiding its items leaves a floating label.
    const pruned = visible.filter((item, i) => {
      if (!item.section) return true;
      const next = visible[i + 1];
      return next && !next.section;
    });

    const navHtml = pruned
      .map((item) => {
        if (item.section) return `<div class="sidebar-section">${item.section}</div>`;
        const active = item.id === activeId ? ' is-active' : '';
        const current = item.id === activeId ? ' aria-current="page"' : '';
        return `<a href="${item.href}" class="nav-item${active}"${current} title="${item.label}">
                  <i class="fas ${item.icon}" aria-hidden="true"></i><span class="nav-label">${item.label}</span>
                </a>`;
      })
      .join('');

    // The three account types should read as different at a glance, not just
    // link to different pages — a colour-coded badge here, an accent border
    // on the whole shell, and (for guests) a persistent reminder banner.
    let roleLabel = 'Operator';
    let roleBadgeClass = 'badge-info';
    let roleTier = 'operator';
    if (isAdmin) { roleLabel = 'Administrator'; roleBadgeClass = 'badge-amber'; roleTier = 'admin'; }
    else if (user.is_guest) { roleLabel = 'Guest session'; roleBadgeClass = 'badge-muted'; roleTier = 'guest'; }

    const sidebar = document.createElement('aside');
    sidebar.className = 'sidebar';
    sidebar.id = 'appSidebar';
    sidebar.dataset.role = roleTier;
    sidebar.innerHTML = `
      <div class="sidebar-brand-row">
        <a href="index.html" class="sidebar-brand" title="SafetyFirst">
          <span class="sidebar-mark"><i class="fas fa-hard-hat" aria-hidden="true"></i></span>
          <span class="sidebar-brand-text">
            <span class="sidebar-name">SafetyFirst</span><br>
            <span class="sidebar-sub">Access Control</span>
          </span>
        </a>
        <button class="sidebar-collapse-btn" id="menuToggle" aria-label="Toggle navigation" aria-expanded="true">
          <i class="fas fa-angles-left" aria-hidden="true"></i>
        </button>
      </div>
      <nav class="sidebar-nav" aria-label="Main">${navHtml}</nav>
      <div class="sidebar-foot">
        <div class="sidebar-user" title="${user.name || 'Account'} — ${roleLabel}">
          <span class="avatar">${this.initials(user.name)}</span>
          <span class="sidebar-user-text">
            <span class="sidebar-user-name">${user.name || 'Account'}</span><br>
            <span class="badge ${roleBadgeClass}" style="margin-top:3px;font-size:.62rem;padding:1px 8px">${roleLabel}</span>
          </span>
        </div>
        <a href="#" data-logout class="btn btn-outline btn-sm" title="Sign out" style="width:100%;color:#cbd5e1;border-color:rgba(255,255,255,.18)">
          <i class="fas fa-arrow-right-from-bracket" aria-hidden="true"></i><span class="btn-label">Sign out</span>
        </a>
      </div>`;

    const main = document.querySelector('.main');
    const topbar = document.createElement('header');
    topbar.className = 'topbar';
    topbar.innerHTML = `
      <div style="display:flex;align-items:center;gap:14px;min-width:0">
        <button class="btn btn-outline btn-sm menu-toggle-mobile" id="menuToggleMobile" aria-label="Open navigation" aria-expanded="false">
          <i class="fas fa-bars" aria-hidden="true"></i>
        </button>
        <div style="min-width:0">
          <h1>${title || ''}</h1>
          ${subtitle ? `<div class="topbar-sub">${subtitle}</div>` : ''}
        </div>
      </div>
      <div id="topbarSlot" style="display:flex;align-items:center;gap:10px"></div>`;

    // Shown on every page a guest can reach, not hand-rolled per page — a
    // guest is trying the product, not using it, and should be reminded of
    // that consistently rather than only where one page happened to add it.
    let guestBanner = null;
    if (user.is_guest) {
      guestBanner = document.createElement('div');
      guestBanner.className = 'guest-banner';
      guestBanner.innerHTML = `
        <i class="fas fa-flask" aria-hidden="true"></i>
        <span>You're trying SafetyFirst as a guest — this session can't be signed back into, but gate activity is still recorded like any other visit.</span>
        <a href="login.html?mode=signup">Create an account<i class="fas fa-arrow-right" aria-hidden="true"></i></a>`;
    }

    // Has to land inside .app, not just anywhere in body — the collapse/close
    // rules are ".app.is-collapsed .sidebar" descendant selectors, so a
    // sidebar sitting outside .app never matches and never actually slides;
    // only .main's margin shifts, which reads as content moving but nothing
    // closing. .sidebar is position:fixed, so nesting it here doesn't touch
    // the flex layout of .app's other children.
    // A sensor alert can fire while any admin/operator tab is just sitting
    // open, so this isn't rendered once like the guest banner — it's a
    // placeholder that _pollAlerts keeps live for as long as the page is.
    const alertBanner = document.createElement('div');
    alertBanner.id = 'alertBanner';
    alertBanner.hidden = true;

    if (main) {
      main.parentElement.prepend(sidebar);
      main.prepend(topbar);
      if (guestBanner) topbar.after(guestBanner);
      (guestBanner || topbar).after(alertBanner);
      this._pollAlerts(alertBanner);
    } else {
      document.body.prepend(sidebar);
    }

    // Two buttons, one state machine. The in-sidebar button collapses the
    // desktop rail, and doubles as the "close" affordance once the mobile
    // drawer is already open. But on mobile the drawer starts off-canvas, so
    // a button living inside it can't be tapped to OPEN it — that trigger has
    // to live in the topbar, outside the thing it's opening.
    const app = document.querySelector('.app');
    const toggle = document.getElementById('menuToggle');
    const mobileToggle = document.getElementById('menuToggleMobile');
    const wide = () => window.matchMedia('(min-width: 1025px)').matches;

    let scrim = null;
    const removeScrim = () => { if (scrim) { scrim.remove(); scrim = null; } };
    const addScrim = () => {
      if (scrim) return;
      scrim = document.createElement('div');
      scrim.className = 'nav-scrim';
      scrim.addEventListener('click', () => setOpen(false));
      document.body.appendChild(scrim);
    };

    // The visible half of "open" — aria-expanded on both buttons plus the
    // icon glyph. Split out from setOpen() because two other code paths
    // (the breakpoint-cross handler, nav-link auto-close) also change
    // whether the drawer is open without going through setOpen(), and both
    // used to leave this half stale — buttons claiming "expanded" after the
    // thing they control had already closed.
    const toggleIcon = toggle.querySelector('i');
    function syncToggleUI(open) {
      toggle.setAttribute('aria-expanded', String(open));
      mobileToggle.setAttribute('aria-expanded', String(open));
      // Points at what the button does next: collapse when open, expand when collapsed.
      toggleIcon.className = open ? 'fas fa-angles-left' : 'fas fa-angles-right';
    }

    function setOpen(open) {
      if (wide()) {
        app.classList.toggle('is-collapsed', !open);
        removeScrim();
      } else {
        sidebar.classList.toggle('is-open', open);
        open ? addScrim() : removeScrim();
      }
      syncToggleUI(open);
    }

    function currentlyOpen() {
      return wide() ? app.classList.contains('is-collapsed')
                    : !sidebar.classList.contains('is-open');
    }

    // Starts collapsed everywhere — content gets the full width by default,
    // and the sidebar rail is one tap away.
    if (wide()) app.classList.add('is-collapsed');
    syncToggleUI(false);
    toggle.addEventListener('click', () => setOpen(currentlyOpen()));
    mobileToggle.addEventListener('click', () => setOpen(currentlyOpen()));

    // Crossing the breakpoint must not strand the drawer half-applied.
    window.matchMedia('(min-width: 1025px)').addEventListener('change', () => {
      sidebar.classList.remove('is-open');
      app.classList.remove('is-collapsed');
      removeScrim();
      syncToggleUI(wide());
    });

    sidebar.querySelectorAll('a[href]').forEach((link) => {
      link.addEventListener('click', () => {
        // Only meaningful on the mobile drawer — on desktop this class was
        // never set, so there's nothing to resync.
        if (!wide()) {
          sidebar.classList.remove('is-open');
          removeScrim();
          syncToggleUI(false);
        }
      });
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && sidebar.classList.contains('is-open')) setOpen(false);
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

  // Polls for an active sensor alert on every page carrying the shell —
  // the "popup at other [screens]" half of the alert feature. Runs on any
  // authenticated tab regardless of what page it's on, since a gas alert
  // is exactly the kind of thing nobody should have to be on the right
  // page to notice.
  _pollAlerts(banner) {
    const ESCAPE = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
    const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ESCAPE[c]);

    let acking = false;

    const render = (alert, isCritical, presentCount) => {
      if (!alert) {
        banner.hidden = true;
        banner.innerHTML = '';
        return;
      }
      banner.hidden = false;
      banner.className = `alert-banner ${isCritical ? 'is-critical' : 'is-warning'}`;
      const countBit = isCritical
        ? `<span class="alert-banner-count">${presentCount} on site today</span>`
        : '';
      banner.innerHTML = `
        <div class="alert-banner-top">
          <i class="fas ${isCritical ? 'fa-triangle-exclamation' : 'fa-circle-info'}"></i>
          <span class="alert-banner-msg"><strong>${esc(alert.kind)}</strong>${alert.message ? ' — ' + esc(alert.message) : ''}${isCritical ? ' — entry is paused until this is cleared.' : ''}</span>
        </div>
        <div class="alert-banner-meta">
          ${countBit}
          <button type="button" data-ack="${alert.id}">${acking ? 'Clearing…' : 'Acknowledge'}</button>
        </div>`;
      const btn = banner.querySelector('[data-ack]');
      if (btn) {
        btn.disabled = acking;
        btn.addEventListener('click', async () => {
          acking = true;
          btn.disabled = true;
          btn.textContent = 'Clearing…';
          try {
            await Auth.fetch(`/api/alerts/${alert.id}/acknowledge`, { method: 'POST' });
          } catch (e) { /* the next poll will show it's still active if this failed */ }
          acking = false;
          poll();
        });
      }
    };

    const poll = async () => {
      try {
        const res = await Auth.fetch('/api/alerts/active');
        if (!res.ok) return;
        const d = await res.json();
        const list = d.alerts || [];
        const critical = list.find((a) => a.severity === 'critical');
        const warning = !critical && list.find((a) => a.severity === 'warning');
        render(critical || warning, !!critical, d.present_count ?? 0);
      } catch (e) { /* a missed poll just tries again next interval */ }
    };

    poll();
    setInterval(poll, 4000);
  },
};

window.Shell = Shell;
