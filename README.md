# PII Redactor

A stateless web app that strips PII from uploaded resumes (`.docx` / `.pdf`) and
returns a redacted copy in the **same format**, with formatting preserved and the
filename suffixed with `" - Redacted"` (e.g. `Resume.docx` → `Resume - Redacted.docx`).
Everything is processed in memory — no accounts, no database, nothing stored.

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
python -m tests.verify        # 29 redaction checks (DOCX/PDF/analyzer, in-memory fixtures)
python -m tests.verify_http   # 16 full-stack checks (routing, upload, headers, errors)
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
- **PDF edge cases:** rotated/vector-outlined text may not map to word rectangles;
  a `search_for` fallback mitigates but isn't total.

## Configuration (`app/config.py`, env-overridable)

`SPACY_MODEL`, `SCORE_THRESHOLD`, `REDACTION_TOKEN`, `MAX_UPLOAD_BYTES`,
`SCANNED_PDF_MIN_CHARS_PER_PAGE`.
