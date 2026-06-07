// Convert page: upload PDF, poll job, offer EPUB download.

const form = document.getElementById("convert-form");
const btn = document.getElementById("convert-btn");
const errorMsg = document.getElementById("error-msg");
const progressSection = document.getElementById("progress-section");
const progressStatus = document.getElementById("progress-status");
const downloadArea = document.getElementById("download-area");
const downloadLink = document.getElementById("download-link");

api("/api/admin/users").then((r) => {
  if (r.ok) document.getElementById("admin-link").hidden = false;
}).catch(() => {});

function showError(msg) {
  errorMsg.textContent = errText(msg);
  errorMsg.hidden = false;
}

// FastAPI returns validation errors as an array of {loc, msg, ...}. Render
// those as readable text instead of "[object Object]".
function errText(detail) {
  if (Array.isArray(detail)) {
    return detail.map((d) => (d && d.msg) ? d.msg : JSON.stringify(d)).join("; ");
  }
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  return detail || "Request failed";
}

function statusLabel(job) {
  const s = job.status;
  if (s === "queued")      return "Uploading…";
  if (s === "converting")  return "Converting… (this can take several minutes)";
  if (s === "clean")       return null;
  if (s === "error")       return `Error: ${job.reason || "conversion failed"}`;
  return s;
}

// --- Visual progress (spinner + animated bar + elapsed timer) ---
let _convStart = 0;
let _convTimer = null;
let _convLabel = "Uploading…";

function fmtElapsed() {
  const s = Math.floor((Date.now() - _convStart) / 1000);
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}m ${String(s % 60).padStart(2, "0")}s` : `${s}s`;
}

function renderProgress() {
  progressStatus.innerHTML =
    `<span class="spinner"></span>` +
    `<span class="stage">${_convLabel}</span>` +
    `<span class="elapsed"> · ${fmtElapsed()}</span>` +
    `<div class="progress" style="margin-top:10px"></div>`;
}

function setProgress(label) {
  _convLabel = label;
  renderProgress();
}

function startProgress() {
  _convStart = Date.now();
  _convLabel = "Uploading…";
  renderProgress();
  if (_convTimer) clearInterval(_convTimer);
  _convTimer = setInterval(renderProgress, 1000);
}

function stopProgress() {
  if (_convTimer) { clearInterval(_convTimer); _convTimer = null; }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!navigator.onLine) { showError("No network connection."); return; }

  errorMsg.hidden = true;
  downloadArea.hidden = true;
  progressSection.hidden = false;
  btn.disabled = true;
  startProgress();

  const fd = new FormData(form);

  let jobId;
  try {
    const r = await api("/api/convert", { method: "POST", body: fd });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      stopProgress();
      showError(err.detail || `Upload failed (${r.status})`);
      btn.disabled = false;
      progressSection.hidden = true;
      return;
    }
    const data = await r.json();
    jobId = data.job_id;
  } catch (err) {
    stopProgress();
    showError(`Network error: ${err.message}`);
    btn.disabled = false;
    progressSection.hidden = true;
    return;
  }

  setProgress("Converting… (OCR if needed)");

  await pollJob(jobId, (job) => {
    if (job.status === "clean") {
      stopProgress();
      progressStatus.innerHTML = `✅ Done in ${fmtElapsed()}`;
      downloadLink.href = `/api/files/${jobId}`;
      downloadArea.hidden = false;
      btn.disabled = false;
      loadPendingDownloads();
    } else if (job.status === "error") {
      stopProgress();
      progressStatus.textContent = "";
      showError(`Conversion failed: ${job.reason || "unknown error"}`);
      btn.disabled = false;
    } else {
      const label = statusLabel(job);
      if (label) setProgress(label);
    }
  });
});

// --- Images to BMP (e-reader) ---
const bmpForm = document.getElementById("bmp-form");
const bmpBtn = document.getElementById("bmp-btn");
const bmpError = document.getElementById("bmp-error");
const bmpProgressSection = document.getElementById("bmp-progress-section");
const bmpProgressStatus = document.getElementById("bmp-progress-status");
const bmpDownloadArea = document.getElementById("bmp-download-area");
const bmpDownloadLink = document.getElementById("bmp-download-link");

function bmpShowError(msg) {
  bmpError.textContent = errText(msg);
  bmpError.hidden = false;
}

if (bmpForm) {
  bmpForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!navigator.onLine) { bmpShowError("No network connection."); return; }

    bmpError.hidden = true;
    bmpDownloadArea.hidden = true;
    bmpProgressSection.hidden = false;
    bmpBtn.disabled = true;
    bmpProgressStatus.innerHTML =
      `<span class="spinner"></span><span class="stage">Converting images…</span>`;

    // Build the body explicitly so the unchecked grayscale box sends "false"
    // (an unchecked checkbox is omitted from a plain FormData).
    const fd = new FormData();
    for (const f of document.getElementById("bmp-files").files) fd.append("files", f);
    fd.append("width", document.getElementById("bmp-width").value || "480");
    fd.append("height", document.getElementById("bmp-height").value || "800");
    fd.append("grayscale", document.getElementById("bmp-grayscale").checked ? "true" : "false");

    let jobId;
    try {
      const r = await api("/api/convert-bmp", { method: "POST", body: fd });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        bmpShowError(err.detail || `Upload failed (${r.status})`);
        bmpBtn.disabled = false;
        bmpProgressSection.hidden = true;
        return;
      }
      jobId = (await r.json()).job_id;
    } catch (err) {
      bmpShowError(`Network error: ${err.message}`);
      bmpBtn.disabled = false;
      bmpProgressSection.hidden = true;
      return;
    }

    await pollJob(jobId, (job) => {
      if (job.status === "clean") {
        const ext = job.ext === "zip" ? "ZIP" : "BMP";
        bmpProgressStatus.innerHTML = `✅ Done — ${ext} ready`;
        bmpDownloadLink.href = `/api/files/${jobId}`;
        bmpDownloadArea.hidden = false;
        bmpBtn.disabled = false;
        loadPendingDownloads();
      } else if (job.status === "error") {
        bmpProgressStatus.textContent = "";
        bmpShowError(`Conversion failed: ${job.reason || "unknown error"}`);
        bmpBtn.disabled = false;
      }
    });
  });
}
