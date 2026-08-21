# SafetyFirst — single-container image (Hugging Face Spaces, Render, etc.)
#
# One container serves both the API and the console. In development they
# are two servers, because a static server that never caches makes CSS
# edits visible immediately; hosted, there is one public URL and the
# console then needs no API address at all — it is the same origin.

FROM python:3.10-slim

# OpenCV needs these even in the headless build: libGL for the image
# codecs it links against, glib for its threading primitives.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces runs the container as uid 1000. Writing the model
# cache, the SQLite file and evidence images all need a home this user
# owns — as root-owned files they fail at runtime, not at build, which is
# the slowest possible way to find out.
RUN useradd -m -u 1000 app
WORKDIR /app

# Dependencies first: this layer is ~2GB with torch and ultralytics and
# should not be rebuilt every time the application code changes.
COPY --chown=app:app requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app backend/ ./backend/
COPY --chown=app:app frontend/ ./frontend/
COPY --chown=app:app best.pt .

# Created explicitly and owned, rather than left for the app to mkdir at
# runtime: on a read-only or root-owned parent that mkdir fails on the
# first refusal photographed, which is exactly when nobody is watching
# the logs.
RUN mkdir -p /app/backend/instance/evidence /app/backend/instance/speech \
    && chown -R app:app /app/backend/instance

USER app

# Ultralytics and matplotlib both write caches to $HOME; without a
# writable one they fail on first inference rather than at startup.
ENV HOME=/home/app \
    YOLO_CONFIG_DIR=/home/app/.config/Ultralytics \
    MPLCONFIGDIR=/home/app/.cache/matplotlib \
    PYTHONUNBUFFERED=1

WORKDIR /app/backend

EXPOSE 7860

# ONE worker, deliberately — threads for concurrency instead.
#
# This process holds real state in memory: the latest frame from each
# CCTV camera (backend/cctv.py) and each user's live detection state
# (backend/detection.py). Those are module-level, so a second worker
# gets its own copy: frames posted to worker A become invisible to a
# viewer served by worker B, and a checkpoint started on one worker
# looks stopped on the other. It fails intermittently and by request,
# which is the hardest kind of bug to see.
#
# Threads share that memory, so concurrency comes from --threads. If
# this ever needs more than one worker, the state has to move to Redis
# or the database first.
#
# --timeout 120 because the first request after a cold start loads
# YOLOv8 (~6MB of weights plus torch init) and the default 30s kills it
# mid-load, producing a worker that restarts forever.
CMD ["gunicorn", "-b", "0.0.0.0:7860", "-w", "1", "--threads", "8", "--timeout", "120", "app:app"]
