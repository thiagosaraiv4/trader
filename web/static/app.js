/* Trader panel — reads /api/v1/* and renders. Read-only.
   All data reaches the DOM through textContent (never innerHTML). */
"use strict";

const REFRESH_MS = 30_000;
const INITIAL = 10_000;
const LWC = window.LightweightCharts;
const TZ_SHIFT = -new Date().getTimezoneOffset() * 60;   // charts show local time

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
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
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
let equityChart, equitySeries, equityBase;
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

let lastCandles = null;

function renderCandles(c) {
  lastCandles = c;
  $("chart-title").textContent = `${c.symbol} · ${c.timeframe}`;
  $("sym").textContent = c.symbol;
  if (!c.bars.length) {
    $("chart-sub").textContent = "sem candles gravados ainda (live_bars.csv)";
    return;
  }
  $("chart-sub").textContent = `${c.bars.length} candles fechados que o robô processou`;
  candleSeries.setData(c.bars.map((b) => ({ ...b, time: b.time + TZ_SHIFT })));

  const names = Object.keys(c.indicators);
  for (const name of Object.keys(indicatorSeries)) {
    if (!names.includes(name)) { priceChart.removeSeries(indicatorSeries[name]); delete indicatorSeries[name]; }
  }
  names.forEach((name, i) => {
    if (!indicatorSeries[name]) {
      indicatorSeries[name] = priceChart.addLineSeries({
        color: css(SERIES_VARS[i % SERIES_VARS.length]), lineWidth: 2,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      });
    }
    indicatorSeries[name].setData(c.indicators[name].map((p) => ({ time: p.time + TZ_SHIFT, value: p.value })));
  });

  // Markers sit on the bar where the fill happened
  const tf = c.timeframe_sec || 60;
  const barTimes = new Set(c.bars.map((b) => b.time));
  const markers = [];
  for (const m of c.markers) {
    const t = Math.floor(m.time / tf) * tf;
    if (!barTimes.has(t)) continue;
    if (m.kind === "entry") {
      markers.push({ time: t + TZ_SHIFT, position: "belowBar", shape: "arrowUp", color: css("--series-1"), text: "C" });
    } else {
      markers.push({ time: t + TZ_SHIFT, position: "aboveBar", shape: "arrowDown",
                     color: m.net >= 0 ? css("--good") : css("--critical"),
                     text: "V " + fmtUsd(m.net, 0) });
    }
  }
  markers.sort((a, b) => a.time - b.time);
  candleSeries.setMarkers(markers);
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
  if (!bar) {
    const b = lastCandles.bars[lastCandles.bars.length - 1];
    bar = { ...b, time: b.time + TZ_SHIFT };
  }
  const ohlc = el("span", { cls: "key" }, [
    "A ", el("b", { text: fmtNum(bar.open) }), " M ", el("b", { text: fmtNum(bar.high) }),
    " m ", el("b", { text: fmtNum(bar.low) }), " F ", el("b", { text: fmtNum(bar.close) }),
  ]);
  legend.append(ohlc);
  Object.keys(indicatorSeries).forEach((name, i) => {
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
    let t = Math.floor(p.time) + TZ_SHIFT;
    if (t <= lastT) t = lastT + 1;            // strictly increasing times
    pts.push({ time: t, value: p.value });
    lastT = t;
  }
  equitySeries.setData(pts);
  equityChart.timeScale().fitContent();
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

  const pos = $("position");
  pos.replaceChildren();
  const kv = (k, v, cls = "") => { pos.append(el("dt", { text: k }), el("dd", { text: v, cls })); };
  const p = b.position;
  if (!p) {
    kv("Situação", "Sem posição");
    kv("Aguardando", "rompimento do canal");
  } else {
    kv("Lado", "Comprado");
    kv("Quantidade", fmtNum(p.size, 6) + " BTC");
    kv("Entrada", fmtNum(p.entry_price));
    kv("Preço atual", fmtNum(p.mark_price));
    kv("Stop", fmtNum(p.stop_price));
    kv("Resultado aberto", `${fmtUsd(p.unrealized)} (${fmtPct(p.unrealized_pct)})`, signCls(p.unrealized));
  }
  if (b.halted) kv("Circuit breaker", "ACIONADO", "neg");
  renderPriceLines(p);

  const params = $("params");
  params.replaceChildren();
  const pr = b.params || {};
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

let lastOk = null;

async function refresh() {
  document.body.classList.add("loading");
  try {
    const [status, bot, candles] = await Promise.all([api("status"), api("bot"), api("candles?limit=500")]);
    renderStatus(status);
    renderCandles(candles);
    renderBot(bot);
    await refreshTab();
    lastOk = Date.now();
  } catch (e) {
    console.warn(e);
  } finally {
    document.body.classList.remove("loading");
    tickUpdated();
  }
}

function tickUpdated() {
  const u = $("updated");
  if (lastOk == null) { u.textContent = "sem conexão com o painel"; return; }
  const age = (Date.now() - lastOk) / 1000;
  u.textContent = age > REFRESH_MS / 1000 * 2.5 ? `sem atualizar há ${fmtAge(age)}` : `atualizado há ${fmtAge(age)}`;
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
