// First-run setup logic (extracted from inline script for a strict CSP).
(function () {
  const form = document.getElementById("setup-form");
  const errorEl = document.getElementById("error-msg");
  const btn = document.getElementById("submit-btn");

  function fail(msg) {
    errorEl.textContent = msg;
    errorEl.hidden = false;
    btn.disabled = false;
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.hidden = true;
    btn.disabled = true;

    const username = document.getElementById("username").value.trim();
    const password = document.getElementById("password").value;
    const confirm = document.getElementById("confirm").value;

    if (password.length < 12) return fail("Password must be at least 12 characters.");
    if (password !== confirm) return fail("Passwords do not match.");

    try {
      const resp = await fetch("/api/auth/setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (resp.ok) {
        window.location.href = "/";
      } else {
        const err = await resp.json().catch(() => ({}));
        fail(err.detail || "Setup failed.");
      }
    } catch {
      fail("Network error — check your connection.");
    }
  });
})();
