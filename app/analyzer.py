"""Presidio analyzer setup and the single text-analysis entry point.

Scope is deliberately narrow (resume documents): we redact only the candidate's
name, email, phone, URLs, and a street address if one is present. Dates,
graduation years, and job-history city names are intentionally left intact, so
broad LOCATION / DATE_TIME and the various ID entities are NOT enabled.
"""
import re
import threading
from functools import lru_cache
from typing import List, Tuple

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider

from . import config

# --- Entity scope -----------------------------------------------------------
# Default Presidio entities we keep.
ENTITIES = ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "URL"]
# Our custom street-address recognizer emits this entity (replaces broad LOCATION).
STREET_ADDRESS_ENTITY = "STREET_ADDRESS"
ANALYZED_ENTITIES = ENTITIES + [STREET_ADDRESS_ENTITY]

# Per-entity confidence floors. Presidio scores a valid phone number at only ~0.4
# when no context words ("Phone:", "Call") are adjacent — common on resumes where
# the number sits alone in a header — so we accept phone at a lower bar. Everything
# else uses the (higher) default threshold from config.
ENTITY_THRESHOLDS = {"PHONE_NUMBER": 0.4}


# --- Custom recognizers -----------------------------------------------------
# Street address: <number> <street words> <type> [unit] [, City, ST ZIP].
# `(?i)` makes it case-insensitive regardless of the recognizer's default flags.
_STREET_REGEX = (
    r"(?i)\b\d{1,6}\s+(?:[A-Za-z0-9.'\-]+\s+){0,4}"
    r"(?:Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Lane|Ln|Drive|Dr|Court|Ct|"
    r"Place|Pl|Square|Sq|Terrace|Ter|Trail|Trl|Way|Circle|Cir|Parkway|Pkwy|"
    r"Highway|Hwy|Loop|Run|Pass|Path|Crossing|Xing)\.?"
    r"(?:\s*,?\s*(?:Apt|Apartment|Suite|Ste|Unit|Rm|Room|Fl|Floor|#)\.?\s*[\w-]+)?"
    r"(?:\s*,\s*[A-Za-z.\s]+,\s*[A-Z]{2}\s*\d{5}(?:-\d{4})?)?"
)

# Loose URLs: resume-style links the default URL recognizer misses because they
# lack a scheme or `www.`. Two patterns: well-known hosts, and any domain that
# is followed by a path (the path requirement curbs false positives).
_KNOWN_HOST_REGEX = (
    r"(?i)\b(?:www\.)?(?:linkedin\.com|github\.com|github\.io|gitlab\.com|"
    r"bitbucket\.org|behance\.net|dribbble\.com|medium\.com|stackoverflow\.com|"
    r"stackexchange\.com|twitter\.com|x\.com|kaggle\.com|gitlab\.io|"
    r"portfolio\.[a-z]{2,})(?:/[^\s,;<>\")]*)?"
)
_DOMAIN_WITH_PATH_REGEX = (
    r"(?i)\b(?:[a-z0-9-]+\.)+"
    r"(?:com|net|org|io|me|dev|co|info|app|xyz|tech|design|page|site|website|"
    r"portfolio|biz|pro)/[^\s,;<>\")]+"
)


def _build_custom_recognizers() -> List[PatternRecognizer]:
    street = PatternRecognizer(
        supported_entity=STREET_ADDRESS_ENTITY,
        name="street_address_recognizer",
        patterns=[Pattern(name="street_address", regex=_STREET_REGEX, score=0.85)],
    )
    loose_url = PatternRecognizer(
        supported_entity="URL",
        name="loose_url_recognizer",
        patterns=[
            Pattern(name="known_host", regex=_KNOWN_HOST_REGEX, score=0.8),
            Pattern(name="domain_with_path", regex=_DOMAIN_WITH_PATH_REGEX, score=0.7),
        ],
    )
    return [street, loose_url]


# Serializes model load vs. unload: without it, a job's pre-OCR unload can race
# a still-running startup warm-up, leaving the model resident during OCR — the
# exact memory stacking OCR_LOW_MEMORY exists to prevent.
_engine_lock = threading.RLock()


def unload_analyzer() -> None:
    """Drop the cached engine and give its memory back to the OS.

    Exists for memory-tight hosts (see OCR_LOW_MEMORY): the spaCy model
    (~210 MB) and Tesseract's working set can't both fit in 512 MB, so the
    model is evicted before OCR and lazily reloaded for the analysis pass.
    """
    with _engine_lock:
        _build_analyzer.cache_clear()
    import ctypes
    import gc

    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def get_analyzer() -> AnalyzerEngine:
    """Return the cached AnalyzerEngine, building (model load) if needed."""
    with _engine_lock:
        return _build_analyzer()


@lru_cache(maxsize=1)
def _build_analyzer() -> AnalyzerEngine:
    """Lazily build and cache a single AnalyzerEngine (loads the spaCy model once)."""
    provider = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": config.SPACY_MODEL}],
        }
    )
    analyzer = AnalyzerEngine(
        nlp_engine=provider.create_engine(),
        supported_languages=["en"],
    )
    for recognizer in _build_custom_recognizers():
        analyzer.registry.add_recognizer(recognizer)
    return analyzer


def _merge_spans(spans: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Merge overlapping/adjacent (start, end) spans into disjoint ranges."""
    if not spans:
        return []
    spans = sorted(spans)
    merged = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:  # overlap or touch
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


# Invisible characters that documents (notably Google Docs exports) glue onto
# words: zero-width space/joiners, word joiner, BOM, soft hyphen, plus NBSP.
# They wreck NER — spaCy sees "​Harlan​ Smith" as one garbage token,
# not a name. Each is replaced with a regular space (same string length, so
# span offsets still line up with the caller's original text).
_INVISIBLE_RE = re.compile("[\u00a0\u00ad\u200b\u200c\u200d\u2060\ufeff]")


def analyze_text(text: str) -> List[Tuple[int, int]]:
    """Return disjoint (start, end) character spans of PII within `text`.

    Spans are merged so callers can replace them without worrying about
    overlapping detections (e.g. an email whose domain also matches a URL).
    Offsets always refer to `text` exactly as passed in.
    """
    if not text or not text.strip():
        return []
    results = get_analyzer().analyze(
        text=_INVISIBLE_RE.sub(" ", text),
        language="en",
        entities=ANALYZED_ENTITIES,
    )
    kept = [
        r for r in results
        if r.score >= ENTITY_THRESHOLDS.get(r.entity_type, config.SCORE_THRESHOLD)
    ]
    return _merge_spans([(r.start, r.end) for r in kept])
