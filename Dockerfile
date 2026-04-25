# © 2026 by Ramon F. Kolb, kx1t.
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

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
LABEL org.opencontainers.image.licenses="GPL-3.0-or-later"

# ffmpeg is optional but speeds up silence detection on demand
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from build stage
COPY --from=build /install/deps /usr/local

WORKDIR /app

# Persistent data volume (uploads, segments, and Whisper models all live here)
VOLUME ["/app/data"]

# App code
COPY app/ /app/
COPY scripts/ /scripts/

ENV PORT=5000
EXPOSE 5000

# Gunicorn with 4 sync workers; increase via GUNICORN_WORKERS env
ENV GUNICORN_WORKERS=4
# Gunicorn worker timeout in seconds. Raise this if transcription of long segments times out (502).
ENV GUNICORN_TIMEOUT=300
# Set ENABLE_TRANSCRIPTION=true to enable per-segment speech recognition (Whisper tiny.en)
ENV ENABLE_TRANSCRIPTION=false
ENV WHISPER_MODEL_SIZE=tiny.en
# Optional Hugging Face token for higher download rate limits when models are first fetched.
ENV HF_TOKEN=
CMD ["sh", "-c", "exec gunicorn --workers $GUNICORN_WORKERS --bind 0.0.0.0:$PORT --timeout $GUNICORN_TIMEOUT server:app"]
