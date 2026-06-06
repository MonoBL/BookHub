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
  errorMsg.textContent = msg;
  errorMsg.hidden = false;
}

function statusLabel(job) {
  const s = job.status;
  if (s === "queued")      return "Uploading...";
  if (s === "converting")  return "Converting... (this can take several minutes)";
  if (s === "clean")       return null;
  if (s === "error")       return `Error: ${job.reason || "conversion failed"}`;
  return s;
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!navigator.onLine) { showError("No network connection."); return; }

  errorMsg.hidden = true;
  downloadArea.hidden = true;
  progressSection.hidden = false;
  btn.disabled = true;
  progressStatus.textContent = "Uploading...";

  const fd = new FormData(form);

  let jobId;
  try {
    const r = await api("/api/convert", { method: "POST", body: fd });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      showError(err.detail || `Upload failed (${r.status})`);
      btn.disabled = false;
      progressSection.hidden = true;
      return;
    }
    const data = await r.json();
    jobId = data.job_id;
  } catch (err) {
    showError(`Network error: ${err.message}`);
    btn.disabled = false;
    progressSection.hidden = true;
    return;
  }

  progressStatus.textContent = "OCR (if needed) → Converting...";

  await pollJob(jobId, (job) => {
    if (job.status === "clean") {
      progressStatus.textContent = "Done!";
      downloadLink.href = `/api/files/${jobId}`;
      downloadArea.hidden = false;
      btn.disabled = false;
    } else if (job.status === "error") {
      progressStatus.textContent = "";
      showError(`Conversion failed: ${job.reason || "unknown error"}`);
      btn.disabled = false;
    } else {
      const label = statusLabel(job);
      if (label) progressStatus.textContent = label;
    }
  });
});
