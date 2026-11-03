# Kessler Industrietechnik – invoice fingerprint

Turns every Kessler PDF (invoice / credit note) into JSON that follows `output_schema.json`, and proves that the
numbers add up – or shows exactly where they do not.

## Setup
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# Bonus (scanned PDF) only: install the tesseract binary, e.g.  sudo apt install tesseract-ocr   /   brew install tesseract
```
Put the assignment PDFs into `pdfs/` (they are included here).

## Run
```bash
for f in pdfs/INV-0*.pdf; do python extract.py "$f"; done      # -> outputs/<pdf-name>.json (+ .notes.json)
python validate.py outputs/                                    # -> outputs/validation_report.md
python tests/check_outputs.py                                  # INV-01 == expected, all outputs match the schema
```
`outputs/<name>.notes.json` is a sidecar (kept out of the schema-strict JSON): which fields were *derived* instead of
printed, page carry-over amounts, and for the scan the OCR trust table.

## Design
```
extract.py                      CLI + layout dispatch (text PDF -> layout parser by table header; image PDF -> OCR)
fingerprint/common.py           generic helpers only: German numbers/dates, PDF line/region access, JSON writer
fingerprint/kessler_common.py   Kessler parts shared by all layouts: footer (name, VAT id), Skonto, output assembly
fingerprint/kessler_legacy.py   legacy layout (RE-…, GS-…)       } each module = matches(pages) + parse(pages)
fingerprint/kessler_v2.py       new layout (2026-R-…)            } adding a layout never touches the others
fingerprint/kessler_scan.py     bonus: OCR of the scanned legacy layout
validate.py                     V1–V7 (+ V8 carry-over check) -> markdown report
```
* Money is handled as `Decimal` and only turned into floats at the very end (no float drift in the checks).
* Layout is detected from the **table header text**, not from the file name or the number format.
* Unknown row shapes **raise** instead of being skipped, so a changed layout fails loudly.

## What the validation found (see `outputs/validation_report.md`)
* INV-01, 02, 03, 05: every check passes (also the page carry-overs of INV-02/04).
* **INV-04 pos. 7 (KI-71004 Zahnriemen) is wrong on the document itself**: 26 × 48,02 = **1.248,52**, printed **1.284,52**
  (digits swapped, +36,00). The printed net for 19 % (5.213,22) and everything below it (VAT, gross, Skonto) use the
  *correct* 1.248,52, while the page-1 carry-over (4.096,22) adds up with the wrong 1.284,52. So V1 and V2 fail by exactly
  36,00 and V3/V4 pass: the line was mistyped, the totals are right. Extracted as printed, reported, not "fixed".
* INV-05 prints no net per VAT rate; it is summed from the lines (flagged in the report, real check = printed net total).

## Bonus: scanned invoice (INV-06) – partial, please read
A 110 dpi, 1-bit, noisy scan; tesseract has no German model installed here. The pipeline OCRs six preprocessed variants,
reads table cells by template position, votes, and picks readings that satisfy the arithmetic (all recorded in the notes).
**Agreement between variants is not correctness** – I compared the output with the rendered image by eye:

| Trust | Fields |
|---|---|
| Correct and reasonably trustworthy | document_number, document_date, service_date, due_date, customer_number, delivery_note_number; line 1 and 2 numbers (qty, unit price, PE, discount, total, VAT); article numbers of lines 2–4 |
| **Wrong / do not use** | order_reference (voted `4520093588`, print reads 4500093588), buyer name/street/city (typos, no German OCR), buyer postal code, line 1 article number and unit, descriptions (all garbled), line 3 unit price/total (4,85/14,55 instead of 3,85/11,55), line 4 (price/total/VAT rate), and therefore **all totals** (derived from the wrong lines) |
| Not read from the scan | supplier name and VAT ID (fingerprint constants – the footer is too small to OCR), `document_type`, `cash_discount` (null) |

Because the totals are derived from OCR'd lines, V1–V4 pass for INV-06 only circularly; the report says so.

## Known limitations
* Legacy price column: price always has exactly 2 decimals (the PE column can touch it, e.g. `14,60100`); a 3-decimal price
  in the legacy layout would need a different split. The new layout allows 2–4 decimals.
* Free-text lines under an item are attached by order; a free-text line that *looks* like an item row would raise.
* Only one VAT-rate layout of Skonto is known per sentence form (two forms are handled: fixed date / "N Tage ab Rechnungsdatum").
  A Skonto offer in the new layout is untested (no sample) – it currently returns `null`.
* Scan: see table above; the OCR path assumes the legacy template geometry and no free-text rows.
