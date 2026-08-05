"""PDF redaction with PyMuPDF (fitz).

PII text is *truly deleted*, not merely covered: we add redaction annotations and
call `apply_redactions()`, which removes the underlying glyphs and then paints a
black box with the `[REDACTED]` label. Re-extracting text over the box yields
nothing.

Beyond the text layer we also handle:
- Embedded pictures (headshots are PII) — blacked out when REDACT_IMAGES is on.
- Link annotations — a "LinkedIn" anchor whose visible text is harmless can still
  carry a mailto:/tel:/profile URI; links with PII in the URI are deleted.
- Text converted to vector outlines (letter shapes drawn as graphics, common in
  Google Docs / Canva exports). There are no characters to search, so pages that
  look like they contain outlined text get an extra OCR pass (PyMuPDF's embedded
  Tesseract). OCR findings are only trusted where the real text layer is blind,
  so OCR noise can't black out body text the normal pass already handled. On
  those pages redactions also remove touched vector line art, so the letter
  outlines are deleted, not merely covered. If OCR can't run (missing tessdata),
  we return a warning naming the affected pages instead of failing silently.

Image-only / scanned PDFs have no text layer to search, so we reject them (v1).
"""
from typing import Dict, List, Optional, Tuple

import fitz

from . import config
from .analyzer import analyze_text, unload_analyzer


class ScannedPDFError(Exception):
    """Raised when a PDF has no usable text layer (likely scanned/image-only)."""


def _is_scanned(doc) -> bool:
    page_count = doc.page_count
    if page_count == 0:
        return True
    total_chars = sum(len(page.get_text("text").strip()) for page in doc)
    return total_chars < config.SCANNED_PDF_MIN_CHARS_PER_PAGE * page_count


def _annots_for_words(
    page,
    words: List[tuple],
    skip_rects: Optional[List[fitz.Rect]] = None,
    use_search_fallback: bool = False,
) -> int:
    """Detect PII across `words` and add redaction annots. Returns annot count.

    `words` are PyMuPDF word tuples (x0, y0, x1, y1, word, block, line, word_no).
    `skip_rects`: word boxes mostly inside any of these rects are ignored (used
    by the OCR pass to defer to the text-layer pass in areas it already covers).
    """
    if not words:
        return 0

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

    added = 0
    for a, b in analyze_text(text):
        # Group intersecting words by (block, line) so a multi-line span draws
        # one box per line instead of one huge box spanning unrelated content.
        groups: Dict[Tuple[int, int], List[fitz.Rect]] = {}
        for ws, we, w in offsets:
            if ws < b and we > a:  # word intersects the PII span
                rect = fitz.Rect(w[0], w[1], w[2], w[3])
                if skip_rects is not None and _mostly_covered(rect, skip_rects):
                    continue
                groups.setdefault((w[5], w[6]), []).append(rect)

        if not groups:
            if use_search_fallback:  # locate the literal substring on the page
                for rect in page.search_for(text[a:b]):
                    _add_annot(page, rect)
                    added += 1
            continue

        for rects in groups.values():
            rect = rects[0]
            for extra in rects[1:]:
                rect |= extra
            _add_annot(page, rect)
            added += 1
    return added


def _mostly_covered(rect: fitz.Rect, others: List[fitz.Rect]) -> bool:
    """True if a meaningful share of `rect` overlaps any rect in `others`.

    Truly invisible content (vector outlines) overlaps the text layer at ~0.00;
    OCR reads of real text overlap at ~0.5+ even when OCR merges two lines into
    one tall box. 0.3 splits the two cleanly.
    """
    area = abs(rect)
    if area <= 0:
        return True
    for other in others:
        if abs(rect & other) >= 0.3 * area:
            return True
    return False


def _redact_page_images(page) -> int:
    """Black out every embedded picture (headshots are PII). Returns annot count."""
    added = 0
    for img in page.get_images(full=True):
        for rect in page.get_image_rects(img[0]):
            if not rect.is_empty:
                _add_annot(page, rect)
                added += 1
    return added


def _delete_pii_links(page) -> None:
    """Remove link annotations whose target URI itself contains PII.

    The visible anchor text is handled by the text pass; this catches the URI
    hiding underneath (mailto:, tel:, personal profile URLs).
    """
    for link in page.get_links():
        uri = link.get("uri") or ""
        if not uri:
            continue
        if uri.lower().startswith(("mailto:", "tel:")) or analyze_text(uri):
            page.delete_link(link)


def _has_vector_text(page) -> bool:
    """Heuristic: does this page contain text converted to vector outlines?

    Letter shapes drawn as filled bezier paths (no font, no characters) show up
    as fill-drawings with many curve segments. Ordinary decoration (rules,
    boxes, a circular photo mask) stays far below the threshold.
    """
    curves = 0
    for drawing in page.get_drawings():
        if drawing.get("fill") is None:
            continue
        curves += sum(1 for item in drawing["items"] if item[0] == "c")
        if curves >= config.VECTOR_TEXT_CURVE_THRESHOLD:
            return True
    return False


# Pages larger than this (points; ~2x A4 area) are not OCR'd: the render buffer
# scales with page area and would blow past a small instance's memory.
_MAX_OCR_PAGE_AREA = 2 * 595 * 842


def _ocr_words(page) -> List[tuple]:
    """OCR the rendered page and return its word tuples.

    Raises on any OCR failure (e.g. missing tessdata, oversized page) — the
    caller downgrades that to a user-facing warning.
    """
    if abs(page.rect) > _MAX_OCR_PAGE_AREA:
        raise ValueError("page too large for OCR")
    textpage = page.get_textpage_ocr(
        flags=0,
        language="eng",
        dpi=config.OCR_DPI,
        full=True,  # OCR the whole rendered page, not just embedded images
        tessdata=config.TESSDATA_DIR,
    )
    return page.get_text("words", textpage=textpage)


def redact_pdf(data: bytes) -> Tuple[bytes, List[str]]:
    """Redact PII from PDF bytes; return (redacted bytes, user-facing warnings).

    Raises ScannedPDFError if the PDF appears to be image-only.
    """
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        if _is_scanned(doc):
            raise ScannedPDFError(
                "This PDF appears to be scanned or image-only (no selectable text). "
                "Text-based redaction can't run on it. OCR isn't supported yet."
            )

        unscanned_pages: List[int] = []

        # --- Pass 1: gather words (text layer + OCR), NO language analysis ---
        # Kept strictly NLP-free so that in OCR_LOW_MEMORY mode the spaCy model
        # and Tesseract are never in memory at the same time: together they
        # exceed a 512 MB instance and get the container OOM-killed.
        has_vector_text = [_has_vector_text(page) for page in doc]
        if config.OCR_ENABLED and config.OCR_LOW_MEMORY and any(has_vector_text):
            unload_analyzer()

        gathered: List[Tuple[List[tuple], Optional[List[tuple]]]] = []
        for page, vector_text in zip(doc, has_vector_text):
            text_words = page.get_text("words")
            ocr_words: Optional[List[tuple]] = None
            if vector_text and config.OCR_ENABLED:
                try:
                    ocr_words = _ocr_words(page)
                except Exception:
                    unscanned_pages.append(page.number + 1)
            elif vector_text:
                unscanned_pages.append(page.number + 1)
            gathered.append((text_words, ocr_words))
            fitz.TOOLS.store_shrink(100)  # drop OCR render from MuPDF's cache

        if any(w[1] is not None for w in gathered):
            # Hand Tesseract's freed working set back to the OS before the
            # model reloads on top of it — RSS peaks stack otherwise.
            _trim_ram()

        # --- Pass 2: analyze and redact (model loads lazily on first use) ----
        for page, (text_words, ocr_words) in zip(doc, gathered):
            _delete_pii_links(page)
            annots = _annots_for_words(page, text_words, use_search_fallback=True)
            if config.REDACT_IMAGES:
                annots += _redact_page_images(page)

            ocr_ran = ocr_words is not None
            if ocr_ran:
                # Areas the text layer already covers are the text pass's
                # responsibility; skipping them keeps OCR misreads from
                # redacting good body text.
                covered = [fitz.Rect(w[:4]) for w in text_words]
                annots += _annots_for_words(page, ocr_words, skip_rects=covered)

            if annots:
                if ocr_ran:
                    # Delete vector line art touched by a redaction box so letter
                    # outlines are truly removed, not hidden under the box.
                    page.apply_redactions(
                        graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED
                    )
                else:
                    page.apply_redactions()

            # We never revisit a finished page; without this the cache holds
            # ~100+ MB until process exit — enough to breach a 512 MB instance.
            fitz.TOOLS.store_shrink(100)

        warnings: List[str] = []
        if unscanned_pages:
            pages = ", ".join(str(n) for n in unscanned_pages)
            warnings.append(
                f"Page{'s' if len(unscanned_pages) > 1 else ''} {pages}: some text "
                "is drawn as graphics (letter outlines) and OCR could not run, so "
                "those areas were NOT scanned for PII. Please review them manually."
            )

        doc.set_metadata({})  # clear /Info (author, title, producer, ...)
        try:
            doc.del_xml_metadata()  # clear XMP
        except Exception:
            pass

        return doc.tobytes(garbage=4, deflate=True), warnings
    finally:
        doc.close()


def _trim_ram() -> None:
    """Best-effort: return freed heap memory to the OS (glibc keeps it otherwise)."""
    import ctypes
    import gc

    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def _add_annot(page, rect: fitz.Rect) -> None:
    fontsize = max(6.0, min(10.0, rect.height - 2.0))
    page.add_redact_annot(
        rect,
        text=config.REDACTION_TOKEN,
        fontsize=fontsize,
        fill=(0, 0, 0),
        text_color=(1, 1, 1),
        align=fitz.TEXT_ALIGN_CENTER,
    )
