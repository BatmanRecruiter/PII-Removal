"use strict";

const MAX_BYTES = 25 * 1024 * 1024;
const MAX_FILES = 20;        // batch cap; set to 0 for unlimited
const ALLOWED_EXT = [".docx", ".pdf"];

const els = {
  dropZone: document.getElementById("drop-zone"),
  fileInput: document.getElementById("file-input"),
  browseBtn: document.getElementById("browse-btn"),
  batch: document.getElementById("batch"),
  batchStatus: document.getElementById("batch-status"),
  rows: document.getElementById("rows"),
  resetBtn: document.getElementById("reset-btn"),
  error: document.getElementById("error"),
  errorMsg: document.getElementById("error-msg"),
  retryBtn: document.getElementById("retry-btn"),
};

let objectUrls = [];  // track for revocation

function show(stateEl) {
  for (const el of [els.dropZone, els.batch, els.error]) {
    el.classList.toggle("hidden", el !== stateEl);
  }
}

function showError(message) {
  els.errorMsg.textContent = message;
  show(els.error);
}

function resetToIdle() {
  objectUrls.forEach(URL.revokeObjectURL);
  objectUrls = [];
  els.rows.innerHTML = "";
  els.fileInput.value = "";
  show(els.dropZone);
}

function extOf(name) {
  const i = name.lastIndexOf(".");
  return i === -1 ? "" : name.slice(i).toLowerCase();
}

function filenameFromDisposition(header, fallback) {
  if (!header) return fallback;
  const star = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (star) {
    try { return decodeURIComponent(star[1]); } catch (_) { /* fall through */ }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain ? plain[1] : fallback;
}

// Build a row element for one file; returns handles to update its status.
function makeRow(name) {
  const li = document.createElement("li");
  li.className = "row";
  li.innerHTML = `
    <span class="row-icon"><span class="dot" aria-hidden="true"></span></span>
    <span class="row-name"></span>
    <span class="row-detail"></span>`;
  li.querySelector(".row-name").textContent = name;
  els.rows.appendChild(li);
  return {
    li,
    icon: li.querySelector(".row-icon"),
    detail: li.querySelector(".row-detail"),
  };
}

function setRowState(row, state, html) {
  row.li.dataset.state = state;
  const icons = {
    queued: '<span class="dot" aria-hidden="true"></span>',
    processing: '<span class="spinner sm" aria-hidden="true"></span>',
    done: '<span class="ok" aria-hidden="true">✓</span>',
    error: '<span class="bad" aria-hidden="true">!</span>',
  };
  row.icon.innerHTML = icons[state] || "";
  row.detail.innerHTML = html || "";
}

const POLL_MS = 2500;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Poll the job until it finishes; returns {ok, warnings?, error?}.
// Slow is normal here: OCR-heavy files take minutes on the small server.
async function waitForJob(jobId, row) {
  const started = Date.now();
  for (;;) {
    await sleep(POLL_MS);
    let res;
    try {
      res = await fetch(`/jobs/${jobId}`);
    } catch (_) {
      continue; // transient network blip — keep polling
    }
    if (res.status === 404) return { ok: false, error: "Job expired — please retry" };
    let data = {};
    try { data = await res.json(); } catch (_) { /* keep polling */ }
    if (!res.ok) return { ok: false, error: data.error || data.detail || "Failed to process" };

    if (data.state === "done") return { ok: true, warnings: data.warnings || [] };
    const secs = Math.round((Date.now() - started) / 1000);
    const note = data.state === "queued" && data.queue_position > 0
      ? `Waiting (${data.queue_position} ahead)…`
      : `Removing PII… ${secs}s`;
    setRowState(row, "processing", note);
  }
}

// Validate one file locally; returns an error string or null.
function localError(file) {
  if (!ALLOWED_EXT.includes(extOf(file.name))) return "Unsupported type (need .docx/.pdf)";
  if (file.size > MAX_BYTES) return "Too large (max 25 MB)";
  return null;
}

async function redactOne(file, row) {
  const bad = localError(file);
  if (bad) { setRowState(row, "error", bad); return false; }

  setRowState(row, "processing", "Uploading…");
  const body = new FormData();
  body.append("file", file, file.name);

  let res;
  try {
    res = await fetch("/redact", { method: "POST", body });
  } catch (_) {
    setRowState(row, "error", "Network error");
    return false;
  }

  let submitted = {};
  try { submitted = await res.json(); } catch (_) { /* non-JSON */ }
  if (!res.ok || !submitted.job_id) {
    setRowState(row, "error", submitted.error || submitted.detail || "Failed to process");
    return false;
  }

  setRowState(row, "processing", "Removing PII…");
  const outcome = await waitForJob(submitted.job_id, row);
  if (!outcome.ok) {
    setRowState(row, "error", outcome.error);
    return false;
  }

  let dl;
  try {
    dl = await fetch(`/jobs/${submitted.job_id}/download`);
  } catch (_) {
    setRowState(row, "error", "Network error");
    return false;
  }
  if (!dl.ok) {
    setRowState(row, "error", "Download failed — please retry");
    return false;
  }

  const blob = await dl.blob();
  const fallback = file.name.replace(/(\.[^.]+)$/, " - Redacted$1");
  const outName = filenameFromDisposition(dl.headers.get("Content-Disposition"), fallback);
  const url = URL.createObjectURL(blob);
  objectUrls.push(url);

  const link = document.createElement("a");
  link.href = url;
  link.download = outName;
  link.className = "btn btn-sm";
  link.textContent = "Download";
  setRowState(row, "done", "");
  row.detail.appendChild(link);

  // Server-side warnings (e.g. text drawn as graphics that couldn't be scanned).
  for (const msg of outcome.warnings) {
    const warn = document.createElement("p");
    warn.className = "row-warning";
    warn.textContent = "⚠ " + msg;
    row.li.appendChild(warn);
  }
  return true;
}

async function runBatch(files) {
  if (MAX_FILES && files.length > MAX_FILES) {
    showError(`You selected ${files.length} files. Please upload at most ${MAX_FILES} at a time.`);
    return;
  }
  els.rows.innerHTML = "";
  show(els.batch);

  const rows = files.map((f) => makeRow(f.name));
  files.forEach((_, i) => setRowState(rows[i], "queued", "Queued"));

  els.batchStatus.textContent =
    files.length === 1 ? "Redacting 1 file…" : `Redacting ${files.length} files…`;

  let ok = 0;
  for (let i = 0; i < files.length; i++) {
    if (await redactOne(files[i], rows[i])) ok++;
  }

  const failed = files.length - ok;
  els.batchStatus.textContent =
    failed === 0
      ? `Done — ${ok} file${ok === 1 ? "" : "s"} redacted.`
      : `Done — ${ok} redacted, ${failed} failed.`;
}

function handleFiles(fileListLike) {
  const files = Array.from(fileListLike);
  if (files.length) runBatch(files);
}

// --- File picker ---
els.browseBtn.addEventListener("click", () => els.fileInput.click());
els.dropZone.addEventListener("click", (e) => {
  if (e.target === els.browseBtn) return;
  els.fileInput.click();
});
els.dropZone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); els.fileInput.click(); }
});
els.fileInput.addEventListener("change", () => handleFiles(els.fileInput.files));

// --- Drag & drop ---
["dragenter", "dragover"].forEach((evt) =>
  els.dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    els.dropZone.classList.add("dragover");
  })
);
["dragleave", "drop"].forEach((evt) =>
  els.dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    els.dropZone.classList.remove("dragover");
  })
);
els.dropZone.addEventListener("drop", (e) => handleFiles(e.dataTransfer.files));

// --- Reset / retry ---
els.resetBtn.addEventListener("click", resetToIdle);
els.retryBtn.addEventListener("click", resetToIdle);
