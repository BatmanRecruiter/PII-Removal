"""FastAPI app: serves the frontend and the stateless /redact endpoint.

Everything is processed in memory — no file is ever written to disk and nothing
is persisted between requests.
"""
import os
from io import BytesIO
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import MAX_UPLOAD_BYTES
from .redact_docx import redact_docx
from .redact_pdf import ScannedPDFError, redact_pdf

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"

# (extension) -> (magic-byte prefix, mime, redact function)
_HANDLERS = {
    ".docx": (b"PK\x03\x04", DOCX_MIME, redact_docx),
    ".pdf": (b"%PDF", PDF_MIME, redact_pdf),
}

app = FastAPI(title="PII Redactor", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


def _redacted_name(original: str) -> str:
    stem, ext = os.path.splitext(os.path.basename(original or "document"))
    return f"{stem} - Redacted{ext}"


@app.post("/redact")
async def redact(file: UploadFile = File(...)):
    _stem, ext = os.path.splitext(file.filename or "")
    ext = ext.lower()
    if ext not in _HANDLERS:
        raise HTTPException(415, "Unsupported file type. Upload a .docx or .pdf.")

    data = await file.read()
    if not data:
        raise HTTPException(400, "The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        raise HTTPException(413, f"File too large. Maximum size is {limit_mb} MB.")

    magic, mime, redact_fn = _HANDLERS[ext]
    if not data.startswith(magic):
        raise HTTPException(415, f"File content does not look like a valid {ext} file.")

    try:
        result = redact_fn(data)
    except ScannedPDFError as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})
    except Exception:
        raise HTTPException(500, "Failed to process the document. It may be corrupt.")

    out_name = _redacted_name(file.filename)
    # RFC 5987 encoding handles spaces/unicode in the filename safely.
    disposition = f"attachment; filename*=UTF-8''{quote(out_name)}"
    return StreamingResponse(
        BytesIO(result),
        media_type=mime,
        headers={"Content-Disposition": disposition},
    )
