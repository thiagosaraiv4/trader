/* Trader panel — reads /api/v1/* and renders. Read-only.
   All data reaches the DOM through textContent (never innerHTML). */
"use strict";

const REFRESH_MS = 30_000;
const INITIAL = 10_000;
const LWC = window.LightweightCharts;
// The chart library draws times as UTC: shift each one by the browser's
// offset AT THAT DATE, so summer time matches the tables
const loc = (t) => t - new Date(t * 1000).getTimezoneOffset() * 60;

// ---------------------------------------------------------------- helpers
const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "text") node.textContent = v;
    else if (k === "cls") node.className = v;
    else node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) if (c != null) node.append(c);
  return node;
}

const nf = (d) => new Intl.NumberFormat("pt-BR", { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtNum = (v, d = 2) => (v == null ? "—" : nf(d).format(v));
const fmtUsd = (v, d = 2) => (v == null ? "—" : "US$ " + nf(d).format(v));
const fmtPct = (v, d = 2, sign = true) =>
  v == null ? "—" : (sign && v > 0 ? "+" : "") + nf(d).format(v * 100) + "%";
const signCls = (v) => (v == null || v === 0 ? "" : v > 0 ? "pos" : "neg");

function fmtTime(epoch, withYear = false) {
  if (epoch == null) return "—";
  const d = new Date(epoch * 1000);
  return d.toLocaleString("pt-BR", {
    day: "2-digit", month: "2-digit", year: withYear ? "2-digit" : undefined,
    hour: "2-digit", minute: "2-digit",
  });
}

function fmtAge(sec) {
  if (sec == null) return "—";
  if (sec < 90) return `${Math.round(sec)} s`;
  if (sec < 5400) return `${Math.round(sec / 60)} min`;
  if (sec < 172800) return `${Math.round(sec / 3600)} h`;
  return `${Math.round(sec / 86400)} dias`;
}

function fmtDuration(sec) {
  if (sec == null) return "—";
  const h = sec / 3600;
  return h < 48 ? `${nf(1).format(h)} h` : `${nf(1).format(h / 24)} d`;
}

async function api(path) {
  const r = await fetch("/api/v1/" + path, { credentials: "same-origin", cache: "no-store" });
  if (r.status === 401) { window.location.href = "/login"; throw new Error("login"); }
  if (!r.ok) throw Object.assign(new Error(`${path}: HTTP ${r.status}`), { status: r.status });
  return r.json();
}

const failText = (e) => (e && e.status ? `indisponível (HTTP ${e.status})` : "indisponível (sem conexão)");

// Strictly increasing local display time for each (sorted) epoch: in the
// hour repeated when summer time ends, two bars would get the same label
function displayTimes(times) {
  const out = new Map();
  let last = -Infinity;
  for (const t of times) {
    let d = loc(t);
    if (d <= last) d = last + 1;
    out.set(t, d);
    last = d;
  }
  return out;
}

// Index of the last element <= x in a sorted array (-1 if none)
function lastAtOrBefore(arr, x) {
  let lo = 0, hi = arr.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (arr[mid] <= x) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}

function table(headers, rows, textCols = []) {
  const thead = el("thead", {}, el("tr", {}, headers.map((h, i) =>
    el("th", { text: h, scope: "col", cls: textCols.includes(i) ? "text" : "" }))));
  const tbody = el("tbody", {}, rows.map((r) =>
    el("tr", {}, r.map((cell, i) => {
      const td = el("td", { cls: textCols.includes(i) ? "text" : "" });
      if (cell && typeof cell === "object" && "text" in cell) {
        td.textContent = cell.text;
        if (cell.cls) td.classList.add(cell.cls);
      } else td.textContent = cell == null ? "—" : String(cell);
      return td;
    }))));
  return el("table", {}, [thead, tbody]);
}

// ---------------------------------------------------------------- charts
let priceChart, candleSeries, indicatorSeries = {}, priceLines = [];
let equityChart, equitySeries, equityBase, equityPoints = -1;
const SERIES_VARS = ["--series-1", "--series-2", "--series-3"];

function chartOptions() {
  return {
    autoSize: true,
    // In-chart logo off (it injects an inline <style>, blocked by our CSP);
    // the attribution the license asks for is in the page footer
    layout: { background: { type: "solid", color: css("--surface") }, textColor: css("--muted"),
              fontFamily: css("--font"), fontSize: 11, attributionLogo: false },
    grid: { vertLines: { color: css("--grid") }, horzLines: { color: css("--grid") } },
    rightPriceScale: { borderColor: css("--axis") },
    timeScale: { borderColor: css("--axis"), timeVisible: true, secondsVisible: false },
    crosshair: { mode: LWC.CrosshairMode.Normal },
    localization: { locale: "pt-BR", priceFormatter: (p) => nf(2).format(p) },
  };
}

function initCharts() {
  priceChart = LWC.createChart($("price-chart"), chartOptions());
  // Up candles hollow, down candles filled: direction is not color-only
  candleSeries = priceChart.addCandlestickSeries({
    upColor: "rgba(0,0,0,0)", downColor: css("--critical"),
    borderUpColor: css("--good"), borderDownColor: css("--critical"),
    wickUpColor: css("--good"), wickDownColor: css("--critical"),
  });
  priceChart.subscribeCrosshairMove(updateLegend);

  equityChart = LWC.createChart($("equity-chart"), chartOptions());
  equitySeries = equityChart.addLineSeries({
    color: css("--series-1"), lineWidth: 2, priceLineVisible: false,
    lastValueVisible: true, crosshairMarkerRadius: 4,
  });
  equityBase = equitySeries.createPriceLine({
    price: INITIAL, color: css("--axis"), lineWidth: 1, lineStyle: LWC.LineStyle.Solid,
    axisLabelVisible: false, title: "",
  });
}

let lastCandles = null, priceFitted = false;

function renderCandles(c) {
  lastCandles = c;
  $("chart-title").textContent = `${c.symbol} · ${c.timeframe}`;
  $("sym").textContent = c.symbol;
  const names = c.bars.length ? Object.keys(c.indicators) : [];
  for (const name of Object.keys(indicatorSeries)) {
    if (!names.includes(name)) { priceChart.removeSeries(indicatorSeries[name]); delete indicatorSeries[name]; }
  }
  if (!c.bars.length) {
    // e.g. the session was just archived: don't keep drawing the old run
    $("chart-sub").textContent = "sem candles gravados ainda (live_bars.csv)";
    candleSeries.setMarkers([]);
    candleSeries.setData([]);
    updateLegend(null);
    return;
  }
  $("chart-sub").textContent = `${c.bars.length} candles fechados que o robô processou`;
  const times = c.bars.map((b) => b.time);
  const disp = displayTimes(times);
  candleSeries.setData(c.bars.map((b) => ({ ...b, time: disp.get(b.time) })));

  names.forEach((name, i) => {
    if (!indicatorSeries[name]) {
      indicatorSeries[name] = priceChart.addLineSeries({
        lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      });
    }
    // Color follows the position in the list every time, like the legend
    indicatorSeries[name].applyOptions({ color: css(SERIES_VARS[i % SERIES_VARS.length]) });
    indicatorSeries[name].setData(c.indicators[name].filter((p) => disp.has(p.time))
      .map((p) => ({ time: disp.get(p.time), value: p.value })));
  });

  // Each fill sits on the last bar that opened at or before it, so a fill
  // inside the still-forming bar shows on the newest closed bar
  const markers = [];
  for (const m of c.markers) {
    const i = lastAtOrBefore(times, m.time);
    if (i < 0) continue;
    const t = disp.get(times[i]);
    if (m.kind === "entry") {
      markers.push({ time: t, position: "belowBar", shape: "arrowUp", color: css("--series-1"), text: "C" });
    } else {
      markers.push({ time: t, position: "aboveBar", shape: "arrowDown",
                     color: m.net >= 0 ? css("--good") : css("--critical"),
                     text: "V " + fmtUsd(m.net, 0) });
    }
  }
  markers.sort((a, b) => a.time - b.time);
  candleSeries.setMarkers(markers);
  // First load: show every candle (few bars would leave the left side empty);
  // later refreshes keep the user's zoom
  if (!priceFitted) { priceChart.timeScale().fitContent(); priceFitted = true; }
  updateLegend(null);
}

function renderPriceLines(position) {
  for (const l of priceLines) candleSeries.removePriceLine(l);
  priceLines = [];
  if (!position) return;
  priceLines.push(candleSeries.createPriceLine({
    price: position.entry_price, color: css("--series-1"), lineWidth: 1,
    lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true, title: "entrada",
  }));
  if (position.stop_price) {
    priceLines.push(candleSeries.createPriceLine({
      price: position.stop_price, color: css("--critical"), lineWidth: 1,
      lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true, title: "stop",
    }));
  }
}

function updateLegend(param) {
  const legend = $("legend");
  legend.replaceChildren();
  if (!lastCandles || !lastCandles.bars.length) return;
  let bar = null;
  if (param && param.time !== undefined && param.seriesData) bar = param.seriesData.get(candleSeries);
  if (!bar) bar = lastCandles.bars[lastCandles.bars.length - 1];
  const ohlc = el("span", { cls: "key" }, [
    "A ", el("b", { text: fmtNum(bar.open) }), " M ", el("b", { text: fmtNum(bar.high) }),
    " m ", el("b", { text: fmtNum(bar.low) }), " F ", el("b", { text: fmtNum(bar.close) }),
  ]);
  legend.append(ohlc);
  // Same order as renderCandles uses for the line colors
  Object.keys(lastCandles.indicators).forEach((name, i) => {
    if (!indicatorSeries[name]) return;
    let v = null;
    if (param && param.seriesData) {
      const p = param.seriesData.get(indicatorSeries[name]);
      v = p ? p.value : null;
    }
    if (v == null) {
      const arr = lastCandles.indicators[name];
      v = arr && arr.length ? arr[arr.length - 1].value : null;
    }
    legend.append(el("span", { cls: "key" }, [
      el("span", { cls: `swatch s${(i % 3) + 1}` }), name + " ", el("b", { text: fmtNum(v) }),
    ]));
  });
}

function renderEquity(curve) {
  const pts = [];
  let lastT = -Infinity;
  for (const p of curve) {
    let t = loc(Math.floor(p.time));
    if (t <= lastT) t = lastT + 1;            // strictly increasing times
    pts.push({ time: t, value: p.value });
    lastT = t;
  }
  equitySeries.setData(pts);
  // Re-fit only when a trade adds a point, not on every refresh (keeps zoom)
  if (pts.length !== equityPoints) equityChart.timeScale().fitContent();
  equityPoints = pts.length;
}

// ---------------------------------------------------------------- panels
const SERVICE_NAMES = { "trading-bot": "Robô principal", "pair-scanner": "Scanner", "maker-bot": "Maker" };
const HB_FOR = { "trading-bot": "bot", "pair-scanner": "scanner", "maker-bot": "maker" };

function renderStatus(s) {
  const pills = $("pills");
  pills.replaceChildren();
  for (const [svc, state] of Object.entries(s.services)) {
    const hb = s.heartbeats[HB_FOR[svc]];
    let cls = "", label;
    if (state === "active") {
      cls = hb && hb.fresh ? "ok" : "warn";
      label = hb && hb.fresh ? "rodando" : "sem sinal recente";
    } else if (state === "failed") { cls = "bad"; label = "falhou"; }
    else if (state === "inactive") { label = "desligado"; }
    else if (hb && hb.fresh) { cls = "ok"; label = "ativo"; }
    else if (hb && hb.heartbeat) { cls = "warn"; label = "parado?"; }
    else continue;                                  // never installed here
    pills.append(el("span", { cls: `pill ${cls}`, title: `${svc}: ${state}` },
      [el("span", { cls: "dot" }), `${SERVICE_NAMES[svc]}: ${label}`]));
  }
  $("logout").hidden = !s.login;
  const hb = s.heartbeats.bot && s.heartbeats.bot.heartbeat;
  if (hb && hb.price != null) $("px").textContent = fmtNum(hb.price);
  if (activeTab === "tab-system") renderSystem(s);
  lastStatus = s;
}

function tile(label, value, hint, cls = "") {
  return el("div", { cls: "tile" }, [
    el("div", { cls: "label", text: label }),
    el("div", { cls: `value ${cls}`, text: value }),
    hint ? el("div", { cls: "hint", text: hint }) : null,
  ]);
}

function renderBot(b) {
  const tiles = $("tiles");
  tiles.replaceChildren();
  if (!b.available) {
    tiles.append(el("div", { cls: "empty", text: "Sem sessão ainda (session.json)." }));
  } else {
    const m = b.metrics;
    const ret = (b.equity - INITIAL) / INITIAL;
    tiles.append(
      tile("Patrimônio (US$)", fmtNum(b.equity), "caixa " + fmtUsd(b.balance)),
      tile("Retorno", fmtPct(ret), "realizado " + fmtPct(m.return_pct), signCls(ret)),
      tile("Trades fechados", String(m.trades), m.trades < 10 ? "amostra pequena" : ""),
      tile("Acerto", fmtPct(m.win_rate, 0, false), "trades com lucro líquido"),
      tile("Queda máxima", fmtPct(m.max_drawdown ? -m.max_drawdown : 0, 1), "nos fechamentos",
           m.max_drawdown ? "neg" : ""),
      tile("Fator de lucro", m.profit_factor == null ? "—" : fmtNum(m.profit_factor), "ganhos ÷ perdas"),
    );
  }

  const pr = b.params || {};
  const pos = $("position");
  pos.replaceChildren();
  const kv = (k, v, cls = "") => { pos.append(el("dt", { text: k }), el("dd", { text: v, cls })); };
  const p = b.position;
  const base = ((lastCandles && lastCandles.symbol) || "BTC/USDT").split("/")[0];
  if (!p) {
    kv("Situação", "Sem posição");
    kv("Aguardando", pr.strategy === "trend_v4"
      ? `rompimento da máxima de ${pr.donchian_entry ?? 20}` : "MME 9 cruzar acima da MM 20");
  } else {
    kv("Lado", "Comprado");
    kv("Quantidade", `${fmtNum(p.size, 6)} ${base}`);
    kv("Entrada", fmtNum(p.entry_price));
    kv("Preço atual", fmtNum(p.mark_price));
    kv("Stop", fmtNum(p.stop_price));
    kv("Resultado aberto", `${fmtUsd(p.unrealized)} (${fmtPct(p.unrealized_pct)})`, signCls(p.unrealized));
  }
  if (b.halted) kv("Circuit breaker", "ACIONADO", "neg");
  renderPriceLines(p);

  const params = $("params");
  params.replaceChildren();
  $("strategy-name").textContent = pr.strategy || "";
  const rows = pr.strategy === "trend_v4" ? [
    ["Candle", pr.timeframe],
    ["Entrada", `rompe a máxima de ${pr.donchian_entry}`],
    ["Saída", `perde a mínima de ${pr.donchian_exit}`],
    ["Tendência", pr.trend_sma ? `acima da MM ${pr.trend_sma}` : "sem filtro"],
    ["Stop inicial", `${pr.stop_atr} ATR`],
    ["Stop móvel", `${pr.trail_atr} ATR`],
    ["Risco por trade", fmtPct(pr.risk_pct, 1, false)],
    ["Posição máxima", fmtPct(pr.max_position_pct, 0, false)],
  ] : Object.entries(pr).map(([k, v]) => [k, String(v)]);
  for (const [k, v] of rows) params.append(el("dt", { text: k }), el("dd", { text: v ?? "—" }));

  renderEquity(b.equity_curve || []);
  renderTrades(b.trades || []);
}

function renderTrades(trades) {
  const panel = $("tab-trades");
  panel.replaceChildren();
  if (!trades.length) {
    panel.append(el("div", { cls: "empty", text: "Nenhum trade fechado nesta sessão ainda." }));
    return;
  }
  const rows = trades.slice().reverse().map((t, i) => [
    trades.length - i,
    fmtTime(t.entry_time, true), fmtNum(t.entry_price),
    fmtTime(t.exit_time, true), fmtNum(t.exit_price),
    { text: fmtUsd(t.net), cls: signCls(t.net) },
    { text: fmtPct(t.net_pct), cls: signCls(t.net) },
    fmtDuration(t.entry_time && t.exit_time ? t.exit_time - t.entry_time : null),
  ]);
  panel.append(table(["#", "Entrada", "Preço", "Saída", "Preço", "Resultado", "% do saldo", "Duração"], rows));
  panel.append(el("p", { cls: "note", text: "Resultado líquido de taxas (variação do caixa entre fechamentos)." }));
}

async function renderScanner() {
  const panel = $("tab-scanner");
  const d = await api("scanner");
  panel.replaceChildren();
  if (!d.available) {
    panel.append(el("div", { cls: "empty", text: "Scanner sem dados (scanner_stats.json)." }));
    return;
  }
  const rows = d.rows.map((r) => [
    r.symbol, fmtNum(r.hours, 1), fmtNum(r.spread_ticks, 0), fmtNum(r.spread_bp, 2),
    fmtNum(r.exec_per_hour, 0), fmtNum(r.touch["5"], 2),
    { text: fmtNum(r.through5, 2), cls: signCls(r.through5) }, r.n, fmtNum(r.fee_bp, 1),
    { text: fmtNum(r.net_bp, 2), cls: signCls(r.net_bp) }, { text: r.verdict },
  ]);
  panel.append(table(["Par", "Horas", "Spread ticks", "Spread bp", "Exec/h", "Otimista +5s",
                      "Pessimista +5s", "n", "Taxa bp", "Líquido bp", "Veredito"], rows, [10]));
  panel.append(el("p", { cls: "note", text:
    `Atualizado ${fmtTime(d.updated)}. Valores em bp (0,01%). Pessimista = ordem no fim da fila; ` +
    "um par só é candidato com líquido positivo acima do ruído e 100+ medições." }));
}

async function renderMaker() {
  const panel = $("tab-maker");
  const d = await api("maker");
  panel.replaceChildren();
  if (!d.available) {
    panel.append(el("div", { cls: "empty", text: "Maker sem dados (maker_trades.csv)." }));
    return;
  }
  const rows = d.variants.map((v) => [
    v.variant, v.trades, { text: fmtUsd(v.pnl), cls: signCls(v.pnl) },
    { text: fmtPct(v.per_trade_pct, 4), cls: signCls(v.per_trade_pct) },
    fmtPct(v.win_rate, 0, false), fmtUsd(v.fees), v.forced_exits,
    fmtPct(v.markout_1s, 4), fmtPct(v.markout_30s, 4), { text: v.verdict },
  ]);
  panel.append(table(["Versão", "Trades", "Resultado", "Por trade", "Acerto", "Taxas",
                      "A mercado", "+1s", "+30s", "Conclusão"], rows, [9]));
  panel.append(el("p", { cls: "note", text:
    "+1s/+30s: preço médio depois de cada compra (negativo = seleção adversa). Simulação, sem ordens reais." }));
}

let activeLog = "bot";
// Long floats in log fields (balances) read better with 2 decimals
const roundLog = (v) => (typeof v === "number" && !Number.isInteger(v) ? Number(v.toFixed(2)) : v);

async function renderLogs() {
  const box = $("log");
  const d = await api(`logs/${activeLog}?lines=150`);
  box.replaceChildren();
  if (!d.lines.length) {
    box.append(el("div", { cls: "empty", text: "Log vazio ou inexistente." }));
    return;
  }
  for (const raw of d.lines.slice().reverse()) {
    let time = "", level = "", msg = raw;
    try {
      const rec = JSON.parse(raw);
      time = rec.timestamp ? fmtTime(Date.parse(rec.timestamp) / 1000, true) : "";
      level = rec.level || "";
      const extra = Object.entries(rec)
        .filter(([k]) => !["timestamp", "level", "event", "component", "logger"].includes(k))
        .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : roundLog(v)}`).join(" ");
      msg = `${rec.event ?? ""}${extra ? "  " + extra : ""}`;
    } catch (_) { /* plain text line */ }
    box.append(el("div", { cls: "line" }, [
      el("span", { cls: "time", text: time }),
      el("span", { cls: `lvl-${level}`, text: level }),
      el("span", { cls: "msg", text: msg }),
    ]));
  }
}

let lastStatus = null;

function renderSystem(s) {
  const panel = $("tab-system");
  panel.replaceChildren();
  s = s || lastStatus;
  if (!s) return;
  const sys = s.system || {};
  const gb = (b) => (b == null ? "—" : `${nf(0).format(b / 1048576)} MB`);
  const svcRows = Object.entries(s.services).map(([svc, state]) => {
    const hb = s.heartbeats[HB_FOR[svc]];
    return [SERVICE_NAMES[svc], state, hb && hb.age_sec != null ? "há " + fmtAge(hb.age_sec) : "—"];
  });
  panel.append(table(["Serviço", "systemd", "Último heartbeat"], svcRows));
  const kv = el("dl", { cls: "kv" });
  const add = (k, v) => kv.append(el("dt", { text: k }), el("dd", { text: v }));
  add("Memória disponível", `${gb(sys.mem_available)} de ${gb(sys.mem_total)}`);
  add("Carga (1 min)", fmtNum(sys.load_1m, 2));
  add("Ligada há", fmtAge(sys.uptime_sec));
  panel.append(kv);
}

// ---------------------------------------------------------------- tabs & refresh
let activeTab = "tab-trades";
const TAB_LOADERS = { "tab-scanner": renderScanner, "tab-maker": renderMaker, "tab-logs": renderLogs,
                      "tab-system": async () => renderSystem() };

function selectTab(btn) {
  for (const b of document.querySelectorAll('[role="tab"]')) {
    const on = b === btn;
    b.setAttribute("aria-selected", String(on));
    $(b.getAttribute("aria-controls")).hidden = !on;
  }
  activeTab = btn.getAttribute("aria-controls");
  refreshTab();
}

async function refreshTab() {
  const loader = TAB_LOADERS[activeTab];
  if (!loader) return;
  try { await loader(); } catch (e) { console.warn(e); }
}

let lastOk = null, failed = [];

function showStatusError(e) {
  $("pills").replaceChildren(el("span", { cls: "pill bad" }, [el("span", { cls: "dot" }), "status " + failText(e)]));
}

function showBotError(e) {
  $("perf-sub").textContent = "dados " + failText(e);
}

function showCandlesError(e) {
  $("chart-sub").textContent = "gráfico " + failText(e);
}

// Each part loads and renders on its own: one failing endpoint must not
// blank or freeze the rest of the panel
const PARTS = [
  { path: "status", label: "status", render: renderStatus, fail: showStatusError },
  { path: "candles?limit=500", label: "gráfico", render: renderCandles, fail: showCandlesError },
  { path: "bot", label: "robô", render: (b) => { $("perf-sub").textContent = "desde o início da sessão"; renderBot(b); },
    fail: showBotError },
];

async function refresh() {
  document.body.classList.add("loading");
  try {
    const results = await Promise.allSettled(PARTS.map((part) => api(part.path)));
    const bad = [];
    results.forEach((r, i) => {
      const part = PARTS[i];
      try {
        if (r.status !== "fulfilled") throw r.reason;
        part.render(r.value);
      } catch (e) {
        console.warn(e);
        bad.push(part.label);
        try { part.fail(e); } catch (_) { /* keep going */ }
      }
    });
    await refreshTab();
    if (bad.length < PARTS.length) lastOk = Date.now();
    failed = bad;
  } finally {
    document.body.classList.remove("loading");
    tickUpdated();
  }
}

function tickUpdated() {
  const u = $("updated");
  if (lastOk == null) { u.textContent = "sem conexão com o painel"; return; }
  const age = (Date.now() - lastOk) / 1000;
  let text = age > REFRESH_MS / 1000 * 2.5 ? `sem atualizar há ${fmtAge(age)}` : `atualizado há ${fmtAge(age)}`;
  if (failed.length) text += ` · falhou: ${failed.join(", ")}`;
  u.textContent = text;
}

function init() {
  if (!LWC) {
    $("chart-sub").textContent = "biblioteca de gráficos não carregou";
    return;
  }
  initCharts();
  for (const b of document.querySelectorAll('[role="tab"]')) b.addEventListener("click", () => selectTab(b));
  for (const b of document.querySelectorAll("#log-select button")) {
    b.addEventListener("click", () => {
      activeLog = b.dataset.log;
      for (const o of document.querySelectorAll("#log-select button")) o.setAttribute("aria-pressed", String(o === b));
      renderLogs().catch(console.warn);
    });
  }
  refresh();
  setInterval(refresh, REFRESH_MS);
  setInterval(tickUpdated, 1000);
}

document.addEventListener("DOMContentLoaded", init);
