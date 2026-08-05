"""Local redaction with full OCR — for files the free-tier website can't fully scan.

The hosted site runs with OCR disabled (512 MB instance can't fit it), so pages
whose text is drawn as graphics only get a warning there. This tool runs the
IDENTICAL redaction pipeline with OCR enabled on your own machine, where memory
and CPU are plentiful (~25 s for a typical 3-page stylized resume).

Usage (from the project root):
    .venv/bin/python -m tools.redact_local <file-or-folder> [more files...]

- Accepts .pdf and .docx files, folders (redacts every supported file inside),
  and Windows-style paths (C:\\Users\\... is converted for WSL automatically).
- Writes "<name> - Redacted<ext>" next to each input. Never overwrites: if that
  name exists, a numbered variant is used.
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("OCR_ENABLED", "1")  # the whole point of running locally

from app.redact_docx import redact_docx  # noqa: E402
from app.redact_pdf import ScannedPDFError, redact_pdf  # noqa: E402

SUPPORTED = {".pdf": redact_pdf, ".docx": redact_docx}


def to_local_path(raw: str) -> Path:
    """Convert 'C:\\Users\\...' to '/mnt/c/Users/...' when running under WSL."""
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", raw)
    if m and not os.path.exists(raw):
        drive, rest = m.groups()
        return Path(f"/mnt/{drive.lower()}/{rest.replace(chr(92), '/')}")
    return Path(raw)


def output_path(src: Path) -> Path:
    base = src.with_name(f"{src.stem} - Redacted{src.suffix}")
    n = 2
    out = base
    while out.exists():
        out = src.with_name(f"{src.stem} - Redacted ({n}){src.suffix}")
        n += 1
    return out


def collect(paths) -> list:
    files = []
    for raw in paths:
        p = to_local_path(raw)
        if p.is_dir():
            files.extend(sorted(
                f for f in p.iterdir()
                if f.suffix.lower() in SUPPORTED and " - Redacted" not in f.stem
            ))
        elif p.suffix.lower() in SUPPORTED:
            files.append(p)
        else:
            print(f"  skipping (not .pdf/.docx): {p}")
    return files


def main(argv) -> int:
    if not argv:
        print(__doc__)
        return 2
    files = collect(argv)
    if not files:
        print("Nothing to redact.")
        return 1

    print(f"Redacting {len(files)} file(s) with OCR enabled...\n")
    failures = 0
    for src in files:
        print(f"* {src.name}")
        if not src.exists():
            print("    ERROR: file not found")
            failures += 1
            continue
        try:
            data = src.read_bytes()
            redact = SUPPORTED[src.suffix.lower()]
            result, warnings = redact(data)
        except ScannedPDFError as exc:
            print(f"    SKIPPED: {exc}")
            failures += 1
            continue
        except Exception as exc:  # keep batch going
            print(f"    ERROR: {exc}")
            failures += 1
            continue
        out = output_path(src)
        out.write_bytes(result)
        print(f"    -> {out.name}")
        for w in warnings:
            print(f"    WARNING: {w}")
    print(f"\nDone. {len(files) - failures} succeeded, {failures} failed/skipped.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
