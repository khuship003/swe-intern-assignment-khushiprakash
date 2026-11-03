#!/usr/bin/env python3
"""Usage: python extract.py <path-to-pdf>   ->   outputs/<pdf-name>.json  (+ outputs/<pdf-name>.notes.json)

Picks the right Kessler layout parser by looking at the table header, so adding a new layout never
touches the existing ones: write a new module with matches()/parse() and register it in LAYOUTS."""
from __future__ import annotations

import sys
from pathlib import Path

from fingerprint import kessler_legacy, kessler_v2
from fingerprint.common import ExtractionError, has_text_layer, open_pages, write_json
from fingerprint.kessler_common import assemble

OUT_DIR = Path(__file__).parent / "outputs"
LAYOUTS = [kessler_legacy, kessler_v2]          # order does not matter: each has its own matches()


def extract(pdf_path: Path) -> tuple[dict, dict]:
    if not has_text_layer(pdf_path):
        from fingerprint import kessler_scan     # lazy: needs OCR deps (tesseract) only for scans
        return kessler_scan.extract_scan(pdf_path)
    pages = open_pages(pdf_path)
    for layout in LAYOUTS:
        if layout.matches(pages):
            return assemble(layout.parse(pages))
    raise ExtractionError("unknown Kessler layout: no table header matched")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    pdf = Path(argv[1])
    try:
        data, notes = extract(pdf)
    except ExtractionError as e:
        print(f"ERROR {pdf.name}: {e}", file=sys.stderr)
        return 1
    write_json(data, OUT_DIR / f"{pdf.stem}.json")
    write_json(notes, OUT_DIR / f"{pdf.stem}.notes.json")
    print(f"OK {pdf.name} -> outputs/{pdf.stem}.json ({len(data['line_items'])} items)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
