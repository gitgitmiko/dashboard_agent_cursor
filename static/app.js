const csrf = document.querySelector('meta[name="csrf"]').content;

function showStatus(id, text, isError) {
  const el = document.querySelector(id);
  if (!el) return;
  el.textContent = text || "";
  el.className = isError ? "err" : "note";
}

async function api(url, options) {
  const opts = options || {};
  const headers = Object.assign({ "X-CSRF-Token": csrf }, opts.headers || {});
  const response = await fetch(url, Object.assign({}, opts, { headers: headers, credentials: "same-origin" }));
  if (response.status === 401) location.href = "/login";
  return response;
}

function fillTable(body, rows, columns, emptyText) {
  body.replaceChildren();
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = columns;
    td.textContent = emptyText;
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    row.forEach((value) => {
      const td = document.createElement("td");
      td.dataset.label = value.label;
      const span = document.createElement("span");
      span.className = "cell-value";
      if (value.node) span.appendChild(value.node);
      else span.textContent = value.text;
      td.appendChild(span);
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
}

function formatTokens(value) {
  return new Intl.NumberFormat("id-ID").format(value || 0);
}

function formatCents(value) {
  const cents = Number(value || 0);
  if (!cents) return "";
  return " · $" + (cents / 100).toFixed(2);
}

function dash(value) {
  return value || "—";
}

async function readDashboard() {
  const response = await api("/api/dashboard");
  if (!response.ok) return null;
  return response.json();
}

function repoRows(repos, withRemove) {
  return (repos || []).map((repo) => {
    const row = [
      { label: "Repo", text: repo.full_name },
      { label: "Branch", text: repo.branch },
      { label: "Di STB", text: dash(repo.local_path) }
    ];
    if (withRemove) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "link";
      remove.textContent = "Hapus";
      remove.addEventListener("click", async () => {
        const result = await api("/api/repos/remove", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: repo.id })
        });
        const payload = await result.json();
        showStatus("#repo-status", payload.message || payload.error, !result.ok);
        if (result.ok) refreshSettings();
      });
      row.push({ label: "Layanan", text: dash(repo.service) });
      row.push({ label: "", node: remove });
    }
    return row;
  });
}

function modelLine(model) {
  if (model === "auto") return "model Auto";
  if (model) return "model " + model;
  return "model belum dipilih";
}

async function refreshHome() {
  const data = await readDashboard();
  if (!data) return;
  document.querySelector("#auto-tokens").textContent = formatTokens(data.usage.auto.tokens);
  document.querySelector("#custom-tokens").textContent = formatTokens(data.usage.custom.tokens);
  document.querySelector("#auto-meta").textContent = data.usage.auto.runs + " pekerjaan" + formatCents(data.usage.auto.cents);
  document.querySelector("#custom-meta").textContent = data.usage.custom.runs + " pekerjaan" + formatCents(data.usage.custom.cents);
  const ready = [
    data.cursor_ready ? "Cursor tersimpan" : "Cursor belum diisi",
    data.telegram_ready ? "Telegram tersambung" : "Telegram belum lengkap"
  ];
  document.querySelector("#ready").textContent = ready.join(" · ") + " · " + modelLine(data.selected_model);
  const job = document.querySelector("#job");
  if (data.job) {
    job.hidden = false;
    job.textContent = "Sedang mengerjakan " + data.job.repo + " sejak " + data.job.since + ".";
  } else {
    job.hidden = true;
  }
  fillTable(document.querySelector("#repos"), repoRows(data.repos, false), 3, "Belum ada repo. Tambah dari Pengaturan.");
  fillTable(
    document.querySelector("#runs"),
    (data.runs || []).map((run) => [
      { label: "Waktu", text: run.time },
      { label: "Repo", text: run.repo },
      { label: "Mode", text: run.mode === "auto" ? "Auto" : run.model },
      { label: "Token", text: formatTokens(run.total_tokens) },
      { label: "Status", text: run.status }
    ]),
    5,
    "Belum ada pekerjaan"
  );
}

let settingsFilled = false;

async function refreshSettings() {
  const data = await readDashboard();
  if (!data) return;
  fillTable(document.querySelector("#repos"), repoRows(data.repos, true), 5, "Belum ada repo");
  if (!settingsFilled) {
    document.querySelector("#chat").value = data.chat_id || "";
    document.querySelector("#cursor-key").placeholder = data.cursor_ready ? "tersimpan " + data.cursor_hint : "belum diisi";
    document.querySelector("#token").placeholder = data.telegram_ready ? "tersimpan " + data.telegram_hint : "belum diisi";
    document.querySelector("#github-token").placeholder = data.github_ready ? "tersimpan " + data.github_hint : "opsional";
    settingsFilled = true;
  }
}

const logout = document.querySelector("#logout");
if (logout) {
  logout.addEventListener("click", async () => {
    await api("/logout", { method: "POST" });
    location.href = "/login";
  });
}

if (document.querySelector("#auto-tokens")) {
  refreshHome();
  setInterval(refreshHome, 15000);
}

if (document.querySelector("#save")) {
  refreshSettings();
  setInterval(refreshSettings, 15000);
  document.querySelector("#repo-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const response = await api("/api/repos", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: document.querySelector("#repo-url").value,
        branch: document.querySelector("#repo-branch").value,
        local_path: document.querySelector("#repo-path").value,
        service: document.querySelector("#repo-service").value
      })
    });
    const data = await response.json();
    showStatus("#repo-status", data.message || data.error, !response.ok);
    if (response.ok) {
      document.querySelector("#repo-url").value = "";
      document.querySelector("#repo-path").value = "";
      document.querySelector("#repo-service").value = "";
      refreshSettings();
    }
  });
  document.querySelector("#save").addEventListener("click", async () => {
    const response = await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        cursor_api_key: document.querySelector("#cursor-key").value,
        telegram_token: document.querySelector("#token").value,
        telegram_chat_id: document.querySelector("#chat").value,
        github_token: document.querySelector("#github-token").value
      })
    });
    const data = await response.json();
    showStatus("#credential-status", data.message || data.error, !response.ok);
    if (response.ok) {
      document.querySelector("#cursor-key").value = "";
      document.querySelector("#token").value = "";
      document.querySelector("#github-token").value = "";
      settingsFilled = false;
      refreshSettings();
    }
  });
  document.querySelector("#test").addEventListener("click", async () => {
    const response = await api("/api/test-telegram", { method: "POST" });
    const data = await response.json();
    showStatus("#credential-status", data.message || data.error, !response.ok);
  });
  document.querySelector("#password-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const response = await api("/api/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        current: document.querySelector("#current-password").value,
        new: document.querySelector("#new-password").value,
        confirm: document.querySelector("#confirm-password").value
      })
    });
    const data = await response.json();
    showStatus("#password-status", data.message || data.error, !response.ok);
    if (response.ok) event.target.reset();
  });
}
