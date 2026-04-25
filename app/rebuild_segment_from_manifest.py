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

"""Create a new segment directly from the original WAV using split-manifest indices.

This avoids joining already split files and instead re-extracts from the source WAV.

Examples:
  python3 rebuild_segment_from_manifest.py \
    --manifest segments/my_audio/my_audio_split_manifest.json \
    --segments 3,4

  python3 rebuild_segment_from_manifest.py \
    --manifest segments/my_audio/my_audio_split_manifest.json \
    --segments 3,4 \
    --output-wav merged_3_4.wav
"""

from __future__ import annotations

import argparse
import json
import re
import wave
from pathlib import Path
from typing import Dict, List


PART_RE = re.compile(r"_part_(\d+)\.wav$", re.IGNORECASE)


def parse_segments(value: str) -> List[int]:
    parts = [v.strip() for v in value.split(",") if v.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("--segments must contain at least one index")
    try:
        idxs = sorted({int(v) for v in parts})
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--segments must be comma-separated integers") from exc
    if any(i <= 0 for i in idxs):
        raise argparse.ArgumentTypeError("segment indices must be >= 1")
    return idxs


def infer_index_from_filename(segment_file: str) -> int | None:
    name = Path(segment_file).name
    match = PART_RE.search(name)
    if not match:
        return None
    return int(match.group(1))


def load_manifest(manifest_path: Path) -> Dict:
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON manifest: {manifest_path}") from exc


def build_segment_map(manifest: Dict) -> Dict[int, Dict]:
    segments = manifest.get("segments")
    if not isinstance(segments, list):
        raise SystemExit("Manifest does not contain a valid 'segments' list")

    seg_map: Dict[int, Dict] = {}
    for seg in segments:
        if not isinstance(seg, dict):
            continue

        idx = seg.get("index")
        if not isinstance(idx, int):
            idx = infer_index_from_filename(str(seg.get("segment_file", "")))
        if idx is None:
            continue

        if "start_frame" not in seg or "end_frame" not in seg:
            continue

        seg_map[idx] = seg

    if not seg_map:
        raise SystemExit("No usable segment entries found in manifest")
    return seg_map


def default_output_name(source_wav: Path, segment_indices: List[int]) -> str:
    label = "_".join(f"s{i:03d}" for i in segment_indices)
    return f"{source_wav.stem}_from_{label}.wav"


def extract_span(source_wav: Path, start_frame: int, end_frame: int, output_wav: Path) -> None:
    with wave.open(str(source_wav), "rb") as in_wav:
        nch = in_wav.getnchannels()
        sw = in_wav.getsampwidth()
        fr = in_wav.getframerate()
        total = in_wav.getnframes()

        start_frame = max(0, min(start_frame, total))
        end_frame = max(start_frame, min(end_frame, total))

        in_wav.setpos(start_frame)
        frames = in_wav.readframes(end_frame - start_frame)

    output_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_wav), "wb") as out_wav:
        out_wav.setnchannels(nch)
        out_wav.setsampwidth(sw)
        out_wav.setframerate(fr)
        out_wav.writeframes(frames)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rebuild a single segment from original WAV using split manifest.")
    p.add_argument("--manifest", type=Path, required=True, help="Path to *_split_manifest.json")
    p.add_argument(
        "--segments",
        type=parse_segments,
        required=True,
        help="Comma-separated segment indices (example: 3,4)",
    )
    p.add_argument(
        "--output-wav",
        type=Path,
        help="Output WAV path. Default: source-stem + selected segment indices",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    manifest = load_manifest(args.manifest)

    source_wav = Path(str(manifest.get("source_wav", "")))
    if not source_wav.exists():
        raise SystemExit(f"Source WAV from manifest not found: {source_wav}")

    seg_map = build_segment_map(manifest)

    missing = [i for i in args.segments if i not in seg_map]
    if missing:
        raise SystemExit(f"Segment index(es) not found in manifest: {missing}")

    start_frame = min(int(seg_map[i]["start_frame"]) for i in args.segments)
    end_frame = max(int(seg_map[i]["end_frame"]) for i in args.segments)

    output_wav = args.output_wav
    if output_wav is None:
        output_wav = args.manifest.parent / default_output_name(source_wav, args.segments)

    extract_span(source_wav, start_frame, end_frame, output_wav)
    print(f"Wrote rebuilt segment: {output_wav}")
    print(f"From source: {source_wav}")
    print(f"Using segment indices: {args.segments}")


if __name__ == "__main__":
    main()
