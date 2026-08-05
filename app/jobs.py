"""In-memory background job store; redaction runs in a separate worker process.

Why jobs instead of answering the upload request directly: OCR-heavy files can
take minutes on a small instance, and both the hosting proxy (Render kills
long-held requests) and a starved web server make one long HTTP request the
wrong shape. So the upload returns a job id immediately, work happens
elsewhere, and the browser polls a cheap status endpoint until the result is
ready.

Why a worker *process* rather than a thread: Python's GIL means a CPU-pegged
thread (Tesseract OCR, spaCy) still starves the web server's event loop — the
live service went completely dark during OCR when this used threads. A separate
process has its own GIL, so the web process stays responsive no matter how long
a file takes. It also keeps the heavy imports (fitz, presidio, spaCy model)
out of the web process entirely: they are imported lazily inside the worker.

Still stateless in the ways that matter: results live only in RAM, are removed
when downloaded, and expire after JOB_TTL_SECONDS regardless. Nothing is ever
written to disk.

One worker on purpose: the small instance has a fraction of a CPU, so running
two OCR passes at once would just make both crawl and double peak memory.
Queued jobs report their position instead.
"""
import multiprocessing
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool, ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional

JOB_TTL_SECONDS = 15 * 60

_orchestrator = ThreadPoolExecutor(max_workers=1)  # serializes jobs, cheap threads
_lock = threading.Lock()
_jobs: Dict[str, "Job"] = {}

# "fork" (Linux): workers are cloned from the web process with no re-import of
# the caller's __main__ — "spawn"/"forkserver" both re-run or re-import the
# launching script, which breaks unguarded scripts and TestClient runs. The
# heavy libraries are imported only after the fork, inside the worker, so the
# web process never carries them.
_mp_context = multiprocessing.get_context("fork")
_pool: Optional[ProcessPoolExecutor] = None
_pool_lock = threading.Lock()


@dataclass
class Job:
    id: str
    filename: str
    mime: str
    state: str = "queued"  # queued -> processing -> done | error
    created: float = field(default_factory=time.time)
    result: Optional[bytes] = None
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None
    error_status: int = 500


def run_redaction(ext: str, data: bytes) -> dict:
    """Executed inside the worker process. Heavy imports stay in that process.

    Returns a plain dict (never raises) so nothing exotic crosses the
    process boundary.
    """
    from .redact_docx import redact_docx
    from .redact_pdf import ScannedPDFError, redact_pdf

    try:
        if ext == ".docx":
            out, warnings = redact_docx(data)
        else:
            out, warnings = redact_pdf(data)
    except ScannedPDFError as exc:  # expected, user-fixable
        return {"ok": False, "error": str(exc), "status": 422}
    except Exception:
        return {"ok": False,
                "error": "Failed to process the document. It may be corrupt.",
                "status": 500}
    return {"ok": True, "data": out, "warnings": warnings}


def _warm_worker() -> dict:
    """Load the spaCy model in the worker so the first upload doesn't pay for it."""
    from .analyzer import get_analyzer

    get_analyzer()
    return {"ok": True}


def _get_pool() -> ProcessPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(max_workers=1, mp_context=_mp_context)
        return _pool


def _reset_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=False, cancel_futures=True)
            _pool = None


def warm() -> None:
    """Fire-and-forget worker warm-up; call once at app startup."""
    def kick() -> None:
        try:
            _get_pool().submit(_warm_worker).result()
        except Exception:
            _reset_pool()  # worker will be recreated on the first real job

    threading.Thread(target=kick, daemon=True).start()


def _purge_expired() -> None:
    now = time.time()
    with _lock:
        for job_id in [j for j, job in _jobs.items() if now - job.created > JOB_TTL_SECONDS]:
            del _jobs[job_id]


def submit(filename: str, mime: str, ext: str, data: bytes) -> str:
    """Queue a redaction job; returns its id immediately."""
    _purge_expired()
    job = Job(id=uuid.uuid4().hex, filename=filename, mime=mime)
    with _lock:
        _jobs[job.id] = job

    def run() -> None:
        with _lock:
            job.state = "processing"
        try:
            res = _get_pool().submit(run_redaction, ext, data).result()
        except BrokenProcessPool:
            _reset_pool()
            res = {"ok": False, "status": 500,
                   "error": "The server ran out of memory processing this file. "
                            "Please try again; if it keeps failing, the file may "
                            "be too complex for this server size."}
        except Exception:
            _reset_pool()
            res = {"ok": False, "status": 500,
                   "error": "Failed to process the document. Please try again."}

        with _lock:
            if res["ok"]:
                job.state, job.result, job.warnings = "done", res["data"], res["warnings"]
            else:
                job.state, job.error, job.error_status = "error", res["error"], res["status"]

    _orchestrator.submit(run)
    return job.id


def get(job_id: str) -> Optional[Job]:
    _purge_expired()
    with _lock:
        return _jobs.get(job_id)


def queue_position(job_id: str) -> int:
    """0 = processing/next; N = jobs queued ahead of this one."""
    with _lock:
        waiting = sorted(
            (j for j in _jobs.values() if j.state == "queued"), key=lambda j: j.created
        )
        for pos, job in enumerate(waiting):
            if job.id == job_id:
                return pos
    return 0


def take_result(job_id: str) -> Optional[Job]:
    """Return a finished job and drop its bytes from the store (single download)."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job.state != "done":
            return None
        del _jobs[job_id]
        return job
