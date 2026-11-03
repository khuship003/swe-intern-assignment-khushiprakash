"""Kessler-specific pieces that are the same in every layout (footer, VAT id, Skonto, output assembly).
Layout-specific code is in kessler_legacy.py and kessler_v2.py."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from .common import (ExtractionError, Page, add_days, iso, money, num, parse_amount,
                     parse_date)

AMT = r"-?\d{1,3}(?:\.\d{3})*,\d{2}-?"          # German amount, minus before or after
ARTICLE = r"[A-Z]{2}-\d{5}"                      # KI-40112, BU-90034, DL-00001


@dataclass
class RawItem:
    position: int
    article_number: str
    description: str
    quantity: Decimal
    unit: str
    unit_price: Decimal
    price_unit: Decimal
    discount_percent: Decimal | None
    line_total: Decimal
    vat_rate: int
    extra_lines: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        desc = " ".join([self.description, *self.extra_lines]).strip()
        desc = re.sub(r"\s+", " ", desc)
        return {
            "position": self.position,
            "article_number": self.article_number,
            "description": desc,
            "quantity": num(self.quantity),
            "unit": self.unit,
            "unit_price": num(self.unit_price),
            "price_unit": num(self.price_unit),
            "discount_percent": num(self.discount_percent) if self.discount_percent is not None else None,
            "line_total": money(self.line_total),
            "vat_rate": self.vat_rate,
        }


@dataclass
class Parsed:
    """Everything a layout parser returns. `notes` goes to the sidecar file, never the main JSON."""
    header: dict
    items: list[RawItem]
    net_by_rate: dict[int, Decimal] | None   # None -> not printed, derive from items
    vat_by_rate: dict[int, Decimal]
    net_total: Decimal | None                # None -> not printed, derive from net_by_rate
    gross_total: Decimal
    cash_discount: dict | None
    notes: dict = field(default_factory=dict)


# ------------------------------------------------------------ footer (same in all layouts)
def parse_footer(pages: list[Page]) -> dict:
    """Supplier name + VAT id come from the page footer (bottom 20% of the page), which is identical in
    both layouts. Reading only the footer avoids picking up the header text that the new layout puts on
    the same row as the sender line."""
    foot_lines = []
    for p in pages:
        foot_lines += [l["text"] for l in p.lines(top=p.p.height * 0.8)]
    text = "\n".join(foot_lines)
    m = re.search(r"USt-IdNr\.?\s*:?\s*(DE[\s\d]{9,12})", text)
    vat = re.sub(r"\s+", "", m.group(1)) if m else None
    m2 = re.search(r"^(.+?)\s·\s.+?\s·\s\d{5}\s", text, re.M)   # 'Name · Street · PLZ City'
    name = m2.group(1).strip() if m2 else None
    return {"name": name, "vat_id": vat}


# ------------------------------------------------------------ Skonto
def parse_cash_discount(text: str, document_date) -> dict | None:
    """Two printed forms:
       'Bei Zahlung bis 16.10.2026 abzüglich 2 % Skonto (= 19,08 EUR).'
       'Bei Zahlung innerhalb von 7 Tagen ab Rechnungsdatum 3 % Skonto (= 188,35 EUR).'
    The second form has no date -> until = document_date + N days (derived, noted)."""
    t = re.sub(r"\s+", " ", text)
    m = re.search(r"Bei Zahlung bis (\d{2}\.\d{2}\.\d{4}) abzüglich (\d+(?:,\d+)?) ?% Skonto "
                  r"\(= ?(" + AMT + r") EUR\)", t)
    if m:
        return {"percent": float(parse_amount(m.group(2))), "until": iso(parse_date(m.group(1))),
                "amount": money(parse_amount(m.group(3)))}
    m = re.search(r"Bei Zahlung innerhalb von (\d+) Tagen ab Rechnungsdatum (\d+(?:,\d+)?) ?% Skonto "
                  r"\(= ?(" + AMT + r") EUR\)", t)
    if m:
        return {"percent": float(parse_amount(m.group(2))),
                "until": iso(add_days(document_date, int(m.group(1)))),
                "amount": money(parse_amount(m.group(3))), "_derived_until": True}
    return None


# ------------------------------------------------------------ output assembly
def assemble(p: Parsed) -> tuple[dict, dict]:
    """Turn a Parsed into (schema-conform output dict, sidecar notes dict)."""
    items = [i.to_json() for i in p.items]
    notes = dict(p.notes)
    derived = notes.setdefault("derived_fields", [])

    net_by_rate = p.net_by_rate
    if net_by_rate is None:
        net_by_rate = {}
        for i in p.items:
            net_by_rate[i.vat_rate] = net_by_rate.get(i.vat_rate, Decimal(0)) + i.line_total
        derived.append("totals.net_by_vat_rate (not printed; summed from line items)")
    net_total = p.net_total
    if net_total is None:
        net_total = sum(net_by_rate.values(), Decimal(0))
        derived.append("totals.net_total (not printed; sum of printed per-rate nets)")
    vat_total = sum(p.vat_by_rate.values(), Decimal(0))
    derived.append("totals.vat_total (not printed; sum of printed per-rate VAT)")

    cd = p.cash_discount
    if cd and cd.pop("_derived_until", False):
        derived.append("cash_discount.until (printed as 'N Tage ab Rechnungsdatum'; = document_date + N)")

    out = dict(p.header)
    out["line_items"] = items
    out["totals"] = {
        "net_by_vat_rate": {str(k): money(v) for k, v in sorted(net_by_rate.items(), reverse=True)},
        "vat_by_vat_rate": {str(k): money(v) for k, v in sorted(p.vat_by_rate.items(), reverse=True)},
        "net_total": money(net_total),
        "vat_total": money(vat_total),
        "gross_total": money(p.gross_total),
    }
    out["cash_discount"] = cd
    return out, notes
