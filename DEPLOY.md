# Deploying SafetyFirst

Backend on **Hugging Face Spaces** (Docker), frontend on **Vercel** (static).

The two hosts have a circular dependency — the frontend needs the backend's
URL, and the backend needs the frontend's origin for CORS — so the order
below matters. Deploy the Space first, deploy Vercel second, then come back
and finish the Space's settings.

---

## 1. Backend → Hugging Face Space

### Create it

New Space → **SDK: Docker** → **Blank**. The repository already carries what
a Space looks for: `Dockerfile` at the root, and the YAML front matter at the
top of `README.md` (`sdk: docker`, `app_port: 7860`).

Push this repository to the Space remote:

```bash
git remote add space https://huggingface.co/spaces/<user>/<space-name>
git push space claude/construction-ppe-detection-ima5c1:main
```

### Set the environment

Space → **Settings** → **Variables and secrets**.

As **secrets** (never as plain variables):

| Name | Value |
|---|---|
| `SECRET_KEY` | a long random string |
| `JWT_SECRET_KEY` | a *different* long random string |

Generate each one separately:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

These are not optional. The app **refuses to start** on a recognised host
while either is still the development default, because that default is
printed in `backend/config.py` in this public repository — anyone who read it
could forge an admin token.

As **variables**:

| Name | Value | Why |
|---|---|---|
| `TRUSTED_PROXY_HOPS` | `1` | Spaces terminate TLS at their own proxy, so every request arrives from one address. Left at `0` the rate limiter treats the whole internet as a single caller and real users lock each other out — the public notice pages first. `1` is the true number of proxies; higher lets a caller forge the address the limiter counts. |
| `CORS_ORIGINS` | your Vercel URL (step 3) | The browser blocks the frontend from calling the API without it. |
| `PUBLIC_BASE_URL` | your Vercel URL (step 3) | Safety notices email an absolute link to `/notice.html`, which Vercel serves. Without it the backend refuses to send rather than mailing a link that will not resolve. |
| `ADMIN_EMAILS` | the email you will sign up with | Otherwise the deployed site has no administrator and no way to appoint one. See below. |

`ADMIN_EMAILS` is the one people forget and cannot recover from. Admin
rights are otherwise granted only by `make_admin.py`, which needs a shell
inside the container - and a Space does not give you one. Set this before
you sign up and that account is an administrator from its first request.
Set it afterwards and restart the Space; it promotes on boot too. Without
it you get an ordinary account and no way to reach the console at all.
Comma separated for more than one.

Optional: `GOOGLE_CLIENT_ID` for Google sign-in, and `SMTP_HOST` / `SMTP_PORT`
/ `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` to email notices automatically.
Without SMTP the notices feature still works — the console hands the officer a
link to send by hand and records when they did.

Leave `CORS_ORIGINS` and `PUBLIC_BASE_URL` until after step 3; you cannot know
the Vercel URL yet.

### Confirm it came up

The Space log should end with gunicorn binding `0.0.0.0:7860`. Then:

```bash
curl https://<user>-<space>.hf.space/api/health
```

Note that URL — it is what Vercel needs next. Hugging Face lowercases it and
replaces `/` with `-`.

---

## 2. Frontend → Vercel

Import the same GitHub repository. `vercel.json` already sets the build
command and output directory; do not override them.

**Settings → Environment Variables**:

| Name | Value |
|---|---|
| `API_BASE_URL` | `https://<user>-<space>.hf.space` — the URL from step 1 |

That variable is the whole configuration. `scripts/vercel-build.js` writes it
into the frontend's `PRODUCTION_API` at build time, so the Space URL is never
committed to the repository and a preview deployment can point somewhere else
than production.

It must be **https**. The build fails on `http://` on purpose: browsers block
an HTTPS page from calling a plain HTTP API, and the error blames mixed
content rather than this setting.

Deploy, and note the URL — `https://<project>.vercel.app`.

---

## 3. Close the loop

Go back to the Space's variables and set both to the Vercel URL, with no
trailing slash:

```
CORS_ORIGINS    = https://<project>.vercel.app
PUBLIC_BASE_URL = https://<project>.vercel.app
```

The Space restarts itself. If you skip this, the site loads and every API call
fails CORS — which looks exactly like a broken backend.

Add more origins as a comma-separated list if you also use preview URLs.

---

## 4. Point the checkpoint at it

On the Pi, in `pi_app/.env`:

```
SAFETYFIRST_API=https://<user>-<space>.hf.space
```

Restart the gate. It should log:

```
Signed in as ...
Inference: backend, falling back to onnx best.onnx (640x640, CPU) if it cannot be reached
```

That second line is the arrangement worth knowing: detection runs on the
Space, and if the Space is asleep, rate-limited, or unreachable, the gate
rules on-device and syncs its decisions when the Space answers again. A Space
cold start does not stop the gate.

---

## Verify end to end

```bash
curl https://<user>-<space>.hf.space/api/health          # backend alive
```

Then in a browser:

1. Open the Vercel URL. The console loads.
2. Sign in. If this fails with a CORS error, step 3 is unfinished.
3. Open the browser console — `[SafetyFirst]` should not be warning about a
   same-origin API. If it is, `API_BASE_URL` did not reach the build.
4. Issue a safety notice and open its link. It should load without a login.

---

## Things that will bite

**The free Space sleeps.** After inactivity it stops, and the next request
pays a cold start while gunicorn loads YOLOv8 — the Dockerfile allows 120s for
exactly this. Wake it before a demo by loading the health URL. The gate's
on-device fallback covers the gap, but the console does not have one.

**Space storage is ephemeral.** The SQLite database and evidence images live
inside the container and are lost on every rebuild or restart. Fine for a
demo; for anything kept, attach persistent storage and point `DATABASE_URL`
and `EVIDENCE_DIR` at it, or use a managed Postgres — `backend/config.py`
already rewrites a `postgres://` URL to the `postgresql://` scheme SQLAlchemy
needs.

**One worker, on purpose.** The Dockerfile runs gunicorn with `-w 1
--threads 8`. The process holds live state in memory — the latest CCTV frame
and each user's detection session — so a second worker gets its own copy and
requests fail intermittently depending on which one answers. Concurrency comes
from threads until that state moves to Redis or the database.

**Both hosts serve the frontend.** The Space also has `frontend/` inside it,
so `https://<space>.hf.space` shows the console too. Useful as a fallback, and
worth knowing so you are not confused about which one you are looking at.
