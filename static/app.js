"use strict";

const MAX_BYTES = 25 * 1024 * 1024;
const ALLOWED_EXT = [".docx", ".pdf"];

const els = {
  dropZone: document.getElementById("drop-zone"),
  fileInput: document.getElementById("file-input"),
  browseBtn: document.getElementById("browse-btn"),
  processing: document.getElementById("processing"),
  processingName: document.getElementById("processing-name"),
  done: document.getElementById("done"),
  downloadLink: document.getElementById("download-link"),
  resetBtn: document.getElementById("reset-btn"),
  error: document.getElementById("error"),
  errorMsg: document.getElementById("error-msg"),
  retryBtn: document.getElementById("retry-btn"),
};

let lastObjectUrl = null;

function show(stateEl) {
  for (const el of [els.dropZone, els.processing, els.done, els.error]) {
    el.classList.toggle("hidden", el !== stateEl);
  }
}

function showError(message) {
  els.errorMsg.textContent = message;
  show(els.error);
}

function resetToIdle() {
  if (lastObjectUrl) {
    URL.revokeObjectURL(lastObjectUrl);
    lastObjectUrl = null;
  }
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

async function uploadFile(file) {
  if (!ALLOWED_EXT.includes(extOf(file.name))) {
    showError("Unsupported file type. Please choose a .docx or .pdf file.");
    return;
  }
  if (file.size > MAX_BYTES) {
    showError("File too large. The maximum size is 25 MB.");
    return;
  }

  els.processingName.textContent = file.name;
  show(els.processing);

  const body = new FormData();
  body.append("file", file, file.name);

  let res;
  try {
    res = await fetch("/redact", { method: "POST", body });
  } catch (_) {
    showError("Network error. Please check your connection and try again.");
    return;
  }

  if (!res.ok) {
    let msg = "Something went wrong while processing the file.";
    try {
      const data = await res.json();
      if (data && data.error) msg = data.error;
      else if (data && data.detail) msg = data.detail;
    } catch (_) { /* non-JSON error body */ }
    showError(msg);
    return;
  }

  const blob = await res.blob();
  const fallback = file.name.replace(/(\.[^.]+)$/, " - Redacted$1");
  const outName = filenameFromDisposition(
    res.headers.get("Content-Disposition"), fallback
  );

  if (lastObjectUrl) URL.revokeObjectURL(lastObjectUrl);
  lastObjectUrl = URL.createObjectURL(blob);
  els.downloadLink.href = lastObjectUrl;
  els.downloadLink.download = outName;
  show(els.done);
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
els.fileInput.addEventListener("change", () => {
  if (els.fileInput.files.length) uploadFile(els.fileInput.files[0]);
});

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
els.dropZone.addEventListener("drop", (e) => {
  if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
});

// --- Reset / retry ---
els.resetBtn.addEventListener("click", resetToIdle);
els.retryBtn.addEventListener("click", resetToIdle);
