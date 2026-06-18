"""PDF redaction with PyMuPDF (fitz).

PII text is *truly deleted*, not merely covered: we add redaction annotations and
call `apply_redactions()`, which removes the underlying glyphs and then paints a
black box with the `[REDACTED]` label. Re-extracting text over the box yields
nothing.

Image-only / scanned PDFs have no text layer to search, so we reject them (v1).
"""
from typing import Dict, List, Tuple

import fitz

from .analyzer import analyze_text
from .config import REDACTION_TOKEN, SCANNED_PDF_MIN_CHARS_PER_PAGE


class ScannedPDFError(Exception):
    """Raised when a PDF has no usable text layer (likely scanned/image-only)."""


def _is_scanned(doc) -> bool:
    page_count = doc.page_count
    if page_count == 0:
        return True
    total_chars = sum(len(page.get_text("text").strip()) for page in doc)
    return total_chars < SCANNED_PDF_MIN_CHARS_PER_PAGE * page_count


def _redact_page(page) -> None:
    words = page.get_text("words")  # (x0, y0, x1, y1, word, block, line, word_no)
    if not words:
        return

    # Reconstruct page text, tracking each word's char range so PII spans
    # (computed on the text) map back to word rectangles.
    offsets: List[Tuple[int, int, tuple]] = []
    pos = 0
    parts: List[str] = []
    for w in words:
        token = w[4]
        parts.append(token)
        offsets.append((pos, pos + len(token), w))
        pos += len(token) + 1  # +1 for the joining space
    text = " ".join(parts)

    spans = analyze_text(text)
    if not spans:
        return

    for a, b in spans:
        # Group intersecting words by (block, line) so a multi-line span draws
        # one box per line instead of one huge box spanning unrelated content.
        groups: Dict[Tuple[int, int], List[fitz.Rect]] = {}
        for ws, we, w in offsets:
            if ws < b and we > a:  # word intersects the PII span
                groups.setdefault((w[5], w[6]), []).append(fitz.Rect(w[0], w[1], w[2], w[3]))

        if not groups:  # fallback: locate the literal substring on the page
            for rect in page.search_for(text[a:b]):
                _add_annot(page, rect)
            continue

        for rects in groups.values():
            rect = rects[0]
            for extra in rects[1:]:
                rect |= extra
            _add_annot(page, rect)

    page.apply_redactions()


def _add_annot(page, rect: fitz.Rect) -> None:
    fontsize = max(6.0, min(10.0, rect.height - 2.0))
    page.add_redact_annot(
        rect,
        text=REDACTION_TOKEN,
        fontsize=fontsize,
        fill=(0, 0, 0),
        text_color=(1, 1, 1),
        align=fitz.TEXT_ALIGN_CENTER,
    )


def redact_pdf(data: bytes) -> bytes:
    """Redact PII from PDF bytes and return redacted PDF bytes.

    Raises ScannedPDFError if the PDF appears to be image-only.
    """
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        if _is_scanned(doc):
            raise ScannedPDFError(
                "This PDF appears to be scanned or image-only (no selectable text). "
                "Text-based redaction can't run on it. OCR isn't supported yet."
            )
        for page in doc:
            _redact_page(page)

        doc.set_metadata({})  # clear /Info (author, title, producer, ...)
        try:
            doc.del_xml_metadata()  # clear XMP
        except Exception:
            pass

        return doc.tobytes(garbage=4, deflate=True)
    finally:
        doc.close()
