# Fingerprint notes – Kessler Industrietechnik GmbH

Two text layouts exist. **L** = legacy (RE-…, GS-…; header values in the *right* column x ≥ 330 pt, buyer block *left*),
**N** = new layout (2026-R-…; header values *left* x < 330 pt, buyer block *right*). Regions are cut with word
coordinates (pdfplumber), so merged lines such as `Kessler … Ostfildern Rechnungs-Nr. RE-…` never matter.

| Field | How I locate it | Why robust | What would break it |
|---|---|---|---|
| `document_type` | L: label of the number line `Gutschrift-Nr.` → credit_note, else invoice. N: `Gutschrift Nr.` (assumed, no sample) vs `Rechnung Nr.` | The label is printed by the system, not free text | A new document kind (e.g. "Korrekturrechnung", "Storno") would be called an invoice |
| `document_number` | L: value after `Rechnungs-Nr.` / `Gutschrift-Nr.`. N: after `Rechnung Nr.`. Taken verbatim | Anchored on the label, not on the number format → the new `2026-R-00117` format needed no regex change | Label renamed; number containing spaces |
| `document_date` | L: line starting `Datum` (anchored with `^`, so `Leistungsdatum` cannot match). N: `Rechnungsdatum` with `19. Oktober 2026` | German month names handled in one parser | Another date style (`2026-10-19`) → `ValueError`, loud |
| `service_date` | L: `Leistungsdatum`. N: `Lieferdatum`. `null` if absent (credit note). `Liefertermin KW 42/2026` is deliberately ignored – it is a promised week, not a service date | Explicit labels only | A new label for the same thing |
| `due_date` | L: `Zahlbar bis <date> ohne Abzug`. N: `Zahlungsziel: … (bis 18. November 2026)`. Only the printed date is used, never computed. `null` when not printed (credit note) | Schema says null if not printed | Rephrased payment sentence → null (silently!) – V6 would still pass, so a missing due date on an invoice is worth a warning |
| `currency` | Constant `EUR` | Every amount is `€`/`EUR` in all samples | Another currency would be wrongly labelled EUR; I did not add a check |
| `supplier.name`, `vat_id` | Footer (bottom 20 % of each page): `Name · Street · PLZ City` and `USt-IdNr. DE… `; spaces removed | Footer is identical in both layouts and sits in the same place; reading only the footer avoids header text that shares a row with the sender line | Footer moved; VAT id with spaces in unusual places (handled up to 12 chars) |
| `buyer.name/street/postal_code/city` | Address block (L: left column between the grey sender line and the `RECHNUNG` heading; N: right column below the sender line). **name = first line**, **PLZ/city = the `\d{5} City` line, street = the line directly above it** | Department / "Abt." / "z. Hd." lines are never first and never directly above the PLZ line, so they drop out by construction; `DEUTSCHLAND` below the city is ignored | Address without a street line; a name that wraps over two lines |
| `buyer.customer_number` | L: `Kunden-Nr.`; N: `Kundennummer` | Label | – |
| `order_reference` | L: digits after `Ihre Bestellung` (so `vom 22.09.2026` is not part of it); N: `Bestellnummer` | Digits-only capture | Alphanumeric order numbers |
| `delivery_note_number` | L: `Lieferschein-Nr.`; N: `Lieferschein` | Label | – |
| `referenced_invoice` | `Bezug Rechnung RE-2026-10133 v. 15.09.2026` → only the `RE-yyyy-nnnnn` part; `null` otherwise | Pattern of the credited invoice number | Different referencing sentence |
| `line_items[].position` | First token of the row; `001` → 1 (int). INV-02 uses 10, 20, 30… and that is kept as printed | – | – |
| `article_number` | `[A-Z]{2}-\d{5}` (KI-, BU-, DL-) right after position (L) / before the description (N) | The article pattern also marks "this row is an item" | A new article prefix style |
| `description` | Text between article and quantity/price **plus** every following non-item line (Charge, Zuschnitt, `lt. Ihrer Anfrage…`, `Ersatz für Pos. 12…`, `Preis je Stück…`) joined with single spaces. Lazy regex + anchored end, so `10,5 St verz.` inside "Scheibe DIN 125 A 10,5 St verz." is not mistaken for quantity/unit | Rows are matched from the *right-hand* numbers, which have a fixed shape | A note line that starts with `<digits> KI-12345` |
| `quantity`, `unit` | L: number + unit before the price (`1.000` thousands separator, `12,5`, `37,5`). N: left columns `Menge`, `Einh.` | German number parser | Quantity with 3 decimals |
| `unit_price`, `price_unit` | L: price (always 2 decimals) immediately followed by PE, which can be **glued** (`14,60100` → 14,60 / 100). N: `Preis/Einh.` incl. 3 decimals (`0,152 €`); no PE printed → 1 | Regex knows price = exactly 2 decimals, then `1\|10\|100\|1000` | A 3-decimal price in L |
| `discount_percent`, `line_total` | L: `Rab. %` column (optional) and `Gesamt`. N: a row `abzgl. … 3 % auf 465,60 € -13,97 €` is merged into the previous item: percent=3, `line_total` = 465,60 + (−13,97) = 451,63; the row never appears as an item. Credit note: trailing minus `225,80-` → −225,80 | The discount row has a unique shape (`abzgl.` … `% auf`) | A discount row with a different wording would become a description line (V1 would flag the mismatch) |
| `vat_rate` | Last column (`19`/`7`, N: `19%`) | – | – |
| rows that are **not** items | `Übertrag …`, `Übertrag von Seite …`, repeated column header, footer, `Seite x von y` are skipped. Carry-over amounts are recorded and re-checked (V8) | Table region = from the header row to `Übertrag`/`Nettobetrag`/`Summe netto`/footer, per page | New page furniture is treated as a description of the last item – V1/V8 would not catch it, review needed |
| `totals.net_by_vat_rate`, `vat_by_vat_rate` | L: `Nettobetrag 19 % USt …` and `zzgl. 19 % USt auf … <vat>`. N: only `MwSt 19% …` (VAT); the net per rate is **not printed** → summed from line items and flagged in the sidecar | Values as printed whenever they are printed | – |
| `net_total`, `vat_total` | L: not printed → sum of the printed per-rate values (flagged). N: `Summe netto` printed; vat_total = sum of printed VAT | Never recalculated from VAT rates | – |
| `gross_total` | L: `Rechnungsbetrag … EUR` / `Gutschriftsbetrag`. N: `Gesamtbetrag` | Label | – |
| `cash_discount` | L: `Bei Zahlung bis <date> abzüglich 2 % Skonto (= 19,08 EUR)` or `innerhalb von 7 Tagen ab Rechnungsdatum 3 % Skonto (= …)` (then `until` = document_date + 7, flagged as derived). N: `Kein Skontoabzug` → `null` | Informational only; never subtracted | New Skonto wording |

## INV-05 – same supplier, new layout (≤ 200 words)
The new layout changed column order and meaning, the position of header and buyer blocks, discount notation and
the totals block. I rewrote only what differs: the item regex (position, quantity, unit first; € signs; zero-padded
positions; no PE column, so `price_unit` is 1), a merge of the `abzgl. … %` row into the previous item, header
labels and column (left; buyer block moved right), long-form dates, and totals (no net per VAT rate, so it is summed
from the lines and flagged as derived). The legacy parser was not touched. Each layout is its own module with
`matches()` and `parse()`; `extract.py` picks one from the table-header text. Shared logic (number and date parsing,
footer, Skonto, output assembly) lives in modules without layout assumptions. INV-01 is compared with the expected
file after every change, so a regression in the old layout shows immediately.
