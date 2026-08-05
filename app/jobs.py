"""In-memory background job store for redaction work.

Why jobs instead of answering the upload request directly: OCR-heavy files can
take minutes on a small instance, and both the hosting proxy (Render kills
long-held requests) and a single-threaded event loop (health checks going dark)
make one long HTTP request the wrong shape. So the upload returns a job id
immediately, work happens on a worker thread, and the browser polls a cheap
status endpoint until the result is ready.

Still stateless in the ways that matter: results live only in RAM, are removed
when downloaded, and expire after JOB_TTL_SECONDS regardless. Nothing is ever
written to disk.

One worker thread on purpose: the small instance has a fraction of a CPU, so
running two OCR passes at once would just make both crawl and double peak
memory. Queued jobs report their position instead.
"""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

JOB_TTL_SECONDS = 15 * 60

_executor = ThreadPoolExecutor(max_workers=1)
_lock = threading.Lock()
_jobs: Dict[str, "Job"] = {}


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


def _purge_expired() -> None:
    now = time.time()
    with _lock:
        for job_id in [j for j, job in _jobs.items() if now - job.created > JOB_TTL_SECONDS]:
            del _jobs[job_id]


def submit(
    filename: str,
    mime: str,
    data: bytes,
    redact_fn: Callable[[bytes], Tuple[bytes, List[str]]],
    known_error: type,
) -> str:
    """Queue a redaction job; returns its id immediately."""
    _purge_expired()
    job = Job(id=uuid.uuid4().hex, filename=filename, mime=mime)
    with _lock:
        _jobs[job.id] = job

    def run() -> None:
        with _lock:
            job.state = "processing"
        try:
            result, warnings = redact_fn(data)
        except known_error as exc:  # expected, user-fixable (e.g. scanned PDF)
            with _lock:
                job.state, job.error, job.error_status = "error", str(exc), 422
        except Exception:
            with _lock:
                job.state = "error"
                job.error = "Failed to process the document. It may be corrupt."
        else:
            with _lock:
                job.state, job.result, job.warnings = "done", result, warnings

    _executor.submit(run)
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
