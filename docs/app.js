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
  renderBenchmark(STATE.benchmark, STATE.correlation);
  renderValueScan(STATE.fundamentals);
  renderTheses(STATE.theses, STATE.positions);
  renderPositions(STATE.positions, "from last bot run");
  renderConfig(STATE.config);
  $("updated-line").innerHTML = STATE.updated_at
    ? `Last bot run <strong>${timeAgo(STATE.updated_at)}</strong> · ${new Date(STATE.updated_at).toLocaleString()}`
    : "No bot run recorded yet — trigger the “AI Stockbroker” workflow in the Actions tab.";
  await loadHistory();
  await loadJournal();
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

  // Make it unmistakable when orders were NOT actually sent to the broker,
  // so the dashboard is never mistaken for the real account activity.
  // Anything not "executed" never reached the broker - including rejected and
  // vetoed orders, which must not suppress the warning.
  const banner = $("orders-note");
  const executed = orders.filter((o) => o.status === "executed");
  const notSent = orders.filter((o) => ["dry_run", "deferred", "proposed"].includes(o.status));
  if (orders.length && !executed.length) {
    const why = {
      dry_run: "this was a <strong>Research only</strong> run",
      deferred: "the market was closed",
      proposed: "live mode needs your approval",
    }[(notSent[0] || {}).status] || "none of them passed the risk checks";
    banner.style.display = "";
    banner.innerHTML = `⚠️ <strong>These orders were NOT placed</strong> — ${why}, so nothing was
      sent to Alpaca and they will not appear in your broker account. The Positions
      panel below shows what is actually held.`;
  } else {
    banner.style.display = "none";
  }
}

function renderPnlLine(a) {
  const el = $("pnl-line");
  if (!el) return;
  if (!a || a.gross_pnl == null) { el.textContent = ""; el.style.display = "none"; return; }
  el.style.display = "";
  const gross = a.gross_pnl || 0, cost = a.ai_cost_total || 0, net = a.net_pnl || 0;
  const covering = net >= 0;
  // With no cost recorded yet there is nothing to judge - don't claim it is
  // "covering its costs" on an empty measurement period.
  const verdict = cost === 0 && gross === 0
    ? `<span class="pill">awaiting first run</span>`
    : `<span class="pill ${covering ? "audit-approve" : "audit-reject"}">` +
      `${covering ? "covering its costs" : "not yet covering costs"}</span>`;
  el.innerHTML =
    `Gross P&L <b class="${gross >= 0 ? "pos" : "neg"}">${money(gross)}</b> ` +
    `− AI cost <b>${money(cost)}</b> = ` +
    `Net <b class="${covering ? "pos" : "neg"}">${money(net)}</b> ` + verdict;
}

/* The scoreboard that decides whether any of this was worth doing: the book's
 * return against simply buying the benchmark and holding it, after AI cost. */
function renderBenchmark(b, corr) {
  const card = $("bench-card");
  if (!card) return;
  if (!b) { card.style.display = "none"; return; }
  card.style.display = "";

  const sign = (v) => (Number(v) >= 0 ? "pos" : "neg");
  const pp = (v) => `${Number(v) >= 0 ? "+" : ""}${(Number(v) || 0).toFixed(2)}pp`;
  const sym = b.symbol || "SPY";

  $("bench-sym-label").textContent = `${sym} buy & hold`;
  $("bench-bot").innerHTML = `<span class="${sign(b.net_return_pct)}">${pct(b.net_return_pct)}</span>`;
  $("bench-bot-note").textContent = `${pct(b.bot_return_pct)} before AI cost`;
  $("bench-index").innerHTML =
    `<span class="${sign(b.benchmark_return_pct)}">${pct(b.benchmark_return_pct)}</span>`;

  const ahead = Number(b.net_alpha_pct) >= 0;
  $("bench-alpha").innerHTML = `<span class="${ahead ? "pos" : "neg"}">${pp(b.net_alpha_pct)}</span>`;

  // The same statement in plain money, so it needs no percentage arithmetic.
  const bh = money(b.buy_and_hold_value), bv = money(b.book_value);
  $("bench-plain").innerHTML = ahead
    ? `Your starting capital left in ${sym} would be <b>${bh}</b>. The bot made it <b class="pos">${bv}</b>. <span class="pill audit-approve">beating the market</span>`
    : `Your starting capital left in ${sym} would be <b>${bh}</b>. The bot made it <b class="neg">${bv}</b>. <span class="pill audit-reject">losing to the market</span>`;

  const period = b.baseline_at ? `since ${new Date(b.baseline_at).toLocaleDateString()}` : "";
  $("bench-period").textContent = b.from_inception === false
    ? `${period} · benchmark measured from a later date, approximate`
    : period;

  // How index-like the book is - a book that tracks the index cannot beat it.
  const el = $("bench-corr");
  if (!corr) { el.innerHTML = ""; return; }
  const c = Number(corr.weighted);
  const cls = c >= 0.85 ? "neg" : c >= 0.7 ? "warn" : "pos";
  const verdict = c >= 0.85 ? "index-like" : c >= 0.7 ? "partly differentiated" : "differentiated";
  const names = Object.entries(corr.per_symbol || {})
    .sort((a, x) => x[1] - a[1])
    .map(([k, v]) => `${k} ${Number(v).toFixed(2)}`)
    .join(" · ");
  const warn = c >= 0.85
    ? `<p class="bench-warn">⚠ This book moves with the index, so it will return roughly what the index returns minus costs. Beating the market requires holding things that behave differently from it.</p>`
    : "";
  el.innerHTML =
    `<div class="bench-corr-head">Correlation to ${corr.symbol || "SPY"}: ` +
    `<b class="${cls}">${c.toFixed(2)}</b> <span class="muted">(${verdict})</span></div>` +
    `<div class="muted small">${names}</div>${warn}`;
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

function renderTheses(theses, positions) {
  const card = $("theses-card");
  const rows = Object.values(theses || {});
  if (!rows.length) { card.style.display = "none"; return; }
  card.style.display = "";
  const priceBy = {};
  (positions || []).forEach((p) => { priceBy[p.symbol] = Number(p.current_price) || 0; });
  $("theses-body").innerHTML = rows.map((t) => {
    const px = priceBy[t.symbol] || 0;
    const tgt = t.target_price, stop = t.stop_price;
    const hit = tgt && px && px >= Number(tgt);
    const brk = stop && px && px <= Number(stop);
    return `<tr>
      <td class="mono">${t.symbol}</td>
      <td><span class="tag">${t.conviction || "—"}</span></td>
      <td class="mono ${hit ? "pos" : ""}">${tgt ? money(tgt) + (hit ? " ✓" : "") : "—"}</td>
      <td class="mono ${brk ? "neg" : ""}">${stop ? money(stop) + (brk ? " ⚠" : "") : "—"}</td>
      <td class="small">${t.thesis || ""}</td>
    </tr>`;
  }).join("");
}

async function loadJournal() {
  let rows = [];
  try {
    const res = await fetch(`data/journal.jsonl?t=${Date.now()}`);
    if (!res.ok) throw new Error("no journal");
    rows = (await res.text()).trim().split("\n").filter(Boolean).map((l) => JSON.parse(l));
  } catch { rows = []; }

  // --- Paper-trading tab: only orders actually sent to the broker ---
  const fills = [];
  rows.forEach((r) => {
    (r.orders || []).forEach((o) => {
      if (o.status === "executed") fills.push({ ...o, t: r.t });
    });
  });
  fills.reverse();
  $("fills-count").textContent = fills.length ? `${fills.length} trades` : "";
  if (fills.length) {
    $("fills-body").innerHTML = fills.map((o) => `<tr>
      <td class="mono small">${o.t ? new Date(o.t).toLocaleString() : ""}</td>
      <td><span class="tag tag-${o.side}">${o.side}</span></td>
      <td class="mono">${o.symbol}</td>
      <td class="mono">${money(o.notional_usd)}</td>
      <td class="small">${o.thesis || o.reasoning || ""}</td>
    </tr>`).join("");
  }

  // --- Research tab: the full thinking record, newest first ---
  const card = $("journal-card");
  if (!rows.length) { card.style.display = "none"; return; }
  card.style.display = "";
  const list = rows.slice().reverse();
  $("journal-count").textContent = `${rows.length} cycles`;
  $("journal-list").innerHTML = list.slice(0, 40).map((r) => {
    const when = r.t ? new Date(r.t).toLocaleString() : "";
    const memo = (r.memo || "").trim();
    const placed = (r.orders || []).some((o) => o.status === "executed");
    const badge = r.skipped
      ? `<span class="pill">skipped</span>`
      : placed
        ? `<span class="pill audit-approve">traded</span>`
        : `<span class="pill audit-flag">research only</span>`;
    return `<details class="journal-entry">
      <summary><span class="mono small">${when}</span> ${badge}
        <strong>${r.actions || "no action"}</strong>
        <span class="muted small">${esc((r.summary || "").slice(0, 110))}</span></summary>
      ${memo ? `<pre class="memo-text">${esc(memo)}</pre>` : ""}
      ${r.audit ? `<p class="muted small"><strong>Auditor:</strong> ${esc(r.audit)}</p>` : ""}
    </details>`;
  }).join("");
}

function esc(t) {
  return String(t).replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]));
}

function initTabs() {
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("is-active", b === btn));
      document.querySelectorAll(".panel").forEach((p) => {
        p.hidden = p.id !== `panel-${btn.dataset.panel}`;
      });
      try { localStorage.setItem("sb_tab", btn.dataset.panel); } catch {}
    });
  });
  let saved = "trading";
  try { saved = localStorage.getItem("sb_tab") || "trading"; } catch {}
  const btn = document.querySelector(`.tab[data-panel="${saved}"]`);
  if (btn && saved !== "trading") btn.click();
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
  initTabs();
  $("refresh-btn").addEventListener("click", loadState);
  $("research-btn").addEventListener("click", research);
  $("connect-btn").addEventListener("click", connectAlpaca);
  loadState();
}
document.addEventListener("DOMContentLoaded", init);
