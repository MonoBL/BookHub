// Search page: search form, results, per-provider banners, job polling.

const form = document.getElementById("search-form");
const input = document.getElementById("search-input");
const resultsSection = document.getElementById("results-section");
const resultsBody = document.getElementById("results-body");
const resultsTitle = document.getElementById("results-title");
const bannersEl = document.getElementById("provider-banners");
const recentSection = document.getElementById("recent-section");
const recentBody = document.getElementById("recent-body");

// Show admin link if admin.
api("/api/admin/users").then((r) => {
  if (r.ok) document.getElementById("admin-link").hidden = false;
}).catch(() => {});

// Load recent downloads.
async function loadRecent() {
  try {
    const r = await api("/api/history");
    if (!r.ok) return;
    const rows = await r.json();
    if (!rows.length) {
      recentBody.innerHTML = "";
      recentSection.hidden = true;
      return;
    }
    recentBody.innerHTML = rows.map((h) => `
      <tr id="hist-${h.id}">
        <td>${esc(h.title || "—")}</td>
        <td><span class="badge badge-${esc(h.ext || "info")}">${esc(h.ext || "?")}</span></td>
        <td>${esc(h.source || "—")}</td>
        <td>${esc(h.verdict || "—")}</td>
        <td><button class="btn-del-hist" data-id="${h.id}" title="Remove">✕</button></td>
      </tr>`).join("");
    recentSection.hidden = false;

    document.querySelectorAll(".btn-del-hist").forEach((b) => {
      b.addEventListener("click", () => deleteHistory(Number(b.dataset.id)));
    });
  } catch {}
}
loadRecent();

async function deleteHistory(id) {
  try {
    const r = await api(`/api/history/${id}`, { method: "DELETE" });
    if (r.ok) {
      const row = document.getElementById(`hist-${id}`);
      if (row) row.remove();
      if (!recentBody.children.length) recentSection.hidden = true;
    }
  } catch {}
}

const clearBtn = document.getElementById("clear-history-btn");
if (clearBtn) {
  clearBtn.addEventListener("click", async () => {
    if (!confirm("Clear all history?")) return;
    try {
      const r = await api("/api/history", { method: "DELETE" });
      if (r.ok) {
        recentBody.innerHTML = "";
        recentSection.hidden = true;
      }
    } catch {}
  });
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function coverPlaceholder() {
  const span = document.createElement("span");
  span.className = "cover cover-empty";
  span.textContent = "📕";
  return span;
}

function fmtSize(bytes) {
  if (!bytes) return "";
  if (bytes > 1048576) return (bytes / 1048576).toFixed(1) + " MB";
  return (bytes / 1024).toFixed(0) + " KB";
}

function renderBanners(providers) {
  bannersEl.innerHTML = providers
    .filter((p) => p.status !== "ok" && p.status !== "disabled")
    .map((p) => `<div class="banner-warn">${esc(p.name)}: ${esc(p.note || p.status)}</div>`)
    .join("");
}

function statusLabel(job) {
  const s = job.status;
  if (s === "queued") return "Queued…";
  if (s === "downloading") return "Downloading…";
  if (s === "verifying") return "Verifying…";
  if (s === "scanning") return "Checking (VirusTotal)…";
  if (s === "clean") return null; // replaced with download button
  if (s === "blocked") return `🚫 Blocked: ${esc(job.reason || "")}`;
  if (s === "unverified") return `⚠️ Unverified: ${esc(job.reason || "")}`;
  if (s === "error") return `Error: ${esc(job.reason || "")}`;
  if (s === "lost") return "Job lost (server restarted) — retry";
  if (s === "timeout") return "Taking too long — check later";
  return s;
}

function vtSummary(job) {
  if (job.vt_total == null) return "";
  const mal = job.vt_malicious || 0;
  const susp = job.vt_suspicious || 0;
  const tot = job.vt_total || 0;
  let agePart = "";
  if (job.vt_analysis_date) {
    const d = new Date(job.vt_analysis_date);
    if (!isNaN(d.getTime())) {
      const days = Math.floor((Date.now() - d.getTime()) / 86400000);
      agePart = days <= 0 ? " · scanned today" : ` · scanned ${days}d ago`;
    }
  }
  const detections = mal + susp;
  const cls = detections > 0 ? "vt-bad" : "vt-good";
  const label = susp > 0 ? `${mal}+${susp}/${tot}` : `${mal}/${tot}`;
  return ` <span class="vt ${cls}" title="VirusTotal detections / engines">🛡 ${label}${agePart}</span>`;
}

function renderResult(r, idx) {
  const sources = (r.extra && r.extra.sources) ? r.extra.sources : [r.source];
  const srcBadges = sources.map((s) => `<span class="badge badge-${esc(s)}">${esc(s)}</span>`).join(" ");
  const activeWarn = r.extra && r.extra.has_active_content
    ? `<span class="badge badge-warn" title="Contains JS/remote refs">active content</span>`
    : "";

  const cover = r.cover_url
    ? `<img class="cover" loading="lazy" alt="" src="/api/cover?u=${encodeURIComponent(r.cover_url)}">`
    : coverPlaceholder().outerHTML;

  return `<tr id="row-${idx}">
    <td class="cover-cell">${cover}</td>
    <td>${esc(r.title)}</td>
    <td>${esc(r.author || "")}</td>
    <td>${esc(fmtSize(r.size_bytes))}</td>
    <td><span class="badge badge-${esc(r.ext)}">${esc(r.ext.toUpperCase())}</span>${activeWarn}</td>
    <td>${srcBadges}</td>
    <td><button class="btn-get" data-idx="${idx}">Get</button> <span class="job-status" id="status-${idx}"></span></td>
  </tr>`;
}

let _results = [];

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!navigator.onLine) return;

  const q = input.value.trim();
  if (!q) return;

  const ext = document.querySelector('input[name="ext"]:checked').value;
  const params = new URLSearchParams({ q });
  if (ext) params.set("ext", ext);

  resultsSection.hidden = true;
  bannersEl.innerHTML = '<div class="banner-info">Searching…</div>';

  try {
    const r = await api(`/api/search?${params}`);
    if (!r.ok) { bannersEl.innerHTML = ""; return; }
    const data = await r.json();

    _results = data.results;
    renderBanners(data.providers);

    resultsBody.innerHTML = _results.map((r, i) => renderResult(r, i)).join("");
    resultsTitle.textContent = `Results (${_results.length})`;
    resultsSection.hidden = _results.length === 0;

    if (_results.length === 0) {
      bannersEl.innerHTML += '<div class="banner-info">No results found.</div>';
    }

    // Attach Get buttons.
    document.querySelectorAll(".btn-get").forEach((btn) => {
      btn.addEventListener("click", () => startDownload(Number(btn.dataset.idx)));
    });

    // Swap broken/blocked cover images for the placeholder (CSP forbids inline onerror).
    document.querySelectorAll("img.cover").forEach((img) => {
      img.addEventListener("error", () => img.replaceWith(coverPlaceholder()));
    });
  } catch (err) {
    bannersEl.innerHTML = `<div class="banner-warn">Search failed: ${esc(err.message)}</div>`;
  }
});

async function startDownload(idx) {
  const result = _results[idx];
  const statusEl = document.getElementById(`status-${idx}`);
  const btn = document.querySelector(`[data-idx="${idx}"]`);
  btn.disabled = true;
  statusEl.textContent = "Starting…";

  let jobId;
  try {
    const r = await api("/api/download", {
      method: "POST",
      body: JSON.stringify({ result }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      statusEl.textContent = `Error: ${esc(err.detail || r.status)}`;
      btn.disabled = false;
      return;
    }
    const data = await r.json();
    jobId = data.job_id;
  } catch (err) {
    statusEl.textContent = `Error: ${esc(err.message)}`;
    btn.disabled = false;
    return;
  }

  const IN_PROGRESS = new Set(["queued", "downloading", "verifying", "scanning"]);

  await pollJob(jobId, (job) => {
    if (job.status === "clean") {
      const activeWarn = job.has_active_content
        ? ` <span class="badge badge-warn">active content</span>`
        : "";
      statusEl.innerHTML =
        `<a href="/api/files/${jobId}" class="btn-download">✅ Download</a>` +
        vtSummary(job) + activeWarn;
    } else if (IN_PROGRESS.has(job.status)) {
      const label = statusLabel(job);
      statusEl.innerHTML =
        `<span class="spinner"></span><span class="stage">${esc(label)}</span>` +
        `<span class="progress"></span>`;
    } else {
      const label = statusLabel(job);
      statusEl.innerHTML = `${esc(label || "")}${vtSummary(job)}`;
    }
  });
}
