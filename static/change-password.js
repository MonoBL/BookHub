// Change-password logic (extracted from inline script for a strict CSP).
(function () {
  const form = document.getElementById("change-password-form");
  const errorEl = document.getElementById("error-msg");
  const btn = document.getElementById("submit-btn");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.hidden = true;

    const newPw = document.getElementById("new-password").value;
    const confirmPw = document.getElementById("confirm-password").value;

    if (newPw !== confirmPw) {
      errorEl.textContent = "Passwords do not match.";
      errorEl.hidden = false;
      return;
    }

    btn.disabled = true;

    try {
      const resp = await fetch("/api/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          current_password: document.getElementById("current-password").value,
          new_password: newPw,
        }),
      });

      if (resp.ok) {
        window.location.href = "/";
      } else {
        const err = await resp.json().catch(() => ({}));
        errorEl.textContent = err.detail || "Failed to change password.";
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
