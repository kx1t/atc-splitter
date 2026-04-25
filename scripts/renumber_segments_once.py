#!/usr/bin/env python3
"""One-time segment renumbering utility.

Renumbers segment WAV files to a clean 001..N sequence per source file and
updates split manifests and transcript maps accordingly.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_helpers() -> None:
    # In-container layout: this script lives in /scripts and app modules in /app.
    app_dir = Path("/app")
    if app_dir.exists():
        sys.path.insert(0, str(app_dir))
        return

    # Local repo layout fallback for development.
    local_app = Path(__file__).resolve().parents[1] / "app"
    sys.path.insert(0, str(local_app))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Renumber split segments and update manifests.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("/app/data"),
        help="Root data directory containing uploads/ and segments/",
    )
    parser.add_argument(
        "--source-stem",
        type=str,
        help="Only renumber one source stem (for example: atc_recording_2026_04_25)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _load_helpers()

    from segment_renumbering import renumber_all_sources, renumber_segments_for_source

    data_dir = args.data_dir
    if not data_dir.exists():
        raise SystemExit(f"Data directory does not exist: {data_dir}")

    if args.source_stem:
        stem = args.source_stem
        result = renumber_segments_for_source(
            source_stem=stem,
            source_wav=data_dir / "uploads" / f"{stem}.wav",
            seg_dir=data_dir / "segments" / stem,
            manifest_file=data_dir / "segments" / stem / f"{stem}_split_manifest.json",
            transcript_file=data_dir / "segments" / stem / f"{stem}_transcripts.json",
        )
        payload = {"renumbered_sources": 1 if result.get("renamed", 0) else 0, "results": [result]}
    else:
        payload = renumber_all_sources(data_dir)

    if args.pretty:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
