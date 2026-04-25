#!/usr/bin/env python3
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

"""Split a WAV file into multiple files using silence detection.

This implementation uses only Python's standard library (no paid or proprietary tools).

Usage:
    python3 split_wav_on_silence.py input.wav --min-silence-ms 250
"""

from __future__ import annotations

import argparse
import audioop
import json
import math
import wave
from pathlib import Path
from typing import List, Tuple


def db_to_rms(sample_width: int, threshold_db: float) -> float:
    max_amp = float((1 << (sample_width * 8 - 1)) - 1)
    return max_amp * (10.0 ** (threshold_db / 20.0))


def find_silent_ranges(
    raw_audio: bytes,
    frame_rate: int,
    sample_width: int,
    channels: int,
    threshold_db: float,
    min_silence_ms: int,
    analysis_step_ms: int,
) -> List[Tuple[int, int]]:
    bytes_per_frame = sample_width * channels
    step_frames = max(1, int(frame_rate * analysis_step_ms / 1000))
    step_bytes = step_frames * bytes_per_frame

    threshold_rms = db_to_rms(sample_width, threshold_db)

    is_silent: List[bool] = []
    for i in range(0, len(raw_audio), step_bytes):
        chunk = raw_audio[i : i + step_bytes]
        if not chunk:
            continue
        rms = audioop.rms(chunk, sample_width)
        is_silent.append(rms <= threshold_rms)

    min_silent_steps = max(1, math.ceil(min_silence_ms / analysis_step_ms))
    silent_ranges: List[Tuple[int, int]] = []

    run_start = None
    for idx, silent in enumerate(is_silent):
        if silent and run_start is None:
            run_start = idx
        if not silent and run_start is not None:
            if idx - run_start >= min_silent_steps:
                start_frame = run_start * step_frames
                end_frame = idx * step_frames
                silent_ranges.append((start_frame, end_frame))
            run_start = None

    if run_start is not None:
        idx = len(is_silent)
        if idx - run_start >= min_silent_steps:
            start_frame = run_start * step_frames
            end_frame = idx * step_frames
            silent_ranges.append((start_frame, end_frame))

    return silent_ranges


def build_chunks(total_frames: int, silent_ranges: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    chunks: List[Tuple[int, int]] = []
    cursor = 0

    for s_start, s_end in silent_ranges:
        if s_start > cursor:
            chunks.append((cursor, s_start))
        cursor = max(cursor, s_end)

    if cursor < total_frames:
        chunks.append((cursor, total_frames))

    return [(start, end) for start, end in chunks if end > start]


def pad_chunks(chunks: List[Tuple[int, int]], total_frames: int, keep_silence_frames: int) -> List[Tuple[int, int]]:
    if keep_silence_frames <= 0:
        return chunks

    padded = []
    for start, end in chunks:
        padded_start = max(0, start - keep_silence_frames)
        padded_end = min(total_frames, end + keep_silence_frames)
        padded.append((padded_start, padded_end))
    return padded


def write_chunks(
    input_wav: Path,
    raw_audio: bytes,
    params: wave._wave_params,
    chunks: List[Tuple[int, int]],
    output_dir: Path,
) -> List[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    bytes_per_frame = params.sampwidth * params.nchannels
    width = max(3, len(str(max(1, len(chunks)))))
    written: List[Path] = []

    for idx, (start_frame, end_frame) in enumerate(chunks, start=1):
        part_path = output_dir / f"{input_wav.stem}_part_{idx:0{width}d}.wav"
        start_byte = start_frame * bytes_per_frame
        end_byte = end_frame * bytes_per_frame
        part_data = raw_audio[start_byte:end_byte]

        with wave.open(str(part_path), "wb") as out_wav:
            out_wav.setnchannels(params.nchannels)
            out_wav.setsampwidth(params.sampwidth)
            out_wav.setframerate(params.framerate)
            out_wav.writeframes(part_data)
        written.append(part_path)

    return written


def write_split_manifest(
    input_wav: Path,
    output_dir: Path,
    params: wave._wave_params,
    chunks: List[Tuple[int, int]],
    segment_paths: List[Path],
    settings: dict,
) -> Path:
    entries = []
    for idx, ((start_frame, end_frame), segment_path) in enumerate(zip(chunks, segment_paths), start=1):
        entries.append(
            {
                "index": idx,
                "segment_file": str(segment_path),
                "start_frame": start_frame,
                "end_frame": end_frame,
                "start_sec": start_frame / params.framerate,
                "end_sec": end_frame / params.framerate,
            }
        )

    manifest = {
        "source_wav": str(input_wav.resolve()),
        "output_dir": str(output_dir.resolve()),
        "audio_params": {
            "channels": params.nchannels,
            "sample_width": params.sampwidth,
            "frame_rate": params.framerate,
            "frame_count": params.nframes,
        },
        "split_settings": settings,
        "segments": entries,
    }

    manifest_path = output_dir / f"{input_wav.stem}_split_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Split WAV file wherever silence is at least N milliseconds.")
    p.add_argument("input_wav", type=Path, help="Input WAV path")
    p.add_argument("--output-dir", type=Path, default=Path("split_output"), help="Output directory")
    p.add_argument("--min-silence-ms", type=int, default=250, help="Minimum silence duration in ms")
    p.add_argument("--silence-threshold-db", type=float, default=-38.0, help="Silence threshold in dBFS")
    p.add_argument("--analysis-step-ms", type=int, default=10, help="Silence analysis window in ms")
    p.add_argument("--keep-silence-ms", type=int, default=80, help="Silence padding around chunks in ms")
    p.add_argument("--min-chunk-ms", type=int, default=120, help="Drop chunks shorter than this duration")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if not args.input_wav.exists():
        raise SystemExit(f"Input file not found: {args.input_wav}")

    with wave.open(str(args.input_wav), "rb") as in_wav:
        params = in_wav.getparams()
        raw_audio = in_wav.readframes(params.nframes)

    silent_ranges = find_silent_ranges(
        raw_audio=raw_audio,
        frame_rate=params.framerate,
        sample_width=params.sampwidth,
        channels=params.nchannels,
        threshold_db=args.silence_threshold_db,
        min_silence_ms=args.min_silence_ms,
        analysis_step_ms=args.analysis_step_ms,
    )

    chunks = build_chunks(params.nframes, silent_ranges)
    keep_silence_frames = int(params.framerate * args.keep_silence_ms / 1000)
    chunks = pad_chunks(chunks, params.nframes, keep_silence_frames)

    min_chunk_frames = int(params.framerate * args.min_chunk_ms / 1000)
    chunks = [(s, e) for (s, e) in chunks if (e - s) >= min_chunk_frames]

    if not chunks:
        print("No non-silent chunks found. Try lowering --silence-threshold-db.")
        return

    written = write_chunks(args.input_wav, raw_audio, params, chunks, args.output_dir)
    manifest_path = write_split_manifest(
        input_wav=args.input_wav,
        output_dir=args.output_dir,
        params=params,
        chunks=chunks,
        segment_paths=written,
        settings={
            "min_silence_ms": args.min_silence_ms,
            "silence_threshold_db": args.silence_threshold_db,
            "analysis_step_ms": args.analysis_step_ms,
            "keep_silence_ms": args.keep_silence_ms,
            "min_chunk_ms": args.min_chunk_ms,
        },
    )
    print(f"Created {len(chunks)} split file(s) in: {args.output_dir}")
    print(f"Wrote split manifest: {manifest_path}")


if __name__ == "__main__":
    main()
