#!/usr/bin/env python3
"""ATC Splitter — Flask backend.

Endpoints:
  POST   /api/upload                 Upload one or more WAV files
  GET    /api/files                  List all uploaded source files with metadata
  DELETE /api/files/<name>           Delete an uploaded file and its segments
  GET    /api/audio/source/<name>    Stream a source WAV
  POST   /api/split/<name>           Auto-split a source file; returns segments
  GET    /api/segments/<name>        List segments for a source file
  GET    /api/audio/segment/<name>/<seg>   Stream a segment WAV
  POST   /api/resplit                Re-split at a specific time position
  POST   /api/rebuild                Combine adjacent segments losslessly
    POST   /api/download-selected      Download one segment or ZIP of selected segments
  DELETE /api/segments/<name>/<seg>  Delete one segment
  GET    /api/manifest/<name>        Return the raw split manifest JSON
"""

from __future__ import annotations

import io
import json
import os
import shutil
import wave
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

from flask import Flask, Response, abort, jsonify, make_response, render_template, request, send_file
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
UPLOADS_DIR = BASE_DIR / "uploads"
SEGMENTS_ROOT = BASE_DIR / "segments"

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
SEGMENTS_ROOT.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB
CORS(app)
# Trust proxy headers so app can run under reverse-proxy prefixes.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

# ---------------------------------------------------------------------------
# Lazy import of audio helpers  (same package)
# ---------------------------------------------------------------------------
from split_wav_on_silence import (  # noqa: E402
    build_chunks,
    find_silent_ranges,
    pad_chunks,
    write_chunks,
    write_split_manifest,
)
from rebuild_segment_from_manifest import extract_span, load_manifest  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def safe_stem(name: str) -> str:
    return Path(name).stem


def source_dir(stem: str) -> Path:
    return SEGMENTS_ROOT / stem


def manifest_path(stem: str) -> Path:
    return source_dir(stem) / f"{stem}_split_manifest.json"


def wav_info(path: Path) -> Dict[str, Any]:
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            nch = wf.getnchannels()
            sw = wf.getsampwidth()
        return {
            "duration_sec": frames / rate if rate else 0,
            "frame_rate": rate,
            "channels": nch,
            "sample_width": sw,
            "frames": frames,
        }
    except Exception:
        return {"duration_sec": 0, "frame_rate": 0, "channels": 0, "sample_width": 0, "frames": 0}


def run_split(source_path: Path, min_silence_ms: int, silence_threshold_db: float,
              analysis_step_ms: int, keep_silence_ms: int, min_chunk_ms: int) -> List[Path]:
    with wave.open(str(source_path), "rb") as in_wav:
        params = in_wav.getparams()
        raw_audio = in_wav.readframes(params.nframes)

    silent = find_silent_ranges(
        raw_audio=raw_audio,
        frame_rate=params.framerate,
        sample_width=params.sampwidth,
        channels=params.nchannels,
        threshold_db=silence_threshold_db,
        min_silence_ms=min_silence_ms,
        analysis_step_ms=analysis_step_ms,
    )
    chunks = build_chunks(params.nframes, silent)
    ksf = int(params.framerate * keep_silence_ms / 1000)
    chunks = pad_chunks(chunks, params.nframes, ksf)
    mcf = int(params.framerate * min_chunk_ms / 1000)
    chunks = [(s, e) for (s, e) in chunks if (e - s) >= mcf]

    seg_dir = source_dir(source_path.stem)
    seg_dir.mkdir(parents=True, exist_ok=True)

    written = write_chunks(source_path, raw_audio, params, chunks, seg_dir)
    write_split_manifest(
        input_wav=source_path,
        output_dir=seg_dir,
        params=params,
        chunks=chunks,
        segment_paths=written,
        settings={
            "min_silence_ms": min_silence_ms,
            "silence_threshold_db": silence_threshold_db,
            "analysis_step_ms": analysis_step_ms,
            "keep_silence_ms": keep_silence_ms,
            "min_chunk_ms": min_chunk_ms,
        },
    )
    return written


def segment_list_for(stem: str) -> List[Dict]:
    seg_dir = source_dir(stem)
    segs = []
    for p in sorted(seg_dir.glob(f"{stem}_part_*.wav")):
        info = wav_info(p)
        segs.append({"name": p.name, "path": str(p), **info})
    return segs


# ---------------------------------------------------------------------------
# Routes — SPA entry point
# ---------------------------------------------------------------------------
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def spa(path: str):
    static_file = app.static_folder + "/" + path
    if path and os.path.exists(static_file):
        return send_file(static_file)
    base_path = (request.script_root or "").rstrip("/")
    if not base_path:
        base_path = "."
    return render_template("index.html", base_path=base_path)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
@app.route("/api/upload", methods=["POST"])
def upload():
    files = request.files.getlist("files")
    if not files:
        abort(400, "No files provided")
    saved = []
    errors = []
    for f in files:
        if not f.filename:
            continue
        name = Path(f.filename).name
        if not name.lower().endswith(".wav"):
            errors.append(f"{name}: only WAV files are accepted")
            continue
        dest = UPLOADS_DIR / name
        f.save(str(dest))
        saved.append({"name": name, **wav_info(dest)})
    return jsonify({"saved": saved, "errors": errors})


# ---------------------------------------------------------------------------
# File list
# ---------------------------------------------------------------------------
@app.route("/api/files", methods=["GET"])
def list_files():
    result = []
    for p in sorted(UPLOADS_DIR.glob("*.wav")):
        has_segments = any(source_dir(p.stem).glob(f"{p.stem}_part_*.wav"))
        result.append({
            "name": p.name,
            "has_segments": has_segments,
            **wav_info(p),
        })
    return jsonify(result)


# ---------------------------------------------------------------------------
# Delete source file
# ---------------------------------------------------------------------------
@app.route("/api/files/<name>", methods=["DELETE"])
def delete_file(name: str):
    p = UPLOADS_DIR / name
    if not p.exists():
        abort(404)
    p.unlink()
    seg_d = source_dir(Path(name).stem)
    if seg_d.exists():
        shutil.rmtree(seg_d)
    return jsonify({"deleted": name})


# ---------------------------------------------------------------------------
# Stream source audio
# ---------------------------------------------------------------------------
@app.route("/api/audio/source/<name>")
def stream_source(name: str):
    p = UPLOADS_DIR / name
    if not p.exists():
        abort(404)
    return send_file(str(p), mimetype="audio/wav", conditional=True)


# ---------------------------------------------------------------------------
# Auto-split
# ---------------------------------------------------------------------------
@app.route("/api/split/<name>", methods=["POST"])
def auto_split(name: str):
    source = UPLOADS_DIR / name
    if not source.exists():
        abort(404, f"Source file not found: {name}")

    data = request.get_json(silent=True) or {}
    min_silence_ms = int(data.get("min_silence_ms", 500))
    silence_threshold_db = float(data.get("silence_threshold_db", -38.0))
    analysis_step_ms = int(data.get("analysis_step_ms", 10))
    keep_silence_ms = int(data.get("keep_silence_ms", 80))
    min_chunk_ms = int(data.get("min_chunk_ms", 120))

    # Clear previous segments for this file
    seg_d = source_dir(source.stem)
    if seg_d.exists():
        for old in seg_d.glob("*.wav"):
            old.unlink()
        for old in seg_d.glob("*.json"):
            old.unlink()

    written = run_split(source, min_silence_ms, silence_threshold_db,
                        analysis_step_ms, keep_silence_ms, min_chunk_ms)
    segs = []
    for p in written:
        segs.append({"name": p.name, **wav_info(p)})

    return jsonify({"source": name, "segments": segs})


# ---------------------------------------------------------------------------
# List segments
# ---------------------------------------------------------------------------
@app.route("/api/segments/<name>")
def get_segments(name: str):
    stem = safe_stem(name)
    return jsonify(segment_list_for(stem))


# ---------------------------------------------------------------------------
# Stream segment audio
# ---------------------------------------------------------------------------
@app.route("/api/audio/segment/<source_stem>/<seg_name>")
def stream_segment(source_stem: str, seg_name: str):
    p = source_dir(source_stem) / seg_name
    if not p.exists():
        abort(404)
    return send_file(str(p), mimetype="audio/wav", conditional=True)


# ---------------------------------------------------------------------------
# Re-split one segment at a time position
# ---------------------------------------------------------------------------
@app.route("/api/resplit", methods=["POST"])
def resplit():
    """Split an existing segment at a given time offset (in seconds from its start).

    Body JSON:
      source_stem  – stem of the original upload
      segment_name – e.g. "my_audio_part_002.wav"
      split_at_sec – seconds from the start of THIS segment where to cut
    """
    data = request.get_json(force=True)
    source_stem: str = data.get("source_stem", "")
    segment_name: str = data.get("segment_name", "")
    split_at_sec: float = float(data.get("split_at_sec", 0))

    seg_path = source_dir(source_stem) / segment_name
    if not seg_path.exists():
        abort(404, f"Segment not found: {segment_name}")

    # Read the segment audio
    with wave.open(str(seg_path), "rb") as wf:
        params = wf.getparams()
        raw = wf.readframes(params.nframes)

    split_frame = int(params.framerate * split_at_sec)
    if split_frame <= 0 or split_frame >= params.nframes:
        abort(400, "split_at_sec is outside the segment duration")

    # Determine the two new part names (insert _a / _b before .wav)
    a_name = seg_path.stem + "_a.wav"
    b_name = seg_path.stem + "_b.wav"

    # Write the two halves
    bpf = params.sampwidth * params.nchannels
    for part_name, start, end in [(a_name, 0, split_frame), (b_name, split_frame, params.nframes)]:
        out = source_dir(source_stem) / part_name
        with wave.open(str(out), "wb") as ow:
            ow.setnchannels(params.nchannels)
            ow.setsampwidth(params.sampwidth)
            ow.setframerate(params.framerate)
            ow.writeframes(raw[start * bpf: end * bpf])

    # Remove original segment
    seg_path.unlink()

    # Update manifest: replace this entry with two new entries
    mp = manifest_path(source_stem)
    if mp.exists():
        mdata = load_manifest(mp)
        seg_entries = mdata.get("segments", [])
        new_entries = []
        for e in seg_entries:
            if Path(e.get("segment_file", "")).name == segment_name:
                orig_start = int(e["start_frame"])
                mid = orig_start + split_frame
                orig_end = int(e["end_frame"])
                orig_idx = int(e["index"])
                fr = mdata["audio_params"]["frame_rate"]
                new_entries.append({
                    "index": orig_idx,
                    "segment_file": str(source_dir(source_stem) / a_name),
                    "start_frame": orig_start,
                    "end_frame": mid,
                    "start_sec": orig_start / fr,
                    "end_sec": mid / fr,
                })
                new_entries.append({
                    "index": orig_idx + 0.5,
                    "segment_file": str(source_dir(source_stem) / b_name),
                    "start_frame": mid,
                    "end_frame": orig_end,
                    "start_sec": mid / fr,
                    "end_sec": orig_end / fr,
                })
            else:
                new_entries.append(e)
        mdata["segments"] = new_entries
        mp.write_text(json.dumps(mdata, indent=2), encoding="utf-8")

    return jsonify({
        "removed": segment_name,
        "created": [a_name, b_name],
    })


# ---------------------------------------------------------------------------
# Combine adjacent segments losslessly via manifest
# ---------------------------------------------------------------------------
@app.route("/api/rebuild", methods=["POST"])
def rebuild():
    """Combine a list of adjacent segment indices into one new WAV.

    Body JSON:
      source_stem  – stem of the original upload
      segments     – list of segment filenames to join e.g. ["my_audio_part_001.wav","my_audio_part_002.wav"]
      output_name  – optional output WAV filename (in the same segments dir)
    """
    data = request.get_json(force=True)
    source_stem: str = data.get("source_stem", "")
    seg_names: List[str] = data.get("segments", [])
    output_name: str = data.get("output_name", "")

    if not source_stem or not seg_names:
        abort(400, "source_stem and segments are required")

    mp = manifest_path(source_stem)
    if not mp.exists():
        abort(404, f"Manifest not found for {source_stem}. Run auto-split first.")

    mdata = load_manifest(mp)
    seg_map = {Path(e["segment_file"]).name: e for e in mdata.get("segments", [])}

    missing = [n for n in seg_names if n not in seg_map]
    if missing:
        abort(404, f"Segments not found in manifest: {missing}")

    start_frame = min(int(seg_map[n]["start_frame"]) for n in seg_names)
    end_frame = max(int(seg_map[n]["end_frame"]) for n in seg_names)

    source_wav = Path(str(mdata["source_wav"]))
    if not source_wav.exists():
        abort(404, f"Source WAV not found on disk: {source_wav}")

    if not output_name:
        parts = "_".join(Path(n).stem.split("_part_")[-1] for n in seg_names)
        output_name = f"{source_stem}_rebuilt_{parts}.wav"

    out_path = source_dir(source_stem) / output_name
    extract_span(source_wav, start_frame, end_frame, out_path)

    # Remove the original segments from disk and manifest
    new_entries = []
    fr = mdata["audio_params"]["frame_rate"]
    rebuild_entry = {
        "index": min(float(seg_map[n]["index"]) for n in seg_names),
        "segment_file": str(out_path),
        "start_frame": start_frame,
        "end_frame": end_frame,
        "start_sec": start_frame / fr,
        "end_sec": end_frame / fr,
        "rebuilt_from": seg_names,
    }
    seen = set()
    for e in sorted(mdata.get("segments", []), key=lambda x: float(x.get("index", 0))):
        if Path(e.get("segment_file", "")).name in seg_names:
            if "added" not in seen:
                new_entries.append(rebuild_entry)
                seen.add("added")
            p = source_dir(source_stem) / Path(e["segment_file"]).name
            if p.exists():
                p.unlink()
        else:
            new_entries.append(e)
    mdata["segments"] = new_entries
    mp.write_text(json.dumps(mdata, indent=2), encoding="utf-8")

    return jsonify({
        "rebuilt": output_name,
        "removed": seg_names,
        "start_sec": start_frame / fr,
        "end_sec": end_frame / fr,
        **wav_info(out_path),
    })


# ---------------------------------------------------------------------------
# Download selected segments (single WAV or ZIP)
# ---------------------------------------------------------------------------
@app.route("/api/download-selected", methods=["POST"])
def download_selected():
    data = request.get_json(force=True)
    source_stem: str = data.get("source_stem", "")
    seg_names: List[str] = data.get("segments", [])

    if not source_stem or not seg_names:
        abort(400, "source_stem and segments are required")

    seg_paths: List[Path] = []
    for seg_name in seg_names:
        p = source_dir(source_stem) / seg_name
        if not p.exists():
            abort(404, f"Segment not found: {seg_name}")
        seg_paths.append(p)

    if len(seg_paths) == 1:
        return send_file(
            str(seg_paths[0]),
            mimetype="audio/wav",
            as_attachment=True,
            download_name=seg_paths[0].name,
        )

    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in seg_paths:
            zf.write(str(p), arcname=p.name)
    mem.seek(0)

    zip_name = f"{source_stem}_selected_segments.zip"
    return send_file(
        mem,
        mimetype="application/zip",
        as_attachment=True,
        download_name=zip_name,
    )


# ---------------------------------------------------------------------------
# Delete single segment
# ---------------------------------------------------------------------------
@app.route("/api/segments/<source_stem>/<seg_name>", methods=["DELETE"])
def delete_segment(source_stem: str, seg_name: str):
    p = source_dir(source_stem) / seg_name
    if p.exists():
        p.unlink()
    return jsonify({"deleted": seg_name})


# ---------------------------------------------------------------------------
# Raw manifest
# ---------------------------------------------------------------------------
@app.route("/api/manifest/<name>")
def get_manifest(name: str):
    stem = safe_stem(name)
    mp = manifest_path(stem)
    if not mp.exists():
        abort(404)
    return jsonify(json.loads(mp.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
