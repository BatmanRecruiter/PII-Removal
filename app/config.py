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

# Black out embedded pictures (resume headshots are PII). Set to "0" to keep them.
REDACT_IMAGES = os.getenv("REDACT_IMAGES", "1") not in ("0", "false", "False")

# A page whose filled vector paths contain at least this many bezier curves is
# assumed to hold text converted to outlines (letter shapes drawn as graphics).
# Such "text" has no characters to search; those pages get an extra OCR pass.
VECTOR_TEXT_CURVE_THRESHOLD = int(os.getenv("VECTOR_TEXT_CURVE_THRESHOLD", "100"))

# --- OCR (for text drawn as graphics) ---------------------------------------
# PyMuPDF wheels embed Tesseract; only the language data file is needed.
# `tessdata/eng.traineddata` is committed to the repo.
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OCR_ENABLED = os.getenv("OCR_ENABLED", "1") not in ("0", "false", "False")
TESSDATA_DIR = os.getenv("TESSDATA_DIR", os.path.join(_BASE_DIR, "tessdata"))
# Render resolution for the OCR pass. 200 dpi reads clean digital output well
# and stays inside a 512 MB instance; raise for higher fidelity.
OCR_DPI = int(os.getenv("OCR_DPI", "200"))
