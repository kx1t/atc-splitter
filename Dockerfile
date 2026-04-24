# ── Stage 1: build ──────────────────────────────────────────────────────────
FROM python:3.11-slim-trixie AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /install
COPY app/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install/deps -r requirements.txt

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.11-slim-trixie

LABEL org.opencontainers.image.title="ATC Splitter"
LABEL org.opencontainers.image.description="Browser-based WAV silence splitter with waveform visualisation"
LABEL org.opencontainers.image.source="https://github.com/kx1t/atc-splitter"
LABEL org.opencontainers.image.licenses="MIT"

# ffmpeg is optional but speeds up silence detection on demand
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from build stage
COPY --from=build /install/deps /usr/local

# App code
WORKDIR /app
COPY app/ /app/

# Persistent data volume (uploads, segments, and Whisper models all live here)
RUN mkdir -p /app/data

VOLUME ["/app/data"]

ENV PORT=5000
EXPOSE 5000

# Gunicorn with 4 sync workers; increase via GUNICORN_WORKERS env
ENV GUNICORN_WORKERS=4
# Set ENABLE_TRANSCRIPTION=true to enable per-segment speech recognition (Whisper tiny.en)
ENV ENABLE_TRANSCRIPTION=false
ENV WHISPER_MODEL_SIZE=tiny.en
CMD ["sh", "-c", "exec gunicorn --workers $GUNICORN_WORKERS --bind 0.0.0.0:$PORT --timeout 120 server:app"]
