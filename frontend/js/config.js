// ---------------------------------------------------------------------------
// SET THIS before deploying: the public HTTPS URL of the backend API.
// Leave blank while developing locally.
//   e.g. 'https://<your-space>.hf.space'  (Hugging Face Spaces)
// The page is served over HTTPS in production, so the API must be HTTPS too —
// a browser blocks an https:// page from calling an http:// endpoint.
// ---------------------------------------------------------------------------
const PRODUCTION_API = '';

// Where the frontend looks for the API.
//
// Resolution order (first match wins):
//   1. ?api=https://host        — one-off override, handy on demo day
//   2. localStorage 'ppe_api'   — sticky override set by (1)
//   3. window.API_BASE_URL      — injected by the host page, if any
//   4. PRODUCTION_API           — used whenever we're not on localhost
//   5. http://localhost:5000    — local development
//
// KIOSK NOTE (Raspberry Pi): browsers only expose the camera on a secure
// context — https:// or http://localhost. A Pi loading this page from another
// machine's LAN IP over plain HTTP (http://192.168.x.x:8000) will silently
// get no camera. An HTTPS-hosted frontend (Vercel etc.) is fine.
(function () {
  const params = new URLSearchParams(window.location.search);
  const fromQuery = params.get('api');
  if (fromQuery) {
    try { localStorage.setItem('ppe_api', fromQuery); } catch (e) { /* private mode */ }
  }

  let stored = null;
  try { stored = localStorage.getItem('ppe_api'); } catch (e) { /* private mode */ }

  const isLocal = ['localhost', '127.0.0.1', ''].includes(window.location.hostname);

  window.API_BASE_URL =
    fromQuery ||
    stored ||
    window.API_BASE_URL ||
    (isLocal ? 'http://localhost:5000' : PRODUCTION_API);

  if (!window.API_BASE_URL) {
    console.error(
      '[SafetyFirst] No API URL configured. Set PRODUCTION_API in js/config.js, ' +
      'or load the page with ?api=https://your-backend'
    );
  }

  // Warn loudly in the console if the camera can't possibly work here.
  const secure = window.isSecureContext ||
    ['localhost', '127.0.0.1'].includes(window.location.hostname);
  if (!secure) {
    console.warn(
      '[SafetyFirst] Insecure context (' + window.location.origin + '). ' +
      'Browsers block camera access outside https:// or http://localhost. ' +
      'Serve this page on the device itself, or put it behind HTTPS.'
    );
  }
  window.IS_SECURE_CONTEXT = secure;
})();

// Google OAuth Client ID (from Google Cloud Console -> APIs & Services ->
// Credentials -> OAuth client ID -> Web application). Leave blank to hide
// the "Sign in with Google" button and fall back to email/password only.
window.GOOGLE_CLIENT_ID = window.GOOGLE_CLIENT_ID || '';
