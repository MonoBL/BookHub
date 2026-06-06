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
  let resp;
  try {
    resp = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
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
// Starts at 1s, backs off to 4s during scanning. Cap ~7 min.
async function pollJob(jobId, onStatus) {
  const TERMINAL = new Set(["clean", "blocked", "unverified", "error", "consumed"]);
  let delay = 1000;
  let elapsed = 0;
  const MAX_MS = 7 * 60 * 1000;

  while (elapsed < MAX_MS) {
    await new Promise((r) => setTimeout(r, delay));
    elapsed += delay;

    if (document.hidden) {
      await new Promise((r) => {
        document.addEventListener("visibilitychange", r, { once: true });
      });
    }

    let resp;
    try {
      resp = await api(`/api/jobs/${jobId}`);
    } catch {
      continue;
    }

    if (resp.status === 404) {
      onStatus({ status: "lost" });
      return;
    }

    const job = await resp.json();
    onStatus(job);

    if (TERMINAL.has(job.status)) return;

    delay = job.status === "scanning" ? 4000 : Math.min(delay * 1.5, 4000);
  }

  onStatus({ status: "timeout" });
}

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
