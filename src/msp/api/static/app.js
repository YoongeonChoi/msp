const state = {
  status: null,
};

function key(prefix) {
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

function money(value) {
  return new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 }).format(value);
}

function renderStatus(data) {
  state.status = data;
  document.querySelector("#mode").textContent = data.system.mode;
  document.querySelector("#version").textContent = `v${data.system.state_version}`;
  document.querySelector("#account").textContent = data.system.account_id;
  document.querySelector("#trading").textContent = String(data.system.trading_enabled);
  document.querySelector("#open-orders").textContent = data.execution.open_orders;
  document.querySelector("#unknown-orders").textContent = data.execution.unknown_orders;
  const krw = data.broker.cash.find((row) => row.currency === "KRW");
  document.querySelector("#cash").textContent = krw ? money(krw.balance) : "0";
}

function renderOrders(rows) {
  const body = document.querySelector("#orders");
  body.innerHTML = rows
    .map(
      (row) => `
        <tr>
          <td>${row.symbol}</td>
          <td>${row.side}</td>
          <td>${row.quantity}</td>
          <td>${money(row.limit_price)}</td>
          <td>${row.status}</td>
        </tr>
      `,
    )
    .join("");
}

function renderAudit(rows) {
  const box = document.querySelector("#audit");
  box.innerHTML = rows
    .slice(0, 12)
    .map(
      (row) => `
        <div class="audit-item">
          <strong>${row.event_type}</strong>
          <span>${row.created_at}</span>
        </div>
      `,
    )
    .join("");
}

function pct(value) {
  return Number(value).toFixed(3);
}

function renderScores(rows) {
  const body = document.querySelector("#scores");
  body.innerHTML = rows
    .slice(0, 8)
    .map(
      (row) => `
        <tr>
          <td>${row.symbol}</td>
          <td>${row.as_of_date}</td>
          <td>${pct(row.momentum_score)}</td>
          <td>${pct(row.quality_score)}</td>
          <td>${pct(row.total_score)}</td>
        </tr>
      `,
    )
    .join("");
}

function renderRebalances(rows) {
  const box = document.querySelector("#rebalances");
  box.innerHTML = rows
    .slice(0, 8)
    .map(
      (row) => `
        <div class="audit-item">
          <strong>${row.status} · ${money(row.total_notional)}</strong>
          <span>${row.id}</span>
          <span>${row.portfolio_hash}</span>
        </div>
      `,
    )
    .join("");
}

async function refresh() {
  const [status, orders, audit, scores, rebalances] = await Promise.all([
    api("/v1/status"),
    api("/v1/orders"),
    api("/v1/audit-events"),
    api("/v1/research-scores"),
    api("/v1/rebalances"),
  ]);
  renderStatus(status);
  renderOrders(orders);
  renderAudit(audit);
  renderScores(scores);
  renderRebalances(rebalances);
}

async function command(name) {
  const version = state.status?.system?.state_version;
  if (name === "arm") {
    await api("/v1/commands/arm", {
      method: "POST",
      headers: { "Idempotency-Key": key("arm") },
      body: JSON.stringify({
        target_mode: "PAPER",
        expected_state_version: version,
        reason: "dashboard paper arm",
      }),
    });
  }
  if (name === "halt") {
    await api("/v1/commands/halt", {
      method: "POST",
      headers: { "Idempotency-Key": key("halt") },
      body: JSON.stringify({
        expected_state_version: version,
        reason: "dashboard halt",
      }),
    });
  }
  if (name === "kill") {
    await api("/v1/commands/kill", {
      method: "POST",
      headers: { "Idempotency-Key": key("kill") },
      body: JSON.stringify({
        mode: "CANCEL_OPEN_ORDERS",
        expected_state_version: version,
        reason: "dashboard kill",
      }),
    });
  }
  if (name === "unlock") {
    await api("/v1/commands/unlock", {
      method: "POST",
      headers: { "Idempotency-Key": key("unlock") },
      body: JSON.stringify({
        expected_state_version: version,
        confirmation_phrase: "I_ACCEPT_MANUAL_RECOVERY",
        reason: "dashboard unlock",
      }),
    });
  }
  await refresh();
}

async function pipeline(action) {
  if (action === "data") {
    await api("/v1/demo/run-data-once", { method: "POST", body: "{}" });
  }
  if (action === "research") {
    await api("/v1/demo/run-research-once", { method: "POST", body: "{}" });
  }
  if (action === "rebalance") {
    await api("/v1/demo/run-portfolio-once", { method: "POST", body: "{}" });
  }
  if (action === "approve") {
    const rebalances = await api("/v1/rebalances");
    const latest = rebalances.find((row) => row.status === "PROPOSED");
    if (!latest) {
      throw new Error("No proposed rebalance");
    }
    await api(`/v1/rebalances/${latest.id}/approve`, {
      method: "POST",
      headers: { "Idempotency-Key": key("approve-rebalance") },
      body: JSON.stringify({
        portfolio_hash: latest.portfolio_hash,
        max_notional: latest.total_notional,
        max_slippage_bps: 30,
        create_orders: true,
      }),
    });
  }
  await refresh();
}

document.querySelector("#refresh").addEventListener("click", () => refresh());

document.querySelectorAll("[data-command]").forEach((button) => {
  button.addEventListener("click", async () => {
    try {
      await command(button.dataset.command);
    } catch (error) {
      alert(error.message);
    }
  });
});

document.querySelectorAll("[data-pipeline]").forEach((button) => {
  button.addEventListener("click", async () => {
    try {
      await pipeline(button.dataset.pipeline);
    } catch (error) {
      alert(error.message);
    }
  });
});

document.querySelector("#order-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const payload = {
    symbol: form.get("symbol"),
    side: form.get("side"),
    quantity: Number(form.get("quantity")),
    limit_price: Number(form.get("limit_price")),
    currency: "KRW",
    approved: true,
    max_notional: Number(form.get("quantity")) * Number(form.get("limit_price")),
    max_slippage_bps: 30,
  };
  try {
    await api("/v1/order-intents", {
      method: "POST",
      headers: { "Idempotency-Key": key("intent") },
      body: JSON.stringify(payload),
    });
    await refresh();
  } catch (error) {
    alert(error.message);
  }
});

refresh();
setInterval(refresh, 5000);
