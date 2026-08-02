// Shared authentication helpers used across all pages.

const AUTH_TOKEN_KEY = 'ppe_auth_token';
const AUTH_USER_KEY = 'ppe_auth_user';

const Auth = {
    getToken() {
        return localStorage.getItem(AUTH_TOKEN_KEY);
    },

    getUser() {
        const raw = localStorage.getItem(AUTH_USER_KEY);
        return raw ? JSON.parse(raw) : null;
    },

    isLoggedIn() {
        return !!this.getToken();
    },

    setSession(token, user) {
        localStorage.setItem(AUTH_TOKEN_KEY, token);
        localStorage.setItem(AUTH_USER_KEY, JSON.stringify(user));
    },

    logout() {
        localStorage.removeItem(AUTH_TOKEN_KEY);
        localStorage.removeItem(AUTH_USER_KEY);
        window.location.href = 'login.html';
    },

    // Redirect to login if there's no session. Call at the top of any
    // protected page.
    requireAuth() {
        if (!this.isLoggedIn()) {
            window.location.href = 'login.html';
        }
    },

    // Redirect away from login/signup if already logged in.
    redirectIfLoggedIn(destination) {
        if (this.isLoggedIn()) {
            window.location.href = destination || 'visit-site.html';
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
    document.querySelectorAll('[data-logout]').forEach((el) => {
        el.addEventListener('click', (e) => {
            e.preventDefault();
            Auth.logout();
        });
    });
});
