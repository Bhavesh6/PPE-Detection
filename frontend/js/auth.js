// Shared authentication helpers used across all pages.

const AUTH_TOKEN_KEY = 'ppe_auth_token';
const AUTH_USER_KEY = 'ppe_auth_user';

// Session lives in localStorage ("Remember me" — survives closing the
// browser) or sessionStorage (cleared the moment the tab/browser closes).
// Whichever store actually holds a session is the one read back, so this
// doesn't need to remember which mode was used at login.
const Auth = {
    _store() {
        return localStorage.getItem(AUTH_TOKEN_KEY) ? localStorage : sessionStorage;
    },

    getToken() {
        return this._store().getItem(AUTH_TOKEN_KEY);
    },

    getUser() {
        const raw = this._store().getItem(AUTH_USER_KEY);
        return raw ? JSON.parse(raw) : null;
    },

    isLoggedIn() {
        return !!this.getToken();
    },

    setSession(token, user, remember = true) {
        const store = remember ? localStorage : sessionStorage;
        const other = remember ? sessionStorage : localStorage;
        other.removeItem(AUTH_TOKEN_KEY);
        other.removeItem(AUTH_USER_KEY);
        store.setItem(AUTH_TOKEN_KEY, token);
        store.setItem(AUTH_USER_KEY, JSON.stringify(user));
    },

    logout() {
        localStorage.removeItem(AUTH_TOKEN_KEY);
        localStorage.removeItem(AUTH_USER_KEY);
        sessionStorage.removeItem(AUTH_TOKEN_KEY);
        sessionStorage.removeItem(AUTH_USER_KEY);
        window.location.href = 'login.html';
    },

    // Redirect to login if there's no session. Call at the top of any
    // protected page.
    requireAuth() {
        if (!this.isLoggedIn()) {
            window.location.href = 'login.html';
        }
    },

    // Redirect away from login/signup if already logged in. With no explicit
    // destination, sends each role to its own landing page — an admin who
    // wanders back to /login.html should land back in the console, not get
    // shown the same gate-operator screen everyone else gets.
    redirectIfLoggedIn(destination) {
        if (this.isLoggedIn()) {
            const user = this.getUser();
            window.location.href = destination || (user && user.is_admin ? 'admin.html' : 'visit-site.html');
        }
    },

    // Redirect to the dashboard if there's no session or the user isn't an
    // admin. Call at the top of admin.html.
    requireAdmin() {
        this.requireAuth();
        const user = this.getUser();
        if (!user || !user.is_admin) {
            window.location.href = 'visit-site.html';
        }
    },

    // fetch() wrapper that attaches the JWT and handles 401s uniformly.
    async fetch(path, options = {}) {
        const headers = Object.assign({}, options.headers, {
            Authorization: `Bearer ${this.getToken()}`,
        });
        if (options.body && !headers['Content-Type']) {
            headers['Content-Type'] = 'application/json';
        }

        const response = await fetch(`${window.API_BASE_URL}${path}`, Object.assign({}, options, { headers }));

        if (response.status === 401) {
            this.logout();
            throw new Error('Session expired, please log in again');
        }

        return response;
    },
};

// Populate any element with [data-user-name] on protected pages, and wire
// up any [data-logout] button.
document.addEventListener('DOMContentLoaded', () => {
    const user = Auth.getUser();
    document.querySelectorAll('[data-user-name]').forEach((el) => {
        if (user) el.textContent = user.name;
    });
    document.querySelectorAll('[data-admin-only]').forEach((el) => {
        if (user && user.is_admin) el.classList.remove('hidden');
    });
    document.querySelectorAll('[data-guest-only]').forEach((el) => {
        if (user && user.is_guest) el.classList.remove('hidden');
    });
    document.querySelectorAll('[data-logout]').forEach((el) => {
        el.addEventListener('click', (e) => {
            e.preventDefault();
            Auth.logout();
        });
    });
});
