"""In-process HTTP verification of the full FastAPI app via Starlette TestClient.

Exercises real routing, multipart upload, validation, dispatch, streaming
response, and Content-Disposition headers. Run: .venv/bin/python verify_http.py
"""
import sys
from io import BytesIO

import fitz
from docx import Document
from fastapi.testclient import TestClient

from urllib.parse import unquote
from app.main import app

client = TestClient(app)
passed, failed = [], []


def check(label, cond):
    (passed if cond else failed).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def make_docx() -> bytes:
    d = Document()
    p = d.add_paragraph()
    p.add_run("Bru"); p.add_run("ce Way").bold = True; p.add_run("ne")
    d.add_paragraph("Email: bruce@wayne-tech.com  Phone: (202) 456-1111")
    d.add_paragraph("Senior Engineer in Austin, joined 2015")
    b = BytesIO(); d.save(b); return b.getvalue()


def make_pdf() -> bytes:
    doc = fitz.open(); pg = doc.new_page()
    pg.insert_text((72, 72), "Bruce Wayne\nbruce@wayne-tech.com (202) 456-1111\nAustin 2015")
    b = BytesIO(); doc.save(b); doc.close(); return b.getvalue()


print("== GET / ==")
r = client.get("/")
check("index returns 200 html", r.status_code == 200 and "PII Redactor" in r.text)
check("healthz ok", client.get("/healthz").json() == {"status": "ok"})

print("\n== POST /redact (docx) ==")
r = client.post("/redact", files={"file": ("Resume.docx", make_docx(),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
check("docx 200", r.status_code == 200)
check("docx filename suffixed", "Resume - Redacted.docx" in unquote(r.headers.get("content-disposition", "")))
check("docx mime correct", "wordprocessingml" in r.headers.get("content-type", ""))
rd = Document(BytesIO(r.content))
txt = "\n".join(p.text for p in rd.paragraphs)
check("docx PII gone (name/email/phone)",
      "Wayne" not in txt and "wayne-tech" not in txt and "456-1111" not in txt)
check("docx keeps Austin + 2015", "Austin" in txt and "2015" in txt)
check("docx token present", "[REDACTED]" in txt)

print("\n== POST /redact (pdf) ==")
r = client.post("/redact", files={"file": ("CV.pdf", make_pdf(), "application/pdf")})
check("pdf 200", r.status_code == 200)
check("pdf filename suffixed", "CV - Redacted.pdf" in unquote(r.headers.get("content-disposition", "")))
rp = fitz.open(stream=r.content, filetype="pdf")
ext = "\n".join(pg.get_text() for pg in rp); rp.close()
check("pdf PII deleted", "Wayne" not in ext and "wayne-tech" not in ext and "456-1111" not in ext)
check("pdf keeps Austin + 2015", "Austin" in ext and "2015" in ext)

print("\n== Error handling ==")
check("rejects .txt (415)",
      client.post("/redact", files={"file": ("x.txt", b"hi", "text/plain")}).status_code == 415)
check("rejects fake docx content (415)",
      client.post("/redact", files={"file": ("x.docx", b"not a zip", "application/octet-stream")}).status_code == 415)
check("rejects empty file (400)",
      client.post("/redact", files={"file": ("x.pdf", b"", "application/pdf")}).status_code == 400)
# scanned pdf -> 422 with JSON error
sd = fitz.open(); sd.new_page(); sb = BytesIO(); sd.save(sb); sd.close()
r = client.post("/redact", files={"file": ("scan.pdf", sb.getvalue(), "application/pdf")})
check("scanned pdf -> 422 json error", r.status_code == 422 and "error" in r.json())

print(f"\n{'='*40}\n{len(passed)} passed, {len(failed)} failed")
if failed:
    print("FAILED:", ", ".join(failed)); sys.exit(1)
print("ALL GREEN")
