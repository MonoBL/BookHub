// Admin panel: users (create/delete/reset), provider health, VT quota, events.

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? esc(iso) : d.toLocaleString();
}

// --- Users ---
const usersBody = document.getElementById("users-body");

async function loadUsers() {
  try {
    const r = await api("/api/admin/users");
    if (!r.ok) return;
    const users = await r.json();
    usersBody.innerHTML = users.map((u) => `
      <tr id="user-${u.id}">
        <td>${esc(u.username)}</td>
        <td>${u.is_admin ? "✓" : ""}</td>
        <td>${fmtTime(u.created_at)}${u.must_change_password ? ' <span class="badge badge-warn">must change</span>' : ""}</td>
        <td style="white-space:nowrap">
          <button class="btn-reset" data-id="${u.id}" data-name="${esc(u.username)}">Reset password</button>
          <button class="btn-danger btn-del-user" data-id="${u.id}" data-name="${esc(u.username)}">Delete</button>
        </td>
      </tr>`).join("");

    usersBody.querySelectorAll(".btn-del-user").forEach((b) =>
      b.addEventListener("click", () => deleteUser(b.dataset.id, b.dataset.name)));
    usersBody.querySelectorAll(".btn-reset").forEach((b) =>
      b.addEventListener("click", () => resetPassword(b.dataset.id, b.dataset.name)));
  } catch {}
}

async function deleteUser(id, name) {
  if (!confirm(`Delete user "${name}"? This cannot be undone.`)) return;
  const r = await api(`/api/admin/users/${id}`, { method: "DELETE" });
  if (r.ok) loadUsers();
  else alert((await r.json().catch(() => ({}))).detail || "Delete failed");
}

async function resetPassword(id, name) {
  const pw = prompt(`New password for "${name}" (min 8 chars).\nThey will be forced to change it on next login.`);
  if (pw === null) return;
  if (pw.length < 8) { alert("Password must be at least 8 characters."); return; }
  const r = await api(`/api/admin/users/${id}/reset-password`, {
    method: "POST",
    body: JSON.stringify({ new_password: pw }),
  });
  if (r.ok) { alert("Password reset. User must change it on next login."); loadUsers(); }
  else alert((await r.json().catch(() => ({}))).detail || "Reset failed");
}

// --- Create user ---
const createForm = document.getElementById("create-user-form");
const createError = document.getElementById("create-error");

createForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  createError.hidden = true;

  const body = {
    username: document.getElementById("new-username").value.trim(),
    password: document.getElementById("new-password").value,
    is_admin: document.getElementById("new-is-admin").checked,
    must_change_password: document.getElementById("new-must-change").checked,
  };

  if (!body.username) { createError.textContent = "Username required."; createError.hidden = false; return; }
  if (body.password.length < 8) { createError.textContent = "Password must be at least 8 characters."; createError.hidden = false; return; }

  const r = await api("/api/admin/users", { method: "POST", body: JSON.stringify(body) });
  if (r.ok) {
    document.getElementById("new-username").value = "";
    document.getElementById("new-password").value = "";
    document.getElementById("new-is-admin").checked = false;
    document.getElementById("new-must-change").checked = true;
    loadUsers();
  } else {
    createError.textContent = (await r.json().catch(() => ({}))).detail || "Create failed.";
    createError.hidden = false;
  }
});

// --- Provider health ---
async function loadProviders() {
  const el = document.getElementById("provider-health");
  try {
    const r = await api("/api/admin/providers");
    if (!r.ok) { el.textContent = "Failed to load."; return; }
    const providers = await r.json();
    el.innerHTML = providers.map((p) => {
      const state = p.enabled ? '<span class="status-ok">enabled</span>' : '<span class="status-muted">disabled</span>';
      const note = p.note ? ` — <span class="status-warn">${esc(p.note)}</span>` : "";
      return `<div style="padding:0.3rem 0"><strong>${esc(p.name)}</strong>: ${state}${note}</div>`;
    }).join("");
  } catch { el.textContent = "Failed to load."; }
}

// --- VT quota ---
async function loadVtQuota() {
  const el = document.getElementById("vt-quota");
  try {
    const r = await api("/api/admin/vt-quota");
    if (!r.ok) { el.textContent = "Failed to load."; return; }
    const q = await r.json();
    if (!q.configured) { el.innerHTML = '<span class="status-warn">No VT_API_KEY set — downloads are blocked (unverified).</span>'; return; }
    el.innerHTML = `<strong>${q.remaining}</strong> / ${q.cap} requests left today`;
  } catch { el.textContent = "Failed to load."; }
}

// --- Events ---
async function loadEvents() {
  const body = document.getElementById("events-body");
  try {
    const r = await api("/api/admin/events?limit=100");
    if (!r.ok) return;
    const events = await r.json();
    body.innerHTML = events.map((ev) => `
      <tr>
        <td style="white-space:nowrap">${fmtTime(ev.ts)}</td>
        <td><span class="badge badge-info">${esc(ev.kind)}</span></td>
        <td>${esc(ev.title || "—")}</td>
        <td>${esc(ev.detail || ev.verdict || "—")}</td>
      </tr>`).join("");
  } catch {}
}

// --- VK token (legacy; VK is deprecated but the override still exists) ---
const vkForm = document.getElementById("vk-token-form");
if (vkForm) {
  vkForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const token = document.getElementById("vk-token-input").value.trim();
    if (!token) return;
    const r = await api("/api/admin/vk-token", { method: "POST", body: JSON.stringify({ token }) });
    alert(r.ok ? "VK token saved (note: VK search is deprecated)." : "Failed to save token.");
    if (r.ok) document.getElementById("vk-token-input").value = "";
    loadProviders();
  });
}

// Initial load.
loadUsers();
loadProviders();
loadVtQuota();
loadEvents();
