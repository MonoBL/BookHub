// Login page logic (extracted from inline script for a strict CSP).
(function () {
  const form = document.getElementById("login-form");
  const errorEl = document.getElementById("error-msg");
  const btn = document.getElementById("submit-btn");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.hidden = true;
    btn.disabled = true;

    const body = {
      username: document.getElementById("username").value,
      password: document.getElementById("password").value,
    };

    try {
      const resp = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      if (resp.ok) {
        const data = await resp.json();
        window.location.href = data.must_change_password ? "/change-password" : "/";
      } else {
        const err = await resp.json().catch(() => ({}));
        if (resp.status === 429) {
          errorEl.textContent = "Too many attempts — try again in a minute.";
        } else {
          errorEl.textContent = err.detail || "Invalid credentials.";
        }
        errorEl.hidden = false;
        btn.disabled = false;
      }
    } catch {
      errorEl.textContent = "Network error — check your connection.";
      errorEl.hidden = false;
      btn.disabled = false;
    }
  });
})();
