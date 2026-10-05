const csrf = document.querySelector('meta[name="csrf"]').content;
const statusEl = document.querySelector("#status");
const cursorEl = document.querySelector("#cursor-key");
const tokenEl = document.querySelector("#token");
const chatEl = document.querySelector("#chat");
const githubEl = document.querySelector("#github-token");
const modeEl = document.querySelector("#model-mode");
const customEl = document.querySelector("#custom-model");
let filled = false;

function showStatus(text, isError) {
  statusEl.textContent = text || "";
  statusEl.className = isError ? "err" : "note";
}

function showPageStatus(text, isError) {
  const page = document.querySelector("#page-status");
  page.textContent = text || "";
  page.className = isError ? "err" : "note";
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

async function refresh() {
  const response = await api("/api/dashboard");
  if (!response.ok) return;
  const data = await response.json();
  document.querySelector("#auto-tokens").textContent = formatTokens(data.usage.auto.tokens);
  document.querySelector("#custom-tokens").textContent = formatTokens(data.usage.custom.tokens);
  document.querySelector("#auto-meta").textContent = data.usage.auto.runs + " pekerjaan" + formatCents(data.usage.auto.cents);
  document.querySelector("#custom-meta").textContent = data.usage.custom.runs + " pekerjaan" + formatCents(data.usage.custom.cents);
  const ready = [
    data.cursor_ready ? "Cursor tersimpan" : "Cursor belum diisi",
    data.telegram_ready ? "Telegram tersambung" : "Telegram belum lengkap"
  ];
  document.querySelector("#ready").textContent = ready.join(" · ") + (data.model_mode === "custom" ? " · model " + data.custom_model : " · model Auto");
  const job = document.querySelector("#job");
  if (data.job) {
    job.hidden = false;
    job.textContent = "Sedang mengerjakan " + data.job.repo + " sejak " + data.job.since + ".";
  } else {
    job.hidden = true;
  }
  fillTable(
    document.querySelector("#repos"),
    (data.repos || []).map((repo) => {
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
        showPageStatus(payload.message || payload.error, !result.ok);
        if (result.ok) refresh();
      });
      return [
        { label: "Repo", text: repo.full_name },
        { label: "Branch", text: repo.branch },
        { label: "Di STB", text: repo.local_path || "—" },
        { label: "", node: remove }
      ];
    }),
    4,
    "Belum ada repo"
  );
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
  if (!filled) {
    chatEl.value = data.chat_id || "";
    modeEl.value = data.model_mode || "auto";
    customEl.value = data.custom_model || "";
    cursorEl.placeholder = data.cursor_ready ? "tersimpan " + data.cursor_hint : "belum diisi";
    tokenEl.placeholder = data.telegram_ready ? "tersimpan " + data.telegram_hint : "belum diisi";
    githubEl.placeholder = data.github_ready ? "tersimpan " + data.github_hint : "opsional";
    filled = true;
  }
}

const settingsDialog = document.querySelector("#settings");
document.querySelector("#open-settings").addEventListener("click", () => settingsDialog.showModal());
document.querySelector("#close-settings").addEventListener("click", () => settingsDialog.close());
document.querySelector("#logout").addEventListener("click", async () => {
  await api("/logout", { method: "POST" });
  location.href = "/login";
});
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
  showPageStatus(data.message || data.error, !response.ok);
  if (response.ok) {
    document.querySelector("#repo-url").value = "";
    refresh();
  }
});
document.querySelector("#save").addEventListener("click", async () => {
  const response = await api("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      cursor_api_key: cursorEl.value,
      telegram_token: tokenEl.value,
      telegram_chat_id: chatEl.value,
      github_token: githubEl.value,
      model_mode: modeEl.value,
      custom_model: customEl.value
    })
  });
  const data = await response.json();
  showStatus(data.message || data.error, !response.ok);
  if (response.ok) {
    cursorEl.value = "";
    tokenEl.value = "";
    githubEl.value = "";
    filled = false;
    refresh();
  }
});
document.querySelector("#test").addEventListener("click", async () => {
  const response = await api("/api/test-telegram", { method: "POST" });
  const data = await response.json();
  showStatus(data.message || data.error, !response.ok);
});
document.querySelector("#save-password").addEventListener("click", async () => {
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
  showStatus(data.message || data.error, !response.ok);
  if (response.ok) {
    document.querySelector("#current-password").value = "";
    document.querySelector("#new-password").value = "";
    document.querySelector("#confirm-password").value = "";
  }
});

refresh();
setInterval(refresh, 15000);
