// Central place to point the frontend at a backend deployment.
// For local dev: run backend/app.py and leave this as localhost.
// For production: set this to your deployed API URL (Render/HF Spaces/etc).
window.API_BASE_URL = window.API_BASE_URL || 'http://localhost:5000';

// Google OAuth Client ID (from Google Cloud Console -> APIs & Services ->
// Credentials -> OAuth client ID -> Web application). Leave blank to hide
// the "Sign in with Google" button and fall back to email/password only.
window.GOOGLE_CLIENT_ID = window.GOOGLE_CLIENT_ID || '';
