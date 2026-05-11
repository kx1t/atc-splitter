#!/usr/bin/env python3
"""ATC Splitter — Flask backend.

Endpoints:
  POST   /api/upload                 Upload one or more WAV files
    POST   /api/upload-chunk           Upload one chunk for a file
    POST   /api/upload-complete        Assemble uploaded chunks and verify SHA-256
  GET    /api/files                  List all uploaded source files with metadata
  DELETE /api/files                   Delete all uploaded files and their segments
  DELETE /api/files/<name>           Delete an uploaded file and its segments
  GET    /api/audio/source/<name>    Stream a source WAV
  POST   /api/split/<name>           Auto-split a source file; returns segments
  GET    /api/segments/<name>        List segments for a source file
  GET    /api/audio/segment/<name>/<seg>   Stream a segment WAV
  POST   /api/resplit                Re-split at a specific time position
  POST   /api/rebuild                Combine adjacent segments losslessly
    POST   /api/renumber/<name>        Renumber all segments in chronological order
    POST   /api/download-selected      Download one segment or ZIP of selected segments
  DELETE /api/segments/<name>/<seg>  Delete one segment
  GET    /api/manifest/<name>        Return the raw split manifest JSON
  GET    /api/transcribe/<name>/<seg> Transcribe a segment with Whisper (requires ENABLE_TRANSCRIPTION=true)
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import threading
import wave
import zipfile
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from flask import Flask, Response, abort, jsonify, make_response, render_template, request, send_file # pyright: ignore[reportMissingImports]
from flask_cors import CORS # pyright: ignore[reportMissingModuleSource]
from werkzeug.middleware.proxy_fix import ProxyFix # pyright: ignore[reportMissingImports]

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
# All persistent data lives under DATA_DIR so a single volume mount suffices.
# Override with the DATA_DIR env var if needed (default: /app/data when in Docker).
DATA_DIR     = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
UPLOADS_DIR  = DATA_DIR / "uploads"
SEGMENTS_ROOT = DATA_DIR / "segments"
CHUNKS_DIR   = UPLOADS_DIR / ".chunks"
MODELS_DIR   = DATA_DIR / "models"
BATCH_INDEX_PATH = DATA_DIR / "batch_index.json"

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
SEGMENTS_ROOT.mkdir(parents=True, exist_ok=True)
CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
MODELS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Transcription (Whisper) — optional, controlled by ENABLE_TRANSCRIPTION env var
# ---------------------------------------------------------------------------
ENABLE_TRANSCRIPTION: bool = os.environ.get("ENABLE_TRANSCRIPTION", "false").strip().lower() in ("1", "true", "yes")
WHISPER_MODEL_SIZE: str = os.environ.get("WHISPER_MODEL_SIZE", "tiny.en").strip()

_whisper_model = None           # set by background thread
_whisper_loading: bool = False
_whisper_error: str | None = None


def _format_build_time(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return "unknown"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text).astimezone(timezone.utc)
    except ValueError:
        return value
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _detect_local_git_commit_short() -> str:
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(BASE_DIR.parent), "rev-parse", "--short=7", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return ""
    return commit


def _resolve_build_metadata() -> Tuple[str, str]:
    build_time = _format_build_time(os.environ.get("APP_BUILD_TIME", ""))
    commit = (os.environ.get("APP_GIT_COMMIT", "") or "").strip()

    if not commit:
        commit = _detect_local_git_commit_short()

    commit_short = commit[:7] if commit else "unknown"
    return build_time, commit_short


APP_BUILD_TIME, APP_GIT_COMMIT_SHORT = _resolve_build_metadata()


def _whisper_log(message: str) -> None:
    print(f"[whisper] {message}", flush=True)


def _model_cache_present() -> bool:
    model_slug = f"faster-whisper-{WHISPER_MODEL_SIZE}".replace("/", "-")
    if any(MODELS_DIR.glob(f"models--*{model_slug}*")):
        return True
    return any(MODELS_DIR.rglob("model.bin"))


def _preload_whisper() -> None:
    global _whisper_model, _whisper_loading, _whisper_error
    _whisper_loading = True
    had_cache = _model_cache_present()
    hf_token = os.environ.get("HF_TOKEN", "").strip()

    if had_cache:
        _whisper_log(f"already downloaded: faster-whisper model '{WHISPER_MODEL_SIZE}' found in {MODELS_DIR}")
    else:
        _whisper_log(f"downloading now: faster-whisper model '{WHISPER_MODEL_SIZE}'")
        if not hf_token:
            _whisper_log("HF_TOKEN not set; using unauthenticated Hugging Face downloads (lower rate limits)")

    try:
        from faster_whisper import WhisperModel  # type: ignore

        _whisper_model = WhisperModel(
            WHISPER_MODEL_SIZE,
            device="cpu",
            compute_type="int8",
            download_root=str(MODELS_DIR),
        )
        if had_cache:
            _whisper_log(f"download complete: already downloaded (cache hit), model '{WHISPER_MODEL_SIZE}' loaded")
        else:
            _whisper_log(f"download complete: model '{WHISPER_MODEL_SIZE}' downloaded and loaded")
    except Exception as exc:  # noqa: BLE001
        _whisper_error = str(exc)
        _whisper_log(f"download error: {exc}")
    finally:
        _whisper_loading = False


def _start_model_preload() -> None:
    if not ENABLE_TRANSCRIPTION:
        _whisper_log("transcription disabled (ENABLE_TRANSCRIPTION=false)")
        return
    _whisper_log("starting background model preload")
    t = threading.Thread(target=_preload_whisper, name="whisper-preload", daemon=True)
    t.start()

# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB
CORS(app)
# Trust proxy headers so app can run under reverse-proxy prefixes.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

# Kick off Whisper model download/load in background (no-op if ENABLE_TRANSCRIPTION=false)
_start_model_preload()

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
from segment_renumbering import renumber_segments_for_source  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def safe_stem(name: str) -> str:
    return Path(name).stem


def source_dir(stem: str) -> Path:
    return SEGMENTS_ROOT / stem


def manifest_path(stem: str) -> Path:
    return source_dir(stem) / f"{stem}_split_manifest.json"


def transcript_path(stem: str) -> Path:
    return source_dir(stem) / f"{stem}_transcripts.json"


def load_transcripts(stem: str) -> Dict[str, str]:
    tp = transcript_path(stem)
    if not tp.exists():
        return {}
    try:
        data = json.loads(tp.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def save_transcripts(stem: str, transcripts: Dict[str, str]) -> None:
    tp = transcript_path(stem)
    cleaned = {
        str(k): str(v)
        for k, v in transcripts.items()
        if str(v).strip()
    }
    if cleaned:
        tp.write_text(json.dumps(cleaned, indent=2, ensure_ascii=False), encoding="utf-8")
    elif tp.exists():
        tp.unlink()


def set_transcript(stem: str, seg_name: str, text: str) -> None:
    transcripts = load_transcripts(stem)
    if text.strip():
        transcripts[seg_name] = text
    else:
        transcripts.pop(seg_name, None)
    save_transcripts(stem, transcripts)


def remove_transcripts(stem: str, seg_names: List[str]) -> None:
    transcripts = load_transcripts(stem)
    changed = False
    for name in seg_names:
        if name in transcripts:
            transcripts.pop(name, None)
            changed = True
    if changed:
        save_transcripts(stem, transcripts)


def remove_segments_from_manifest(stem: str, seg_names: List[str]) -> None:
    mp = manifest_path(stem)
    if not mp.exists() or not seg_names:
        return

    name_set = set(seg_names)
    mdata = load_manifest(mp)
    entries = mdata.get("segments", [])
    filtered = [
        e for e in entries
        if Path(e.get("segment_file", "")).name not in name_set
    ]

    if len(filtered) != len(entries):
        mdata["segments"] = filtered
        mp.write_text(json.dumps(mdata, indent=2), encoding="utf-8")


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


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def upload_chunk_dir(upload_id: str) -> Path:
    return CHUNKS_DIR / upload_id


def normalize_batch_name(name: str) -> str:
    # Collapse internal whitespace and trim to keep names readable and stable.
    cleaned = " ".join((name or "").strip().split())
    if not cleaned:
        return "Uncategorized"
    return cleaned[:120]


def load_batch_index() -> Dict[str, str]:
    if not BATCH_INDEX_PATH.exists():
        return {}
    try:
        raw = json.loads(BATCH_INDEX_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in raw.items():
        name = Path(str(key)).name
        if not name:
            continue
        out[name] = normalize_batch_name(str(value))
    return out


def save_batch_index(index: Dict[str, str]) -> None:
    cleaned = {
        Path(str(name)).name: normalize_batch_name(batch)
        for name, batch in index.items()
        if Path(str(name)).name
    }
    if cleaned:
        BATCH_INDEX_PATH.write_text(json.dumps(cleaned, indent=2, ensure_ascii=False), encoding="utf-8")
    elif BATCH_INDEX_PATH.exists():
        BATCH_INDEX_PATH.unlink()


def set_file_batch(file_name: str, batch_name: str) -> None:
    name = Path(file_name).name
    if not name:
        return
    index = load_batch_index()
    index[name] = normalize_batch_name(batch_name)
    save_batch_index(index)


def remove_files_from_batches(file_names: List[str]) -> None:
    if not file_names:
        return
    index = load_batch_index()
    changed = False
    for file_name in file_names:
        key = Path(file_name).name
        if key in index:
            index.pop(key, None)
            changed = True
    if changed:
        save_batch_index(index)


def cleanup_batch_index_for_existing_files() -> Dict[str, str]:
    index = load_batch_index()
    existing = {p.name for p in UPLOADS_DIR.glob("*.wav")}
    cleaned = {name: batch for name, batch in index.items() if name in existing}
    if cleaned != index:
        save_batch_index(cleaned)
    return cleaned


def segment_sort_key(entry: Dict[str, Any]) -> float:
    try:
        return float(entry.get("index", 0))
    except (TypeError, ValueError):
        return 0.0


def segment_part_labels(entry: Dict[str, Any], file_name: str) -> List[str]:
    labels = entry.get("merged_part_indices")
    if isinstance(labels, list) and labels:
        return [str(label) for label in labels]

    stem = Path(file_name).stem
    z_merge_match = re.search(r"_part_([^_]+)z_([0-9A-Za-z_]+)$", stem)
    if z_merge_match:
        return [part for part in z_merge_match.group(2).split("_") if part]

    merged_match = re.search(r"_part_([0-9A-Za-z_]+)_merged$", stem)
    if merged_match:
        return [part for part in merged_match.group(1).split("_") if part]

    part_match = re.search(r"_part_(\d+)([A-Za-z]+)?$", stem)
    if part_match:
        number = part_match.group(1)
        suffix = part_match.group(2) or ""
        return [f"{number}{suffix}"]

    try:
        index_value = float(entry.get("index", 0))
    except (TypeError, ValueError):
        return [stem]

    if index_value.is_integer():
        return [f"{int(index_value):03d}"]

    return [str(index_value).replace(".", "p")]


def segment_current_label(file_name: str) -> str:
    stem = Path(file_name).stem
    part_match = re.search(r"_part_(.+)$", stem)
    if part_match:
        body = part_match.group(1)
        return body.split("_", 1)[0]

    return stem


def split_segment_names(file_name: str) -> Tuple[str, str]:
    stem = Path(file_name).stem
    suffix = Path(file_name).suffix or ".wav"

    z_merge_match = re.match(r"^(.*_part_)([^_]+)(_.+)$", stem)
    if z_merge_match:
        prefix, current_label, remainder = z_merge_match.groups()
        return (
            f"{prefix}{current_label}a{remainder}{suffix}",
            f"{prefix}{current_label}b{remainder}{suffix}",
        )

    return (f"{stem}a{suffix}", f"{stem}b{suffix}")


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
    seen = set()
    transcripts = load_transcripts(stem)

    mp = manifest_path(stem)
    if mp.exists():
        try:
            mdata = load_manifest(mp)
            entries = sorted(mdata.get("segments", []), key=segment_sort_key)
            for entry in entries:
                name = Path(entry.get("segment_file", "")).name
                p = seg_dir / name
                if not name or not p.exists() or name in seen:
                    continue
                info = wav_info(p)
                segs.append({
                    "name": p.name,
                    "path": str(p),
                    "transcription": transcripts.get(p.name, ""),
                    **info,
                })
                seen.add(name)
        except SystemExit:
            pass

    for p in sorted(seg_dir.glob("*.wav")):
        if p.name in seen:
            continue
        info = wav_info(p)
        segs.append({
            "name": p.name,
            "path": str(p),
            "transcription": transcripts.get(p.name, ""),
            **info,
        })
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
    return render_template("index.html", base_path=base_path,
                           enable_transcription=ENABLE_TRANSCRIPTION,
                           build_time=APP_BUILD_TIME,
                           build_commit_short=APP_GIT_COMMIT_SHORT)


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
# Chunk upload
# ---------------------------------------------------------------------------
@app.route("/api/upload-chunk", methods=["POST"])
def upload_chunk():
    upload_id = (request.form.get("upload_id") or "").strip()
    file_name = Path(request.form.get("file_name") or "").name
    chunk_index = request.form.get("chunk_index")
    total_chunks = request.form.get("total_chunks")
    total_size = request.form.get("total_size")
    file_sha256_value = (request.form.get("file_sha256") or "").strip().lower()
    batch_name = normalize_batch_name(request.form.get("batch_name") or "")
    chunk_file = request.files.get("chunk")

    if not upload_id or not file_name or chunk_index is None or total_chunks is None or total_size is None:
        abort(400, "Missing required chunk metadata")
    if not file_name.lower().endswith((".wav", ".mp3")):
        abort(400, "Only WAV or MP3 files are accepted")
    if chunk_file is None:
        abort(400, "Missing chunk file")

    try:
        chunk_index_i = int(chunk_index)
        total_chunks_i = int(total_chunks)
        total_size_i = int(total_size)
    except ValueError:
        abort(400, "Invalid numeric chunk metadata")

    if chunk_index_i < 0 or total_chunks_i <= 0 or chunk_index_i >= total_chunks_i:
        abort(400, "Invalid chunk index or total chunks")
    if total_size_i <= 0:
        abort(400, "Invalid total file size")

    chunk_dir = upload_chunk_dir(upload_id)
    chunk_dir.mkdir(parents=True, exist_ok=True)

    meta_path = chunk_dir / "meta.json"
    if not meta_path.exists():
        meta = {
            "file_name": file_name,
            "total_chunks": total_chunks_i,
            "total_size": total_size_i,
            "file_sha256": file_sha256_value,
            "batch_name": batch_name,
        }
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    else:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if (
            meta.get("file_name") != file_name
            or int(meta.get("total_chunks", -1)) != total_chunks_i
            or int(meta.get("total_size", -1)) != total_size_i
            or (meta.get("file_sha256") or "") != file_sha256_value
            or normalize_batch_name(meta.get("batch_name") or "") != batch_name
        ):
            abort(409, "Chunk metadata mismatch for upload_id")

    chunk_path = chunk_dir / f"{chunk_index_i:08d}.part"
    chunk_file.save(str(chunk_path))

    return jsonify({"ok": True, "upload_id": upload_id, "chunk_index": chunk_index_i})


@app.route("/api/upload-complete", methods=["POST"])
def upload_complete():
    data = request.get_json(force=True)
    upload_id = (data.get("upload_id") or "").strip()
    file_name = Path(data.get("file_name") or "").name

    if not upload_id or not file_name:
        abort(400, "upload_id and file_name are required")
    if not file_name.lower().endswith((".wav", ".mp3")):
        abort(400, "Only WAV or MP3 files are accepted")

    chunk_dir = upload_chunk_dir(upload_id)
    meta_path = chunk_dir / "meta.json"
    if not chunk_dir.exists() or not meta_path.exists():
        abort(404, "Upload session not found")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("file_name") != file_name:
        abort(409, "File name mismatch for upload session")

    batch_name = normalize_batch_name(meta.get("batch_name") or data.get("batch_name") or "")

    total_chunks = int(meta["total_chunks"])
    expected_size = int(meta["total_size"])
    expected_sha256 = (meta.get("file_sha256") or "").lower()

    temp_target = chunk_dir / "assembled.tmp"
    with temp_target.open("wb") as out:
        for idx in range(total_chunks):
            part = chunk_dir / f"{idx:08d}.part"
            if not part.exists():
                abort(400, f"Missing chunk {idx}")
            with part.open("rb") as pf:
                shutil.copyfileobj(pf, out)

    actual_size = temp_target.stat().st_size
    if actual_size != expected_size:
        abort(400, f"Assembled size mismatch: expected {expected_size}, got {actual_size}")

    actual_sha256 = file_sha256(temp_target)
    if expected_sha256 and actual_sha256 != expected_sha256:
        abort(400, "SHA-256 mismatch after assembly")

    dest = UPLOADS_DIR / file_name
    if dest.exists():
        dest.unlink()
        remove_files_from_batches([file_name])
    shutil.move(str(temp_target), str(dest))

    # Convert MP3 → WAV (keep original sample rate and channel count)
    if file_name.lower().endswith(".mp3"):
        wav_name = Path(file_name).stem + ".wav"
        wav_dest = UPLOADS_DIR / wav_name
        if wav_dest.exists():
            wav_dest.unlink()
            remove_files_from_batches([wav_name])
        import subprocess  # noqa: PLC0415
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(dest), "-vn", str(wav_dest)],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            dest.unlink(missing_ok=True)
            abort(500, f"MP3 conversion failed: {exc.stderr.decode(errors='replace').strip()}")
        dest.unlink(missing_ok=True)  # remove the .mp3 after successful conversion
        dest = wav_dest
        file_name = wav_name

    set_file_batch(file_name, batch_name)

    shutil.rmtree(chunk_dir, ignore_errors=True)

    return jsonify({
        "saved": {"name": file_name, **wav_info(dest)},
        "sha256": actual_sha256,
    })


# ---------------------------------------------------------------------------
# File list
# ---------------------------------------------------------------------------
@app.route("/api/files", methods=["GET"])
def list_files():
    batch_index = cleanup_batch_index_for_existing_files()
    result = []
    for p in sorted(UPLOADS_DIR.glob("*.wav")):
        has_segments = any(source_dir(p.stem).glob("*.wav"))
        result.append({
            "name": p.name,
            "batch_name": batch_index.get(p.name, "Uncategorized"),
            "has_segments": has_segments,
            **wav_info(p),
        })
    return jsonify(result)


@app.route("/api/batches", methods=["GET"])
def list_batches():
    batch_index = cleanup_batch_index_for_existing_files()
    grouped: Dict[str, List[Dict[str, Any]]] = {}

    for p in sorted(UPLOADS_DIR.glob("*.wav")):
        batch_name = batch_index.get(p.name, "Uncategorized")
        file_entry = {
            "name": p.name,
            "has_segments": any(source_dir(p.stem).glob("*.wav")),
            **wav_info(p),
        }
        grouped.setdefault(batch_name, []).append(file_entry)

    batches = []
    for batch_name in sorted(grouped.keys(), key=lambda n: n.lower()):
        files = sorted(grouped[batch_name], key=lambda f: str(f.get("name", "")).lower())
        batches.append({
            "name": batch_name,
            "file_count": len(files),
            "files": files,
        })

    return jsonify(batches)


# ---------------------------------------------------------------------------
# Delete all source files
# ---------------------------------------------------------------------------
@app.route("/api/files", methods=["DELETE"])
def delete_all_files():
    deleted = []
    for p in list(UPLOADS_DIR.glob("*.wav")):
        p.unlink()
        seg_d = source_dir(p.stem)
        if seg_d.exists():
            shutil.rmtree(seg_d)
        deleted.append(p.name)
    remove_files_from_batches(deleted)
    return jsonify({"deleted": deleted})


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
    remove_files_from_batches([name])
    return jsonify({"deleted": name})


@app.route("/api/batches/<path:batch_name>", methods=["DELETE"])
def delete_batch(batch_name: str):
    target_batch = normalize_batch_name(batch_name)
    batch_index = cleanup_batch_index_for_existing_files()

    existing_files = {p.name for p in UPLOADS_DIR.glob("*.wav")}
    to_delete = [name for name in existing_files if batch_index.get(name, "Uncategorized") == target_batch]
    if not to_delete:
        abort(404, f"Batch not found: {target_batch}")

    deleted: List[str] = []
    for name in sorted(to_delete):
        p = UPLOADS_DIR / name
        if p.exists():
            p.unlink()
        seg_d = source_dir(Path(name).stem)
        if seg_d.exists():
            shutil.rmtree(seg_d)
        deleted.append(name)

    remove_files_from_batches(deleted)
    return jsonify({"batch": target_batch, "deleted": deleted})


@app.route("/api/batches/<path:batch_name>/download", methods=["POST"])
def download_batch(batch_name: str):
    target_batch = normalize_batch_name(batch_name)
    batch_index = cleanup_batch_index_for_existing_files()

    source_files = sorted([
        p for p in UPLOADS_DIR.glob("*.wav")
        if batch_index.get(p.name, "Uncategorized") == target_batch
    ], key=lambda p: p.name.lower())
    if not source_files:
        abort(404, f"Batch not found: {target_batch}")

    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for source in source_files:
            stem = source.stem
            seg_dir = source_dir(stem)
            seg_files = sorted(seg_dir.glob("*.wav"), key=lambda p: p.name.lower()) if seg_dir.exists() else []

            if seg_files:
                # Keep numbering predictable across all segment exports in this batch.
                renumber_segments_for_source(
                    source_stem=stem,
                    source_wav=source,
                    seg_dir=seg_dir,
                    manifest_file=manifest_path(stem),
                    transcript_file=transcript_path(stem),
                )
                seg_files = sorted(seg_dir.glob("*.wav"), key=lambda p: p.name.lower())
                for seg in seg_files:
                    zf.write(str(seg), arcname=f"{target_batch}/{stem}/{seg.name}")
            else:
                zf.write(str(source), arcname=f"{target_batch}/{source.name}")

    mem.seek(0)
    batch_slug = re.sub(r"[^A-Za-z0-9._-]+", "_", target_batch).strip("_") or "batch"
    return send_file(
        mem,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{batch_slug}_all_segments.zip",
    )


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
    min_silence_ms = int(data.get("min_silence_ms", 250))
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

    a_name, b_name = split_segment_names(seg_path.name)

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
                current_label = segment_current_label(segment_name)
                new_entries.append({
                    "index": orig_idx,
                    "segment_file": str(source_dir(source_stem) / a_name),
                    "start_frame": orig_start,
                    "end_frame": mid,
                    "start_sec": orig_start / fr,
                    "end_sec": mid / fr,
                    "merged_part_indices": [f"{current_label}a"],
                })
                new_entries.append({
                    "index": orig_idx + 0.5,
                    "segment_file": str(source_dir(source_stem) / b_name),
                    "start_frame": mid,
                    "end_frame": orig_end,
                    "start_sec": mid / fr,
                    "end_sec": orig_end / fr,
                    "merged_part_indices": [f"{current_label}b"],
                })
            else:
                new_entries.append(e)
        mdata["segments"] = new_entries
        mp.write_text(json.dumps(mdata, indent=2), encoding="utf-8")

    remove_transcripts(source_stem, [segment_name])

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

    ordered_seg_names = [
        Path(entry.get("segment_file", "")).name
        for entry in sorted(mdata.get("segments", []), key=segment_sort_key)
        if Path(entry.get("segment_file", "")).name in seg_names
    ]
    if not ordered_seg_names:
        abort(400, "No valid segments selected for merge")

    start_frame = min(int(seg_map[n]["start_frame"]) for n in ordered_seg_names)
    end_frame = max(int(seg_map[n]["end_frame"]) for n in ordered_seg_names)

    source_wav = Path(str(mdata["source_wav"]))
    if not source_wav.exists():
        abort(404, f"Source WAV not found on disk: {source_wav}")

    merged_part_indices: List[str] = []
    for seg_name in ordered_seg_names:
        merged_part_indices.extend(segment_part_labels(seg_map[seg_name], seg_name))

    if not output_name:
        parts = "_".join(merged_part_indices)
        output_name = f"{source_stem}_part_{merged_part_indices[0]}z_{parts}.wav"

    out_path = source_dir(source_stem) / output_name
    extract_span(source_wav, start_frame, end_frame, out_path)

    # Remove the original segments from disk and manifest
    new_entries = []
    fr = mdata["audio_params"]["frame_rate"]

    existing_transcripts = load_transcripts(source_stem)
    merged_transcript_parts = [existing_transcripts.get(name, "").strip() for name in ordered_seg_names]
    merged_transcript = " ".join(part for part in merged_transcript_parts if part).strip()
    rebuild_entry = {
        "index": min(float(seg_map[n]["index"]) for n in ordered_seg_names),
        "segment_file": str(out_path),
        "start_frame": start_frame,
        "end_frame": end_frame,
        "start_sec": start_frame / fr,
        "end_sec": end_frame / fr,
        "merged_from": ordered_seg_names,
        "merged_part_indices": merged_part_indices,
    }
    seen = set()
    for e in sorted(mdata.get("segments", []), key=lambda x: float(x.get("index", 0))):
        if Path(e.get("segment_file", "")).name in ordered_seg_names:
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

    remove_transcripts(source_stem, ordered_seg_names)
    if merged_transcript:
        set_transcript(source_stem, output_name, merged_transcript)

    return jsonify({
        "merged": output_name,
        "removed": ordered_seg_names,
        "start_sec": start_frame / fr,
        "end_sec": end_frame / fr,
        **wav_info(out_path),
    })


# ---------------------------------------------------------------------------
# Renumber segments and keep manifest/transcripts in sync
# ---------------------------------------------------------------------------
@app.route("/api/renumber/<name>", methods=["POST"])
def renumber(name: str):
    source = UPLOADS_DIR / name
    if not source.exists():
        abort(404, f"Source file not found: {name}")

    stem = source.stem
    seg_dir = source_dir(stem)
    if not seg_dir.exists():
        abort(404, f"No segments found for: {name}")

    result = renumber_segments_for_source(
        source_stem=stem,
        source_wav=source,
        seg_dir=seg_dir,
        manifest_file=manifest_path(stem),
        transcript_file=transcript_path(stem),
    )
    return jsonify(result)


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

    transcripts = load_transcripts(source_stem)
    selected_transcripts = {
        name: transcripts.get(name, "")
        for name in seg_names
        if transcripts.get(name, "").strip()
    }

    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in seg_paths:
            zf.write(str(p), arcname=p.name)

        if selected_transcripts:
            csv_buf = io.StringIO(newline="")
            writer = csv.writer(csv_buf)
            writer.writerow(["file_name", "transcribed_text"])
            for name in seg_names:
                text = selected_transcripts.get(name)
                if text:
                    writer.writerow([name, text])
            zf.writestr("transcriptions.csv", csv_buf.getvalue())
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
    remove_segments_from_manifest(source_stem, [seg_name])
    remove_transcripts(source_stem, [seg_name])
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
# Transcription (Whisper)
# ---------------------------------------------------------------------------
@app.route("/api/transcribe/<source_stem>/<seg_name>", methods=["GET"])
def transcribe_segment(source_stem: str, seg_name: str):
    if not ENABLE_TRANSCRIPTION:
        return jsonify({"error": "Transcription is disabled"}), 403

    if _whisper_error:
        return jsonify({"error": f"Whisper failed to load: {_whisper_error}"}), 503

    if _whisper_model is None:
        # Still loading — tell the client to retry
        return jsonify({"status": "loading", "message": "Model is loading, please retry shortly"}), 202

    p = source_dir(source_stem) / seg_name
    if not p.exists():
        abort(404)

    try:
        segments, _info = _whisper_model.transcribe(str(p), language="en")
        text = " ".join(segment.text.strip() for segment in segments).strip()
        set_transcript(source_stem, seg_name, text)
        return jsonify({"text": text})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/transcription", methods=["POST"])
def update_transcription():
    data = request.get_json(force=True)
    source_stem: str = data.get("source_stem", "")
    seg_name: str = data.get("segment_name", "")
    text: str = str(data.get("text", ""))

    if not source_stem or not seg_name:
        return jsonify({"error": "source_stem and segment_name are required"}), 400

    seg_path = source_dir(source_stem) / seg_name
    if not seg_path.exists():
        return jsonify({"error": f"Segment not found: {seg_name}"}), 404

    set_transcript(source_stem, seg_name, text)
    return jsonify({"saved": seg_name, "text": text})


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
