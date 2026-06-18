"""In-place DOCX redaction.

We never rebuild the document; we mutate the existing XML so all formatting is
preserved. The hard part is that Word splits a single logical string across many
`<w:r>`/`<w:t>` elements, so a PII span routinely crosses element boundaries. We
handle this by grouping every `<w:t>` under its nearest-ancestor paragraph,
concatenating their text, detecting PII on the whole paragraph, then rewriting
the individual `<w:t>` elements.

Grouping by *nearest* paragraph (rather than walking a paragraph's descendants)
means nested paragraphs inside text boxes (`w:txbxContent`) are handled exactly
once and never double-counted against their containing paragraph.

Coverage: every story part in the package — main document body, tables (nested
included), headers, footers, text boxes, and footnotes/endnotes/comments when
present — because we iterate all parts and find every `<w:p>`.
"""
from io import BytesIO
from typing import Dict, List, Optional, Tuple

from docx import Document
from docx.oxml.ns import qn

from .analyzer import analyze_text
from .config import REDACTION_TOKEN

_W_P = qn("w:p")
_W_T = qn("w:t")
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def _nearest_paragraph(elem):
    """Walk up from a `<w:t>` to its closest ancestor `<w:p>`."""
    node = elem.getparent()
    while node is not None:
        if node.tag == _W_P:
            return node
        node = node.getparent()
    return None


def _span_containing(pos: int, spans: List[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    """Return the disjoint span (a, b) with a <= pos < b, else None."""
    for a, b in spans:
        if a <= pos < b:
            return (a, b)
        if a > pos:
            break  # spans are sorted & disjoint
    return None


def _redact_run_group(t_elements: List) -> None:
    """Detect and redact PII across one paragraph's ordered `<w:t>` elements."""
    texts = [t.text or "" for t in t_elements]
    full = "".join(texts)
    if not full.strip():
        return
    spans = analyze_text(full)
    if not spans:
        return

    offset = 0
    for t, original in zip(t_elements, texts):
        start, end = offset, offset + len(original)
        offset = end
        if not original:
            continue
        buf: List[str] = []
        j = start
        while j < end:
            span = _span_containing(j, spans)
            if span is None:
                buf.append(full[j])
                j += 1
            else:
                a, _b = span
                if j == a:  # emit the token once, at the span's global start
                    buf.append(REDACTION_TOKEN)
                j += 1  # redacted char is dropped
        new_text = "".join(buf)
        if new_text != original:
            t.text = new_text
            # Preserve surrounding whitespace so Word doesn't collapse it.
            if new_text != new_text.strip():
                t.set(_XML_SPACE, "preserve")


def _iter_story_roots(doc):
    """Yield the XML root of every package part that can hold paragraphs."""
    for part in doc.part.package.iter_parts():
        root = getattr(part, "element", None)
        if root is not None:
            yield root


def _group_by_paragraph(root) -> Dict[object, List]:
    """Map each paragraph element to its `<w:t>` children, in document order."""
    groups: Dict[object, List] = {}
    for t in root.iter(_W_T):
        paragraph = _nearest_paragraph(t)
        if paragraph is not None:
            groups.setdefault(paragraph, []).append(t)
    return groups


def _scrub_metadata(doc) -> None:
    """Clear core properties (author etc.) and extended Company/Manager fields."""
    cp = doc.core_properties
    for attr in ("author", "last_modified_by", "title", "subject",
                 "comments", "category", "keywords"):
        try:
            setattr(cp, attr, "")
        except (ValueError, TypeError):
            pass

    # Company / Manager live in extended props (docProps/app.xml), not core props.
    for part in doc.part.package.iter_parts():
        if str(part.partname).endswith("app.xml"):
            root = getattr(part, "element", None)
            if root is None:
                continue
            for child in list(root):
                if child.tag.split("}")[-1] in ("Company", "Manager"):
                    child.text = ""


def redact_docx(data: bytes) -> bytes:
    """Redact PII from DOCX bytes and return the redacted DOCX bytes."""
    doc = Document(BytesIO(data))
    for root in _iter_story_roots(doc):
        for _paragraph, t_elements in _group_by_paragraph(root).items():
            _redact_run_group(t_elements)
    _scrub_metadata(doc)
    out = BytesIO()
    doc.save(out)
    return out.getvalue()
