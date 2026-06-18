"""End-to-end verification harness — builds fixtures in memory and checks redaction.

Run: .venv/bin/python verify.py
"""
import sys
from io import BytesIO

import fitz
from docx import Document

from app.analyzer import analyze_text
from app.redact_docx import redact_docx
from app.redact_pdf import ScannedPDFError, redact_pdf

PII = {
    "name": "Bruce Wayne",
    "email": "bruce@wayne-tech.com",
    "phone": "(202) 456-1111",
    "url": "linkedin.com/in/brucewayne",
    "address": "1007 Mountain Drive, Austin, TX 78701",
}
# These must SURVIVE (resume content we intentionally keep).
# Real city "Austin" in job history must survive (NER tags it GPE, which we keep).
KEEP = ["2015", "2019", "Austin", "Senior Engineer", "May 2020"]

passed, failed = [], []


def check(label, cond):
    (passed if cond else failed).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


# --- 1. Analyzer unit check -------------------------------------------------
print("\n== Analyzer ==")
text = f"{PII['name']} | {PII['email']} | {PII['phone']} | {PII['url']} | {PII['address']}"
spans = analyze_text(text)
redacted_text = ""
last = 0
for a, b in spans:
    redacted_text += text[last:a] + "X"
    last = b
redacted_text += text[last:]
for key, val in PII.items():
    check(f"analyzer removes {key}", val not in redacted_text)
# A bare date must survive analysis untouched.
date_spans = analyze_text("In 2015 I joined; graduated May 2019.")
check("analyzer keeps dates (no spans)", date_spans == [])


# --- 2. DOCX ----------------------------------------------------------------
print("\n== DOCX ==")
doc = Document()
# Name split across 3 runs (simulates Word splitting).
p = doc.add_paragraph()
p.add_run("Bru")
r = p.add_run("ce Way")
r.bold = True
p.add_run("ne")
doc.add_paragraph(f"Email: {PII['email']}   Phone: {PII['phone']}")
doc.add_paragraph(f"Portfolio: {PII['url']}")
doc.add_paragraph(f"Address: {PII['address']}")
doc.add_paragraph("Experience: Senior Engineer in Austin (May 2020 - 2019, joined 2015)")
# table
tbl = doc.add_table(rows=1, cols=1)
tbl.rows[0].cells[0].text = f"Reference: {PII['name']}, {PII['email']}"
# header & footer
sec = doc.sections[0]
sec.header.paragraphs[0].text = f"{PII['name']} - Resume"
sec.footer.paragraphs[0].text = f"Contact: {PII['phone']}"
doc.core_properties.author = "Bruce Wayne"

buf = BytesIO()
doc.save(buf)
out = redact_docx(buf.getvalue())

rd = Document(BytesIO(out))
all_text = "\n".join(par.text for par in rd.paragraphs)
for t in rd.tables:
    for row in t.rows:
        for cell in row.cells:
            all_text += "\n" + cell.text
for s in rd.sections:
    all_text += "\n" + s.header.paragraphs[0].text + "\n" + s.footer.paragraphs[0].text

check("docx body name removed (multi-run)", "Wayne" not in all_text and "Bruce" not in all_text)
check("docx email removed", PII["email"] not in all_text)
check("docx phone removed", "456-1111" not in all_text)
check("docx url removed", PII["url"] not in all_text)
check("docx street address removed", "1007 Mountain Drive" not in all_text)
check("docx table PII removed", PII["email"] not in all_text)
check("docx header name removed", "Bruce Wayne - Resume" not in all_text)
check("docx footer phone removed", "456-1111" not in all_text)
check("docx [REDACTED] token present", "[REDACTED]" in all_text)
check("docx keeps year 2015", "2015" in all_text)
check("docx keeps city Austin", "Austin" in all_text)
check("docx keeps 'Senior Engineer'", "Senior Engineer" in all_text)
check("docx metadata author scrubbed", rd.core_properties.author in ("", None))
# bold formatting on the redacted run should still carry the token
check("docx file is valid (re-openable)", len(all_text) > 0)


# --- 3. PDF -----------------------------------------------------------------
print("\n== PDF ==")
pdf = fitz.open()
page = pdf.new_page()
body = (
    f"{PII['name']}\n{PII['email']}   {PII['phone']}\n{PII['url']}\n"
    f"{PII['address']}\nSenior Engineer, Gotham. Joined 2015, promoted 2019. Based in Austin."
)
page.insert_text((72, 72), body, fontsize=11)
pdf_bytes = BytesIO()
pdf.save(pdf_bytes)
pdf.close()

out_pdf = redact_pdf(pdf_bytes.getvalue())
rp = fitz.open(stream=out_pdf, filetype="pdf")
extracted = "\n".join(pg.get_text("text") for pg in rp)

check("pdf name deleted", "Wayne" not in extracted)
check("pdf email deleted", PII["email"] not in extracted)
check("pdf phone deleted", "456-1111" not in extracted)
check("pdf url deleted", "brucewayne" not in extracted)
check("pdf address deleted", "Mountain Drive" not in extracted)
check("pdf keeps year 2015", "2015" in extracted)
check("pdf keeps city Austin", "Austin" in extracted)
check("pdf metadata scrubbed", not (rp.metadata or {}).get("author"))
rp.close()


# --- 4. Scanned PDF rejection ----------------------------------------------
print("\n== Scanned PDF ==")
scan = fitz.open()
scan.new_page()  # blank page, no text layer
scan_bytes = BytesIO()
scan.save(scan_bytes)
scan.close()
try:
    redact_pdf(scan_bytes.getvalue())
    check("scanned pdf rejected", False)
except ScannedPDFError:
    check("scanned pdf rejected", True)


# --- summary ----------------------------------------------------------------
print(f"\n{'='*40}\n{len(passed)} passed, {len(failed)} failed")
if failed:
    print("FAILED:", ", ".join(failed))
    sys.exit(1)
print("ALL GREEN")
