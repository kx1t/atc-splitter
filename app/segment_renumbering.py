#!/usr/bin/env python3
"""Helpers to renumber segment WAV files and keep metadata in sync."""

from __future__ import annotations

import json
import uuid
import wave
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _segment_sort_key(item: Tuple[Path, Dict[str, Any] | None]) -> Tuple[float, float, float, str]:
    seg_path, entry = item
    start_frame = _to_float(entry.get("start_frame")) if entry else None
    start_sec = _to_float(entry.get("start_sec")) if entry else None
    index = _to_float(entry.get("index")) if entry else None
    return (
        start_frame if start_frame is not None else float("inf"),
        start_sec if start_sec is not None else float("inf"),
        index if index is not None else float("inf"),
        seg_path.name,
    )


def _read_wave_frame_count(path: Path) -> int:
    with wave.open(str(path), "rb") as wf:
        return int(wf.getnframes())


def renumber_segments_for_source(
    source_stem: str,
    source_wav: Path,
    seg_dir: Path,
    manifest_file: Path,
    transcript_file: Path,
) -> Dict[str, Any]:
    wav_files = sorted([p for p in seg_dir.glob("*.wav") if p.is_file()])
    if not wav_files:
        return {"source_stem": source_stem, "renamed": 0, "mapping": []}

    manifest_data: Dict[str, Any] = {}
    manifest_entries: List[Dict[str, Any]] = []
    by_name: Dict[str, Dict[str, Any]] = {}

    if manifest_file.exists():
        manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
        entries = manifest_data.get("segments", [])
        if isinstance(entries, list):
            manifest_entries = [e for e in entries if isinstance(e, dict)]
            by_name = {
                Path(str(e.get("segment_file", ""))).name: e
                for e in manifest_entries
                if e.get("segment_file")
            }

    ordered_pairs: List[Tuple[Path, Dict[str, Any] | None]] = [
        (wav_path, by_name.get(wav_path.name)) for wav_path in wav_files
    ]
    ordered_pairs.sort(key=_segment_sort_key)

    width = max(3, len(str(len(ordered_pairs))))
    mapping: List[Tuple[Path, Path]] = []
    for idx, (old_path, _entry) in enumerate(ordered_pairs, start=1):
        new_name = f"{source_stem}_part_{idx:0{width}d}.wav"
        new_path = seg_dir / new_name
        mapping.append((old_path, new_path))

    unchanged = sum(1 for old_path, new_path in mapping if old_path.name == new_path.name)

    # Two-phase rename avoids collisions when target names already exist.
    temp_paths: Dict[Path, Path] = {}
    for old_path, _new_path in mapping:
        tmp_path = seg_dir / f".__renumber_tmp_{uuid.uuid4().hex}.wav"
        old_path.rename(tmp_path)
        temp_paths[old_path] = tmp_path

    for old_path, new_path in mapping:
        temp_paths[old_path].rename(new_path)

    frame_rate = int(manifest_data.get("audio_params", {}).get("frame_rate") or 0)
    if frame_rate <= 0 and source_wav.exists():
        with wave.open(str(source_wav), "rb") as src_wav:
            frame_rate = int(src_wav.getframerate())

    rebuilt_entries: List[Dict[str, Any]] = []
    cursor_frame = 0
    for idx, (old_path, new_path) in enumerate(mapping, start=1):
        old_entry = by_name.get(old_path.name, {})

        start_frame = _to_float(old_entry.get("start_frame"))
        end_frame = _to_float(old_entry.get("end_frame"))

        if start_frame is None or end_frame is None or end_frame < start_frame:
            nframes = _read_wave_frame_count(new_path)
            start_frame = float(cursor_frame)
            end_frame = float(cursor_frame + nframes)

        cursor_frame = max(cursor_frame, int(end_frame))

        item: Dict[str, Any] = {
            "index": idx,
            "segment_file": str(new_path),
            "start_frame": int(start_frame),
            "end_frame": int(end_frame),
        }

        if frame_rate > 0:
            item["start_sec"] = int(start_frame) / frame_rate
            item["end_sec"] = int(end_frame) / frame_rate
        elif "start_sec" in old_entry:
            item["start_sec"] = old_entry["start_sec"]
            item["end_sec"] = old_entry.get("end_sec", old_entry["start_sec"])

        for key in ("merged_from", "merged_part_indices"):
            if key in old_entry:
                item[key] = old_entry[key]

        rebuilt_entries.append(item)

    if not manifest_data:
        manifest_data = {
            "source_wav": str(source_wav),
            "output_dir": str(seg_dir),
            "audio_params": {},
            "split_settings": {},
            "segments": [],
        }

    manifest_data["segments"] = rebuilt_entries
    if "source_wav" not in manifest_data:
        manifest_data["source_wav"] = str(source_wav)
    if "output_dir" not in manifest_data:
        manifest_data["output_dir"] = str(seg_dir)
    if frame_rate > 0:
        audio_params = manifest_data.setdefault("audio_params", {})
        if isinstance(audio_params, dict):
            audio_params["frame_rate"] = frame_rate
    manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    if transcript_file.exists():
        try:
            transcript_map = json.loads(transcript_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            transcript_map = {}
        if isinstance(transcript_map, dict):
            remapped: Dict[str, str] = {}
            for old_path, new_path in mapping:
                text = transcript_map.get(old_path.name)
                if isinstance(text, str) and text.strip():
                    remapped[new_path.name] = text
            if remapped:
                transcript_file.write_text(
                    json.dumps(remapped, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            else:
                transcript_file.unlink(missing_ok=True)

    return {
        "source_stem": source_stem,
        "renamed": len(mapping),
        "unchanged": unchanged,
        "mapping": [{"old": old.name, "new": new.name} for old, new in mapping],
    }


def renumber_all_sources(data_dir: Path) -> Dict[str, Any]:
    uploads_dir = data_dir / "uploads"
    segments_root = data_dir / "segments"

    if not segments_root.exists():
        return {"renumbered_sources": 0, "results": []}

    results: List[Dict[str, Any]] = []
    for seg_dir in sorted([p for p in segments_root.iterdir() if p.is_dir()]):
        source_stem = seg_dir.name
        source_wav = uploads_dir / f"{source_stem}.wav"
        manifest_file = seg_dir / f"{source_stem}_split_manifest.json"
        transcript_file = seg_dir / f"{source_stem}_transcripts.json"

        result = renumber_segments_for_source(
            source_stem=source_stem,
            source_wav=source_wav,
            seg_dir=seg_dir,
            manifest_file=manifest_file,
            transcript_file=transcript_file,
        )
        if result.get("renamed", 0) > 0:
            results.append(result)

    return {"renumbered_sources": len(results), "results": results}
