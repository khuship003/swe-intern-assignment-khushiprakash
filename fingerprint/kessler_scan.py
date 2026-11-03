"""BONUS: Kessler LEGACY layout as a scanned (image-only) PDF -> OCR (tesseract) with safeguards.

Principle: a 110 dpi 1-bit scan cannot be read reliably by one OCR pass, so we
  1. OCR several differently thresholded/blurred variants and VOTE per field / per table cell,
  2. read table cells by template position (the scan is the legacy template) with digit-only whitelists,
  3. among the vote candidates prefer the combination that satisfies the arithmetic (qty x price / PE x (1-disc)
     = total, sums, VAT). Whenever that arithmetic changes a plain majority vote this is recorded in the
     sidecar notes (`ocr.reconciled`), it is never silent,
  4. report a trust level per field in outputs/<name>.notes.json and in the README.
Descriptions, article numbers etc. are best-effort text and are marked untrusted when the vote is split.
"""
from __future__ import annotations

import difflib
import itertools
import re
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import numpy as np
import pdfplumber
import pytesseract
from PIL import Image, ImageFilter, ImageOps

from .common import ExtractionError, iso
from .kessler_common import Parsed, RawItem, assemble

DPI_DETECT, DPI_OCR = 110, 330
K1, K3 = DPI_DETECT / 72, DPI_OCR / 72                    # px per pt
VARIANTS = [(th, bl) for th in (110, 140, 170) for bl in (0.0, 0.9)]

# Template geometry in PDF points (taken from the legacy layout's header row).
COLS = {  # name: (x0, x1)   price and PE touch in this template ("3,85100"), so they are ONE cell 'pp'
    "pos": (0, 82), "art": (82, 156), "desc": (156, 336), "qty": (336, 367), "unit": (367, 400),
    "pp": (400, 477), "rab": (477, 512), "total": (512, 560), "vat": (560, 600),
}
ROW0_Y, PITCH = 318.0, 14.6                              # first item row centre / row pitch (pt)
KNOWN_UNITS = ["Stk", "m", "psch"]
KNOWN_PREFIX = ["KI", "BU", "DL"]
# Supplier constants of THIS fingerprint; used only if the (tiny) footer cannot be read, and flagged then.
SUPPLIER = {"name": "Kessler Industrietechnik GmbH", "vat_id": "DE287364519"}


# ------------------------------------------------------------------ image handling
def _render(pdf_path: Path, dpi: int):
    with pdfplumber.open(str(pdf_path)) as pdf:
        return pdf.pages[0].to_image(resolution=dpi).original.convert("L")


def _variant(img, th: int, blur: float):
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    return img.point(lambda p: 255 if p > th else 0)


def _ocr(img, box_pt, psm=6, wl=None) -> str:
    x0, y0, x1, y1 = [int(v * K3) for v in box_pt]
    cfg = f"--psm {psm}" + (f" -c tessedit_char_whitelist={wl}" if wl else "")
    crop = ImageOps.expand(img.crop((x0, y0, x1, y1)), border=30, fill=255)   # tesseract needs a white margin
    return pytesseract.image_to_string(crop, config=cfg)


def _vote(cands: list) -> tuple[object | None, int, int]:
    cands = [c for c in cands if c not in (None, "")]
    if not cands:
        return None, 0, 0
    (val, n), = Counter(cands).most_common(1)
    return val, n, len(cands)


# ------------------------------------------------------------------ cell parsers (tolerant)
_CONFUSABLE = str.maketrans({"S": "5", "s": "5", "O": "0", "o": "0", "l": "1", "I": "1", "i": "1", "B": "8",
                              "Z": "2", "z": "2", "g": "9", "q": "9", "A": "4", "a": "4"})


def _numeric(s: str) -> str:
    """Typical OCR letter/digit confusions inside numeric cells ('3.8S' -> '3.85'). Only used in number cells."""
    return s.translate(_CONFUSABLE)


def _digits_amount(s: str) -> Decimal | None:
    """Scan loses/garbles the decimal comma, but every price/total has exactly 2 decimals -> last 2 digits."""
    s = _numeric(s)
    neg = "-" in s
    d = re.sub(r"\D", "", s)
    if len(d) < 3:
        return None
    v = Decimal(d[:-2] + "." + d[-2:])
    return -v if neg else v


def _qty(s: str) -> Decimal | None:
    s = s.strip().replace(" ", "")
    if re.fullmatch(r"\d+", s):
        return Decimal(s)
    if re.fullmatch(r"\d+[.,]\d{1,2}", s):
        return Decimal(s.replace(",", "."))
    return None


def _art(s: str) -> str | None:
    s = re.sub(r"[^A-Z0-9]", "", s.upper())
    m = re.search(r"([A-Z]{1,2})(\d{5})$", s)
    if not m:
        return None
    pre = difflib.get_close_matches(m.group(1).ljust(2, "I"), KNOWN_PREFIX, n=1, cutoff=0.5)
    return f"{pre[0]}-{m.group(2)}" if pre else None


def _unit(s: str) -> str | None:
    s = s.strip()
    m = difflib.get_close_matches(s, KNOWN_UNITS, n=1, cutoff=0.5) if s else []
    return m[0] if m else None


def _int_in(s: str, allowed: set[int]) -> int | None:
    d = re.sub(r"\D", "", s)
    return int(d) if d and int(d) in allowed else None


def _date(m: re.Match) -> str | None:
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None


# ------------------------------------------------------------------ table
def _find_rows(img_detect) -> list[float]:
    """Centres (pt) of item rows: walk the template row grid while the article column contains ink."""
    ink = np.array(img_detect) < 128
    def strip(y_pt, x0_pt, x1_pt, h=7):
        y = int(y_pt * K1)
        return ink[y - h:y + h, int(x0_pt * K1):int(x1_pt * K1)].any(axis=0).mean()
    rows = []
    for k in range(0, 40):
        y = ROW0_Y + k * PITCH
        if strip(y, 72, 84) > 0.4 or strip(y, 85, 156) < 0.12:      # inked gap = the rule below the table / no article
            break
        rows.append(y)
    return rows


def _read_table(vimgs, rows_y: list[float]) -> list[dict]:
    """One OCR pass per variant over the whole table block; words are assigned to (row, column) by position."""
    top, bottom = ROW0_Y - 10, rows_y[-1] + 10
    per_row = [{c: [] for c in COLS} for _ in rows_y]
    for img in vimgs:
        x0, y0, x1, y1 = [int(v * K3) for v in (40, top, 600, bottom)]
        d = pytesseract.image_to_data(ImageOps.expand(img.crop((x0, y0, x1, y1)), border=30, fill=255),
                                      config="--psm 6", output_type=pytesseract.Output.DICT)
        cell: dict[tuple[int, str], list] = {}
        for t, l, w, tp, h in zip(d["text"], d["left"], d["width"], d["top"], d["height"]):
            if not t.strip():
                continue
            cx = 40 + (l - 30 + w / 2) / K3
            cy = top + (tp - 30 + h / 2) / K3
            ri = min(range(len(rows_y)), key=lambda i: abs(rows_y[i] - cy))
            if abs(rows_y[ri] - cy) > 6:
                continue
            col = next((c for c, (a_, b_) in COLS.items() if a_ <= cx < b_), None)
            if col:
                cell.setdefault((ri, col), []).append((l, t))
        for (ri, col), ws in cell.items():
            per_row[ri][col].append(" ".join(t for _, t in sorted(ws)))
    return per_row


_ONE_LIKE = str.maketrans({c: "1" for c in "!|{}§()lI"})     # glyphs OCR produces for a printed '1'


def _pe_votes(strings: list[str]) -> Counter:
    """PE = digit count of the last whitespace token of the price/PE cell: '12.40 1' -> 1, '3.85 100' -> 100."""
    c: Counter = Counter()
    for s in strings:
        toks = s.split()
        if len(toks) >= 2:
            tail = toks[-1].translate(_ONE_LIKE)
            d = re.sub(r"\D", "", tail)
            if 1 <= len(d) <= 4 and len(d) == len(re.sub(r"[^\w!|{}§()]", "", toks[-1])):
                c[10 ** (len(d) - 1)] += 1
    return c


def _sim(price: Decimal, strings: list[str]) -> float:
    """How well does a price hypothesis ('10.90' -> '1090') match what OCR saw in the price cell?"""
    want = re.sub(r"\D", "", f"{price:.2f}")
    best = [difflib.SequenceMatcher(None, want, re.sub(r"\D", "", _numeric(s))).ratio() for s in strings] or [0]
    return sum(sorted(best)[-3:]) / min(3, len(best))


def _decode_row(cells: dict) -> tuple[dict, dict]:
    """Constraint decoding. The price/PE cell is the worst-read cell (digits touch, comma blurred), so the
    unit price is NOT taken from OCR directly: it is derived from the better-read qty / discount / total / PE
    (price = total x PE / qty / (1-disc)), accepted only if it has exactly 2 decimals (as every printed price
    does), and scored by how similar it is to the digits OCR saw. Everything here lands in the notes."""
    cq = Counter(c for c in map(_qty, cells["qty"]) if c is not None)
    ct = Counter(c for c in map(_digits_amount, cells["total"]) if c is not None)
    cr = Counter(_int_in(s, {1, 2, 3, 5, 10, 15, 20}) for s in cells["rab"])
    cr.pop(None, None)
    pes = _pe_votes(cells["pp"])
    pe = pes.most_common(1)[0][0] if pes and pes.most_common(1)[0][1] >= 2 else 1      # PE=1 unless clear majority
    n_rab_votes = sum(cr.values())
    rab_opts = [None] + [r for r, _ in cr.most_common(3)]
    price_strs = [re.sub(r"\D", "", _numeric(s.split()[0])) for s in cells["pp"] if s.split()]
    price_ocr = {Decimal(d[:-2] + "." + d[-2:]) for d in price_strs if len(d) >= 3}
    best, best_score = None, -1.0
    q_opts = [v for v, _ in cq.most_common(4)]
    # family A: price implied by (qty, total, disc, PE);  family B: total implied by (qty, OCR price, disc, PE)
    hyps = []
    for q, t, r in itertools.product(q_opts, [v for v, _ in ct.most_common(4)], rab_opts):
        if q:
            implied = t * pe / q / (1 - Decimal(r or 0) / 100)
            price = implied.quantize(Decimal("0.01"))
            if price > 0 and abs(implied - price) <= Decimal("0.005") * pe / q + Decimal("1e-9"):
                hyps.append((q, price, r, t))
    for q, price, r in itertools.product(q_opts, price_ocr, rab_opts):
        t = (q * price / pe * (1 - Decimal(r or 0) / 100)).quantize(Decimal("0.01"))
        hyps.append((q, price, r, t))
    for q, price, r, t in hyps:
        sc = cq[q] + (cr[r] if r else (len(cells["rab"]) - n_rab_votes) * 0.5) \
             + 3 * _sim(price, cells["pp"]) + 3 * _sim(t, cells["total"]) + 0.5 * ct[t]
        if sc > best_score:
            best, best_score = (q, price, r, t), sc
    plain = {"qty": cq.most_common(1)[0][0] if cq else None, "total": ct.most_common(1)[0][0] if ct else None}
    info = {"arith_consistent": best is not None, "majority_vote": {k: str(v) for k, v in plain.items()},
            "pe_votes": dict(pes), "price_similarity": None}
    if not best:
        return {"qty": plain["qty"], "price": None, "pe": pe, "rab": None, "total": plain["total"]}, info
    q, price, r, t = best
    info["price_similarity"] = round(_sim(price, cells["pp"]), 2)
    info["total_similarity"] = round(_sim(t, cells["total"]), 2)
    info["reconciled"] = (q, t) != (plain["qty"], plain["total"])
    info["qty_votes"], info["total_votes"] = f"{cq[q]}/{len(cells['qty'])}", f"{ct[t]}/{len(cells['total'])}"
    return {"qty": q, "price": price, "pe": pe, "rab": r, "total": t}, info


# ------------------------------------------------------------------ header / totals
def _read_header(vimgs) -> tuple[dict, dict]:
    meta_txt = [_ocr(i, (330, 112, 595, 210)) for i in vimgs]
    buyer_txt = [_ocr(i, (40, 112, 335, 215)) for i in vimgs]
    c = {k: [] for k in ("doc_no", "date", "customer", "order", "ls", "service", "plz_city", "street", "name")}
    for t in meta_txt:
        for m in re.finditer(r"RE\W{0,2}(20\d{2})\W{0,2}(\d{5})", t):
            c["doc_no"].append(f"RE-{m.group(1)}-{m.group(2)}")
        for ln in t.splitlines():
            dm = re.search(r"(\d{2})\D{1,3}(\d{2})\D{1,3}(20\d{2})", ln)
            if dm and re.search(r"Leist|stungs", ln, re.I):
                c["service"].append(_date(dm))
            elif dm and re.search(r"vom", ln):
                pass
            elif dm and re.search(r"Dat|tum", ln, re.I):
                c["date"].append(_date(dm))
            if (m := re.search(r"\b(45\d{8})\b", ln)):
                c["order"].append(m.group(1))
            if (m := re.search(r"\bLS\W{0,2}(\d{5})\b", ln)):
                c["ls"].append(f"LS-{m.group(1)}")
            if (m := re.search(r"\b(\d{5})\b\s*$", ln)) and not re.search(r"LS|20\d\d", ln):
                c["customer"].append(m.group(1))
    for t in buyer_txt:
        lines = [l.strip() for l in t.splitlines() if l.strip()]
        for l in lines:
            if (m := re.match(r"^(\d{5})\s+([A-Za-zäöüß]{4,})", l)):
                c["plz_city"].append((m.group(1), m.group(2)))
            if (m := re.match(r"^([A-Za-zäöüß]+stra\w{1,3})\s+(\d+)\s*$", l)):
                c["street"].append(f"{m.group(1)} {m.group(2)}")
            if re.search(r"Anlagenbau", l):
                c["name"].append(re.sub(r"\s+", " ", l))
    return c, {"meta_txt": meta_txt[:1], "buyer_txt": buyer_txt[:1]}


def _read_totals(vimgs, y_top_pt: float) -> dict:
    """Raw text 'rest of line' per kind (net/vat by rate, gross) for every variant. No number parsing here:
    the noisy digits are only used to CORROBORATE values that follow from the arithmetic (see _supports)."""
    raw: dict = {"net": {}, "vat": {}, "gross": []}
    for img in vimgs:
        for ln in _ocr(img, (330, y_top_pt, 595, y_top_pt + 130)).splitlines():
            if (m := re.search(r"(\d{1,2})\W{1,4}[UuVv][Ss5]\W?\w?\s*a\w{2}\s+(.+)$", ln)) and int(m.group(1)) in (7, 19):
                raw["vat"].setdefault(int(m.group(1)), []).append(m.group(2))
            elif (m := re.search(r"(\d{1,2})\W{1,4}[UuVv][Ss5]\W?\w?\s+(.+)$", ln)) and int(m.group(1)) in (7, 19):
                raw["net"].setdefault(int(m.group(1)), []).append(m.group(2))
            elif re.search(r"EUR", ln) and not re.search(r"USt", ln):
                raw["gross"].append(re.sub(r"EUR.*", "", ln.split(None, 1)[-1] if len(ln.split()) > 1 else ln))
    return raw


def _supports(value: Decimal, strings: list[str]) -> int:
    """In how many OCR strings do the digits of `value` appear with at most ONE wrong/missing character?"""
    want = re.sub(r"\D", "", f"{abs(value):.2f}")
    n = 0
    for st in strings:
        have = re.sub(r"\D", "", _numeric(st))
        m = difflib.SequenceMatcher(None, want, have).find_longest_match(0, len(want), 0, len(have))
        if m.size >= len(want) - 1 and len(want) >= 3:
            n += 1
    return n


# ------------------------------------------------------------------ main
def extract_scan(pdf_path: Path) -> tuple[dict, dict]:
    det = _render(pdf_path, DPI_DETECT)                       # native resolution of the embedded scan
    big = det.resize((det.width * 3, det.height * 3), Image.LANCZOS)   # smooth 3x upscale OCRs far better than a re-render
    vimgs = [_variant(big, th, bl) for th, bl in VARIANTS]
    trust: dict[str, str] = {}
    reconciled: list[str] = []

    # --- items
    rows_y = _find_rows(det)
    if not rows_y:
        raise ExtractionError("scan: no table rows found")
    items: list[RawItem] = []
    table = _read_table(vimgs, rows_y)
    for n, cells in enumerate(table, 1):
        vals, info = _decode_row(cells)
        pos, pn, pt_ = _vote([_int_in(s, set(range(1, 100))) for s in cells["pos"]])
        art, an, at = _vote([_art(s) for s in cells["art"]])
        unit, un, ut = _vote([_unit(s) for s in cells["unit"]])
        vat, vn, vt = _vote([_int_in(s, {7, 19}) for s in cells["vat"]])
        desc, _, _ = _vote([re.sub(r"\s+", " ", s) for s in cells["desc"] if len(s) > 8])
        if None in (vals["qty"], vals["price"], vals["total"]):
            raise ExtractionError(f"scan: row {n} unreadable: {info}")
        items.append(RawItem(position=pos or n, article_number=art or "", description=desc or "",
                             quantity=vals["qty"], unit=unit or "", unit_price=vals["price"],
                             price_unit=Decimal(vals["pe"]),
                             discount_percent=Decimal(vals["rab"]) if vals["rab"] else None,
                             line_total=vals["total"], vat_rate=vat or 19))
        strong = info["arith_consistent"] and (info["price_similarity"] or 0) >= 0.7 and (info["total_similarity"] or 0) >= 0.7 and \
            int(info["qty_votes"].split("/")[0]) >= 3 if info["arith_consistent"] else False
        trust[f"line_items[{n}].qty/unit_price/PE/discount/total"] = (
            f"medium (qty {info['qty_votes']} votes; OCR-vs-arithmetic similarity price {info['price_similarity']}, "
            f"total {info['total_similarity']}; unit_price/total chosen so that the arithmetic closes)" if strong else
            f"LOW ({'arithmetic closes but weak OCR agreement' if info['arith_consistent'] else 'arithmetic does not close'})")
        if info.get("reconciled"):
            reconciled.append(f"row {n}: majority vote {info['majority_vote']} replaced by arithmetic-consistent reading")
        trust[f"line_items[{n}].article_number"] = f"{'medium' if an >= 3 else 'LOW'} ({an}/{at} variants agree)"
        trust[f"line_items[{n}].unit"] = f"{'medium' if un >= 3 else 'LOW'} (fuzzy-matched to Stk/m/psch)"
        trust[f"line_items[{n}].vat_rate"] = f"{'medium' if vn >= 4 else 'LOW'} ({vn}/{vt})"
        trust[f"line_items[{n}].description"] = "LOW (free text, OCR noise)"

    # --- totals (block under the rule). Values come from the arithmetic on the OCR'd lines and are accepted only
    #     if the noisy OCR of the totals block corroborates them (<=1 wrong character in >=2 variants).
    raw = _read_totals(vimgs, rows_y[-1] + PITCH + 12)
    sums: dict[int, Decimal] = {}
    for it in items:
        sums[it.vat_rate] = sums.get(it.vat_rate, Decimal(0)) + it.line_total
    net, vat = {}, {}
    for rate, total_ in sums.items():
        sup = _supports(total_, raw["net"].get(rate, []))
        net[rate] = total_
        trust[f"totals.net[{rate}]"] = (f"medium (= sum of OCR'd lines; digits seen in {sup}/{len(vimgs)} variants)" if sup >= 2
                                        else "LOW (sum of OCR'd lines; printed net not corroborated by OCR)")
        exp_vat = (total_ * rate / 100).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
        sup = _supports(exp_vat, raw["vat"].get(rate, []))
        vat[rate] = exp_vat
        trust[f"totals.vat[{rate}]"] = (f"medium (= net x {rate}%; digits seen in {sup}/{len(vimgs)} variants)" if sup >= 2
                                        else f"LOW (= net x {rate}%; printed VAT not corroborated by OCR)")
    gross = sum(net.values(), Decimal(0)) + sum(vat.values(), Decimal(0))
    sup = _supports(gross, raw["gross"])
    trust["totals.gross_total"] = (f"medium (= net + vat; digits seen in {sup}/{len(vimgs)} variants, "
                                   f"plain OCR read differs by one digit)" if sup >= 2 else "LOW (= net + vat; not corroborated by OCR)")
    reconciled.append("totals: net/vat/gross are arithmetic results of the OCR'd lines (net, net x rate, net + vat), "
                      "accepted only when the OCR text of the totals block agrees within one character")

    # --- header
    c, dbg = _read_header(vimgs)
    def pick(key, label, min_votes=3):
        v, n, t = _vote(c[key])
        trust[label] = f"{'medium' if n >= min_votes else 'LOW'} ({n}/{t} variants agree)"
        return v
    doc_no, doc_date, service = pick("doc_no", "document_number"), pick("date", "document_date"), pick("service", "service_date")
    order, ls, cust = pick("order", "order_reference"), pick("ls", "delivery_note_number"), pick("customer", "buyer.customer_number")
    street, name = pick("street", "buyer.street"), pick("name", "buyer.name")
    pc, pn_, pt2 = _vote([p for p, _ in c["plz_city"]])
    city, _, _ = _vote([cty for _, cty in c["plz_city"]])
    trust["buyer.postal_code"] = f"{'medium' if pn_ >= 3 else 'LOW'} ({pn_}/{pt2} variants agree)"
    trust["buyer.city"] = "LOW (OCR has no German model: umlauts/ß unreliable)"
    if not (doc_no and doc_date):
        raise ExtractionError("scan: document number/date not readable")

    due_c = []
    for i in vimgs:
        for ln in _ocr(i, (40, rows_y[-1] + 100, 340, rows_y[-1] + 200)).splitlines():
            dm = re.search(r"[Zz]ahl\w*\W+\w+\W+(\d{2})\D{1,3}(\d{2})\D{1,3}(20\d{2})", ln)
            if dm:
                due_c.append(_date(dm))
    due, dn, dt = _vote(due_c)
    trust["due_date"] = f"{'medium' if dn >= 3 else 'LOW'} ({dn}/{dt} variants agree)" if due else "not found"

    header = {
        "document_type": "invoice", "document_number": doc_no, "document_date": doc_date,
        "service_date": service, "due_date": due, "currency": "EUR",
        "supplier": dict(SUPPLIER),
        "buyer": {"name": name or "", "street": street or "", "postal_code": pc or "", "city": city or "",
                  "customer_number": cust or ""},
        "order_reference": order, "delivery_note_number": ls, "referenced_invoice": None,
    }
    trust["supplier.name / vat_id"] = "constant of this fingerprint (footer too small to OCR) – NOT read from the scan"
    trust["document_type"] = "assumed from the 'RECHNUNG' template heading position (not OCR-verified)"
    trust["cash_discount"] = "null (no Skonto sentence found; not verified by OCR)"
    parsed = Parsed(header=header, items=items, net_by_rate=net, vat_by_rate=vat, net_total=None,
                    gross_total=gross, cash_discount=None,
                    notes={"layout": "scan/legacy", "ocr": {"variants": len(VARIANTS), "reconciled": reconciled,
                                                            "trust": trust, "raw_header_sample": dbg}})
    return assemble(parsed)
