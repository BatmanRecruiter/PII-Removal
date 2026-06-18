"""Central configuration knobs for the PII redactor.

Everything tunable lives here so behaviour can be adjusted without hunting
through the codebase. Values can be overridden via environment variables.
"""
import os

# spaCy model used by Presidio's NLP engine. `md` fits Render's 512MB free tier;
# `lg` does not. Swap to "en_core_web_lg" only on a larger instance.
SPACY_MODEL = os.getenv("SPACY_MODEL", "en_core_web_md")

# Presidio confidence threshold. Detections scoring below this are dropped.
SCORE_THRESHOLD = float(os.getenv("SCORE_THRESHOLD", "0.5"))

# The token every detected PII span is replaced with (uniform across DOCX & PDF).
REDACTION_TOKEN = os.getenv("REDACTION_TOKEN", "[REDACTED]")

# Reject uploads larger than this (bytes). Kept conservative for the small instance.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))

# A PDF is treated as scanned/image-only when its average extractable text per
# page falls below this many characters (no usable text layer to search).
SCANNED_PDF_MIN_CHARS_PER_PAGE = int(os.getenv("SCANNED_PDF_MIN_CHARS_PER_PAGE", "10"))
