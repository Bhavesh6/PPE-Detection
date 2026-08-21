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

  // Empty string means "same origin" — relative URLs, i.e. the server
  // that served this page is also the API. That is the shape of a single
  // hosted deployment (Hugging Face Space, Render, one container), where
  // the backend serves the console itself and there is no second host to
  // name. It is the last resort rather than the first, so a split
  // deployment can still point somewhere else.
  const SAME_ORIGIN = '';

  let resolved;
  if (fromQuery) resolved = fromQuery;
  else if (stored) resolved = stored;
  else if (window.API_BASE_URL) resolved = window.API_BASE_URL;
  else if (isLocal) resolved = 'http://localhost:5000';
  else if (PRODUCTION_API) resolved = PRODUCTION_API;
  else resolved = SAME_ORIGIN;

  window.API_BASE_URL = resolved;

  if (resolved === SAME_ORIGIN && !isLocal) {
    // Not an error — but if this page is being served by a plain static
    // server rather than the backend, every API call will 404 against
    // that static server, which looks like the API being broken rather
    // than unconfigured. Say so once, here, where it is diagnosable.
    console.info(
      '[SafetyFirst] Using same-origin API (' + window.location.origin + '). ' +
      'If this page is served separately from the backend, load it once with ' +
      '?api=https://your-backend — it is remembered afterwards.'
    );
  }

  // Is this the gate device, or somebody's browser?
  //   ?device=1  marks this browser as the checkpoint device and sticks.
  //   ?device=0  clears it.
  // The Pi kiosk is launched once with ?device=1; every other browser stays
  // a plain client and never sees the device-only screens.
  const deviceFlag = params.get('device');
  if (deviceFlag !== null) {
    try {
      if (deviceFlag === '0') localStorage.removeItem('ppe_device');
      else localStorage.setItem('ppe_device', '1');
    } catch (e) { /* private mode */ }
  }
  let isDevice = false;
  try { isDevice = localStorage.getItem('ppe_device') === '1'; } catch (e) { /* private mode */ }
  window.IS_DEVICE = isDevice;

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
