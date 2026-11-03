# AI usage

## Tools and what I used them for
* **Claude (Anthropic)** – used heavily: reading the PDFs' structure, drafting `extract.py`, the layout parsers,
  `validate.py`, the OCR pipeline, the tests, and the first draft of the documentation. I reviewed the output
  against the PDFs and the expected file, and I am able to explain every module (see MARKING_NOTES.md for the design).
* No other generative tools. `pdfplumber` (text + word coordinates), `tesseract` (OCR) and standard libraries did the actual work.

## Two concrete examples where the AI's answer was wrong or incomplete
1. **Footer regex took the wrong line (INV-05).** The suggested regex for the supplier name searched the whole page text.
   On the new layout it matched `Rechnungsdatum 19. Oktober 2026 Industrietechnik GmbH` because two items share a text row.
   Found by printing all five outputs next to each other (V5 only checks the VAT id and could not see it); fixed by reading
   only the footer region (FAILURE_LOG #2).
2. **"Reconcile OCR with arithmetic" looked like a great idea and produced wrong numbers.** The suggested decoder chose values
   that make quantity × price = total; with a 110 dpi scan where `1` looks like `4` it returned a consistent but wrong line 3 and
   line 4, and the validator happily passed. Found by comparing with the page image; fixed by trust labels, a validation-report
   warning and an honest "wrong / do not use" table (FAILURE_LOG #6).

Smaller things: a first sketch split rows by whitespace and would have broken on `14,60100` (#1); a whitelisted per-cell OCR
returned blanks (#4).
