#!/usr/bin/env python3
"""Usage: python validate.py outputs/   ->   outputs/validation_report.md

Reads every extracted <doc>.json (ignores *.notes.json and the report) and runs checks V1-V7
(+ V8 carry-over, informational). Every FAIL lists line / expected / printed / difference.
A FAIL caused by the document itself is a finding, not a bug."""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

TOL = Decimal("0.01")


def D(x) -> Decimal:
    return Decimal(str(x))


def f2(x: Decimal) -> str:
    x = x.quantize(Decimal("0.01"))
    return f"{abs(x):,.2f}" if x == 0 else f"{x:,.2f}"      # never print '-0.00'


def check_v1(doc) -> tuple[bool, list[str]]:
    sign = -1 if doc["document_type"] == "credit_note" else 1
    ok, out = True, []
    for it in doc["line_items"]:
        disc = D(it["discount_percent"] or 0)
        exp = sign * D(it["quantity"]) * D(it["unit_price"]) / D(it["price_unit"]) * (1 - disc / 100)
        printed = D(it["line_total"])
        diff = printed - exp
        good = abs(diff) <= TOL
        ok &= good
        if not good:
            out.append(f"  - **FAIL pos {it['position']}** ({it['article_number']}): "
                       f"{it['quantity']} × {it['unit_price']} ÷ {it['price_unit']} × (1 − {disc}%) "
                       f"→ expected **{f2(exp)}**, printed **{f2(printed)}**, difference **{f2(diff)}**")
    out.insert(0, f"  - {len(doc['line_items'])} lines checked, "
                  f"{sum(1 for l in out)} deviating" + (" (credit note: expected sign flipped)" if sign < 0 else ""))
    return ok, out


def check_v2(doc, notes) -> tuple[bool, list[str]]:
    t, ok, out = doc["totals"], True, []
    sums: dict[str, Decimal] = {}
    for it in doc["line_items"]:
        sums[str(it["vat_rate"])] = sums.get(str(it["vat_rate"]), Decimal(0)) + D(it["line_total"])
    derived = any("net_by_vat_rate" in s for s in notes.get("derived_fields", []))
    for rate in sorted(set(sums) | set(t["net_by_vat_rate"]), key=int, reverse=True):
        s, n = sums.get(rate, Decimal(0)), D(t["net_by_vat_rate"].get(rate, 0))
        good = abs(s - n) <= TOL
        ok &= good
        tag = "PASS" if good else "**FAIL**"
        extra = " (net per rate is not printed on this layout → derived from the lines, so this comparison is circular)" if derived else ""
        out.append(f"  - {tag} {rate}%: Σ line_total = {f2(s)}, net_by_vat_rate = {f2(n)}, diff {f2(s - n)}{extra}")
    # real cross-check against the printed net total
    nt, sn = D(t["net_total"]), sum((D(v) for v in t["net_by_vat_rate"].values()), Decimal(0))
    good = abs(nt - sn) <= TOL
    ok &= good
    out.append(f"  - {'PASS' if good else '**FAIL**'} net_total {f2(nt)} vs Σ net_by_vat_rate {f2(sn)}, diff {f2(nt - sn)}"
               + (" (net_total is printed on this layout → this is the real check)" if derived else ""))
    return ok, out


def check_v3(doc) -> tuple[bool, list[str]]:
    t, ok, out = doc["totals"], True, []
    for rate, net in t["net_by_vat_rate"].items():
        exp, vat = D(net) * int(rate) / 100, D(t["vat_by_vat_rate"].get(rate, 0))
        good = abs(exp - vat) <= TOL
        ok &= good
        out.append(f"  - {'PASS' if good else '**FAIL**'} {rate}%: {f2(D(net))} × {rate}% = {f2(exp)}, printed {f2(vat)}, diff {f2(vat - exp)}")
    return ok, out


def check_v4(doc) -> tuple[bool, list[str]]:
    t = doc["totals"]
    s = D(t["net_total"]) + D(t["vat_total"])
    diff = D(t["gross_total"]) - s
    good = abs(diff) <= TOL
    return good, [f"  - net {f2(D(t['net_total']))} + vat {f2(D(t['vat_total']))} = {f2(s)}, gross printed {f2(D(t['gross_total']))}, diff {f2(diff)}"]


def check_v5(doc) -> tuple[bool, list[str]]:
    v = doc["supplier"]["vat_id"] or ""
    return bool(re.fullmatch(r"DE\d{9}", v)), [f"  - supplier.vat_id = `{v}`"]


def check_v6(doc) -> tuple[bool, list[str]]:
    ok, out, parsed = True, [], {}
    for k in ("document_date", "service_date", "due_date"):
        v = doc.get(k)
        if v is None:
            out.append(f"  - {k}: not printed (null)")
            continue
        try:
            parsed[k] = date.fromisoformat(v)
            out.append(f"  - {k}: {v} valid")
        except ValueError:
            ok = False
            out.append(f"  - **FAIL** {k}: {v!r} is not a valid date")
    cd = doc.get("cash_discount")
    if cd:
        try:
            date.fromisoformat(cd["until"])
        except ValueError:
            ok = False
            out.append(f"  - **FAIL** cash_discount.until {cd['until']!r} invalid")
    if "due_date" in parsed and "document_date" in parsed:
        good = parsed["due_date"] >= parsed["document_date"]
        ok &= good
        out.append(f"  - {'PASS' if good else '**FAIL**'} due_date {parsed['due_date']} not before document_date {parsed['document_date']}")
    return ok, out


def check_v7(doc) -> tuple[bool, list[str]]:
    credit = doc["document_type"] == "credit_note"
    vals = {f"line {i['position']} line_total": D(i["line_total"]) for i in doc["line_items"]}
    t = doc["totals"]
    vals.update({f"net[{k}]": D(v) for k, v in t["net_by_vat_rate"].items()})
    vals.update({f"vat[{k}]": D(v) for k, v in t["vat_by_vat_rate"].items()})
    vals.update({k: D(t[k]) for k in ("net_total", "vat_total", "gross_total")})
    bad = [k for k, v in vals.items() if (v >= 0 if credit else v <= 0)]
    out = [f"  - {'credit note → all amounts must be negative' if credit else 'invoice → all amounts must be positive'}; "
           f"{len(vals)} amounts checked"]
    out += [f"  - **FAIL** {k} = {f2(vals[k])}" for k in bad]
    return not bad, out


def check_v8(notes) -> tuple[bool | None, list[str]]:
    cos = notes.get("carry_overs") or []
    if not cos:
        return None, []
    ok, out = True, []
    for c in cos:
        diff = D(c["printed"]) - D(c["sum_of_lines_so_far"])
        good = abs(diff) <= TOL
        ok &= good
        out.append(f"  - {'PASS' if good else '**FAIL**'} 'Übertrag' after page {c['page']}: printed {f2(D(c['printed']))}, "
                   f"Σ lines so far {f2(D(c['sum_of_lines_so_far']))}, diff {f2(diff)}")
    return ok, out


def validate_doc(path: Path) -> tuple[list[str], dict[str, bool | None]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    npath = path.with_suffix(".notes.json")
    notes = json.loads(npath.read_text(encoding="utf-8")) if npath.exists() else {}
    checks = [
        ("V1", "quantity × unit_price ÷ price_unit × (1 − discount) = line_total (±0.01)", check_v1(doc)),
        ("V2", "Σ line_total per VAT rate = net_by_vat_rate; Σ net = net_total (±0.01)", check_v2(doc, notes)),
        ("V3", "net × rate = vat per VAT rate (±0.01)", check_v3(doc)),
        ("V4", "net_total + vat_total = gross_total (±0.01)", check_v4(doc)),
        ("V5", "supplier VAT ID = DE + 9 digits", check_v5(doc)),
        ("V6", "dates valid; due_date ≥ document_date", check_v6(doc)),
        ("V7", "sign convention (credit note negative / invoice positive)", check_v7(doc)),
    ]
    v8 = check_v8(notes)
    if v8[0] is not None:
        checks.append(("V8", "page carry-over ('Übertrag') equals running sum of lines (informational extra)", v8))
    lines = [f"## {path.stem}  —  {doc['document_type']} {doc['document_number']}", ""]
    if notes.get("ocr"):
        lines += ["> ⚠ **OCR document – read the checks below with care.** Net/VAT/gross were derived from the OCR'd line items "
                  "(the noisy printed totals only corroborate them), so V1–V4 are largely *circular*: a PASS shows internal "
                  "consistency, not that the digits match the paper. Per-field trust levels are in the `.notes.json` sidecar "
                  "and the README.", ""]
    if any("net_by_vat_rate" in d for d in notes.get("derived_fields", [])):
        lines += ["> ℹ This layout does not print a net amount per VAT rate; `net_by_vat_rate` is summed from the lines. "
                  "The independent check is net_total (printed) vs Σ lines in V2.", ""]
    status = {}
    for cid, rule, (ok, detail) in checks:
        status[cid] = ok
        lines.append(f"- **{cid} {'PASS' if ok else 'FAIL'}** — {rule}")
        lines += detail
    lines.append("")
    return lines, status


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    out_dir = Path(argv[1])
    files = sorted(p for p in out_dir.glob("*.json") if not p.name.endswith(".notes.json"))
    if not files:
        print(f"no JSON files in {out_dir}", file=sys.stderr)
        return 1
    body, summary = [], []
    for f in files:
        lines, status = validate_doc(f)
        body += lines
        summary.append((f.stem, status))
    cols = ["V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8"]
    head = ["# Validation report", "",
            "Generated by `python validate.py outputs/`. Tolerance ±0.01. "
            "A FAIL that comes from the document itself is a finding about the document, not a bug in the extractor.", "",
            "| Document | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for stem, st in summary:
        head.append(f"| {stem} | " + " | ".join("–" if st.get(c) is None else ("PASS" if st[c] else "**FAIL**") for c in cols) + " |")
    head.append("")
    (out_dir / "validation_report.md").write_text("\n".join(head + body) + "\n", encoding="utf-8")
    print(f"wrote {out_dir / 'validation_report.md'}")
    fails = sum(1 for _, st in summary for v in st.values() if v is False)
    print(f"{len(files)} documents, {fails} failed checks")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
