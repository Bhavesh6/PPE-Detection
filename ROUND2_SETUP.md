> **Historical.** This predates the admin console, the RFID gate hardware,
> configurable policy, evidence capture, and everything else described in
> the root [`README.md`](README.md) — read that first. Kept here for
> history, not as current setup instructions.

# Round 2 — Local Setup Checklist

Working doc for getting this running on your own Windows PC. Full technical
details live in `README.md`; this is just the step-by-step for right now.

## 1. Install tools (PowerShell)

```powershell
irm https://claude.ai/install.ps1 | iex
winget install --id Python.Python.3.12 -e
winget install --id Git.Git -e
```

Close and reopen PowerShell (fresh window), then verify all three:

```powershell
claude --version
python --version
git --version
```

## 2. Get the code

```powershell
git clone https://github.com/Bhavesh6/PPE-Detection.git
cd PPE-Detection
git checkout claude/construction-ppe-detection-ima5c1
```

## 3. Run the backend

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
cd backend
python app.py
```

Should print `Running on http://127.0.0.1:5000`. `best.pt` is already in the
repo root, no manual model placement needed. Leave this terminal running.

## 4. Run the frontend (new terminal)

```powershell
cd PPE-Detection\frontend
python -m http.server 8000
```

Open **http://localhost:8000** in your browser (not a `file://` path, or
camera access and Google Sign-In will break).

## 5. Try it

Sign Up -> lands on the live dashboard -> **Start Detection** -> grant
camera permission -> point it at yourself / whatever's around -> watch for
hardhat/vest/violation boxes.

## Where things stand

**Done** (backend + frontend + auth rebuild, all pushed to
[PR #1](https://github.com/Bhavesh6/PPE-Detection/pull/1)):
- Email/password + Google Sign-In auth (JWT-based)
- Per-user detection state (old version shared one global counter across
  every visitor — fixed)
- Live counts + session totals on the dashboard
- Verified end-to-end: signup, login, detection loop, logout, auth guard
  all confirmed working against the real model
- Fixed a bug where a blocked/failed AOS CDN script could silently kill
  the whole live dashboard (buttons stop responding)
- Installed UI/UX design skills (`ui-ux-pro-max`, `impeccable`, etc.) in
  `.claude/skills/` for the polish pass

**Not done yet**:
- **Google OAuth Client ID** — needs your friend's Google account. 5 min
  in [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
  → Create Credentials → OAuth client ID → Web application → add
  `http://localhost:8000` (and your eventual production domain) as an
  Authorized JavaScript origin. Drop the Client ID into `.env` and
  `frontend/js/config.js`. Until then, email/password still works fine.
- **UI/UX polish pass** on `index.html` / `visit-site.html` — do this
  once you've confirmed the app runs on your machine.
- **Deployment** for a live demo link (Render/HF Spaces backend, Vercel
  frontend) — last step, once everything above is solid.
- **Merging into `Nexus369/construction-ppe-detection`** once that repo
  unlocks after round 1 judging.

## If you start a local Claude Code session here

It won't remember this conversation, but it'll see the same code and the
same `.claude/skills/`. Worth telling it explicitly: "read README.md and
ROUND2_SETUP.md, and check PR #1 on this repo for context" so it doesn't
start blind.
