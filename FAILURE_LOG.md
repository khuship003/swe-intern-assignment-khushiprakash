# Failure log

Each entry: what went wrong → how I noticed → what I changed → how I verified.
Most of the code was written by Claude. Entries below are things I reproduced or checked myself on my own computer.

## 1. Price and PE column are glued: `14,60100`
* **Wrong:** my first idea was to split each row by whitespace and read the last columns by index. On rows with PE 100
  the price and PE touch in the PDF (`14,60100`, `3,85100`), so the split gives one token instead of two.
* **Noticed:** dumping word coordinates of INV-02 before writing the parser: `14,60100` was a single word (x 432–471).
* **Changed:** parse rows with one anchored regex: price = exactly two decimals, then `\s*(1000|100|10|1)`, then an optional
  discount, then the total and the VAT rate. A comment in `kessler_legacy.py` explains why.
* **Verified:** INV-01 position 5 (`400 Stk 14,60100 58,40`) → unit_price 14.6, price_unit 100 and the whole file equals
  `INV-01_expected_output.json`; V1 passes for all 26 lines of INV-02.

## 2. INV-05: supplier name was `Rechnungsdatum 19. Oktober 2026 Industrietechnik GmbH`
* **Wrong:** `parse_footer` searched the whole page text for `^(.+?GmbH) · ` – in the new layout the logo text
  `Industrietechnik GmbH · seit 1987` sits on the same text row as `Rechnungsdatum …`, so the regex matched that merged line.
* **Noticed:** I printed all extracted outputs side by side before writing the validator; only INV-05 had a strange supplier name
  (V5 would not have caught it – it only checks the VAT id).
* **Changed:** read the supplier only from the footer region (bottom 20 % of the page) and require the shape
  `Name · Street · PLZ City`.
* **Verified:** re-ran all five PDFs; the name is `Kessler Industrietechnik GmbH` everywhere and INV-01 still equals the expected file.

## 3. Suspected an extraction bug in INV-04 – it was the document
* **Wrong:** V1 and V2 failed for INV-04 (pos. 7, difference 36,00). My first assumption was a parsing bug (thousands separator
  `1.284,52`?).
* **Noticed:** the raw text line reads `26 Stk 48,02 1 1.284,52 19`; 26 × 48,02 = 1.248,52. The printed net for 19 %
  (5.213,22) equals the sum of all lines *with* 1.248,52, while the page-1 carry-over (4.096,22) equals the sum *with* 1.284,52.
* **Changed:** nothing in the extractor (business rule: extract what is printed). Added check V8 (carry-over) because it
  helped to localise the error to a single line, and wrote the finding in the README.
* **Verified:** V3/V4 pass (totals consistent with 1.248,52), V1/V2 differ by exactly 36,00, V8 passes – only a digit swap in
  that one line explains all three at once.

## 4. OCR: whitelisted per-cell OCR returned blanks
* **Wrong:** for the scan I cropped every table cell and ran tesseract (`--psm 7`, digit whitelist) – the price, total and PE
  cells came back empty; qty once read `8` instead of `16`.
* **Noticed:** printing the raw candidates per cell (price `['', '', '', '0', '', '00']`). Saving a crop showed the text was
  there, so the problem was the method (no white margin, tiny crops), not the geometry – the header-word positions
  matched the template within 1 pt.
* **Changed:** OCR the whole table block once per image variant with `image_to_data` and assign words to (row, column)
  by their coordinates; treat price+PE as one cell because they touch.
* **Verified:** qty read 16/16 votes, totals such as `174 40` appeared in most variants.

## 5. OCR: re-rendering at 330 dpi was worse than upscaling the native pixels
* **Wrong:** I rendered the PDF page at 330 dpi (pdfium) and OCR'd that; the document number came out as `RE-2026-11465` and
  the order of magnitude of several amounts was off.
* **Noticed:** my very first experiment (Lanczos 3× upscale of the embedded 110 dpi image) had read `RE 2026 10466`
  correctly, so I compared the two pipelines on the same crop.
* **Changed:** render at the native 110 dpi, upscale 3× with Lanczos, then blur/threshold variants.
* **Verified:** document number, dates, customer and delivery note are now read correctly (compared with the page image).

## 6. OCR: "arithmetic reconciliation" produced confident wrong numbers
* **Wrong:** I let the decoder pick unit price/total so that qty × price ÷ PE × (1−disc) = total. For line 3 it chose
  4,85 / 14,55 (print: 3,85 / 11,55) because the scan renders `1` like `4`; line 4 got 3,45 instead of 34,90 and VAT 19 instead of 7.
  The arithmetic closes, so V1–V4 passed – on wrong data.
* **Noticed:** comparing the OCR output with the page image by eye; the net/gross did not match the printed 421,55 / 538,98.
* **Changed:** kept the method but (a) label every such reading `LOW` in the notes unless OCR agreement is strong,
  (b) put an OCR warning into the validation report because the checks are circular there, (c) wrote an explicit
  "wrong / do not use" table into the README.
* **Verified:** the README table matches a manual comparison with the image; the report shows the warning for INV-06.
  The fields are still wrong – I left them wrong and documented them instead of hard-coding the print.

## 7. Validator would have passed INV-05 V2 for the wrong reason
* **Wrong:** the new layout has no net per VAT rate; I sum it from the lines, so "Σ lines = net_by_vat_rate" is true by
  construction.
* **Noticed:** while reviewing the report text for INV-05 – a PASS that can never fail is not a check.
* **Changed:** extractor writes `derived_fields` into a sidecar; V2 labels those comparisons as circular and adds the real
  comparison (printed `Summe netto` vs Σ nets).
* **Verified:** the INV-05 section of `validation_report.md` shows both lines, with the explanation.
