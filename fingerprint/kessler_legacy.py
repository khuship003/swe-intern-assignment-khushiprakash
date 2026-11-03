"""Kessler LEGACY layout (invoices RE-2026-xxxxx and credit notes GS-2026-xxxxx).

Recognised by the table header 'Pos. Art.-Nr. Bezeichnung Menge ME Einzelpreis € PE Rab. % Gesamt € USt %'.
Header block: label/value pairs in the right column (x >= 330); buyer address block in the left column.
Items: one text row each, optional free-text rows beneath, 'Übertrag' rows at page breaks.
"""
from __future__ import annotations

import re
from decimal import Decimal

from .common import ExtractionError, Page, parse_amount, parse_date, iso
from .kessler_common import (AMT, ARTICLE, Parsed, RawItem, parse_cash_discount, parse_footer)

RIGHT_X = 330          # x where the right-hand label/value column starts
HEADER_RE = re.compile(r"^Pos\.\s+Art\.-Nr\.\s+Bezeichnung\s+Menge\s+ME\s+Einzelpreis")

# One item row. Note `price\s*pe`: in the PDF the price and PE columns can touch ("14,60100").
# The price always has exactly 2 decimals, so the digits after it are the PE (1/10/100/1000).
ITEM_RE = re.compile(
    r"^(?P<pos>\d+)\s+(?P<art>" + ARTICLE + r")\s+(?P<desc>.+?)\s+"
    r"(?P<qty>\d{1,3}(?:\.\d{3})*(?:,\d+)?|\d+)\s+(?P<unit>[A-Za-z]+)\s+"
    r"(?P<price>\d{1,3}(?:\.\d{3})*,\d{2})\s*(?P<pe>1000|100|10|1)\s+"
    r"(?:(?P<disc>\d+(?:,\d+)?)\s+)?"
    r"(?P<total>" + AMT + r")\s+(?P<vat>\d{1,2})$"
)


def matches(pages: list[Page]) -> bool:
    return any(HEADER_RE.match(l["text"]) for p in pages for l in p.all_lines())


def _meta(page1: Page) -> dict:
    """Label/value pairs of the right-hand header column."""
    heading_top = next((l["top"] for l in page1.all_lines() if l["text"] in ("RECHNUNG", "GUTSCHRIFT")), 400)
    lines = [l for l in page1.lines(x0=RIGHT_X, top=100, bottom=heading_top) ]
    meta: dict[str, str] = {}
    for l in lines:
        t = l["text"]
        for key, rx in [
            ("doc_no", r"^(?:Rechnungs|Gutschrift)-Nr\.\s+(\S+)"),
            ("date", r"^Datum\s+(\S+)"),
            ("customer", r"^Kunden-Nr\.\s+(\S+)"),
            ("order", r"^Ihre Bestellung\s+(\d+)"),
            ("delivery_note", r"^Lieferschein-Nr\.\s+(\S+)"),
            ("service_date", r"^Leistungsdatum\s+(\S+)"),
            ("ref_invoice", r"^Bezug Rechnung\s+(\S+)"),
        ]:
            m = re.match(rx, t)
            if m:
                meta[key] = m.group(1)
        if re.match(r"^Gutschrift-Nr\.", t):
            meta["is_credit"] = "1"
    return meta


def _buyer(page1: Page) -> dict:
    lines_all = page1.all_lines()
    sender = next((l for l in lines_all if l["text"].startswith("Kessler Industrietechnik GmbH ·")), None)
    heading = next((l for l in lines_all if l["text"] in ("RECHNUNG", "GUTSCHRIFT")), None)
    if not sender or not heading:
        raise ExtractionError("legacy layout: sender line or heading not found")
    block = [l["text"] for l in page1.lines(x0=0, x1=RIGHT_X, top=sender["top"] + 5, bottom=heading["top"])]
    ci = next((i for i, t in enumerate(block) if re.match(r"^\d{5}\s+\S", t)), None)
    if ci is None or ci < 2:
        raise ExtractionError(f"legacy layout: cannot find 'PLZ Ort' line in buyer block {block}")
    plz, city = re.match(r"^(\d{5})\s+(.+)$", block[ci]).groups()
    return {"name": block[0], "street": block[ci - 1], "postal_code": plz, "city": city}


def _parse_items(pages: list[Page]) -> tuple[list[RawItem], list[dict]]:
    items: list[RawItem] = []
    carry_overs: list[dict] = []
    for page in pages:
        in_table = False
        for l in page.all_lines():
            t = l["text"]
            if HEADER_RE.match(t):
                in_table = True
                continue
            if not in_table:
                continue
            if re.match(r"^(Nettobetrag|Kessler Industrietechnik GmbH ·|Volksbank|USt-IdNr)", t):
                break                                           # totals / footer
            if t.startswith("Übertrag von"):                     # carry-over header on a new page
                continue
            m = re.match(r"^Übertrag\s+(" + AMT + r")$", t)
            if m:                                               # carry-over at bottom of page
                carry_overs.append({"page": page.number, "printed": float(parse_amount(m.group(1))),
                                    "sum_of_lines_so_far": float(sum((i.line_total for i in items), Decimal(0)))})
                break
            m = ITEM_RE.match(t)
            if m:
                g = m.groupdict()
                items.append(RawItem(
                    position=int(g["pos"]), article_number=g["art"], description=g["desc"],
                    quantity=parse_amount(g["qty"]), unit=g["unit"], unit_price=parse_amount(g["price"]),
                    price_unit=Decimal(g["pe"]),
                    discount_percent=parse_amount(g["disc"]) if g["disc"] else None,
                    line_total=parse_amount(g["total"]), vat_rate=int(g["vat"])))
            elif re.match(r"^\d+\s+" + ARTICLE, t):
                raise ExtractionError(f"item row not understood (page {page.number}): {t!r}")
            elif items:                                         # free text under an item
                items[-1].extra_lines.append(t)
    return items, carry_overs


def parse(pages: list[Page]) -> Parsed:
    p1 = pages[0]
    meta = _meta(p1)
    for k in ("doc_no", "date", "customer"):
        if k not in meta:
            raise ExtractionError(f"legacy layout: header field {k!r} not found")
    credit = "is_credit" in meta
    doc_date = parse_date(meta["date"])

    items, carry_overs = _parse_items(pages)
    if not items:
        raise ExtractionError("no line items found")

    # ---- totals block (last page, from the first 'Nettobetrag' on)
    last_lines = [l["text"] for l in pages[-1].all_lines()]
    start = next(i for i, t in enumerate(last_lines) if t.startswith("Nettobetrag"))
    tail = last_lines[start:]
    net, vat, gross = {}, {}, None
    for t in tail:
        if (m := re.match(r"^Nettobetrag (\d+) ?% USt\s+(" + AMT + r")$", t)):
            net[int(m.group(1))] = parse_amount(m.group(2))
        elif (m := re.match(r"^zzgl\. (\d+) ?% USt auf " + AMT + r"\s+(" + AMT + r")$", t)):
            vat[int(m.group(1))] = parse_amount(m.group(2))
        elif (m := re.match(r"^(?:Rechnungsbetrag|Gutschriftsbetrag)\s+(" + AMT + r") EUR$", t)):
            gross = parse_amount(m.group(1))
    if not net or gross is None:
        raise ExtractionError("legacy layout: totals block not understood")

    due = re.search(r"Zahlbar bis (\d{2}\.\d{2}\.\d{4})", "\n".join(tail))
    footer = parse_footer(pages)
    ref = re.match(r"(RE-\d{4}-\d+)", meta["ref_invoice"]) if "ref_invoice" in meta else None

    header = {
        "document_type": "credit_note" if credit else "invoice",
        "document_number": meta["doc_no"],
        "document_date": iso(doc_date),
        "service_date": iso(parse_date(meta["service_date"])) if "service_date" in meta else None,
        "due_date": iso(parse_date(due.group(1))) if due else None,
        "currency": "EUR",
        "supplier": {"name": footer["name"], "vat_id": footer["vat_id"]},
        "buyer": {**_buyer(p1), "customer_number": meta["customer"]},
        "order_reference": meta.get("order"),
        "delivery_note_number": meta.get("delivery_note"),
        "referenced_invoice": ref.group(1) if ref else None,
    }

    return Parsed(header=header, items=items, net_by_rate=net, vat_by_rate=vat, net_total=None,
                  gross_total=gross, cash_discount=parse_cash_discount("\n".join(tail), doc_date),
                  notes={"layout": "legacy", "carry_overs": carry_overs})
