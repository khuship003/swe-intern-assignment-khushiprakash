"""Generic, supplier-independent helpers: number/date parsing, PDF line access, JSON writing.

Nothing in this file knows anything about Kessler. Supplier-specific logic lives in
kessler_legacy.py / kessler_v2.py / kessler_scan.py.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pdfplumber

MONTHS_DE = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
}


# ---------------------------------------------------------------- numbers
def parse_amount(s: str) -> Decimal:
    """German '1.234,56' -> Decimal('1234.56'). Trailing minus ('225,80-') or leading
    minus ('-13,97') both mean negative. Raises ValueError on anything else."""
    s = s.strip().replace("€", "").replace("EUR", "").strip()
    neg = s.startswith("-") or s.endswith("-")
    s = s.strip("-").strip()
    if not re.fullmatch(r"\d{1,3}(?:\.\d{3})*(?:,\d+)?|\d+(?:,\d+)?", s):
        raise ValueError(f"not a German number: {s!r}")
    val = Decimal(s.replace(".", "").replace(",", "."))
    return -val if neg else val


def money(d: Decimal) -> float:
    """Round half-up to cents and hand back a plain float for JSON."""
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def num(d: Decimal) -> float:
    """Plain number without forcing 2 decimals (quantities, unit prices like 0,152)."""
    return float(d)


# ---------------------------------------------------------------- dates
def parse_date(s: str) -> date:
    """'06.10.2026' or '19. Oktober 2026' -> date. Raises ValueError if invalid."""
    s = s.strip()
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        d, mo, y = map(int, m.groups())
        return date(y, mo, d)
    m = re.fullmatch(r"(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)\s+(\d{4})", s)
    if m and m.group(2).lower() in MONTHS_DE:
        return date(int(m.group(3)), MONTHS_DE[m.group(2).lower()], int(m.group(1)))
    raise ValueError(f"unrecognised date: {s!r}")


def iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def add_days(d: date, n: int) -> date:
    return d + timedelta(days=n)


# ---------------------------------------------------------------- PDF access
def group_lines(words: list[dict], y_tol: float = 3.0) -> list[dict]:
    """Group pdfplumber words into text lines (sorted top->bottom, left->right)."""
    lines: list[dict] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        for ln in lines:
            if abs(ln["top"] - w["top"]) <= y_tol:
                ln["words"].append(w)
                break
        else:
            lines.append({"top": w["top"], "words": [w]})
    for ln in lines:
        ln["words"].sort(key=lambda w: w["x0"])
        ln["text"] = " ".join(w["text"] for w in ln["words"])
        ln["x0"] = ln["words"][0]["x0"]
    return sorted(lines, key=lambda l: l["top"])


class Page:
    """One PDF page with helpers to read a rectangular region as text lines."""

    def __init__(self, plumber_page, number: int):
        self.p = plumber_page
        self.number = number
        self.words = plumber_page.extract_words(keep_blank_chars=False, use_text_flow=False)

    def lines(self, x0=0, x1=10_000, top=0, bottom=10_000) -> list[dict]:
        ws = [w for w in self.words
              if w["x0"] >= x0 and w["x1"] <= x1 + 1 and w["top"] >= top and w["bottom"] <= bottom]
        return group_lines(ws)

    def all_lines(self) -> list[dict]:
        return group_lines(self.words)


def open_pages(path: str | Path) -> list[Page]:
    with pdfplumber.open(str(path)) as pdf:
        return [Page(p, i + 1) for i, p in enumerate(pdf.pages)]


def has_text_layer(path: str | Path) -> bool:
    with pdfplumber.open(str(path)) as pdf:
        return any((p.extract_text() or "").strip() for p in pdf.pages)


# ---------------------------------------------------------------- output
def write_json(obj: dict, out_path: str | Path) -> None:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class ExtractionError(Exception):
    pass
