# PII Redactor

A stateless web app that strips PII from uploaded resumes (`.docx` / `.pdf`) and
returns a redacted copy in the **same format**, with formatting preserved and the
filename suffixed with `" - Redacted"` (e.g. `Resume.docx` → `Resume - Redacted.docx`).
Everything is processed in memory — no accounts, no database, nothing written to
disk. A finished result is held in RAM only until it's downloaded (or for at most
15 minutes), because redaction runs as a **background job**: `POST /redact`
validates the upload and immediately returns `{"job_id": ...}`; the browser polls
`GET /jobs/{id}` (`queued` / `processing` / `done` + `warnings`, or an error) and
then fetches `GET /jobs/{id}/download` once. This keeps requests short — OCR-heavy
files can take minutes on a small instance, long-held HTTP requests get killed by
the hosting proxy, and the old inline approach starved the event loop (even
`/healthz` went dark during OCR). One worker thread processes jobs sequentially
on purpose: a fraction-of-a-CPU instance running two OCR passes at once would
just crawl and double peak memory. The spaCy model is preloaded at startup so
the first upload doesn't pay for it.

You can upload **multiple files at once** (drag-drop or picker). The browser
redacts them one at a time and shows a per-file status with an individual download
for each — so every file keeps its own correct name and format (no zip). The batch
cap is **20** files, set by `MAX_FILES` at the top of `static/app.js` (set it to `0`
for unlimited). The per-file **25 MB** limit is enforced server-side
(`MAX_UPLOAD_BYTES` in `app/config.py`).

## What gets redacted

Scope is deliberately narrow for resumes:

| Redacted | How |
|---|---|
| Candidate name | Presidio `PERSON` |
| Email address | Presidio `EMAIL_ADDRESS` |
| Phone number | Presidio `PHONE_NUMBER` |
| URLs / links | Presidio `URL` + a regex for scheme-less links (linkedin.com/in/…, github.com/…) |
| Street address (if present) | Custom street-address regex recognizer |
| Embedded pictures (headshots) | Blacked out in PDFs / removed from DOCX (`REDACT_IMAGES=0` to keep) |
| Hyperlink targets | Link annotations with `mailto:`/`tel:`/PII URIs are deleted from PDFs |
| Text drawn as graphics | OCR pass (PyMuPDF's embedded Tesseract) on pages with vector letter outlines |

Replaced uniformly with the token **`[REDACTED]`** (in PDFs the underlying text is
truly deleted, then a black box with the label is drawn).

**Intentionally NOT redacted** so resume content survives: employment/graduation
**dates**, job-history **city/state names** (broad `LOCATION` is off), and the
various national-ID / financial entities. Document **metadata** (author, company,
XMP) *is* scrubbed, since the author field often contains the candidate's name.

## Run locally

```bash
uv venv --python 3.11 .venv          # or: python3.11 -m venv .venv
uv pip install -r requirements.txt   # or: pip install -r requirements.txt
python -m spacy download en_core_web_md
uvicorn app.main:app --reload
# open http://localhost:8000
```

## Tests

Run from the project root so the `app` package resolves:

```bash
python -m tests.verify        # 38 redaction checks (DOCX/PDF/analyzer/OCR, in-memory fixtures)
python -m tests.verify_http   # 21 full-stack checks (routing, job flow, headers, errors)
```

## Deploy (Render, free tier)

Push the repo and create a Blueprint from `render.yaml`. It pins Python via the
`PYTHON_VERSION` env var (`runtime.txt` / `.python-version` are committed as
backup), downloads `en_core_web_md` at build time, and runs a **single** Uvicorn
worker (each worker loads its own copy of the model; >1 would OOM the 512 MB
instance).

## Known limitations

- **Bare `City, ST` line not caught.** Because broad `LOCATION` is intentionally
  off (to keep job-history cities), a top-of-resume location line like `Austin, TX`
  with no street is **not** redacted. Re-enable `LOCATION` (optionally only for the
  first lines) in `app/analyzer.py` if you want it.
- **Detection isn't perfect.** Presidio NER can miss names or false-positive on
  common words. This reduces but does not *guarantee* removal — review output before
  sharing. Tune `SCORE_THRESHOLD` in `app/config.py`.
- **Scanned/image-only PDFs are rejected** (no OCR in v1).
- **DOCX out of scope:** text inside images, SmartArt, embedded objects, and
  field-code results. (Body, tables, headers, footers, text boxes, and
  footnotes/comments are covered.)
- **Text drawn as graphics is OCR-redacted (slower).** Some exporters (Google Docs
  themes, Canva, etc.) convert stylized headers/sidebars to vector letter
  *outlines* — shapes, not characters, so normal text search can't see them.
  Pages that look like they contain outlined text get an OCR pass using the
  Tesseract engine **embedded in the PyMuPDF wheel** (no system install; only
  `tessdata/eng.traineddata`, committed to the repo, is needed). OCR findings are
  trusted only where the text layer is blind, and redactions on those pages also
  delete touched vector line art so outlines are removed, not just covered.
  Budget ~10 s per affected page locally — several minutes on Render's free
  0.1-CPU instance (the job flow + UI elapsed counter make that survivable). If
  OCR can't run (e.g. tessdata missing), the job's status response carries a
  per-file warning (shown amber in the UI) naming the pages to review manually.
  OCR is best-effort: unusual fonts/colors can still evade it — spot-check
  stylized documents.
- **Invisible characters are neutralized.** Zero-width spaces and similar
  characters that some exporters glue onto words (which used to hide names from
  the NER model) are converted to spaces before analysis.

## Configuration (`app/config.py`, env-overridable)

`SPACY_MODEL`, `SCORE_THRESHOLD`, `REDACTION_TOKEN`, `MAX_UPLOAD_BYTES`,
`SCANNED_PDF_MIN_CHARS_PER_PAGE`, `REDACT_IMAGES`, `VECTOR_TEXT_CURVE_THRESHOLD`,
`OCR_ENABLED`, `TESSDATA_DIR`, `OCR_DPI`.
