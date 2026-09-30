const csrf = document.querySelector('meta[name="csrf"]').content;
const priceEl = document.querySelector("#price");
const metaEl = document.querySelector("#meta");
const statusEl = document.querySelector("#status");
const aboveEl = document.querySelector("#above");
const belowEl = document.querySelector("#below");
const tokenEl = document.querySelector("#token");
const chatEl = document.querySelector("#chat");
const apiKeyEl = document.querySelector("#api-key");
const apiSecretEl = document.querySelector("#api-secret");
const pairEl = document.querySelector("#pair");
const chartSvg = document.querySelector("#chart");
let filled = false;
let shownSymbol = "";
let shownQuote = "USDT";
let chartInterval = "1h";
let chartLayout = null;

function showStatus(text, isError) {
  statusEl.textContent = text || "";
  statusEl.className = isError ? "err" : "note";
}

async function api(url, options) {
  const opts = options || {};
  const headers = Object.assign({ "X-CSRF-Token": csrf }, opts.headers || {});
  const response = await fetch(url, Object.assign({}, opts, { headers: headers, credentials: "same-origin" }));
  if (response.status === 401) location.href = "/login";
  return response;
}

function fillTable(body, rows, cells, emptyText) {
  const headers = Array.from(body.closest("table").querySelectorAll("thead th"), (th) => th.textContent);
  body.replaceChildren();
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = cells;
    td.textContent = emptyText || "Tidak ada data";
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    row.forEach((value, index) => {
      const td = document.createElement("td");
      td.dataset.label = headers[index] || "";
      const span = document.createElement("span");
      span.className = "cell-value";
      if (value && typeof value === "object") {
        span.textContent = value.text;
        if (value.className) span.classList.add(value.className);
      } else {
        span.textContent = value;
      }
      td.appendChild(span);
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
}

function renderAccount(account) {
  const fees = document.querySelector("#fees");
  if (!account || account.error) {
    fees.textContent = account && account.error ? account.error : "Data akun belum tersedia";
  } else {
    fees.textContent = "Maker " + account.maker_fee + " · Taker " + account.taker_fee + (account.updated_at ? " · " + account.updated_at : "");
  }
  const data = account || {};
  document.querySelector("#total-idr").textContent = data.total_idr || "…";
  document.querySelector("#idr-rate").textContent = data.idr_note || "";
  fillTable(document.querySelector("#balances"), (data.balances || []).map((row) => [row.asset, row.free, row.locked, row.value, row.idr || "-"]), 5);
  const sideLabel = (side) => side === "Beli" ? { text: side, className: "buy" } : side === "Jual" ? { text: side, className: "sell" } : side;
  const statusLabel = (status) => status === "Dibatalkan" ? { text: status, className: "cancelled" } : status;
  const orderCells = (row) => [row.time, sideLabel(row.side), row.type, row.price, row.quantity, row.filled, statusLabel(row.status)];
  fillTable(document.querySelector("#open-orders"), (data.open_orders || []).map(orderCells), 7);
  fillTable(document.querySelector("#order-history"), (data.order_history || []).map(orderCells), 7);
  fillTable(document.querySelector("#trades"), (data.trades || []).map((row) => [row.time, sideLabel(row.side), row.price, row.quantity, row.total, row.fee]), 6);
}

function renderPlan(plan) {
  document.querySelector("#plan-status").textContent = (plan && plan.status) || "";
  const list = document.querySelector("#plan-detail");
  list.replaceChildren();
  String((plan && plan.detail) || "").split("\n").filter(Boolean).forEach((line) => {
    const item = document.createElement("li");
    item.textContent = line;
    list.appendChild(item);
  });
}

async function refresh() {
  const response = await api("/api/status");
  if (!response.ok) return;
  const data = await response.json();
  const symbol = data.symbol || "HBAR/USDT";
  const quote = data.quote || "USDT";
  shownQuote = quote;
  priceEl.textContent = data.price_text === "-" ? "…" : data.price_text + " " + quote;
  const change = data.change_percent;
  priceEl.className = "price " + (change > 0 ? "up" : change < 0 ? "down" : "");
  const changeText = change === null || change === undefined ? "" : (change > 0 ? "+" : "") + change + "% dalam 24 jam";
  const range = "Tertinggi " + data.high_text + " · Terendah " + data.low_text;
  metaEl.textContent = data.ok ? [changeText, range, data.updated_at].filter(Boolean).join(" · ") : (data.error || "Harga belum tersedia");
  document.querySelector("#pair-title").textContent = symbol;
  document.title = symbol + " · Pemantau";
  chartSvg.setAttribute("aria-label", "Grafik harga " + symbol);
  document.querySelector("#open-orders-title").textContent = "Order " + symbol + " terbuka";
  document.querySelector("#order-history-title").textContent = "Riwayat order " + symbol;
  document.querySelector("#trades-title").textContent = "Riwayat transaksi " + symbol;
  document.querySelector("#trade-total").textContent = "Total " + quote;
  const chips = document.querySelector("#chips");
  chips.replaceChildren();
  [
    [data.telegram_ready ? "Telegram tersambung" : "Telegram belum diisi", data.telegram_ready],
    [data.tokocrypto_ready ? "API Tokocrypto tersimpan" : "API Tokocrypto belum diisi", data.tokocrypto_ready]
  ].forEach(([text, ok]) => {
    const chip = document.createElement("span");
    chip.className = "chip" + (ok ? " ok" : "");
    chip.textContent = text;
    chips.appendChild(chip);
  });
  if (!filled) {
    aboveEl.value = data.above ?? "";
    belowEl.value = data.below ?? "";
    chatEl.value = data.chat_id || "";
    pairEl.value = symbol;
    tokenEl.placeholder = data.telegram_ready ? "tersimpan " + data.telegram_hint : "belum diisi";
    apiKeyEl.placeholder = data.tokocrypto_ready ? "tersimpan " + data.tokocrypto_key_hint : "belum diisi";
    apiSecretEl.placeholder = data.tokocrypto_ready ? "tersimpan" : "belum diisi";
    filled = true;
  }
  if (symbol !== shownSymbol) {
    shownSymbol = symbol;
    loadChart(chartInterval);
  }
  renderAccount(data.account);
  renderPlan(data.plan);
  fillTable(
    document.querySelector("#notifications"),
    (data.notifications || []).map((row) => [
      row.time,
      { text: row.status, className: row.status === "Terkirim" ? "buy" : "sell" },
      { text: row.error ? row.text + "\n" + row.error : row.text, className: "notice" }
    ]),
    3,
    "Belum ada notifikasi"
  );
}

function svgEl(name, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
  return node;
}

function formatAxisPrice(price) {
  if (price >= 100) return price.toFixed(2);
  if (price >= 1) return price.toFixed(3);
  return price.toFixed(5);
}

function formatTipTime(ms, interval) {
  const date = new Date(ms);
  const zone = { timeZone: "Asia/Jakarta" };
  if (interval === "1d" || interval === "1w") {
    return date.toLocaleDateString("id-ID", Object.assign({ weekday: "short", day: "2-digit", month: "short", year: "numeric" }, zone));
  }
  return date.toLocaleString("id-ID", Object.assign({ day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }, zone));
}

function formatAxisTime(ms, interval) {
  const date = new Date(ms);
  const zone = { timeZone: "Asia/Jakarta" };
  if (interval === "1d" || interval === "1w") {
    return date.toLocaleDateString("id-ID", Object.assign({ day: "2-digit", month: "short" }, zone));
  }
  if (interval === "4h") {
    return date.toLocaleString("id-ID", Object.assign({ day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }, zone));
  }
  return date.toLocaleTimeString("id-ID", Object.assign({ hour: "2-digit", minute: "2-digit" }, zone));
}

function drawChart(candles, interval) {
  chartSvg.replaceChildren();
  const note = document.querySelector("#chart-note");
  if (!candles.length) {
    note.textContent = "Data grafik belum tersedia";
    return;
  }
  const narrow = window.matchMedia("(max-width: 800px)").matches;
  const width = narrow ? 390 : 800;
  const height = narrow ? 300 : 340;
  const left = narrow ? 6 : 12;
  const right = narrow ? 72 : 72;
  const top = narrow ? 10 : 16;
  const bottom = narrow ? 28 : 36;
  const font = narrow ? 15 : 12;
  chartSvg.setAttribute("viewBox", "0 0 " + width + " " + height);
  const plotW = width - left - right;
  const plotH = height - top - bottom;
  let min = Math.min.apply(null, candles.map((candle) => candle.low));
  let max = Math.max.apply(null, candles.map((candle) => candle.high));
  if (min === max) {
    min -= 0.001;
    max += 0.001;
  }
  const pad = (max - min) * 0.08;
  min -= pad;
  max += pad;
  const yOf = (price) => top + ((max - price) / (max - min)) * plotH;
  const slot = plotW / candles.length;
  const bodyW = Math.max(1.5, slot * 0.62);
  candles.forEach((candle, index) => {
    const x = left + index * slot + slot / 2;
    const rising = candle.close >= candle.open;
    const color = rising ? "#8fce6a" : "#e07a6a";
    chartSvg.appendChild(svgEl("line", { x1: x, x2: x, y1: yOf(candle.high), y2: yOf(candle.low), stroke: color, "stroke-width": 1 }));
    const topY = Math.min(yOf(candle.open), yOf(candle.close));
    const bodyH = Math.max(1, Math.abs(yOf(candle.close) - yOf(candle.open)));
    chartSvg.appendChild(svgEl("rect", { x: x - bodyW / 2, y: topY, width: bodyW, height: bodyH, fill: color }));
  });
  [max, (max + min) / 2, min].forEach((price) => {
    const label = svgEl("text", { x: width - 4, y: yOf(price) + 4, fill: "#b7b2a6", "font-size": font, "text-anchor": "end" });
    label.textContent = formatAxisPrice(price);
    chartSvg.appendChild(label);
  });
  const timeMarks = narrow ? [0, candles.length - 1] : [0, Math.floor((candles.length - 1) / 2), candles.length - 1];
  timeMarks.forEach((index, mark) => {
    const anchor = mark === 0 ? "start" : mark === timeMarks.length - 1 ? "end" : "middle";
    const label = svgEl("text", {
      x: left + index * slot + slot / 2,
      y: height - 10,
      fill: "#b7b2a6",
      "font-size": font,
      "text-anchor": anchor
    });
    label.textContent = formatAxisTime(candles[index].time, interval);
    chartSvg.appendChild(label);
  });
  const guide = svgEl("line", {
    x1: 0,
    x2: 0,
    y1: top,
    y2: height - bottom,
    stroke: "#efe8d8",
    "stroke-width": 1,
    "stroke-opacity": 0.45,
    "pointer-events": "none",
    visibility: "hidden"
  });
  guide.setAttribute("data-guide", "1");
  chartSvg.appendChild(guide);
  chartLayout = { candles: candles, interval: interval, left: left, top: top, bottom: bottom, slot: slot, width: width, height: height, narrow: narrow };
  const last = candles[candles.length - 1];
  note.textContent = candles.length + " candle · penutupan " + last.close + " " + shownQuote;
  hideChartTip();
}

function chartSvgX(event) {
  const rect = chartSvg.getBoundingClientRect();
  const layout = chartLayout;
  if (!rect.width || !rect.height || !layout) return null;
  return {
    x: ((event.clientX - rect.left) / rect.width) * layout.width,
    y: ((event.clientY - rect.top) / rect.height) * layout.height
  };
}

function candleIndexAt(point) {
  if (!chartLayout || !point) return -1;
  const layout = chartLayout;
  if (point.y < layout.top || point.y > layout.height - layout.bottom) return -1;
  const index = Math.floor((point.x - layout.left) / layout.slot);
  if (index < 0 || index >= layout.candles.length) return -1;
  return index;
}

function hideChartTip() {
  const tip = document.querySelector("#chart-tip");
  if (tip) tip.hidden = true;
  const guide = chartSvg.querySelector("[data-guide]");
  if (guide) guide.setAttribute("visibility", "hidden");
}

function showChartTip(index, event) {
  const layout = chartLayout;
  const candle = layout.candles[index];
  const tip = document.querySelector("#chart-tip");
  tip.replaceChildren();
  const when = document.createElement("p");
  when.className = "chart-tip-time";
  when.textContent = formatTipTime(candle.time, layout.interval);
  tip.appendChild(when);
  const rising = candle.close >= candle.open;
  [
    ["Buka", candle.open],
    ["Tertinggi", candle.high],
    ["Terendah", candle.low],
    ["Tutup", candle.close]
  ].forEach(([label, price]) => {
    const row = document.createElement("p");
    const name = document.createElement("span");
    name.textContent = label;
    const value = document.createElement("strong");
    value.textContent = formatAxisPrice(price) + " " + shownQuote;
    if (label === "Tutup") value.className = rising ? "up" : "down";
    row.appendChild(name);
    row.appendChild(value);
    tip.appendChild(row);
  });
  tip.hidden = false;
  const guide = chartSvg.querySelector("[data-guide]");
  if (guide) {
    const x = layout.left + index * layout.slot + layout.slot / 2;
    guide.setAttribute("x1", String(x));
    guide.setAttribute("x2", String(x));
    guide.setAttribute("visibility", "visible");
  }
  const narrowTip = window.matchMedia("(max-width: 800px)").matches;
  tip.classList.toggle("pinned", narrowTip);
  if (narrowTip) {
    tip.style.left = "8px";
    tip.style.right = "8px";
    tip.style.top = "8px";
    return;
  }
  tip.style.right = "";
  const wrap = chartSvg.parentElement.getBoundingClientRect();
  let leftPx = event.clientX - wrap.left + 16;
  let topPx = event.clientY - wrap.top + 16;
  if (leftPx + tip.offsetWidth > wrap.width - 8) leftPx = event.clientX - wrap.left - tip.offsetWidth - 16;
  if (topPx + tip.offsetHeight > wrap.height - 8) topPx = event.clientY - wrap.top - tip.offsetHeight - 12;
  tip.style.left = Math.max(8, leftPx) + "px";
  tip.style.top = Math.max(8, topPx) + "px";
}

function hoverChart(event) {
  const index = candleIndexAt(chartSvgX(event));
  if (index < 0) {
    hideChartTip();
    return;
  }
  showChartTip(index, event);
}

async function loadChart(interval) {
  chartInterval = interval;
  document.querySelectorAll("#timeframes button").forEach((button) => {
    button.classList.toggle("active", button.dataset.interval === interval);
  });
  const response = await api("/api/klines?interval=" + encodeURIComponent(interval));
  const data = await response.json();
  if (!response.ok) {
    document.querySelector("#chart-note").textContent = data.error || "Grafik gagal dimuat";
    return;
  }
  drawChart(data.candles || [], interval);
}

const settingsDialog = document.querySelector("#settings");
document.querySelector("#open-settings").addEventListener("click", () => settingsDialog.showModal());
document.querySelector("#close-settings").addEventListener("click", () => settingsDialog.close());
document.querySelector("#timeframes").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (button) loadChart(button.dataset.interval);
});
chartSvg.addEventListener("pointermove", (event) => {
  if (event.pointerType === "touch") return;
  hoverChart(event);
});
chartSvg.addEventListener("pointerleave", hideChartTip);
chartSvg.addEventListener("pointercancel", hideChartTip);
window.addEventListener("resize", () => {
  if (!chartLayout) return;
  const narrow = window.matchMedia("(max-width: 800px)").matches;
  if (narrow !== chartLayout.narrow) drawChart(chartLayout.candles, chartLayout.interval);
});
chartSvg.addEventListener("pointerdown", (event) => {
  if (event.pointerType === "mouse") return;
  hoverChart(event);
});
document.querySelector("#logout").addEventListener("click", async () => {
  await api("/logout", { method: "POST" });
  location.href = "/login";
});
document.querySelector("#save").addEventListener("click", async () => {
  const response = await api("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      above: aboveEl.value,
      below: belowEl.value,
      telegram_token: tokenEl.value,
      telegram_chat_id: chatEl.value,
      tokocrypto_api_key: apiKeyEl.value,
      tokocrypto_api_secret: apiSecretEl.value,
      pair: pairEl.value
    })
  });
  const data = await response.json();
  showStatus(data.message || data.error, !response.ok);
  if (response.ok) {
    tokenEl.value = "";
    apiKeyEl.value = "";
    apiSecretEl.value = "";
    filled = false;
    refresh();
  }
});
document.querySelector("#test").addEventListener("click", async () => {
  const response = await api("/api/test-telegram", { method: "POST" });
  const data = await response.json();
  showStatus(data.message || data.error, !response.ok);
  if (response.ok) refresh();
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
setInterval(refresh, 5000);
setInterval(() => loadChart(chartInterval), 30000);
