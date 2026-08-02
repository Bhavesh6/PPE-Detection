# Construction Site PPE Detection System

Real-time safety monitoring for construction sites: the browser streams webcam
frames to a Flask API, which runs a YOLOv8 model to detect hard hats, safety
vests, gloves, and violations, then returns bounding boxes the frontend draws
as an overlay.

This round adds authentication (email/password + Google Sign-In) and a
rewritten backend that gives every logged-in user their own detection session
instead of one global counter shared by everyone hitting the site.

## Project Structure

```
backend/
  app.py            # Flask app factory, CORS, JWT/DB setup
  config.py         # Reads settings from environment variables
  extensions.py     # SQLAlchemy + JWTManager instances
  models.py         # User model (password or Google-linked accounts)
  auth.py           # /api/auth/signup, /login, /google, /me
  detection.py      # /api/start, /stop, /status, /results, /socket (all JWT-protected)
  ppe_detection.py  # YOLOv8 model loading + frame inference (ML side, unchanged logic)
frontend/
  index.html        # Landing page
  login.html         # Email/password + Google Sign-In
  signup.html         # Account creation
  visit-site.html     # Live monitoring dashboard (requires login)
  js/
    config.js        # API_BASE_URL + GOOGLE_CLIENT_ID
    auth.js           # Token storage, auth-guarded fetch, logout
    camera.js          # getUserMedia capture + frame streaming
    main.js             # Nav/fade-in utilities
best.pt             # Trained YOLOv8 weights
requirements.txt
Dockerfile           # For Render / Hugging Face Spaces / any container host
render.yaml
vercel.json          # Deploys frontend/ as a static site
```

## How auth works

- **Email/password**: `/api/auth/signup` and `/api/auth/login` return a JWT.
  Passwords are hashed with Werkzeug's `generate_password_hash` — never
  stored in plaintext.
- **Google Sign-In**: the frontend loads Google Identity Services, gets an
  ID token, and POSTs it to `/api/auth/google`. The backend verifies the
  token's signature against Google's public keys (`google.oauth2.id_token`)
  before trusting it — it never accepts a client-asserted email.
- The JWT is stored in `localStorage` and sent as `Authorization: Bearer
  <token>` on every API call via the `Auth.fetch()` helper in `auth.js`.
- `visit-site.html` calls `Auth.requireAuth()` on load and bounces
  unauthenticated visitors to `login.html`.

## Local Setup

### 1. Backend

```bash
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env           # then edit .env
```

Fill in `.env`:
- `SECRET_KEY`, `JWT_SECRET_KEY`: any random strings for local dev.
- `GOOGLE_CLIENT_ID`: see [Setting up Google Sign-In](#setting-up-google-sign-in-5-minutes) below.
  You can leave this blank — the app still works with email/password, the
  Google button just won't render.

Run the API:

```bash
cd backend
python app.py
```

The API listens on `http://localhost:5000` and creates `app.db` (SQLite)
automatically on first run.

### 2. Frontend

Edit `frontend/js/config.js` if your API isn't on `localhost:5000`, and set
`GOOGLE_CLIENT_ID` there too (must match the backend's).

Serve the static files (opening `index.html` directly as a `file://` URL
will break camera access and Google Sign-In, which both require a real
origin):

```bash
cd frontend
python -m http.server 8000
```

Open `http://localhost:8000`.

## Setting up Google Sign-In (5 minutes)

1. Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials).
2. Create a project (or pick an existing one).
3. **Create Credentials -> OAuth client ID -> Web application**.
4. Under **Authorized JavaScript origins**, add every origin you'll serve
   the frontend from, e.g. `http://localhost:8000` and your production
   domain (`https://your-app.vercel.app`).
5. Copy the generated **Client ID** into:
   - `backend/.env` -> `GOOGLE_CLIENT_ID`
   - `frontend/js/config.js` -> `window.GOOGLE_CLIENT_ID`
6. Restart the backend so it picks up the new env var.

No client secret is needed — this uses Google Identity Services' ID-token
flow, verified server-side by ID alone.

## Deployment

- **Backend**: any container host works via the `Dockerfile` (Hugging Face
  Spaces, Render, Fly.io, etc). `render.yaml` is included for Render.
  Set `GOOGLE_CLIENT_ID` and `CORS_ORIGINS` (your deployed frontend URL) as
  environment variables on the host — don't hardcode them.
- **Frontend**: static hosting (Vercel, Netlify, GitHub Pages). `vercel.json`
  points Vercel at the `frontend/` directory. Before deploying, update
  `frontend/js/config.js` with the deployed backend's URL and set
  `GOOGLE_CLIENT_ID`, then add the frontend's deployed origin to the OAuth
  client's Authorized JavaScript origins in Google Cloud Console.

## What changed from the previous version

- Added real authentication (email/password + Google Sign-In); the
  detection dashboard now requires login.
- Backend detection state was global (`violation_count`, `detection_active`,
  etc. as module-level variables shared by *every* visitor). It's now keyed
  per authenticated user, so concurrent users don't see or reset each
  other's counts.
- Added cumulative session totals alongside the existing live/per-frame
  counts (the old counters were overwritten every frame, so violations
  weren't actually being tracked over time).
- CORS is now restricted to configured frontend origins instead of `*`,
  and all detection endpoints require a valid JWT.
- Removed the unauthenticated serverless mock endpoints that returned
  hardcoded fake detections regardless of input.
- `js/main.js` no longer references a `/api/upload` endpoint that never
  existed on the backend.

## Demo mode

If the API is unreachable, `visit-site.html` falls back to a clearly
labeled "Demo Mode" showing simulated detections so the UI still has
something to show — it never silently pretends to be a live connection.

## Credits

- YOLOv8 by Ultralytics (model trained by the team)
- TailwindCSS for styling
