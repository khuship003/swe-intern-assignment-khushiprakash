"""Kessler NEW layout (introduced Oct 2026, numbers like 2026-R-00117).

Differences from the legacy layout (this is everything that had to change):
  * header is a label/value list in the LEFT column, buyer address block moved to the RIGHT (x >= 330)
  * column order is Pos | Menge | Einh. | Artikel | Beschreibung | Preis/Einh. | MwSt | Betrag (with € signs)
  * no PE and no Rab.% column -> price_unit is 1; a discount is its own row 'abzgl. ... % auf X €  -Y €'
  * positions are zero-padded ('001'), dates are '19. Oktober 2026'
  * totals: only 'Summe netto', 'MwSt 19%', 'Gesamtbetrag' (no net per VAT rate)
  * payment: 'Zahlungsziel: 30 Tage netto (bis 18. November 2026). Kein Skontoabzug.'
"""
from __future__ import annotations

import re
from decimal import Decimal

from .common import ExtractionError, Page, iso, parse_amount, parse_date
from .kessler_common import AMT, ARTICLE, Parsed, RawItem, parse_footer

SPLIT_X = 330
HEADER_RE = re.compile(r"^Pos\s+Menge\s+Einh\.\s+Artikel\s+Beschreibung\s+Preis/Einh\.")

ITEM_RE = re.compile(
    r"^(?P<pos>\d{3})\s+(?P<qty>\d{1,3}(?:\.\d{3})*(?:,\d+)?|\d+)\s+(?P<unit>[A-Za-z]+)\s+"
    r"(?P<art>" + ARTICLE + r")\s+(?P<desc>.+?)\s+"
    r"(?P<price>\d{1,3}(?:\.\d{3})*,\d{2,4})\s*€\s+(?P<vat>\d{1,2})\s?%\s+(?P<total>" + AMT + r")\s*€$"
)
DISCOUNT_RE = re.compile(
    r"^abzgl\.\s+.+?\s+(?P<pct>\d+(?:,\d+)?)\s?%\s+auf\s+(?P<base>" + AMT + r")\s*€\s+(?P<amt>" + AMT + r")\s*€$"
)


def matches(pages: list[Page]) -> bool:
    return any(HEADER_RE.match(l["text"]) for p in pages for l in p.all_lines())


def _meta(page1: Page) -> dict:
    head_top = next((l["top"] for l in page1.all_lines() if HEADER_RE.match(l["text"])), 400)
    meta = {}
    for l in page1.lines(x0=0, x1=SPLIT_X, top=0, bottom=head_top):
        t = l["text"]
        for key, rx in [
            ("doc_no", r"^(?:Rechnung|Gutschrift) Nr\.\s+(\S+)"),
            ("date", r"^(?:Rechnungsdatum|Gutschriftdatum|Datum)\s+(.+)$"),
            ("service_date", r"^(?:Lieferdatum|Leistungsdatum)\s+(.+)$"),
            ("customer", r"^Kundennummer\s+(\S+)"),
            ("order", r"^Bestellnummer\s+(\d+)"),
            ("delivery_note", r"^Lieferschein\s+(\S+)"),
            ("ref_invoice", r"^Bezug Rechnung\s+(\S+)"),
        ]:
            m = re.match(rx, t)
            if m:
                meta[key] = m.group(1).strip()
        if t.startswith("Gutschrift Nr."):
            meta["is_credit"] = "1"
    return meta


def _buyer(page1: Page) -> dict:
    head_top = next((l["top"] for l in page1.all_lines() if HEADER_RE.match(l["text"])), 400)
    sender = next((l for l in page1.lines(x0=SPLIT_X - 5, top=0, bottom=head_top)
                   if "Gewerbestraße 14" in l["text"]), None)
    start = sender["top"] + 5 if sender else 0
    block = [l["text"] for l in page1.lines(x0=SPLIT_X, top=start, bottom=head_top - 20)
             if "Gewerbestraße 14" not in l["text"]]
    ci = next((i for i, t in enumerate(block) if re.match(r"^\d{5}\s+\S", t)), None)
    if ci is None or ci < 2:
        raise ExtractionError(f"v2 layout: cannot find 'PLZ Ort' in buyer block {block}")
    plz, city = re.match(r"^(\d{5})\s+(.+)$", block[ci]).groups()
    return {"name": block[0], "street": block[ci - 1], "postal_code": plz, "city": city}


def parse(pages: list[Page]) -> Parsed:
    p1 = pages[0]
    meta = _meta(p1)
    for k in ("doc_no", "date", "customer"):
        if k not in meta:
            raise ExtractionError(f"v2 layout: header field {k!r} not found")
    credit = "is_credit" in meta
    doc_date = parse_date(meta["date"])

    items: list[RawItem] = []
    tail: list[str] = []
    for page in pages:
        in_table = False
        for l in page.all_lines():
            t = l["text"]
            if HEADER_RE.match(t):
                in_table = True
                continue
            if not in_table:
                continue
            if tail or t.startswith("Summe netto"):
                if re.match(r"^(Kessler Industrietechnik GmbH ·|Volksbank|USt-IdNr)", t):
                    break
                tail.append(t)
                continue
            if (m := ITEM_RE.match(t)):
                g = m.groupdict()
                items.append(RawItem(
                    position=int(g["pos"]), article_number=g["art"], description=g["desc"],
                    quantity=parse_amount(g["qty"]), unit=g["unit"], unit_price=parse_amount(g["price"]),
                    price_unit=Decimal(1), discount_percent=None,
                    line_total=parse_amount(g["total"]), vat_rate=int(g["vat"])))
            elif (m := DISCOUNT_RE.match(t)):
                if not items:
                    raise ExtractionError("discount row before any item")
                it = items[-1]
                it.discount_percent = parse_amount(m.group("pct"))
                it.line_total = parse_amount(m.group("base")) + parse_amount(m.group("amt"))  # base + (-discount)
            elif re.match(r"^\d{3}\s", t):
                raise ExtractionError(f"item row not understood (page {page.number}): {t!r}")
            elif items:
                items[-1].extra_lines.append(t)
    if not items:
        raise ExtractionError("no line items found")

    vat, net_total, gross = {}, None, None
    for t in tail:
        if (m := re.match(r"^Summe netto\s+(" + AMT + r")\s*€$", t)):
            net_total = parse_amount(m.group(1))
        elif (m := re.match(r"^MwSt (\d+) ?%\s+(" + AMT + r")\s*€$", t)):
            vat[int(m.group(1))] = parse_amount(m.group(2))
        elif (m := re.match(r"^Gesamtbetrag\s+(" + AMT + r")\s*€$", t)):
            gross = parse_amount(m.group(1))
    if net_total is None or gross is None or not vat:
        raise ExtractionError("v2 layout: totals block not understood")

    tail_text = " ".join(tail)
    due = re.search(r"Zahlungsziel:.*?\(bis (\d{1,2}\.\s*[A-Za-zäöü]+\s+\d{4}|\d{2}\.\d{2}\.\d{4})\)", tail_text)
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
    # 'Kein Skontoabzug' -> null. (No v2 sample shows a Skonto offer, so none is parsed here yet.)
    return Parsed(header=header, items=items, net_by_rate=None, vat_by_rate=vat, net_total=net_total,
                  gross_total=gross, cash_discount=None, notes={"layout": "v2"})
