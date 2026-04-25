<!--
  © 2026 by Ramon F. Kolb, kx1t.
  This program is free software: you can redistribute it and/or modify
  it under the terms of the GNU General Public License as published by
  the Free Software Foundation, either version 3 of the License, or
  (at your option) any later version.

  This program is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
  GNU General Public License for more details.

  You should have received a copy of the GNU General Public License
  along with this program. If not, see <https://www.gnu.org/licenses/>.
-->

# ATC Splitter

Browser-based WAV audio silence splitter with waveform visualization.

Upload WAV files → visualize waveform → auto-split on silence → play segments → re-split at any point → losslessly rebuild adjacent segments → renumber segments (001..N) → export CSV transcripts.

## Quick start with Docker

```bash
# Pull and run the latest image
docker compose up -d
# Then open http://localhost:5000
```

## Local development

```bash
cd app
pip install -r requirements.txt
python server.py
```

## Optional: offline speech recognition (US English)

```bash
./setup_vosk_en_us.sh          # downloads model (~50 MB)
python batch_split_and_transcribe.py \
  --input-dir . \
  --segments-root segments \
  --csv-out transcripts.csv \
  --min-silence-ms 500 \
  --enable-speech-reco \
  --vosk-model ./vosk-model-small-en-us-0.15
```

## Building the image locally

```bash
docker build -t atc-splitter .
```

## One-time segment renumbering script

The container image includes a /scripts directory with a one-time renumber utility
that harmonizes split/merge history and rewrites manifests to sequential indices.

```bash
docker compose exec atc-splitter python /scripts/renumber_segments_once.py --pretty
```

Optional: renumber only one source stem.

```bash
docker compose exec atc-splitter python /scripts/renumber_segments_once.py --source-stem my_audio --pretty
```

## Configuration (docker-compose.yml environment variables)

| Variable | Default | Description |
| --- | --- | --- |
| PORT | 5000 | HTTP port inside container |
| GUNICORN_WORKERS | 4 | Number of worker processes |

## License

GPLv3 (GNU General Public License, version 3 or later)
