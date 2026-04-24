# ATC Splitter

Browser-based WAV audio silence splitter with waveform visualization.

Upload WAV files → visualize waveform → auto-split on silence → play segments → re-split at any point → losslessly rebuild adjacent segments → export CSV transcripts.

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

## Configuration (docker-compose.yml environment variables)

| Variable | Default | Description |
|---|---|---|
| PORT | 5000 | HTTP port inside container |
| GUNICORN_WORKERS | 4 | Number of worker processes |

## License

MIT
