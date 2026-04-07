#!/usr/bin/env python3
"""
Export Firefox places.sqlite bookmark text using the same SQLiteProcessor as ingest.

Writes a single .txt file suitable for `pal pull` on the output directory.

Example:
  uv run python scripts/export_firefox_bookmarks.py \\
    --places ~/Library/Application\\ Support/Firefox/Profiles/<profile>/places.sqlite \\
    --out-dir ~/.pal/exports/firefox-bookmarks
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    default_out = Path.home() / ".pal" / "exports" / "firefox-bookmarks"
    parser = argparse.ArgumentParser(description="Export Firefox bookmarks from places.sqlite to plain text.")
    parser.add_argument(
        "--places",
        type=Path,
        required=True,
        help="Path to places.sqlite (Firefox profile).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=default_out,
        help=f"Directory for the export (default: {default_out})",
    )
    parser.add_argument(
        "--out-file",
        type=str,
        default="firefox-bookmarks.txt",
        help="Output filename inside --out-dir (default: firefox-bookmarks.txt)",
    )
    args = parser.parse_args()

    places = args.places.expanduser().resolve()
    if not places.is_file():
        print(f"Not a file: {places}", file=sys.stderr)
        return 1

    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / args.out_file

    src = REPO / "src"
    sys.path.insert(0, str(src))
    os.chdir(REPO)

    from processors import SQLiteProcessor

    data = places.read_bytes()
    text = SQLiteProcessor().extract(data, str(places))
    if not isinstance(text, str):
        print("Unexpected extract() result (expected str)", file=sys.stderr)
        return 1

    out_path.write_text(text, encoding="utf-8")
    print(f"Wrote {len(text)} chars to {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
