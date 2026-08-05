"""FastAPI app: serves the frontend and the stateless redaction endpoints.

Everything is processed in memory — no file is ever written to disk and nothing
is persisted between requests (finished results are held in RAM only until
downloaded or expired; see app/jobs.py).

Redaction runs as a background job: POST /redact validates the upload and
returns a job id at once; the browser polls GET /jobs/{id} and then fetches
GET /jobs/{id}/download. Long uploads would otherwise be killed by the hosting
proxy and starve the event loop while OCR runs.
"""
import os
from io import BytesIO
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import jobs
from .analyzer import get_analyzer
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


@app.on_event("startup")
def preload_model() -> None:
    """Load the spaCy model while booting, not during the first user's upload."""
    get_analyzer()


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
    """Validate the upload and queue it; returns {"job_id": ...} immediately."""
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

    job_id = jobs.submit(
        filename=_redacted_name(file.filename),
        mime=mime,
        data=data,
        redact_fn=redact_fn,
        known_error=ScannedPDFError,
    )
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown or expired job.")
    body = {"state": job.state}
    if job.state == "queued":
        body["queue_position"] = jobs.queue_position(job_id)
    elif job.state == "done":
        body["warnings"] = job.warnings
    elif job.state == "error":
        return JSONResponse(status_code=job.error_status, content={"error": job.error})
    return body


@app.get("/jobs/{job_id}/download")
def job_download(job_id: str):
    job = jobs.take_result(job_id)
    if job is None:
        raise HTTPException(404, "Result not ready, already downloaded, or expired.")
    # RFC 5987 encoding handles spaces/unicode in the filename safely.
    disposition = f"attachment; filename*=UTF-8''{quote(job.filename)}"
    return StreamingResponse(
        BytesIO(job.result),
        media_type=job.mime,
        headers={"Content-Disposition": disposition},
    )
