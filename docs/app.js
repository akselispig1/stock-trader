/* AI Stockbroker dashboard.
 * Reads the bot's committed data (docs/data/*), and optionally talks directly
 * to Alpaca (real-time account, approve orders) and Anthropic (in-browser
 * research). All keys live only in this browser's localStorage. */

const ALPACA_PAPER = "https://paper-api.alpaca.markets";
const ALPACA_LIVE = "https://api.alpaca.markets";
const ANTHROPIC_URL = "https://api.anthropic.com/v1/messages";
const LS = {
  anthropicKey: "sb_anthropic_key",
  alpacaKey: "sb_alpaca_key",
  alpacaSecret: "sb_alpaca_secret",
  alpacaLive: "sb_alpaca_live",
};

let STATE = null; // the loaded state.json

/* ---------- helpers ---------- */
const $ = (id) => document.getElementById(id);
const money = (n) => {
  // Round to cents first so a tiny negative (e.g. -0.003) doesn't render "-$0.00".
  const cents = Math.round((Number(n) || 0) * 100) / 100;
  return (cents < 0 ? "-$" : "$") +
    Math.abs(cents).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};
const pct = (n) => `${Number(n) >= 0 ? "+" : ""}${(Number(n) || 0).toFixed(2)}%`;

function lsGet(k) { try { return localStorage.getItem(k) || ""; } catch { return ""; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch {} }

function timeAgo(iso) {
  if (!iso) return "never";
  const d = new Date(iso);
  const secs = Math.round((Date.now() - d.getTime()) / 1000);
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return d.toLocaleString();
}

/* ---------- load committed data ---------- */
async function loadState() {
  try {
    const res = await fetch(`data/state.json?t=${Date.now()}`);
    STATE = await res.json();
  } catch (e) {
    $("updated-line").textContent = "Could not load bot data yet.";
    return;
  }
  renderMode(STATE.config?.trading_mode);
  renderAccount(STATE.account);
  renderMarketState(STATE.market_open);
  renderDecision(STATE);
  renderAudit(STATE.audit);
  renderValueScan(STATE.fundamentals);
  renderPositions(STATE.positions, "from last bot run");
  renderConfig(STATE.config);
  $("updated-line").innerHTML = STATE.updated_at
    ? `Last bot run <strong>${timeAgo(STATE.updated_at)}</strong> · ${new Date(STATE.updated_at).toLocaleString()}`
    : "No bot run recorded yet — trigger the “AI Stockbroker” workflow in the Actions tab.";
  await loadHistory();
}

async function loadHistory() {
  let rows = [];
  try {
    const res = await fetch(`data/history.jsonl?t=${Date.now()}`);
    const text = await res.text();
    rows = text.trim().split("\n").filter(Boolean).map((l) => JSON.parse(l));
  } catch {}
  renderChart(rows);
}

/* ---------- renderers ---------- */
function renderMode(mode) {
  const badge = $("mode-badge");
  const live = mode === "live";
  badge.textContent = live ? "LIVE" : "PAPER";
  badge.className = "badge " + (live ? "badge-live" : "badge-paper");
}

function setStat(id, label, value) {
  const card = $(id).closest(".stat");
  if (card) card.querySelector(".stat-label").textContent = label;
  $(id).textContent = value;
}

function renderAccount(a) {
  if (!a) return;
  const daypl = $("stat-daypl");

  if (a.capital_cap) {
    // Capped mode: show the managed budget rather than the full paper balance.
    const invested = a.invested || 0;
    const cash = a.effective_cash != null ? a.effective_cash : a.cash || 0;
    const net = a.net_pnl != null
      ? a.net_pnl
      : (STATE?.positions || []).reduce((s, p) => s + (Number(p.unrealized_pl) || 0), 0);
    setStat("stat-equity", `Budget (${a.capital_currency || "cap"})`, money(a.capital_cap));
    setStat("stat-bp", "Invested", money(invested));
    setStat("stat-cash", "Available", money(cash));
    setStat("stat-daypl", "Net P&L (after AI cost)", money(net));
    daypl.classList.toggle("pos", net >= 0);
    daypl.classList.toggle("neg", net < 0);
    renderPnlLine(a);
    return;
  }
  renderPnlLine(null);

  setStat("stat-equity", "Equity", money(a.equity));
  setStat("stat-cash", "Cash", money(a.cash));
  setStat("stat-bp", "Buying power", money(a.buying_power));
  const dayPl = (a.equity || 0) - (a.last_equity || 0);
  const pctVal = a.last_equity ? (dayPl / a.last_equity) * 100 : 0;
  setStat("stat-daypl", "Day P&L", `${money(dayPl)} (${pct(pctVal)})`);
  daypl.classList.toggle("pos", dayPl >= 0);
  daypl.classList.toggle("neg", dayPl < 0);
}

function renderMarketState(open) {
  const el = $("market-state");
  el.textContent = open ? "● Market open" : "○ Market closed";
  el.className = "pill " + (open ? "open" : "closed");
}

function renderDecision(s) {
  $("market-summary").textContent = s.market_summary || "The bot has not produced a decision yet.";
  const memo = (s.memo || "").trim();
  $("memo-text").textContent = memo;
  $("memo-details").style.display = memo ? "" : "none";

  const body = $("orders-body");
  const orders = s.orders || [];
  if (!orders.length) {
    const msg = s.skipped
      ? "⏭️ Triage skipped a full research cycle to save cost."
      : "No orders this cycle — the AI chose to hold.";
    body.innerHTML = `<tr><td colspan="6" class="muted center">${msg}</td></tr>`;
    return;
  }
  body.innerHTML = orders.map((o) => {
    const canApprove = o.status === "proposed";
    const approveBtn = canApprove
      ? `<button class="btn btn-small btn-primary approve-btn" data-symbol="${o.symbol}" data-side="${o.side}" data-notional="${o.notional_usd}">Approve</button>`
      : "";
    return `<tr>
      <td><span class="tag tag-${o.side}">${o.side}</span></td>
      <td class="mono">${o.symbol}</td>
      <td class="mono">${money(o.notional_usd)}</td>
      <td class="mono">${o.confidence != null ? (o.confidence * 100).toFixed(0) + "%" : "—"}</td>
      <td><span class="status status-${o.status}">${o.status}</span>${o.detail ? `<br><span class="muted small">${o.detail}</span>` : ""} ${approveBtn}</td>
      <td class="small">${o.reasoning || ""}</td>
    </tr>`;
  }).join("");

  document.querySelectorAll(".approve-btn").forEach((b) =>
    b.addEventListener("click", () => approveOrder(b))
  );
}

function renderPnlLine(a) {
  const el = $("pnl-line");
  if (!el) return;
  if (!a || a.gross_pnl == null) { el.textContent = ""; el.style.display = "none"; return; }
  el.style.display = "";
  const gross = a.gross_pnl || 0, cost = a.ai_cost_total || 0, net = a.net_pnl || 0;
  const covering = net >= 0;
  el.innerHTML =
    `Gross P&L <b class="${gross >= 0 ? "pos" : "neg"}">${money(gross)}</b> ` +
    `− AI cost <b>${money(cost)}</b> = ` +
    `Net <b class="${covering ? "pos" : "neg"}">${money(net)}</b> ` +
    `<span class="pill ${covering ? "audit-approve" : "audit-reject"}">` +
    `${covering ? "covering its costs" : "not yet covering costs"}</span>`;
}

function renderAudit(audit) {
  const card = $("audit-card");
  if (!audit) { card.style.display = "none"; return; }
  card.style.display = "";
  const verdict = audit.verdict || "flag";
  const pill = $("audit-verdict");
  const label = {
    approve: "✓ Approved", flag: "⚠ Flagged", reject: "✗ Rejected", unavailable: "— Unavailable",
  }[verdict] || verdict;
  pill.textContent = label;
  pill.className = "pill audit-" + verdict;
  $("audit-summary").textContent = audit.summary || "";
  const ts = audit.transparency_score;
  $("audit-transparency").textContent =
    ts != null ? `Transparency score: ${Math.round(ts * 100)}%` : "";
}

function renderValueScan(scan) {
  const card = $("value-card");
  const signals = (scan && scan.signals) || [];
  // Only show names with an actual edge (undervalued or rich); hide the "fair" noise.
  const notable = signals.filter((s) => s.undervalued || s.verdict === "rich");
  if (!notable.length) { card.style.display = "none"; return; }
  card.style.display = "";
  $("value-asof").textContent = scan.generated_at ? `as of ${new Date(scan.generated_at).toLocaleDateString()}` : "";
  const rank = { cheap: 0, rich: 1, fair: 2 };
  notable.sort((a, b) => (rank[a.verdict] ?? 3) - (rank[b.verdict] ?? 3));
  $("value-body").innerHTML = notable.map((s) => {
    const cls = s.undervalued ? "tag-buy" : s.verdict === "rich" ? "tag-sell" : "";
    const label = s.undervalued ? "undervalued" : s.verdict;
    return `<tr><td class="mono">${s.symbol}</td>` +
      `<td><span class="tag ${cls}">${label}</span></td>` +
      `<td class="small">${s.note || ""}</td></tr>`;
  }).join("");
}

function renderPositions(positions, source) {
  $("positions-source").textContent = source;
  const body = $("positions-body");
  if (!positions || !positions.length) {
    body.innerHTML = `<tr><td colspan="4" class="muted center">No open positions.</td></tr>`;
    return;
  }
  body.innerHTML = positions.map((p) => {
    const pl = Number(p.unrealized_pl) || 0;
    const plpc = (Number(p.unrealized_plpc) || 0) * 100;
    const cls = pl >= 0 ? "pos" : "neg";
    return `<tr>
      <td class="mono">${p.symbol}</td>
      <td class="num">${(Number(p.qty) || 0).toLocaleString(undefined, { maximumFractionDigits: 4 })}</td>
      <td class="num">${money(p.market_value)}</td>
      <td class="num ${cls}">${money(pl)}<br><span class="small">${pct(plpc)}</span></td>
    </tr>`;
  }).join("");
}

function renderConfig(c) {
  if (!c) return;
  const r = c.risk || {};
  const items = [
    ["Trading mode", c.trading_mode],
    ["Risk level", (r.level || "medium")],
    ["Capital cap", c.capital_cap ? `${c.capital_cap.toLocaleString()} ${c.capital_currency || ""}`.trim() : "off (full account)"],
    ["AI model", c.model],
    ["Web search", c.enable_web_search ? "on" : "off"],
    ["Watchlist", (c.watchlist || []).join(", ") || "—"],
    ["Max orders / run", r.max_orders_per_run],
    ["Max $ / order", r.max_notional_per_order != null ? money(r.max_notional_per_order) : "—"],
    ["Max alloc / symbol", r.max_allocation_pct_per_symbol != null ? r.max_allocation_pct_per_symbol + "%" : "—"],
    ["Cash reserve", r.min_cash_reserve_pct != null ? r.min_cash_reserve_pct + "%" : "—"],
  ];
  $("config-list").innerHTML = items
    .map(([k, v]) => `<div><dt>${k}</dt><dd>${v ?? "—"}</dd></div>`)
    .join("");
}

function renderChart(rows) {
  const svg = $("equity-chart");
  const empty = $("chart-empty");
  const pts = rows.map((r) => Number(r.equity)).filter((n) => Number.isFinite(n) && n > 0);
  if (pts.length < 2) {
    svg.innerHTML = "";
    empty.style.display = "";
    $("equity-range").textContent = "";
    return;
  }
  empty.style.display = "none";
  const W = 800, H = 220, pad = 8;
  const min = Math.min(...pts), max = Math.max(...pts);
  const span = max - min || 1;
  const x = (i) => pad + (i / (pts.length - 1)) * (W - 2 * pad);
  const y = (v) => H - pad - ((v - min) / span) * (H - 2 * pad);
  const line = pts.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `${line} L${x(pts.length - 1).toFixed(1)},${H - pad} L${x(0).toFixed(1)},${H - pad} Z`;
  svg.innerHTML = `<path class="chart-area" d="${area}" /><path class="chart-line" d="${line}" />`;
  const first = pts[0], last = pts[pts.length - 1];
  const change = ((last - first) / first) * 100;
  $("equity-range").textContent = `${money(first)} → ${money(last)} (${pct(change)}) · ${pts.length} runs`;
}

/* ---------- Anthropic in-browser research ---------- */
async function research() {
  const key = $("anthropic-key").value.trim();
  const ticker = $("research-ticker").value.trim().toUpperCase();
  const out = $("research-output");
  if (!key) { out.textContent = "Enter your Anthropic API key first."; return; }
  if (!ticker) { out.textContent = "Enter a ticker symbol."; return; }
  lsSet(LS.anthropicKey, key);
  const model = STATE?.config?.model || "claude-opus-5";
  out.innerHTML = `<span class="spinner"></span>Researching ${ticker}…`;
  $("research-btn").disabled = true;

  const body = {
    model,
    max_tokens: 1400,
    system:
      "You are a sharp equity analyst. Given a ticker, use web search to check " +
      "the latest news, price context and sentiment, then give a brief, balanced " +
      "read: what's going on, key risks, and a lean (bullish / bearish / neutral) " +
      "with a one-line rationale. Be concise. Not financial advice.",
    tools: [{ type: "web_search_20260209", name: "web_search", max_uses: 4 }],
    messages: [{ role: "user", content: `Give me your current read on ${ticker}.` }],
  };

  try {
    const res = await fetch(ANTHROPIC_URL, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "anthropic-dangerous-direct-browser-access": "true",
      },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data?.error?.message || `HTTP ${res.status}`);
    const text = (data.content || [])
      .filter((b) => b.type === "text")
      .map((b) => b.text)
      .join("\n")
      .trim();
    out.textContent = text || "(no response)";
  } catch (e) {
    out.textContent = `Error: ${e.message}`;
  } finally {
    $("research-btn").disabled = false;
  }
}

/* ---------- Alpaca live connect ---------- */
function alpacaHeaders() {
  return {
    "APCA-API-KEY-ID": lsGet(LS.alpacaKey),
    "APCA-API-SECRET-KEY": lsGet(LS.alpacaSecret),
    "content-type": "application/json",
  };
}
function alpacaBase() {
  return lsGet(LS.alpacaLive) === "1" ? ALPACA_LIVE : ALPACA_PAPER;
}

async function connectAlpaca() {
  const key = $("alpaca-key").value.trim();
  const secret = $("alpaca-secret").value.trim();
  const live = $("alpaca-live").checked;
  const status = $("connect-status");
  if (!key || !secret) { status.textContent = "Enter both key and secret."; return; }
  lsSet(LS.alpacaKey, key);
  lsSet(LS.alpacaSecret, secret);
  lsSet(LS.alpacaLive, live ? "1" : "0");
  status.innerHTML = `<span class="spinner"></span>Connecting…`;
  try {
    const [acct, pos] = await Promise.all([
      fetch(`${alpacaBase()}/v2/account`, { headers: alpacaHeaders() }).then((r) => r.json()),
      fetch(`${alpacaBase()}/v2/positions`, { headers: alpacaHeaders() }).then((r) => r.json()),
    ]);
    if (acct.code || acct.message) throw new Error(acct.message || "auth failed");
    renderAccount({
      equity: +acct.equity, cash: +acct.cash, buying_power: +acct.buying_power,
      last_equity: +acct.last_equity, portfolio_value: +acct.portfolio_value,
    });
    renderPositions(
      (Array.isArray(pos) ? pos : []).map((p) => ({
        symbol: p.symbol, qty: +p.qty, market_value: +p.market_value,
        unrealized_pl: +p.unrealized_pl, unrealized_plpc: +p.unrealized_plpc,
      })),
      "live · just now"
    );
    status.textContent = `Connected to ${live ? "LIVE" : "paper"} account — panels now show real-time data.`;
  } catch (e) {
    status.textContent = `Connection failed: ${e.message}. (Alpaca's browser CORS covers the trading API; double-check your keys and paper/live toggle.)`;
  }
}

async function approveOrder(btn) {
  if (!lsGet(LS.alpacaKey)) { alert("Connect your Alpaca keys first (Live account view panel)."); return; }
  const { symbol, side, notional } = btn.dataset;
  if (!confirm(`Place a ${side.toUpperCase()} order for ${money(+notional)} of ${symbol}?`)) return;
  btn.disabled = true;
  btn.textContent = "Sending…";
  try {
    const res = await fetch(`${alpacaBase()}/v2/orders`, {
      method: "POST",
      headers: alpacaHeaders(),
      body: JSON.stringify({
        symbol, side, type: "market", time_in_force: "day",
        notional: Number(notional).toFixed(2),
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data?.message || `HTTP ${res.status}`);
    btn.textContent = "✓ Submitted";
  } catch (e) {
    btn.textContent = "Failed";
    btn.disabled = false;
    alert(`Order failed: ${e.message}`);
  }
}

/* ---------- init ---------- */
function init() {
  $("anthropic-key").value = lsGet(LS.anthropicKey);
  $("alpaca-key").value = lsGet(LS.alpacaKey);
  $("alpaca-secret").value = lsGet(LS.alpacaSecret);
  $("alpaca-live").checked = lsGet(LS.alpacaLive) === "1";
  $("refresh-btn").addEventListener("click", loadState);
  $("research-btn").addEventListener("click", research);
  $("connect-btn").addEventListener("click", connectAlpaca);
  loadState();
}
document.addEventListener("DOMContentLoaded", init);
