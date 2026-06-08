// Shared utilities. Loaded on every page.

// Offline banner
(function () {
  const banner = document.getElementById("offline-banner");
  if (!banner) return;
  function update() {
    banner.style.display = navigator.onLine ? "none" : "block";
  }
  window.addEventListener("online", update);
  window.addEventListener("offline", update);
  update();
})();

// Central fetch wrapper. Redirects to /login on 401. Throws on error.
async function api(path, options = {}) {
  const opts = { ...options };
  const headers = { ...(options.headers || {}) };
  // Only label string bodies as JSON. For FormData (file uploads) the browser
  // must set multipart/form-data with its own boundary — never override it.
  if (
    opts.body &&
    typeof opts.body === "string" &&
    !("Content-Type" in headers)
  ) {
    headers["Content-Type"] = "application/json";
  }
  opts.headers = headers;

  let resp;
  try {
    resp = await fetch(path, opts);
  } catch (e) {
    throw new Error("Network unreachable");
  }
  if (resp.status === 401) {
    window.location.href = "/login";
    throw new Error("Not authenticated");
  }
  return resp;
}

// Job polling helper.
// Calls onStatus(job) on each poll. Stops on terminal status.
// Starts at 400ms, backs off to 5s on long phases. Cap ~70 min so comic-mode
// conversions (OCR every page) have time to finish.
async function pollJob(jobId, onStatus) {
  const TERMINAL = new Set(["clean", "blocked", "unverified", "error", "consumed", "external"]);
  // Poll fast at first for snappy feedback, then ease off.
  let delay = 400;
  let elapsed = 0;
  const MAX_MS = 70 * 60 * 1000;

  while (elapsed < MAX_MS) {
    if (document.hidden) {
      await new Promise((r) => {
        document.addEventListener("visibilitychange", r, { once: true });
      });
    }

    let resp;
    try {
      resp = await api(`/api/jobs/${jobId}`);
    } catch {
      await new Promise((r) => setTimeout(r, delay));
      elapsed += delay;
      continue;
    }

    if (resp.status === 404) {
      onStatus({ status: "lost" });
      return;
    }

    const job = await resp.json();
    onStatus(job);

    if (TERMINAL.has(job.status)) return;

    await new Promise((r) => setTimeout(r, delay));
    elapsed += delay;
    // Ramp 400ms up; long phases (scanning, converting) cap higher to avoid
    // hammering the server over minutes-long jobs.
    const longPhase = job.status === "scanning" || job.status === "converting";
    delay = Math.min(delay * 1.4, longPhase ? 5000 : 1500);
  }

  onStatus({ status: "timeout" });
}

// HTML-escape helper (app.js loads before page scripts on every page).
function escHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// Pending downloads: files this user prepared but hasn't grabbed yet. They are
// kept ~30 min then auto-deleted, so we surface them on page load and refresh.
// Renders into #downloads-section if that element exists on the page.
async function loadPendingDownloads() {
  const section = document.getElementById("downloads-section");
  if (!section) return;
  let rows;
  try {
    const r = await api("/api/downloads");
    if (!r.ok) return;
    rows = await r.json();
  } catch {
    return;
  }
  if (!rows.length) {
    section.hidden = true;
    section.innerHTML = "";
    return;
  }
  const items = rows.map((d) => {
    let exp = "";
    if (d.expires_at) {
      const mins = Math.max(0, Math.round((new Date(d.expires_at) - Date.now()) / 60000));
      exp = mins > 0 ? ` · expires in ${mins} min` : " · expiring now";
    }
    const fmt = escHtml((d.ext || "file").toUpperCase());
    return `<div class="dl-item">
      <span class="dl-title">${escHtml(d.title || "Download")}</span>
      <span class="badge badge-info">${fmt}</span>
      <a href="${escHtml(d.download_url)}" class="btn-download">⬇ Download</a>
      <span class="dl-exp">${exp}</span>
    </div>`;
  }).join("");
  section.innerHTML =
    `<div class="banner-info" style="border-color:var(--ok);color:var(--text)">
       <strong>You have ${rows.length} download${rows.length > 1 ? "s" : ""} waiting.</strong>
       Grab them before they auto-delete.
     </div>${items}`;
  section.hidden = false;
}

// Refresh the waiting list when a download link is clicked (file is consumed
// and deleted server-side after the browser fetches it).
document.addEventListener("click", (e) => {
  const a = e.target.closest("#downloads-section a.btn-download");
  if (a) setTimeout(loadPendingDownloads, 2500);
});

// Initial load + periodic refresh (TTL countdown / consumed cleanup).
loadPendingDownloads();
setInterval(loadPendingDownloads, 30000);

// Register service worker
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

// Logout
(function () {
  const link = document.getElementById("logout-link");
  if (!link) return;
  link.addEventListener("click", async (e) => {
    e.preventDefault();
    await api("/api/auth/logout", { method: "POST" }).catch(() => {});
    window.location.href = "/login";
  });
})();
