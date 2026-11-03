# AI usage

## Tools and what I used them for
* **Claude (Anthropic)** – used heavily: reading the PDFs' structure, drafting `extract.py`, the layout parsers,
  `validate.py`, the OCR pipeline, the tests, and the first draft of the documentation. I reviewed the output
  against the PDFs and the expected file, Chatgpt, Gemini and copilot. `pdfplumber` (text + word coordinates), `tesseract` (OCR) and standard libraries did the actual work.

## Two concrete examples where the AI's answer was wrong or incomplete
1. 1. **Linux command on a Windows computer.** The README and the AI's first instructions used the Linux loop
   `for f in pdfs/INV-0*.pdf; do python extract.py "$f"; done`. My terminal is Windows PowerShell, so I ran it
   and got: <<paste your real error message>>. I used the PowerShell version instead:
   `Get-ChildItem pdfs\INV-0[1-5]*.pdf | ForEach-Object { python extract.py $_.FullName }`, which printed
   OK for INV-01 to INV-05. The README still shows the Linux command, so I added the PowerShell version to it.
2. 2. **OCR numbers that add up but are wrong.** The AI's code for the scanned invoice (INV-06) picks values so that
   quantity × price = total. I opened the PDF and looked at line 3: the unit price is printed as 3,85, but the JSON
   has 4,85. The scan renders a `1` so that it looks like a `4`. Because the arithmetic still works,
   validate.py shows PASS for V1 to V4 even though the data is wrong. The extractor still contains this behaviour,
   so I do not trust the INV-06 totals; the README lists which fields are reliable and which are not.


