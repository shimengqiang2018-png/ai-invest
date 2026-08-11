/* 动量轮动策略台 — 前端逻辑（零依赖，原生 JS + Canvas） */
"use strict";

/* 启动轨迹上报：每一步都会写到 server.log，便于定位卡在哪一步 */
function trace(msg) {
  try {
    fetch("/api/trace?msg=" + encodeURIComponent(String(msg).slice(0, 300)), {
      keepalive: true,
    }).catch(() => {});
  } catch (err) {
    /* 忽略：trace 失败不影响主流程 */
  }
}
trace("app.js 已加载 (v5)");

/* ============================================================
 * 工具函数
 * ========================================================== */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function esc(v) {
  return String(v ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fmtNum(v, d = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return Number(v).toLocaleString("zh-CN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

function fmtNum3(v) {
  return fmtNum(v, 3);
}

function fmtPct(v, d = 2, sign = false) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  const prefix = sign && Number(v) > 0 ? "+" : "";
  return prefix + Number(v).toFixed(d) + "%";
}

function pctCls(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "";
  return Number(v) > 0 ? "up" : Number(v) < 0 ? "down" : "";
}

function shortName(name, code) {
  if (!name || name === code) return code;
  return name.length > 8 ? name.slice(0, 8) : name;
}

function statusMeta(status) {
  return {
    ok: ["正常", "ok"],
    provisional: ["盘中 / 非正式", "provisional"],
    no_signal: ["无信号", "no_signal"],
    unknown: ["异常", "unknown"],
  }[status] || [status || "—", "unknown"];
}

function strategyLabel(key) {
  return {
    momentum: "动量轮动",
    momentum_cash: "动量现金",
    grid: "网格核心",
    grid_observe: "网格观察",
    grid_sell_only: "网格只卖",
    base: "底仓",
    core: "网格核心",
    observe: "网格观察",
    sell_only: "网格只卖",
    reduce_base: "减仓后底仓",
    cash: "现金储备",
  }[key] || key || "其他";
}

const STRATEGY_COLORS = {
  动量轮动: "#4c9aff",
  网格核心: "#2ebd85",
  网格观察: "#f0b90b",
  网格只卖: "#f6465d",
  底仓: "#9d8cff",
  减仓后底仓: "#e58bff",
  动量现金: "#5f6c7d",
  现金储备: "#8b98a9",
};

function strColor(label) {
  return STRATEGY_COLORS[label] || "#8b98a9";
}

/* ============================================================
 * 全局状态
 * ========================================================== */

const state = {
  pools: null,
  activeTab: "overview",
  signals: null,
  overview: null,
  backtest: null,
  audit: null,
  screener: null,
  etfScan: null,
  positions: null,
  enumData: null,
  kline: new Map(),
  realtimeTimer: null,
  signalsItems: null,
  signalsSelected: null,
  signalsRt: null,
  signalsSort: { key: null, dir: "desc" },
  parseResult: null,
  parseImages: [],
  gridParseImages: [],
  dbOffset: 0,
};

let toastTimer = null;

function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isErr ? " err" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 3600);
}

let loadingTick = null;

function setLoading(on, text = "加载中…") {
  const box = $("#loading");
  box.hidden = !on;
  const el = $("#loading-text");
  el.textContent = text;
  clearInterval(loadingTick);
  if (on) {
    const t0 = Date.now();
    loadingTick = setInterval(() => {
      const secs = Math.round((Date.now() - t0) / 1000);
      if (secs >= 5) {
        el.textContent =
          `${text}… 已等待 ${secs}s（首次联网刷新可能需要 1-3 分钟）`;
      }
    }, 1000);
  }
}

async function api(path, { quiet = false, progress = null, timeoutMs = 0, method = "GET", body = null } = {}) {
  // 默认超时：任何请求都不能无限等待（服务重启/网络断开时页面才不会卡死）
  if (!timeoutMs) timeoutMs = quiet ? 45000 : 90000;
  const ctrl = timeoutMs > 0 ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), timeoutMs) : null;
  if (!quiet) setLoading(true, progress || "请求数据…");
  try {
    const res = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl ? ctrl.signal : undefined,
    });
    let payload;
    try {
      payload = await res.json();
    } catch {
      throw new Error("服务返回了无法解析的内容");
    }
    if (!res.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${res.status}`);
    return payload;
  } catch (err) {
    if (err && err.name === "AbortError") {
      throw new Error("等待超时（请求仍在后台处理，可稍后刷新查看结果）");
    }
    trace(`api error ${path}: ${err && err.message ? err.message : String(err)}`);
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
    if (!quiet) setLoading(false);
  }
}

function setServerStatus(ok, text) {
  const dot = $("#serverDot");
  dot.className = "dot " + (ok ? "ok" : "err");
  $("#serverStatus").textContent = text;
}

function renderFatalError(err) {
  setLoading(false);
  setServerStatus(false, "初始化失败");
  $("#dataAsOf").textContent = "加载失败: " + (err && err.message ? err.message : String(err));
  let box = $("#fatalBox");
  if (!box) {
    box = document.createElement("div");
    box.id = "fatalBox";
    box.className = "fatal-box";
    document.querySelector(".main").prepend(box);
  }
  box.innerHTML = `
    <div class="fatal-title">页面初始化失败</div>
    <div class="fatal-msg">${esc(err && err.message ? err.message : String(err))}</div>
    <button class="btn" id="fatalReload">重新加载</button>`;
  $("#fatalReload").addEventListener("click", () => location.reload());
}

window.addEventListener("error", (ev) => {
  const msg = ev && ev.error && ev.error.message ? ev.error.message : (ev.message || "未知脚本错误");
  trace("window error: " + msg);
  renderFatalError(new Error(msg));
});
window.addEventListener("unhandledrejection", (ev) => {
  const reason = ev && ev.reason;
  trace("unhandled rejection: " + (reason && reason.message ? reason.message : String(reason)));
  renderFatalError(reason instanceof Error ? reason : new Error(String(reason)));
});

function applyEnvelope(body) {
  if (body.cached) {
    const badge = $("#cacheBadge");
    badge.hidden = false;
    badge.textContent = body.stale ? "使用旧缓存（网络失败）" : "已缓存";
    badge.className = "badge" + (body.stale ? " stale" : "");
  } else {
    $("#cacheBadge").hidden = true;
  }
  if (body.server_time) {
    const t = new Date(body.server_time);
    $("#lastUpdate").textContent = "更新于 " + t.toLocaleString("zh-CN");
  }
  return body.data;
}

/* ============================================================
 * 页面切换
 * ========================================================== */

const PAGE_TITLES = {
  overview: "总览",
  signals: "信号扫描",
  grid: "网格",
  backtest: "回测分析",
  screener: "选品池",
  positions: "持仓",
  db: "数据存储",
  audit: "风险审计",
};

function switchTab(name) {
  if (state.realtimeTimer) {
    clearInterval(state.realtimeTimer);
    state.realtimeTimer = null;
  }
  trace("action: 切换页面 " + name);
  state.activeTab = name;
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  $("#pageTitle").textContent = PAGE_TITLES[name];
  $("#cacheBadge").hidden = true;
  history.replaceState(null, "", "#" + name);
  renderTab(name);
}

async function renderTab(name, force = false) {
  try {
    if (name === "overview") await renderOverview(force);
    else if (name === "signals") await renderSignals(force);
    else if (name === "grid") await renderGrid(force);
    else if (name === "backtest") await renderBacktest(force);
    else if (name === "screener") await renderScreener(force);
    else if (name === "positions") await renderPositions();
    else if (name === "db") await renderDb();
    else if (name === "audit") await renderAudit(force);
  } catch (err) {
    toast("加载失败: " + err.message, true);
    setServerStatus(false, "接口异常");
  }
}

/* ============================================================
 * 总览
 * ========================================================== */

async function renderOverview(force = false) {
  const positionsP = !state.positions || force
    ? api("/api/positions", { quiet: true })
        .then((pbody) => { state.positions = applyEnvelope(pbody); })
        .catch(() => null)
    : Promise.resolve();
  const body = await api("/api/overview" + (force ? "?refresh=1" : ""), {
    progress: "正在运行策略监测（动量+网格+风险审计）…",
    timeoutMs: force ? 600000 : 120000,
  });
  state.overview = applyEnvelope(body);
  trace("overview data ok");
  const ov = state.overview;
  const mom = ov.momentum || {};
  const risk = ov.risk || {};
  const gridGroups = ov.grid_groups || {};

  const [momLabel, momCls] = statusMeta(mom.status);
  const momSelected = mom.selected;
  const asOf = mom.as_of || "—";
  $("#dataAsOf").textContent = `动量信号日期: ${asOf}`;

  const cards = [
    {
      k: "动量状态",
      v: momLabel,
      d: momSelected
        ? `目标: ${momSelected.code} ${momSelected.name}`
        : mom.status === "no_signal"
        ? "切换 511880 银华日利"
        : "—",
      cls: momCls === "ok" ? "" : momCls === "unknown" ? "warn" : "",
    },
    {
      k: "RSRS 目标",
      v: momSelected ? `${momSelected.code}` : "—",
      d: momSelected
        ? `${momSelected.name} · ${momSelected.signal_strength || "none"}`
        : "—",
      cls: momSelected ? "up" : "",
    },
    {
      k: "Sharpe / MaxDD",
      v: risk.sharpe != null ? fmtNum(risk.sharpe) : "—",
      d: risk.max_dd_pct != null ? `MaxDD ${fmtPct(risk.max_dd_pct)}` : "审计不可用",
      cls: risk.sharpe != null && risk.sharpe >= 1 ? "up" : "warn",
    },
    {
      k: "VaR(95%) / IC",
      v: risk.var_95_loss_pct != null ? fmtPct(risk.var_95_loss_pct) : "—",
      d:
        risk.ic_10d != null
          ? `IC10 ${fmtNum(risk.ic_10d)} · IC20 ${fmtNum(risk.ic_20d)}`
          : "—",
      cls: "",
    },
    {
      k: "网格组",
      v: [
        gridGroups.stop ? `停 ${gridGroups.stop.length}` : "",
        gridGroups.caution ? `警 ${gridGroups.caution.length}` : "",
        gridGroups.ok ? `稳 ${gridGroups.ok.length}` : "",
      ].filter(Boolean).join(" · ") || "—",
      d: "stop / caution / ok",
      cls: "",
    },
  ];
  $("#ov-cards").innerHTML = cards
    .map(
      (c) => `
      <div class="card">
        <div class="k">${esc(c.k)}</div>
        <div class="v ${esc(c.cls)}">${esc(c.v)}</div>
        <div class="d">${esc(c.d)}</div>
      </div>`
    )
    .join("");

  const advice = ov.advice || {};
  const adviceRows = [
    { t: "动量轮动", v: advice.momentum_action || "无正式动作", cls: "momentum" },
    { t: "网格趋势", v: advice.grid_action || "无正式动作", cls: "grid" },
  ];
  $("#ov-advice").innerHTML = adviceRows
    .map(
      (a) => `
      <div class="advice-row ${a.cls}">
        <div class="a-t">${esc(a.t)}</div>
        <div>${esc(a.v)}</div>
      </div>`
    )
    .join("");

  const items = (mom.items || []).slice(0, 4);
  $("#ov-signals").innerHTML = items.length
    ? `
    <table>
      <thead><tr>
        <th>代码</th><th>名称</th><th>RSRS</th><th>年化斜率</th>
        <th>R²</th><th>信号</th><th>通过</th>
      </tr></thead>
      <tbody>
        ${items
          .map((it) => {
            const isSel = momSelected && it.code === momSelected.code;
            return `
            <tr class="${isSel ? "sel" : ""}">
              <td>${esc(it.code)}</td>
              <td>${esc(it.name)}</td>
              <td class="num">${fmtNum(it.rsrs_score, 3)}</td>
              <td class="num ${pctCls(it.slope_annual_pct)}">${fmtPct(it.slope_annual_pct, 1, true)}</td>
              <td class="num">${fmtNum(it.r_squared, 2)}</td>
              <td><span class="tag ${esc(it.signal_strength || "none")}">${esc(it.signal_strength || "none")}</span></td>
              <td>${it.pass ? '<span class="pass-yes">✓</span>' : '<span class="pass-no">✗</span>'}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无动量信号数据</div>';

  await positionsP;
  if (state.positions && state.positions.holdings) {
    renderPositionChips("ov-positions", 6);
  } else {
    $("#ov-positions").innerHTML = '<div class="empty">暂无持仓数据</div>';
  }
  trace("overview render done");
}

/* ============================================================
 * 信号扫描
 * ========================================================== */

async function renderSignals(force) {
  const pools = await ensurePools();
  populateSignalPools(pools);
  const pool = $("#sig-pool").value;
  const momentum = $("#sig-momentum").value;
  const custom = $("#sig-custom").value.trim();
  state.lastSignalsPool = pool;
  const query = custom && /^\d{6}(,\d{6})*$/.test(custom)
    ? `pool=${encodeURIComponent(custom)}&momentum=${momentum}`
    : `pool=${pool}&momentum=${momentum}`;
  const res = await api(`/api/signals?${query}${force ? "&refresh=1" : ""}`, {
    progress: "正在扫描动量信号…",
    timeoutMs: force ? 300000 : 60000,
  });
  state.signals = applyEnvelope(res);
  const sig = state.signals;
  // 联动：动量周期与回测分析页保持一致
  syncBacktestMomentum(momentum);
  // 联动：若用回测预设池扫描，同步回测分析页的预设选择
  if (!custom && pools.backtest_presets && pools.backtest_presets[pool]) {
    syncBacktestPreset(pool);
  }

  const [label, cls] = statusMeta(sig.status);
  $("#dataAsOf").textContent = `动量信号日期: ${sig.as_of || "—"}`;
  $("#sig-note").textContent =
    `${sig.pool_label || ""} · 状态 ${label} · 数据完整 ${sig.pool_complete ? "是" : "否"}`;
  $("#sig-subtitle").textContent =
    `RSRS ${sig.momentum_period || 25}日 · MA20 · 截至 ${sig.as_of || "—"} · ${(sig.items || []).length} 只`;

  let adviceHtml = "";
  if (sig.selected) {
    adviceHtml = `
      <div class="advice-item">
        <div class="a-k">建议目标</div>
        <div class="a-v">${esc(sig.selected.code)} ${esc(sig.selected.name)}
          <span class="tag strong">${esc(sig.selected.signal_strength)}</span></div>
      </div>`;
  }
  if (sig.rotation) {
    adviceHtml += `
      <div class="advice-item">
        <div class="a-k">轮动决策</div>
        <div class="a-v">${esc(sig.rotation.action || "—")}
          ${sig.rotation.target ? `→ ${esc(sig.rotation.target.code)} ${esc(sig.rotation.target.name)}` : ""}</div>
      </div>`;
  }
  if (!adviceHtml && sig.status === "no_signal") {
    adviceHtml = `
      <div class="advice-item">
        <div class="a-k">轮动决策</div>
        <div class="a-v">无标的通过 → 持币 / 511880 银华日利</div>
      </div>`;
  }
  $("#sig-advice").innerHTML = adviceHtml;

  const items = sig.items || [];
  const selectedCode = sig.selected && sig.selected.code;
  state.signalsItems = items;
  state.signalsSelected = selectedCode;
  renderSignalsTable();

  // 实时行情：加载一次后每 30 秒自动刷新（只刷新价格列，不重跑信号）
  const rtCodes = items.map((it) => it.code).filter(Boolean);
  if (rtCodes.length) {
    loadRealtimeIntoTable(rtCodes);
    clearInterval(state.realtimeTimer);
    state.realtimeTimer = setInterval(() => loadRealtimeIntoTable(rtCodes), 30000);
  }

  // 调试/自动化钩子: ?autokline=1 时自动加载选中目标的 K 线
  if (new URLSearchParams(location.search).get("autokline") === "1") {
    const target = selectedCode || (items[0] && items[0].code);
    const row = target && $(`#sig-table tr[data-code="${target}"]`);
    if (row) row.click();
  }
  // 调试/自动化钩子: ?autosort=rsrs 时按指定列自动排序
  const autoSort = new URLSearchParams(location.search).get("autosort");
  if (autoSort) sortSignals(autoSort);
}

/* ============================================================
 * 信号表格渲染 + 列排序
 * ========================================================== */

const SIG_COLUMNS = [
  ["code", "代码"],
  ["name", "名称"],
  ["rt_price", "实时价"],
  ["close", "收盘(日K)"],
  ["rsrs", "RSRS"],
  ["slope", "年化斜率"],
  ["r2", "R²"],
  ["ma", "MA20"],
  ["ma60", "MA60"],
  ["vol20", "波动20"],
  ["vol_ratio", "波动/中位"],
  ["rsi", "RSI"],
  ["pct5", "5日"],
  ["pct20", "20日"],
  ["c_rsrs", "RSRS✓"],
  ["c_ma", "MA20✓"],
  ["c_vol", "波动✓"],
  ["c_volr", "量能✓"],
  ["c_rsi", "RSI✓"],
  ["strength", "信号"],
  ["pass", "通过"],
];

function sigSortValue(key, it) {
  const num = (v) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };
  switch (key) {
    case "code": return String(it.code || "");
    case "name": return String(it.name || "");
    case "rt_price":
      return state.signalsRt && state.signalsRt.quotes
        ? num(state.signalsRt.quotes[it.code] && state.signalsRt.quotes[it.code].price)
        : null;
    case "close": return num(it.close);
    case "rsrs": return num(it.rsrs_score);
    case "slope": return num(it.slope_annual_pct);
    case "r2": return num(it.r_squared);
    case "ma": return num(it.ma);
    case "ma60": return num(it.ma60);
    case "vol20": return num(it.vol_20d);
    case "vol_ratio": return num(it.metrics && it.metrics.volatility_ratio);
    case "rsi": return num(it.rsi);
    case "pct5": return num(it.pct_5d);
    case "pct20": return num(it.pct_20d);
    case "c_rsrs": return !!(it.metrics && it.metrics.rsrs_ok);
    case "c_ma": return !!it.above_ma;
    case "c_vol": return !!(it.metrics && it.metrics.volatility_ok);
    case "c_volr": return !!(it.metrics && it.metrics.volume_ok);
    case "c_rsi": return !!(it.metrics && it.metrics.rsi_ok);
    case "strength": return { strong: 3, medium: 2, none: 1 }[it.signal_strength] || 0;
    case "pass": return !!it.pass;
    default: return null;
  }
}

function compareSignals(a, b, key, dir) {
  const va = sigSortValue(key, a);
  const vb = sigSortValue(key, b);
  let result;
  if (va == null && vb == null) result = 0;
  else if (va == null) result = 1;   // 空值排最后
  else if (vb == null) result = -1;
  else if (typeof va === "boolean") result = va === vb ? 0 : va ? 1 : -1;
  else if (typeof va === "number") result = va - vb;
  else result = String(va).localeCompare(String(vb), "zh-CN");
  return dir === "desc" ? -result : result;
}

function renderSignalsTable() {
  const items = state.signalsItems || [];
  const selectedCode = state.signalsSelected;
  const sort = state.signalsSort || { key: null, dir: "desc" };
  const sorted = sort.key
    ? [...items].sort((a, b) => compareSignals(a, b, sort.key, sort.dir))
    : items;
  if (!items.length) {
    $("#sig-table").innerHTML = '<div class="empty">无信号数据（检查数据缓存或网络）</div>';
    return;
  }
  const arrow = (key) =>
    sort.key === key ? (sort.dir === "asc" ? " ▲" : " ▼") : "";
  const head = SIG_COLUMNS.map(([key, label]) => {
    const cls = ["sortable", sort.key === key ? "sorted" : ""].filter(Boolean).join(" ");
    return `<th class="${cls}" data-key="${key}">${label}${arrow(key)}</th>`;
  }).join("");
  const body = sorted
    .map((it, i) => {
      const m = it.metrics || {};
      const ok = (v) => (v ? '<span class="pass-yes">✓</span>' : '<span class="pass-no">✗</span>');
      return `
      <tr data-code="${esc(it.code)}" data-name="${esc(it.name)}" class="${it.code === selectedCode ? "sel" : ""}">
        <td class="dim">${i + 1}</td>
        <td>${esc(it.code)}</td>
        <td>${esc(it.name)}</td>
        <td class="rt-price num">—</td>
        <td class="num">${fmtNum(it.close)}</td>
        <td class="num">${fmtNum(it.rsrs_score, 3)}</td>
        <td class="num ${pctCls(it.slope_annual_pct)}">${fmtPct(it.slope_annual_pct, 1, true)}</td>
        <td class="num">${fmtNum(it.r_squared, 2)}</td>
        <td class="num">${fmtNum(it.ma)}</td>
        <td class="num">${fmtNum(it.ma60)}</td>
        <td class="num">${fmtNum(it.vol_20d, 1)}%</td>
        <td class="num">${fmtNum(m.volatility_ratio, 2)}</td>
        <td class="num">${fmtNum(it.rsi, 1)}</td>
        <td class="num ${pctCls(it.pct_5d)}">${fmtPct(it.pct_5d, 2, true)}</td>
        <td class="num ${pctCls(it.pct_20d)}">${fmtPct(it.pct_20d, 2, true)}</td>
        <td>${ok(m.rsrs_ok)}</td>
        <td>${ok(it.above_ma)}</td>
        <td>${ok(m.volatility_ok)}</td>
        <td>${ok(m.volume_ok)}</td>
        <td>${ok(m.rsi_ok)}</td>
        <td><span class="tag ${esc(it.signal_strength || "none")}">${esc(it.signal_strength || "none")}</span></td>
        <td>${it.pass ? '<span class="pass-yes">✓</span>' : '<span class="pass-no">✗</span>'}</td>
      </tr>`;
    })
    .join("");
  $("#sig-table").innerHTML = `
    <table>
      <thead><tr><th>#</th>${head}</tr></thead>
      <tbody>${body}</tbody>
    </table>`;
  $$("#sig-table tr[data-code]").forEach((row) => {
    row.addEventListener("click", () => loadKlineChart(row.dataset.code, row.dataset.name));
  });
  if (state.signalsRt) renderRealtimeCells(state.signalsRt);
}

function sortSignals(key) {
  const prev = state.signalsSort || { key: null, dir: "desc" };
  let dir;
  if (prev.key === key) {
    dir = prev.dir === "asc" ? "desc" : "asc";
  } else {
    const sample = sigSortValue(key, (state.signalsItems || [])[0] || {});
    dir = typeof sample === "string" ? "asc" : "desc";
  }
  state.signalsSort = { key, dir };
  trace("action: 信号排序 " + key + " " + dir);
  renderSignalsTable();
}

/* ============================================================
 * 网格策略
 * ========================================================== */

async function renderGrid(force) {
  const body = await api("/api/grid" + (force ? "?refresh=1" : ""), {
    progress: "正在分析网格标的…",
    timeoutMs: force ? 180000 : 60000,
  });
  const grid = body.data;
  const items = grid.items || [];
  const groups = grid.groups || {};
  const groupMeta = [
    ["stop", "⛔ 暂停买入", groups.stop || []],
    ["caution", "⚠ 谨慎", groups.caution || []],
    ["ok", "✅ 正常", groups.ok || []],
    ["unknown", "— 未知", groups.unknown || []],
  ];
  $("#grid-groups").innerHTML = groupMeta
    .map(([key, label, codes]) => {
      if (!codes.length) return "";
      return `
        <div class="advice-item">
          <div class="a-k">${label}（${codes.length}）</div>
          <div class="a-v" style="font-size:12.5px">${esc(codes.join(" "))}</div>
        </div>`;
    })
    .join("");
  $("#grid-subtitle").textContent = `${items.length} 只标的 · 刷新于 ${(grid.as_of || "").slice(0, 19).replace("T", " ")}`;
  $("#grid-note").textContent = "";

  $("#grid-table").innerHTML = items.length
    ? `
    <table>
      <thead><tr>
        <th>代码</th><th>名称</th><th>现价</th><th>基准价</th>
        <th>买入触发</th><th>卖出触发</th><th>评分</th><th>BB宽</th>
        <th>MA状态</th><th>判断</th><th>触发次数</th><th>最近触发</th>
      </tr></thead>
      <tbody>
        ${items
          .map((g) => {
            const score = g.score;
            const scoreCls = score == null ? "dim" : score <= -4 ? "down" : score <= -2 ? "warn" : "up";
            const last = g.last_trigger;
            const lastText = last
              ? `${last.date} ${last.action === "buy" ? "买" : "卖"} ${fmtNum3(last.price)} × ${fmtNum(last.shares, 0)}`
              : "—";
            const cur = g.current_price;
            const sellPrice = g.sell_price;
            const buyPrice = g.buy_price;
            const hitSell =
              cur != null && sellPrice != null && cur >= sellPrice
                ? '<span class="pass-yes"> 卖✓</span>'
                : "";
            const hitBuy =
              cur != null && buyPrice != null && cur <= buyPrice
                ? '<span class="pass-no"> 买✓</span>'
                : "";
            return `
            <tr title="${esc(g.error || g.verdict || "")}">
              <td>${esc(g.code)}</td>
              <td>${esc(g.name)}</td>
              <td class="num ${pctCls(g.change_pct)}">${fmtNum3(cur)}${hitSell}${hitBuy}</td>
              <td class="num">${fmtNum3(g.base_price)}</td>
              <td class="num">${fmtNum3(buyPrice)}</td>
              <td class="num">${fmtNum3(sellPrice)}</td>
              <td class="num ${scoreCls}">${score == null ? "—" : score}</td>
              <td class="num">${g.bb_width != null ? fmtPct(g.bb_width, 1) : "—"}</td>
              <td>${esc(g.ma_state || "—")}</td>
              <td class="dim">${esc(g.verdict || g.error || "—")}</td>
              <td class="num">${g.trigger_count || 0}</td>
              <td class="num dim">${esc(lastText)}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无网格配置（tools/grid_trading.py CONFIGS 为空）</div>';

  const positions = grid.positions || [];
  const totalMv = positions.reduce((s, p) => s + (p.market_value || 0), 0);
  const totalPnl = positions.reduce((s, p) => s + (p.pnl || 0), 0);
  $("#grid-positions-summary").textContent = positions.length
    ? `${positions.length} 只 · 总市值 ¥${fmtNum(totalMv, 2)} · 浮动盈亏 ${
        totalPnl >= 0 ? "+" : ""
      }¥${fmtNum(totalPnl, 2)}`
    : "暂无网格持仓";
  $("#grid-positions").innerHTML = positions.length
    ? `
    <table>
      <thead><tr><th>代码</th><th>名称</th><th>策略归属</th><th>总持仓</th>
        <th>底仓</th><th>网格仓</th><th>现价</th><th>成本</th><th>市值</th>
        <th>浮动盈亏</th><th>盈亏率</th><th>当前基准价</th><th>备注</th></tr></thead>
      <tbody>
        ${positions
          .map((p) => {
            const pnl = p.pnl;
            const pnlCls = pnl == null ? "dim" : pnl >= 0 ? "up" : "down";
            const note = p.note
              ? `<span class="down" title="${esc(p.note)}">⚠ ${esc(p.note)}</span>`
              : '<span class="dim">—</span>';
            return `
            <tr>
              <td>${esc(p.code)}</td>
              <td>${esc(p.name)}</td>
              <td>${esc(p.strategy)}</td>
              <td class="num">${fmtNum(p.total_shares, 0)}</td>
              <td class="num dim">${fmtNum(p.base_position, 0)}</td>
              <td class="num">${fmtNum(p.grid_position, 0)}</td>
              <td class="num">${fmtNum3(p.price)}</td>
              <td class="num dim">${fmtNum3(p.cost)}</td>
              <td class="num">${fmtNum(p.market_value, 2)}</td>
              <td class="num ${pnlCls}">${pnl == null ? "—" : (pnl >= 0 ? "+" : "") + fmtNum(pnl, 2)}</td>
              <td class="num ${pnlCls}">${p.pnl_pct == null ? "—" : fmtPct(p.pnl_pct, 2)}</td>
              <td class="num">${fmtNum3(p.base_price)}</td>
              <td class="dim">${note}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无网格持仓</div>';

  const codeSel = $("#grid-trigger-code");
  if (codeSel && !codeSel.options.length) {
    items.forEach((g) => {
      const opt = document.createElement("option");
      opt.value = g.code;
      opt.textContent = `${g.code} ${g.name || ""}`;
      codeSel.appendChild(opt);
    });
    if (codeSel.options.length && !$("#grid-trigger-date").value) {
      $("#grid-trigger-date").value = new Date().toISOString().slice(0, 10);
    }
    if (codeSel.options.length && !$("#grid-trigger-time").value) {
      $("#grid-trigger-time").value = new Date().toTimeString().slice(0, 5);
    }
  }
  await loadModelProviders("grid-trigger-provider");
  await loadGridTriggers();
}

async function loadGridTriggers() {
  const code = $("#grid-trigger-filter-code").value.trim();
  const type = $("#grid-trigger-filter-type").value;
  const start = $("#grid-trigger-filter-start").value;
  const end = $("#grid-trigger-filter-end").value;
  const query = new URLSearchParams({ limit: "500" });
  if (code) query.set("code", code);
  if (type) query.set("trigger_type", type);
  if (start) query.set("start", start);
  if (end) query.set("end", end);
  let records = [];
  try {
    const body = await api(`/api/grid/triggers/list?${query}`, { quiet: true });
    records = body.data.records || [];
  } catch (err) {
    $("#grid-triggers").innerHTML =
      `<div class="empty">查询失败：${esc(err.message)}</div>`;
    return;
  }
  $("#grid-triggers-count").textContent = `共 ${records.length} 条`;
  $("#grid-triggers").innerHTML = records.length
    ? `
    <table>
      <thead><tr><th>日期</th><th>动作</th><th>类型</th><th>代码</th><th>名称</th>
        <th>价格</th><th>数量</th><th>基准价变化</th><th>来源</th></tr></thead>
      <tbody>
        ${records
          .map((t) => `
            <tr>
              <td class="num">${esc(t.trigger_date || "")}</td>
              <td class="${t.action === "buy" ? "up" : "down"}">${t.action === "buy" ? "买入" : "卖出"}</td>
              <td>${esc({ grid: "网格", add: "加仓", reduce: "减仓", momentum: "动量" }[t.trigger_type] || "网格")}</td>
              <td>${esc(t.code)}</td>
              <td>${esc(t.name || "—")}</td>
              <td class="num">${fmtNum3(t.price)}</td>
              <td class="num">${fmtNum(t.shares, 0)}</td>
              <td class="num dim">${fmtNum3(t.base_price_before)} → ${fmtNum3(t.base_price_after)}</td>
              <td class="dim">${esc(t.source || "—")}</td>
            </tr>`)
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无匹配的触发记录</div>';
}

async function addGridTrigger() {
  const payload = {
    code: $("#grid-trigger-code").value,
    date: $("#grid-trigger-date").value,
    time: $("#grid-trigger-time").value,
    action: $("#grid-trigger-action").value,
    trigger_type: $("#grid-trigger-type").value,
    price: $("#grid-trigger-price").value,
    shares: $("#grid-trigger-shares").value,
  };
  const bb = $("#grid-trigger-bb").value;
  const ba = $("#grid-trigger-ba").value;
  if (bb) payload.base_price_before = bb;
  if (ba) payload.base_price_after = ba;
  if (!payload.code || !payload.date || !payload.price || !payload.shares) {
    toast("请填写完整（标的/日期/价格/数量）", true);
    return;
  }
  try {
    const body = await api("/api/grid/triggers", { method: "POST", body: payload });
    const rec = body.data.record;
    toast(
      `${body.data.db_status === "inserted" ? "已录入" : "记录已存在（更新）"} ` +
        `${rec.code} ${rec.date} ${rec.action === "buy" ? "买入" : "卖出"} ` +
        `${fmtNum3(rec.price)}×${rec.shares}`
    );
    ["#grid-trigger-price", "#grid-trigger-shares", "#grid-trigger-bb", "#grid-trigger-ba"]
      .forEach((id) => ($(id).value = ""));
    await renderGrid(true);
  } catch (err) {
    toast("录入失败: " + err.message, true);
  }
}

async function parseGridTriggers() {
  const files = state.gridParseImages || [];
  if (!files.length) {
    toast("请先选择成交截图", true);
    return;
  }
  const provider = $("#grid-trigger-provider").value;
  if (!provider) {
    toast("未配置可用识别模型", true);
    return;
  }
  try {
    $("#grid-trigger-note").textContent = "识别中（模型调用可能需要 10-60 秒）…";
    const body = await api("/api/grid/triggers/parse", {
      method: "POST",
      body: { provider, images: files },
      timeoutMs: 120000,
    });
    $("#grid-trigger-note").textContent =
      `识别完成（${body.data.pipeline === "vision" ? "视觉模型" : "本地OCR+模型"}），` +
      `共 ${body.data.records.length} 条，请核验后确认录入`;
    renderGridParseResult(body.data.records);
  } catch (err) {
    $("#grid-trigger-note").textContent = "";
    toast("识别失败: " + err.message, true);
  }
}

function renderGridParseResult(records) {
  const box = $("#grid-trigger-parse-result");
  if (!records.length) {
    box.innerHTML = '<div class="empty">未识别到成交记录</div>';
    return;
  }
  box.innerHTML = `
    <div class="advice-banner" id="grid-trigger-verify-hint"></div>
    <table>
      <thead><tr><th>核验</th><th>代码</th><th>名称</th><th>日期</th><th>时间</th>
        <th>动作</th><th>类型</th><th>价格</th><th>数量</th><th>基准价前</th><th>基准价后</th><th></th></tr></thead>
      <tbody>
        ${records
          .map((r, i) => gridTriggerRowHtml(r, i))
          .join("")}
      </tbody>
    </table>
    <div class="toolbar">
      <button class="btn" id="grid-trigger-add-row">+ 添加一行</button>
      <button class="btn" id="grid-trigger-drop-verify">移除需核对</button>
      <button class="btn" id="grid-trigger-confirm">确认录入 ${records.length} 条</button>
      <button class="btn" id="grid-trigger-clear">清空</button>
    </div>`;
  updateGridTriggerHint();
  $("#grid-trigger-add-row").addEventListener("click", () => {
    const tbody = box.querySelector("tbody");
    const index = tbody ? tbody.children.length : 0;
    tbody.insertAdjacentHTML(
      "beforeend",
      gridTriggerRowHtml({ action: "buy" }, index)
    );
    const count = box.querySelectorAll("tbody tr").length;
    const btn = $("#grid-trigger-confirm");
    if (btn) btn.textContent = `确认录入 ${count} 条`;
    updateGridTriggerHint();
  });
  $("#grid-trigger-drop-verify").addEventListener("click", () => {
    const rows = [...box.querySelectorAll("tbody tr")];
    let removed = 0;
    rows.forEach((tr) => {
      if (!gridTriggerRowValid(collectGridTriggerRow(tr))) {
        tr.remove();
        removed++;
      }
    });
    const count = box.querySelectorAll("tbody tr").length;
    const btn = $("#grid-trigger-confirm");
    if (btn) btn.textContent = `确认录入 ${count} 条`;
    $("#grid-trigger-note").textContent = removed
      ? `已移除 ${removed} 条待核对记录，剩余 ${count} 条`
      : `没有待核对记录（共 ${count} 条）`;
    updateGridTriggerHint();
  });
  $("#grid-trigger-confirm").addEventListener("click", confirmGridTriggers);
  $("#grid-trigger-clear").addEventListener("click", () => {
    box.innerHTML = "";
    $("#grid-trigger-note").textContent = "";
    state.gridParseImages = [];
    $("#grid-trigger-files").value = "";
  });
}

function gridTriggerRowHtml(r, i) {
  const dup = r.duplicate ? '<span class="tag unknown">重复</span>' : "";
  const verified = r.status === "ok" ? "1" : "0";
  const warnHtml = (r.warns || []).length
    ? `<div class="dim">${esc((r.warns || []).join("；"))}</div>`
    : "";
  const issueHtml = (r.issues || []).length
    ? `<div class="warn">${esc((r.issues || []).join("；"))}</div>`
    : "";
  return `
  <tr data-idx="${i}" data-status="${esc(r.status || "")}"
      data-dup="${r.duplicate ? "1" : "0"}" data-verified="${verified}">
    <td>${verifyBadge(r.status)}${dup}${warnHtml}${issueHtml}</td>
    <td><input data-f="code" value="${esc(r.code || "")}" size="6"></td>
    <td class="dim">${esc(r.name || "—")}</td>
    <td><input data-f="date" value="${esc(r.date || "")}" size="10"></td>
    <td><input data-f="time" type="time" value="${esc(r.time || "")}" size="6"></td>
    <td>
      <select data-f="action">
        <option value="buy"${r.action === "buy" ? " selected" : ""}>买入</option>
        <option value="sell"${r.action === "sell" ? " selected" : ""}>卖出</option>
      </select>
    </td>
    <td>
      <select data-f="trigger_type">
        ${["grid", "add", "reduce", "momentum"]
          .map(
            (t) =>
              `<option value="${t}"${(r.trigger_type || "grid") === t ? " selected" : ""}>${
                { grid: "网格", add: "加仓", reduce: "减仓", momentum: "动量" }[t]
              }</option>`
          )
          .join("")}
      </select>
    </td>
    <td><input data-f="price" type="number" step="0.001" value="${r.price ?? ""}" size="7"></td>
    <td><input data-f="shares" type="number" step="1" value="${r.shares ?? ""}" size="7"></td>
    <td><input data-f="base_price_before" type="number" step="0.001" value="${r.base_price_before ?? ""}" size="7"></td>
    <td><input data-f="base_price_after" type="number" step="0.001" value="${r.base_price_after ?? ""}" size="7"></td>
    <td>
      <button class="btn mini" data-verify>${verified === "1" ? "✓已核实" : "核实"}</button>
      <button class="btn mini" data-del title="删除该行">删除</button>
    </td>
  </tr>`;
}

function gridTriggerVerifyState() {
  const rows = [...$$("#grid-trigger-parse-result tbody tr")];
  return {
    total: rows.length,
    verified: rows.filter((tr) => tr.dataset.verified === "1").length,
    pending: rows.filter((tr) => tr.dataset.verified !== "1").length,
    warn: rows.filter((tr) => tr.dataset.status === "warn").length,
    err: rows.filter((tr) => tr.dataset.status === "error").length,
    dup: rows.filter((tr) => tr.dataset.dup === "1").length,
  };
}

function updateGridTriggerHint() {
  const hint = $("#grid-trigger-verify-hint");
  if (!hint) return;
  const s = gridTriggerVerifyState();
  if (s.pending) {
    hint.innerHTML =
      `<div class="advice-item"><div class="a-k">⚠ 待核实 ${s.pending} 条</div>` +
      `<div class="a-v" style="font-size:12.5px">逐条点击行内「核实」按钮；错误/警告行也可删除或修改字段后重新核实</div></div>`;
  } else {
    hint.innerHTML =
      `<div class="advice-item"><div class="a-k">✓ 全部 ${s.total} 条已核实</div>` +
      `<div class="a-v" style="font-size:12.5px">${s.dup ? `其中 ${s.dup} 条与历史重复，确认时自动跳过` : "无重复记录"}，可点「确认录入」</div></div>`;
  }
}

function collectGridTriggerRow(tr) {
  const record = {};
  $$("input[data-f], select[data-f]", tr).forEach((el) => {
    const key = el.dataset.f;
    const value = el.value.trim();
    if (
      key === "price" ||
      key === "base_price_before" ||
      key === "base_price_after" ||
      key === "shares"
    ) {
      record[key] = value === "" ? null : Number(value);
    } else {
      record[key] = value;
    }
  });
  return record;
}

function gridTriggerRowValid(r) {
  const code = String(r.code || "").trim();
  const date = String(r.date || "").trim();
  const time = String(r.time || "").trim();
  const action = String(r.action || "").trim();
  const type = String(r.trigger_type || "grid").trim();
  const price = Number(r.price);
  const shares = Number(r.shares);
  return (
    /^\d{6}$/.test(code) &&
    /^\d{4}-\d{2}-\d{2}$/.test(date) &&
    (!time || /^\d{2}:\d{2}(:\d{2})?$/.test(time)) &&
    (action === "buy" || action === "sell") &&
    ["grid", "add", "reduce", "momentum"].includes(type) &&
    price > 0 &&
    Number.isInteger(shares) &&
    shares > 0
  );
}

async function confirmGridTriggers() {
  const rows = [...$$("#grid-trigger-parse-result tbody tr")];
  const records = rows.map(collectGridTriggerRow);
  const invalidCount = records.filter((r) => !gridTriggerRowValid(r)).length;
  if (invalidCount) {
    toast(
      `还有 ${invalidCount} 条记录待核对（请修正，或点「移除需核对」）`,
      true
    );
    return;
  }
  const verifyState = gridTriggerVerifyState();
  if (verifyState.pending) {
    toast(
      `还有 ${verifyState.pending} 条待核实，请逐条点击行内「核实」后再确认`,
      true
    );
    return;
  }
  try {
    const body = await api("/api/grid/triggers/confirm", {
      method: "POST",
      body: { records },
      timeoutMs: 60000,
    });
    toast(`确认录入 ${body.data.added} 条（新增 ${body.data.records.filter((r) => r.db_status === "inserted").length} / 重复 ${body.data.records.filter((r) => r.db_status === "duplicate").length}）`);
    $("#grid-trigger-parse-result").innerHTML = "";
    $("#grid-trigger-note").textContent = "";
    state.gridParseImages = [];
    $("#grid-trigger-files").value = "";
    await renderGrid(true);
  } catch (err) {
    toast("确认失败: " + err.message, true);
  }
}

async function renderGridOptimize(force) {
  const codes = $("#grid-opt-codes").value.trim();
  const capital = ($("#grid-opt-capital").value || "100000").trim();
  const start = $("#grid-opt-start").value;
  const query =
    `codes=${encodeURIComponent(codes)}&capital=${capital}` +
    `&start=${start}${force ? "&refresh=1" : ""}`;
  const body = await api(`/api/grid/optimize?${query}`, {
    progress: "正在并行扫描网格参数寻优（多标的并行，首次可能需要 1-2 分钟）…",
    timeoutMs: force ? 900000 : 180000,
  });
  const data = body.data;
  const results = data.results || [];
  const best = Object.values(data.best_per_code || {});
  const params = data.params || {};
  $("#grid-opt-note").textContent =
    `${params.codes ? params.codes.length : 0} 个标的 × ` +
    `内置候选（间距 ${(params.spacings || []).join("/")}%、层数 ${(params.levels || []).join("/")}、` +
    `每格金额 ${(params.grid_values || []).join("/")} 元折算股数）= ${results.length} 组 · ` +
    `资金 ${params.capital || "—"} · ${params.start || "2y"}`;

  $("#grid-opt-best").innerHTML = best.length
    ? `
    <table>
      <thead><tr>
        <th>代码</th><th>名称</th><th>最优间距</th><th>层数</th><th>每格股数</th>
        <th>每格金额</th><th>基准价</th><th>价格区间</th><th>持仓区间</th>
        <th>年化</th><th>总收益</th><th>MaxDD</th><th>Sharpe</th><th>胜率</th><th>等级</th>
      </tr></thead>
      <tbody>
        ${best
          .map((r) => {
            const gc2 = r.grid_config || {};
            const pr = gc2.price_range || {};
            const posr = gc2.position_range || {};
            return `
            <tr class="sel">
              <td>${esc(r.code)}</td>
              <td>${esc(r.name)}</td>
              <td class="num">${r.spacing}%</td>
              <td class="num">${r.levels}</td>
              <td class="num">${fmtNum(r.shares, 0)}</td>
              <td class="num">${r.grid_value != null ? "¥" + fmtNum(r.grid_value, 0) : "—"}</td>
              <td class="num">${gc2.base_price != null ? fmtNum3(gc2.base_price) : "—"}</td>
              <td class="num">${pr.min != null ? `${fmtNum3(pr.min)}~${fmtNum3(pr.max)}` : "—"}</td>
              <td class="num">${posr.min != null ? `${fmtNum(posr.min, 0)}~${fmtNum(posr.max, 0)}` : "—"}</td>
              <td class="num ${pctCls(r.annual_return_pct)}">${fmtPct(r.annual_return_pct, 2, true)}</td>
              <td class="num ${pctCls(r.total_return_pct)}">${fmtPct(r.total_return_pct, 2, true)}</td>
              <td class="num down">${fmtPct(r.max_dd_pct, 2)}</td>
              <td class="num">${fmtNum(r.sharpe)}</td>
              <td class="num">${fmtPct(r.win_rate_pct, 1)}</td>
              <td class="num">${esc(r.grade || "—")}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无优化结果</div>';

  const sorted = [...results].sort(
    (a, b) => (b.annual_return_pct || 0) - (a.annual_return_pct || 0)
  ).slice(0, 100);
  $("#grid-opt-results").innerHTML = sorted.length
    ? `
    <div class="muted" style="margin-bottom:8px">共 ${results.length} 组，仅显示按年化前 100 组</div>
    <table>
      <thead><tr>
        <th>代码</th><th>名称</th><th>间距%</th><th>层数</th><th>每格</th>
        <th>基准价</th><th>价格区间</th><th>年化</th><th>总收益</th><th>MaxDD</th><th>Sharpe</th>
        <th>胜率</th><th>买/卖</th><th>交易</th><th>等级</th>
      </tr></thead>
      <tbody>
        ${sorted
          .map((r) => {
            const gc3 = r.grid_config || {};
            const pr3 = gc3.price_range || {};
            return `
            <tr title="${esc(gc3.trigger_desc || "")}">
              <td>${esc(r.code)}</td>
              <td>${esc(r.name)}</td>
              <td class="num">${r.spacing}%</td>
              <td class="num">${r.levels}</td>
              <td class="num">${fmtNum(r.shares, 0)}</td>
              <td class="num">${gc3.base_price != null ? fmtNum3(gc3.base_price) : "—"}</td>
              <td class="num">${pr3.min != null ? `${fmtNum3(pr3.min)}~${fmtNum3(pr3.max)}` : "—"}</td>
              <td class="num ${pctCls(r.annual_return_pct)}">${fmtPct(r.annual_return_pct, 2, true)}</td>
              <td class="num ${pctCls(r.total_return_pct)}">${fmtPct(r.total_return_pct, 2, true)}</td>
              <td class="num down">${fmtPct(r.max_dd_pct, 2)}</td>
              <td class="num">${fmtNum(r.sharpe)}</td>
              <td class="num">${fmtPct(r.win_rate_pct, 1)}</td>
              <td class="num">${r.triggered_buy ?? 0}/${r.triggered_sell ?? 0}</td>
              <td class="num">${r.trades ?? 0}</td>
              <td class="num">${esc(r.grade || "—")}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无数据</div>';
}

async function loadRealtimeIntoTable(codes) {
  try {
    const body = await api(`/api/realtime?codes=${encodeURIComponent(codes.join(","))}`, {
      quiet: true,
    });
    renderRealtimeCells(body.data || {});
  } catch (err) {
    /* 实时行情失败时保留表格里的收盘价，不做打扰 */
  }
}

function renderRealtimeCells(rt) {
  state.signalsRt = rt;
  const quotes = rt.quotes || {};
  const live = !!rt.live;
  const badge = $("#sig-live");
  if (badge) {
    if (live) {
      badge.textContent = `● 实时 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
      badge.className = "live-badge on";
    } else {
      badge.textContent = "○ 实时行情不可用（显示最近收盘）";
      badge.className = "live-badge off";
    }
  }
  $$("#sig-table tr[data-code]").forEach((row) => {
    const q = quotes[row.dataset.code];
    const cell = row.querySelector(".rt-price");
    if (!q || !cell) return;
    if (q.source === "tencent_realtime") {
      const cls = pctCls(q.change_pct);
      cell.innerHTML =
        `<span class="${cls}">${fmtNum(q.price)}</span>` +
        (q.change_pct != null
          ? `<span class="${cls}" style="font-size:11px;display:block">${fmtPct(q.change_pct, 2, true)}</span>`
          : "");
    } else {
      cell.innerHTML = `<span>${fmtNum(q.price)}</span><span class="dim" style="font-size:11px;display:block">收盘</span>`;
    }
  });
}

async function loadKlineChart(code, name) {
  $("#sig-chart-title").textContent = `${code} ${name} · 最近 300 日`;
  try {
    const body = await api(`/api/kline?code=${code}&count=300`, { quiet: true });
    const data = body.data;
    state.kline.set(code, data);
    drawKlineChart($("#sig-chart"), data.bars, code);
    const meta = data.meta || {};
    const firstDate = data.bars.length ? data.bars[0].date : "—";
    const lastDate = data.bars.length ? data.bars[data.bars.length - 1].date : "—";
    $("#sig-legend").innerHTML = `
      <span><span class="sw" style="background:#f6465d"></span>阳线（涨）</span>
      <span><span class="sw" style="background:#0ecb81"></span>阴线（跌）</span>
      <span><span class="sw" style="background:#4c9aff"></span>MA20</span>
      <span><span class="sw" style="background:#f0b90b"></span>MA60</span>
      <span class="muted">展示 ${esc(firstDate)} ~ ${esc(lastDate)} · ${data.bars.length} 根（${esc(meta.source)}）</span>`;
  } catch (err) {
    toast("K 线加载失败: " + err.message, true);
  }
}

/* ============================================================
 * 回测分析
 * ========================================================== */

async function ensurePools() {
  if (state.pools) return state.pools;
  const body = await api("/api/pools", { quiet: true });
  state.pools = body.data;
  return state.pools;
}

/* 把回测分析预设池加入信号池下拉（内容联动） */
function populateSignalPools(pools) {
  const sel = $("#sig-pool");
  if (sel.dataset.built) return;
  sel.dataset.built = "1";
  const presets = Object.entries(pools.backtest_presets || {});
  if (!presets.length) return;
  const group = document.createElement("optgroup");
  group.label = "回测预设池（与回测分析联动）";
  presets.forEach(([key, p]) => {
    const opt = document.createElement("option");
    opt.value = key;
    opt.textContent = `${key} — ${p.desc}`;
    group.appendChild(opt);
  });
  sel.appendChild(group);
}

function syncBacktestPreset(value) {
  const bp = $("#bt-preset");
  if (!bp || !bp.options.length) return;
  if ([...bp.options].some((o) => o.value === value)) bp.value = value;
}

function syncSignalPool(value) {
  const sp = $("#sig-pool");
  if (!sp) return;
  if ([...sp.options].some((o) => o.value === value)) sp.value = value;
}

function syncBacktestMomentum(value) {
  const bm = $("#bt-momentum");
  if (!bm) return;
  if ([...bm.options].some((o) => o.value === value)) bm.value = value;
}

function syncSignalMomentum(value) {
  const sm = $("#sig-momentum");
  if (!sm) return;
  if ([...sm.options].some((o) => o.value === value)) sm.value = value;
}

async function renderBacktest(force) {
  const pools = await ensurePools();
  const presetSel = $("#bt-preset");
  if (presetSel.options.length === 0) {
    Object.entries(pools.backtest_presets || {}).forEach(([key, p]) => {
      const opt = document.createElement("option");
      opt.value = key;
      opt.textContent = `${key} — ${p.desc}`;
      presetSel.appendChild(opt);
    });
    presetSel.value =
      state.lastSignalsPool && pools.backtest_presets[state.lastSignalsPool]
        ? state.lastSignalsPool
        : "best4";
    presetSel.addEventListener("change", () => syncSignalPool(presetSel.value));
  }
  const preset = presetSel.value;
  const momentum = $("#bt-momentum").value;
  const freq = $("#bt-freq").value;
  const start = $("#bt-start").value;
  const commission = $("#bt-commission").value;
  const minCommission = $("#bt-min-commission").value;
  const body = await api(
    `/api/backtest?preset=${preset}&momentum=${momentum}&freq=${freq}` +
      `&start=${start}&commission=${commission}&min_commission=${minCommission}` +
      `${force ? "&refresh=1" : ""}`,
    {
      progress: "正在运行回测（首次可能需要 1-3 分钟）…",
      timeoutMs: force ? 600000 : 60000,
    }
  );
  state.backtest = applyEnvelope(body);
  const bt = state.backtest;
  const perf = bt.performance || {};
  const period = bt.period || {};

  $("#dataAsOf").textContent = `回测区间: ${period.start || "—"} ~ ${period.end || "—"} · ${period.years || "—"} 年`;
  const startLabel = { full: "全部", "5y": "近5年", "3y": "近3年", "1y": "近1年" }[start] || start;
  const commissionBp = Number(commission) * 10000;
  const commissionLabel =
    commissionBp === 0
      ? "免佣"
      : `佣金万${Number.isInteger(commissionBp) ? commissionBp : commissionBp.toFixed(1)}`;
  $("#bt-note").textContent =
    `${preset} · RSRS ${momentum}日 · ${freq} · 区间 ${startLabel} · ` +
    `${commissionLabel} · ${minCommission === "0" ? "免5" : "最低5元"} · ` +
    `窗口截断 ${period.window_truncated ? "是" : "否"}`;

  const daily = perf.daily || {};
  const cards = [
    ["总收益率", fmtPct(perf.total_return_pct, 1, true), "", pctCls(perf.total_return_pct)],
    ["年化收益", fmtPct(perf.annual_return_pct, 1, true), "", pctCls(perf.annual_return_pct)],
    ["超额收益", fmtPct(perf.excess_return_pct, 1, true), "vs 等权 B&H", pctCls(perf.excess_return_pct)],
    ["最大回撤", fmtPct(perf.max_dd_pct, 1), `${perf.max_dd_days || "—"} 天`, "down"],
    ["Sharpe", fmtNum(perf.sharpe), "", perf.sharpe >= 1 ? "up" : "warn"],
    ["Sortino", fmtNum(perf.sortino), "", ""],
    ["Calmar", fmtNum(perf.calmar), "", ""],
    ["年化波动", fmtPct(perf.annual_vol_pct, 1), "", ""],
    ["交易次数", fmtNum(perf.num_trades, 0), "", ""],
    ["最终净值", "¥" + fmtNum(perf.final_nav, 0), "初始 ¥100,000", ""],
  ];
  $("#bt-cards").innerHTML = cards
    .map(
      ([k, v, d, cls]) => `
      <div class="card">
        <div class="k">${esc(k)}</div>
        <div class="v ${cls}">${esc(v)}</div>
        <div class="d">${esc(d)}</div>
      </div>`
    )
    .join("");

  const nav = bt.daily_nav || [];
  drawLineChart(
    $("#bt-chart"),
    [
      {
        name: "策略净值",
        color: "#4c9aff",
        data: nav.map(([d, v]) => ({ x: d, y: v })),
        yFmt: (v) => "¥" + fmtNum(v, 0),
      },
    ],
    { yLabel: "净值(¥)", valueFmt: (v) => "¥" + fmtNum(v, 0) }
  );
  const bench = perf.benchmark_equal_weight_pct;
  $("#bt-legend").innerHTML = `
    <span><span class="sw" style="background:#4c9aff"></span>策略净值</span>
    <span class="muted">等权 B&H: ${fmtPct(bench, 1, true)} · 年化 ${fmtPct(perf.benchmark_annual_pct, 1, true)}</span>`;

  renderEnumTable();
  renderTradesTable(bt.trades || []);
}

async function renderEnumTable() {
  if (!state.enumData) {
    const body = await api("/api/enum", { quiet: true });
    state.enumData = body.data;
  }
  const enumData = state.enumData;
  const sub = $("#enum-sub");
  if (sub) {
    sub.textContent =
      `${enumData.config || ""} · 生成于 ${(enumData.generated_at || "").slice(0, 10)} · ` +
      `有效 ${enumData.valid_results ?? "—"} 组`;
  }
  const results = (enumData.results || []).slice(0, 10);
  const maxAnn = results.length ? Math.max(...results.map((r) => r.ann)) : 0;
  $("#bt-enum").innerHTML = results.length
    ? `
    <table>
      <thead><tr>
        <th>#</th><th>组合</th><th>年化</th><th>总收益</th>
        <th>最大回撤</th><th>夏普</th><th>卡玛</th><th>胜率</th><th>交易</th>
      </tr></thead>
      <tbody>
        ${results
          .map((r, i) => `
            <tr>
              <td class="dim">${i + 1}</td>
              <td class="num">${esc(r.label || r.combo)}</td>
              <td class="num ${r.ann === maxAnn ? "up" : ""}">${fmtPct(r.ann, 1, true)}</td>
              <td class="num">${fmtPct(r.total, 1, true)}</td>
              <td class="num down">${fmtPct(r.dd, 1)}</td>
              <td class="num">${fmtNum(r.sharpe)}</td>
              <td class="num">${fmtNum(r.calmar)}</td>
              <td class="num">${fmtNum(r.wr, 1)}%</td>
              <td class="num">${r.trades}</td>
            </tr>`)
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无枚举数据</div>';
}

function renderTradesTable(trades) {
  const rows = [...trades].reverse().slice(0, 20);
  const buy = (t) => (t.action || "").includes("买入");
  $("#bt-trades").innerHTML = rows.length
    ? `
    <table>
      <thead><tr>
        <th>日期</th><th>动作</th><th>代码</th><th>名称</th>
        <th>价格</th><th>数量</th><th>金额</th><th>原因</th>
      </tr></thead>
      <tbody>
        ${rows
          .map((t) => `
            <tr>
              <td class="num">${esc(t.date)}</td>
              <td class="${buy(t) ? "up" : "down"}">${esc(t.action)}</td>
              <td>${esc(t.code)}</td>
              <td>${esc(shortName(t.name, t.code))}</td>
              <td class="num">${fmtNum(t.price)}</td>
              <td class="num">${fmtNum(t.shares, 0)}</td>
              <td class="num">${fmtNum(t.amount, 0)}</td>
              <td class="dim">${esc(t.reason || "")}</td>
            </tr>`)
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无交易记录</div>';
}

/* ============================================================
 * 选品池
 * ========================================================== */

async function renderScreener(force) {
  const body = await api(`/api/etf-scan?top=640${force ? "" : ""}`);
  state.etfScan = applyEnvelope(body);
  const scan = state.etfScan;
  const results = scan.results || [];

  const cats = [...new Set(results.map((r) => r.category || "其他"))].sort();
  const catSel = $("#sc-category");
  if (catSel.options.length <= 1) {
    cats.forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c;
      opt.textContent = c;
      catSel.appendChild(opt);
    });
  }
  $("#sc-subtitle").textContent =
    `共 ${scan.total_tested || results.length} 只候选 · 生成于 ${scan.generated_at || "—"}`;

  const topN = Math.min(Number($("#sc-top").value), results.length);
  const cat = catSel.value;
  let filtered = results;
  if (cat) filtered = filtered.filter((r) => r.category === cat);
  filtered = [...filtered]
    .sort((a, b) => (b.composite_score || 0) - (a.composite_score || 0))
    .slice(0, topN);

  $("#sc-table").innerHTML = `
    <table>
      <thead><tr>
        <th>#</th><th>代码</th><th>名称</th><th>类别</th><th>子类</th>
        <th>年化</th><th>最大回撤</th><th>夏普</th><th>波动</th>
        <th>信号胜率10d</th><th>信号胜率20d</th><th>综合评分</th>
      </tr></thead>
      <tbody>
        ${filtered
          .map((r, i) => `
            <tr>
              <td class="dim">${i + 1}</td>
              <td>${esc(r.code)}</td>
              <td>${esc(r.name)}</td>
              <td>${esc(r.category || "")}</td>
              <td class="dim">${esc(r.subcategory || "")}</td>
              <td class="num ${pctCls(r.annual_return)}">${fmtPct(r.annual_return * 100, 1, true)}</td>
              <td class="num down">${fmtPct(r.max_drawdown * 100, 1)}</td>
              <td class="num">${fmtNum(r.sharpe_ratio)}</td>
              <td class="num">${fmtPct(r.volatility * 100, 1)}</td>
              <td class="num">${fmtPct((r.signal_win_rate_10d || 0) * 100, 1)}</td>
              <td class="num">${fmtPct((r.signal_win_rate_20d || 0) * 100, 1)}</td>
              <td class="num"><b>${fmtNum(r.composite_score, 1)}</b></td>
            </tr>`)
          .join("")}
      </tbody>
    </table>`;

  // 四维选品（按需加载）
  try {
    if (!state.screener || force) {
      const sbody = await api("/api/screener" + (force ? "?refresh=1" : ""), {
        quiet: !force,
        progress: "正在运行四维选品（首次可能需要 1-3 分钟）…",
        timeoutMs: force ? 600000 : 60000,
      });
      state.screener = applyEnvelope(sbody);
    }
    renderScreenerRecommend(state.screener);
  } catch (err) {
    $("#sc-recommend").innerHTML = `<div class="empty">四维选品不可用：${esc(err.message)}</div>`;
    $("#sc-corr").innerHTML = "";
  }
}

function renderScreenerRecommend(sc) {
  const rec = sc.recommended || [];
  $("#sc-recommend").innerHTML = rec.length
    ? `
    <table>
      <thead><tr>
        <th>代码</th><th>名称</th><th>类别</th><th>方向</th><th>流动</th>
        <th>独立</th><th>波动</th><th>总分</th><th>CAGR</th><th>日均额</th>
      </tr></thead>
      <tbody>
        ${rec
          .map((r) => `
            <tr>
              <td>${esc(r.code)}</td>
              <td>${esc(r.name)}</td>
              <td>${esc(r.category)}</td>
              <td class="num">${r.s_dir}/5</td>
              <td class="num">${r.s_liq}/5</td>
              <td class="num">${r.s_corr}/5</td>
              <td class="num">${r.s_vol}/5</td>
              <td class="num"><b>${r.total}/20</b></td>
              <td class="num ${pctCls(r.cagr)}">${fmtPct(r.cagr * 100, 0, true)}</td>
              <td class="num">${fmtNum(r.avg_amt, 0)}亿</td>
            </tr>`)
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无推荐</div>';

  renderCorrMatrix(sc.correlation_matrix || {});
}

function renderCorrMatrix(matrix) {
  const codes = Object.keys(matrix).sort();
  if (!codes.length) {
    $("#sc-corr").innerHTML = '<div class="empty">无相关性数据</div>';
    return;
  }
  const cellColor = (v) => {
    const t = Math.max(0, Math.min(1, (v - 0.2) / 0.8));
    const r = Math.round(40 + t * 190);
    const g = Math.round(60 + (1 - t) * 90);
    const b = Math.round(120 + (1 - t) * 90);
    return `rgba(${r},${g},${b},0.85)`;
  };
  const header = `<div class="corr-row"><span class="corr-code"></span>` +
    codes.map((c) => `<span class="corr-cell dim">${esc(c.slice(2))}</span>`).join("") + `</div>`;
  const rows = codes
    .map((c1) => {
      const cells = codes
        .map((c2) => {
          const v = matrix[c1]?.[c2];
          if (v == null) return `<span class="corr-cell">—</span>`;
          return `<span class="corr-cell" style="background:${cellColor(v)};color:#fff" title="${esc(c1)}/${esc(c2)} = ${fmtNum(v, 2)}">${fmtNum(v, 2)}</span>`;
        })
        .join("");
      return `<div class="corr-row"><span class="corr-code">${esc(c1)}</span>${cells}</div>`;
    })
    .join("");
  $("#sc-corr").innerHTML = `<div class="corr-grid">${header}${rows}</div>`;
}

/* ============================================================
 * 持仓
 * ========================================================== */

async function renderPositions() {
  loadModelProviders();
  const [body, gridBody] = await Promise.all([
    api("/api/positions"),
    api("/api/grid", { quiet: true }).catch(() => null),
  ]);
  state.positions = applyEnvelope(body);
  const pos = state.positions;
  const summary = pos.account_summary || {};
  const holdings = pos.holdings || [];
  const gridMap = {};
  ((gridBody && gridBody.data && gridBody.data.items) || []).forEach((g) => {
    gridMap[g.code] = g;
  });
  const dateStr = pos.date ? String(pos.date).slice(0, 10) : "—";
  $("#dataAsOf").textContent = `持仓快照: ${dateStr} · ${pos.source || ""}`;
  $("#pos-subtitle").textContent = `${holdings.length} 只持仓`;

  const cards = [
    ["总资产", "¥" + fmtNum3(summary.total_assets), "", ""],
    ["证券市值", "¥" + fmtNum3(summary.securities_value), "", ""],
    ["可用现金", "¥" + fmtNum3(summary.available_cash), "", ""],
    ["仓位", fmtPct(summary.position_ratio, 3), "", ""],
    ["持仓盈亏", "¥" + fmtNum3(summary.total_pnl), fmtPct(summary.total_pnl_pct, 3, true), pctCls(summary.total_pnl)],
    ["当日盈亏", "¥" + fmtNum3(summary.daily_pnl), fmtPct(summary.daily_pnl_pct, 3, true), pctCls(summary.daily_pnl)],
  ];
  $("#pos-summary").innerHTML = cards
    .map(
      ([k, v, d, cls]) => `
      <div class="card">
        <div class="k">${esc(k)}</div>
        <div class="v ${cls}">${esc(v)}</div>
        <div class="d">${esc(d)}</div>
      </div>`
    )
    .join("");

  // 策略归属：子账户分桶 + 红线提示
  const buckets = pos.strategy_summary || {};
  const bucketColors = {
    网格子账户: "#2ebd85",
    动量子账户: "#4c9aff",
    底仓: "#9d8cff",
    现金储备: "#8b98a9",
    其他: "#5f6c7d",
  };
  const bucketEntries = Object.entries(buckets).sort((a, b) => b[1].market_value - a[1].market_value);
  $("#pos-buckets").innerHTML = bucketEntries.length
    ? bucketEntries
        .map(([name, info]) => {
          const color = bucketColors[name] || "#5f6c7d";
          return `
          <div class="card">
            <div class="k" style="color:${color}">${esc(name)}（${info.count} 只）</div>
            <div class="v">¥${fmtNum(info.market_value, 0)}</div>
            <div class="d">占比 ${info.weight != null ? fmtPct(info.weight, 2) : "—"}%</div>
          </div>`;
        })
        .join("")
    : '<div class="empty">暂无数据</div>';
  const notes = pos.notes || [];
  $("#pos-notes").innerHTML = notes
    .map(
      (n) => `
      <div class="advice-item">
        <div class="a-k">双策略红线</div>
        <div class="a-v" style="font-size:12.5px">${esc(n)}</div>
      </div>`
    )
    .join("");

  $("#pos-table").innerHTML = holdings.length
    ? `
    <table>
      <thead><tr>
        <th>代码</th><th>名称</th><th>持仓</th><th>可用</th><th>现价</th>
        <th>成本</th><th>市值</th><th>持仓盈亏</th><th>盈亏率</th>
        <th>当日盈亏</th><th>子账户</th><th>策略归属</th>
      </tr></thead>
      <tbody>
        ${holdings
          .map((h) => {
            const label = strategyLabel(h.strategy);
            return `
            <tr>
              <td>${esc(h.code)}</td>
              <td>${esc(h.name)}</td>
              <td class="num">${fmtNum(h.shares, 0)}</td>
              <td class="num">${fmtNum(h.available, 0)}</td>
              <td class="num">${fmtNum3(h.price)}</td>
              <td class="num">${fmtNum3(h.cost)}</td>
              <td class="num">${fmtNum3(h.market_value)}</td>
              <td class="num ${pctCls(h.pnl)}">${fmtNum3(h.pnl)}</td>
              <td class="num ${pctCls(h.pnl_pct)}">${fmtPct(h.pnl_pct, 3, true)}</td>
              <td class="num ${pctCls(h.daily_pnl)}">${fmtNum(h.daily_pnl, 0)}</td>
              <td class="dim">${esc(h.bucket || "—")}</td>
              <td><span class="tag" style="background:${strColor(label)}22;color:${strColor(label)}">${esc(label)}</span></td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无持仓数据</div>';

  // 网格标的状态（持仓中属于网格标的的）
  const gridHoldings = holdings.filter((h) => gridMap[h.code]);
  $("#pos-grid-targets").innerHTML = gridHoldings.length
    ? `
    <table>
      <thead><tr>
        <th>代码</th><th>名称</th><th>现价</th><th>基准价</th>
        <th>买入触发</th><th>卖出触发</th><th>趋势评分</th><th>判断</th>
      </tr></thead>
      <tbody>
        ${gridHoldings
          .map((h) => {
            const g = gridMap[h.code];
            const cur = g.current_price;
            const hitSell = cur != null && g.sell_price != null && cur >= g.sell_price;
            const hitBuy = cur != null && g.buy_price != null && cur <= g.buy_price;
            const score = g.score;
            const scoreCls = score == null ? "dim" : score <= -4 ? "down" : score <= -2 ? "warn" : "up";
            return `
            <tr>
              <td>${esc(g.code)}</td>
              <td>${esc(g.name)}</td>
              <td class="num ${pctCls(g.change_pct)}">${fmtNum3(cur)}
                ${hitSell ? '<span class="pass-yes"> 卖✓</span>' : ""}
                ${hitBuy ? '<span class="pass-no"> 买✓</span>' : ""}</td>
              <td class="num">${fmtNum3(g.base_price)}</td>
              <td class="num">${fmtNum3(g.buy_price)}</td>
              <td class="num">${fmtNum3(g.sell_price)}</td>
              <td class="num ${scoreCls}">${score == null ? "—" : score}</td>
              <td class="dim">${esc(g.verdict || g.error || "—")}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">当前持仓中没有网格标的（或网格数据未加载）</div>';

  renderPositionChips("pos-groups", 12, true);
}

/* ============================================================
 * 持仓图片解析（多厂商模型）
 * ========================================================== */

let modelsProviderCache = null;

async function loadModelProviders(selId) {
  try {
    if (!modelsProviderCache) {
      const body = await api("/api/models", { quiet: true });
      modelsProviderCache = body.data.providers || [];
    }
    const providers = modelsProviderCache;
    const targets = selId
      ? [selId]
      : ["pos-provider", "grid-trigger-provider"];
    const usable = providers.filter((p) => p.configured);
    const pool = usable.length ? usable : providers;
    for (const target of targets) {
      const sel = document.getElementById(target);
      if (!sel) continue;
      sel.innerHTML = "";
      if (!pool.length) {
        sel.innerHTML = '<option value="">未配置可用模型</option>';
        continue;
      }
      pool.forEach((p) => {
        const opt = document.createElement("option");
        opt.value = p.name;
        opt.textContent =
          `${p.name}（${p.model}）` +
          `${p.vision ? "" : " · 本地OCR"}` +
          `${p.configured ? "" : " · 未配置Key"}`;
        sel.appendChild(opt);
      });
      const preferred = usable[0] || pool[0];
      sel.value = preferred.name;
    }
  } catch (err) {
    /* 静默：无模型配置时上传按钮会提示 */
  }
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || "");
      const comma = result.indexOf(",");
      resolve({
        name: file.name,
        mime: file.type || "image/jpeg",
        data_b64: comma >= 0 ? result.slice(comma + 1) : result,
      });
    };
    reader.onerror = () => reject(new Error(`读取图片失败: ${file.name}`));
    reader.readAsDataURL(file);
  });
}

function verifyBadge(status) {
  if (status === "ok") return '<span class="tag ok">✓ 通过</span>';
  if (status === "warn") return '<span class="tag medium">⚠ 核实</span>';
  return '<span class="tag unknown">✗ 错误</span>';
}

function renderParseResult() {
  const data = state.parseResult;
  if (!data) return;
  const verified = data.verification || {};
  const meta = $("#pos-parse-meta");
  const statusText =
    verified.status === "ok" ? "通过" : verified.status === "warn" ? "存在警告" : "存在错误";
  const pipeline =
    data.parsed && data.parsed.parse_pipeline === "local_ocr" ? "本地OCR+模型" : "视觉模型";
  meta.textContent =
    `厂商 ${data.provider || "—"} · ${pipeline} · 核验 ${statusText} · ` +
    `${verified.counts.holdings || 0} 只持仓 / ${verified.counts.trades || 0} 笔交易`;

  const summary = verified.account_summary || {};
  const cards = [
    ["总资产", "¥" + fmtNum3(summary.total_assets), "", ""],
    ["证券市值", "¥" + fmtNum3(summary.securities_value), "", ""],
    ["可用资金", "¥" + fmtNum3(summary.available_cash), "", ""],
    ["持仓盈亏", "¥" + fmtNum3(summary.total_pnl), fmtPct(summary.total_pnl, 3, true), pctCls(summary.total_pnl)],
    ["当日盈亏", "¥" + fmtNum3(summary.daily_pnl), "", pctCls(summary.daily_pnl)],
  ];
  const issuesHtml = (summary.issues || []).length
    ? `<div class="advice-row grid" style="grid-column:1/-1">
         <div class="a-t">汇总核验问题</div><div>${esc((summary.issues || []).join("；"))}</div>
       </div>`
    : "";
  const summaryEdit = (summary.issues || []).length
    ? `<div class="advice-row grid" style="grid-column:1/-1">
         <div class="a-t">修改后点「核实汇总」清除问题</div>
         <label>总资产 <input class="edit-input num" data-summary="total_assets" type="number" step="0.01" value="${summary.total_assets ?? ""}"></label>
         <label>证券市值 <input class="edit-input num" data-summary="securities_value" type="number" step="0.01" value="${summary.securities_value ?? ""}"></label>
         <label>可用资金 <input class="edit-input num" data-summary="available_cash" type="number" step="0.01" value="${summary.available_cash ?? ""}"></label>
         <button class="btn ghost" data-summary-verify style="padding:4px 10px;font-size:12px">核实汇总</button>
       </div>`
    : "";
  $("#pos-parse-summary").innerHTML =
    cards
      .map(([k, v, d, cls]) => `
        <div class="card">
          <div class="k">${esc(k)}</div>
          <div class="v ${cls}">${esc(v)}</div>
          <div class="d">${esc(d)}</div>
        </div>`)
      .join("") + (summaryEdit || issuesHtml);

  const holdings = verified.holdings || [];
  const editable = (key, val, step, d) =>
    `<input class="edit-input num" data-edit="${key}" type="number" step="${step}" value="${val ?? ""}" title="${esc(d || "")}">`;
  const numCell = (val, d = 3) => `<span class="num">${fmtNum(val, d)}</span>`;
  $("#pos-parse-holdings").innerHTML = holdings.length
    ? `
    <table>
      <thead><tr>
        <th>代码</th><th>名称</th><th>持仓</th><th>可用</th><th>现价</th>
        <th>成本</th><th>市值</th><th>占比</th><th>盈亏</th><th>盈亏率</th><th>来源</th><th>核验</th><th>操作</th>
      </tr></thead>
      <tbody>
        ${holdings
          .map((h) => {
            const editableRow = h.status !== "ok";
            const action =
              h.status !== "ok"
                ? `<button class="btn ghost" data-verify="${esc(h.code || "")}" style="padding:4px 10px;font-size:12px">核实修改</button>`
                : h.verified
                ? '<span class="pass-yes">已核实 ✓</span>'
                : '<span class="pass-yes">✓</span>';
            return `
            <tr data-code="${esc(h.code || "")}" title="${esc((h.issues || []).join("；") + (h.realtime_note ? "；" + h.realtime_note : ""))}">
              <td>${esc(h.code || "—")}</td>
              <td>${esc(h.name || "—")}</td>
              <td class="num">${editableRow ? editable("shares", h.shares, 1) : numCell(h.shares, 0)}</td>
              <td class="num">${editableRow ? editable("available", h.available, 1) : numCell(h.available, 0)}</td>
              <td class="num">${editableRow ? editable("price", h.price, 0.001) : numCell(h.price)}</td>
              <td class="num">${editableRow ? editable("cost", h.cost, 0.001) : numCell(h.cost)}</td>
              <td class="num">${editableRow ? editable("market_value", h.market_value, 0.001) : numCell(h.market_value)}</td>
              <td class="num">${h.weight_pct != null ? fmtPct(h.weight_pct, 2) : "—"}</td>
              <td class="num ${pctCls(h.pnl)}">${editableRow ? editable("pnl", h.pnl, 0.001) : fmtNum3(h.pnl)}</td>
              <td class="num ${pctCls(h.pnl_pct)}">${editableRow ? editable("pnl_pct", h.pnl_pct, 0.001) : fmtPct(h.pnl_pct, 3, true)}</td>
              <td class="dim">${esc(h.source || "—")}</td>
              <td>${verifyBadge(h.status)}</td>
              <td>${action}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">未识别到持仓</div>';

  const trades = verified.trades || [];
  $("#pos-parse-trades").innerHTML = trades.length
    ? `
    <table>
      <thead><tr>
        <th>日期</th><th>方向</th><th>代码</th><th>名称</th>
        <th>价格</th><th>数量</th><th>金额</th><th>来源</th><th>核验</th><th>操作</th>
      </tr></thead>
      <tbody>
        ${trades
          .map((t, ti) => `
            <tr data-trade-index="${ti}" title="${esc((t.issues || []).join("；"))}">
              <td class="num">${esc(t.date || "—")}</td>
              <td class="${String(t.action || "").includes("卖") ? "down" : "up"}">${esc(t.action || "—")}</td>
              <td>${esc(t.code || "—")}</td>
              <td>${esc(t.name || "—")}</td>
              <td class="num">${t.status === "error" ? `<input class="edit-input num" data-trade-edit="price" type="number" step="0.001" value="${t.price ?? ""}">` : fmtNum3(t.price)}</td>
              <td class="num">${t.status === "error" ? `<input class="edit-input num" data-trade-edit="shares" type="number" step="1" value="${t.shares ?? ""}">` : fmtNum(t.shares, 0)}</td>
              <td class="num">${t.status === "error" ? `<input class="edit-input num" data-trade-edit="amount" type="number" step="0.01" value="${t.amount ?? ""}">` : fmtNum3(t.amount)}</td>
              <td class="dim">${esc(t.source || "—")}</td>
              <td>${verifyBadge(t.status)}</td>
              <td>${t.status === "error" ? `<button class="btn ghost" data-trade-verify style="padding:4px 10px;font-size:12px">核实</button>` : ""}</td>
            </tr>`)
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">未识别到交易记录</div>';

  const cross = verified.cross_validation || [];
  if (cross.length) {
    const cvBox = document.createElement("div");
    cvBox.className = "table-wrap";
    cvBox.style.marginTop = "10px";
    cvBox.innerHTML = `
      <div class="panel-title">成交记录反推核验 <span class="panel-sub">成交反推 vs 截图（偏差>3% 标记错误）</span></div>
      <table>
        <thead><tr>
          <th>代码</th><th>名称</th><th>反推持仓</th><th>截图持仓</th>
          <th>反推成本</th><th>截图成本</th><th>成交笔数</th>
        </tr></thead>
        <tbody>
          ${cross
            .map((c) => {
              const shareDiff =
                c.calc_shares != null && c.reported_shares != null
                  ? Math.abs(c.calc_shares - c.reported_shares) / Math.max(Math.abs(c.calc_shares), 1)
                  : 0;
              const cls = shareDiff > 0.03 ? "warn" : "";
              return `
              <tr class="${cls ? "sel" : ""}">
                <td>${esc(c.code)}</td>
                <td>${esc(c.name || "—")}</td>
                <td class="num">${fmtNum(c.calc_shares, 0)}</td>
                <td class="num">${fmtNum(c.reported_shares, 0)}</td>
                <td class="num">${c.calc_cost != null ? fmtNum(c.calc_cost, 4) : "—"}</td>
                <td class="num">${c.reported_cost != null ? fmtNum(c.reported_cost, 4) : "—"}</td>
                <td class="num">${c.trades || 0}</td>
              </tr>`;
            })
            .join("")}
        </tbody>
      </table>`;
    $("#pos-parse-trades").appendChild(cvBox);
  }

  // ⚠ 核实项：编辑后标记已核实
  $$("#pos-parse-holdings [data-verify]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const row = btn.closest("tr[data-code]");
      const code = row && row.dataset.code;
      const holding = (verified.holdings || []).find((h) => h.code === code);
      if (!holding) return;
      row.querySelectorAll("[data-edit]").forEach((input) => {
        const value = input.value === "" ? null : Number(input.value);
        holding[input.dataset.edit] = value !== null && Number.isFinite(value) ? value : null;
      });
      holding.status = "ok";
      holding.verified = true;
      holding.warnings = [];
      holding.errors = [];
      holding.issues = [];
      recomputeVerified(verified);
      renderParseResult();
      toast(`${holding.name || code} 已核实`);
    });
  });

  // ⚠ 汇总：编辑后核实，清除汇总问题
  const summaryVerifyBtn = $("#pos-parse-summary [data-summary-verify]");
  if (summaryVerifyBtn) {
    summaryVerifyBtn.addEventListener("click", () => {
      $$("#pos-parse-summary [data-summary]").forEach((input) => {
        const value = input.value === "" ? null : Number(input.value);
        summary[input.dataset.summary] = value !== null && Number.isFinite(value) ? value : null;
      });
      summary.issues = [];
      recomputeVerified(verified);
      renderParseResult();
      toast("汇总已核实");
    });
  }

  // ⚠ 交易：编辑后核实
  $$("#pos-parse-trades [data-trade-verify]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const row = btn.closest("tr[data-trade-index]");
      const index = row && Number(row.dataset.tradeIndex);
      const trade = (verified.trades || [])[index];
      if (!trade) return;
      row.querySelectorAll("[data-trade-edit]").forEach((input) => {
        const value = input.value === "" ? null : Number(input.value);
        trade[input.dataset.tradeEdit] = value !== null && Number.isFinite(value) ? value : null;
      });
      trade.status = "ok";
      trade.errors = [];
      trade.issues = [];
      recomputeVerified(verified);
      renderParseResult();
      toast(`${trade.code || ""} 交易已核实`);
    });
  });

  recomputeVerified(verified);

  $("#pos-parse-result").hidden = false;
}

function recomputeVerified(verified) {
  const holdings = verified.holdings || [];
  const trades = verified.trades || [];
  const summary = verified.account_summary || {};
  const hasError =
    holdings.some((h) => h.status === "error") ||
    trades.some((t) => t.status === "error") ||
    (summary.issues || []).length > 0;
  const hasWarn =
    holdings.some((h) => h.status === "warn") ||
    trades.some((t) => t.status === "warn");
  verified.status = hasError ? "error" : hasWarn ? "warn" : "ok";
  verified.counts = {
    holdings: holdings.length,
    trades: trades.length,
    holdings_ok: holdings.filter((h) => h.status === "ok").length,
    holdings_warn: holdings.filter((h) => h.status === "warn").length,
    holdings_error: holdings.filter((h) => h.status === "error").length,
    trades_error: trades.filter((t) => t.status === "error").length,
  };
  $("#pos-confirm").disabled = hasError;
  return !hasError;
}

async function runPositionsParse() {
  const provider = $("#pos-provider").value;
  if (!provider || provider === "未配置视觉模型") {
    toast("请先在 model_config.json 配置视觉模型厂商并设置 API Key", true);
    return;
  }
  if (!state.parseImages.length) {
    toast("请先选择截图", true);
    return;
  }
  $("#pos-parse-status").textContent = "解析中（模型调用可能需要 10-60 秒）…";
  $("#pos-confirm").disabled = true;
  try {
    const body = await api("/api/positions/parse", {
      progress: "正在调用视觉模型解析持仓…",
      timeoutMs: 120000,
      method: "POST",
      body: { provider, images: state.parseImages },
    });
    state.parseResult = body.data;
    renderParseResult();
    $("#pos-confirm").disabled =
      (state.parseResult.verification || {}).status === "error";
    $("#pos-parse-status").textContent = "";
  } catch (err) {
    toast("解析失败: " + err.message, true);
    $("#pos-parse-status").textContent = "解析失败";
  }
}

async function confirmPositionsUpdate() {
  const verified = state.parseResult && state.parseResult.verification;
  if (!verified || !verified.holdings) return;
  if (verified.status === "error") {
    toast("存在核验错误项，无法更新（请重新解析或修正）", true);
    return;
  }
  if (!confirm(`确认将解析结果（${verified.holdings.length} 只持仓）更新到系统持仓？`)) return;
  try {
    const body = await api("/api/positions/update", {
      progress: "正在更新持仓…",
      timeoutMs: 30000,
      method: "POST",
      body: { verified, source: "AI 图片解析" },
    });
    state.positions = body.data;
    discardParseResult();
    await renderPositions();
    toast("持仓已更新到系统" + (body.data.excel_file ? "，已生成 Excel" : ""));
  } catch (err) {
    toast("更新失败: " + err.message, true);
  }
}

function discardParseResult() {
  state.parseResult = null;
  state.parseImages = [];
  $("#pos-parse-result").hidden = true;
  $("#pos-parse-status").textContent = "";
  $("#pos-file").value = "";
  $("#pos-confirm").disabled = true;
}

function renderPositionChips(targetId, limit, showAll = false) {
  const holdings = (state.positions && state.positions.holdings) || (state.overview ? [] : []);
  if (!holdings.length) {
    $("#" + targetId).innerHTML = '<div class="empty">暂无数据</div>';
    return;
  }
  const groups = {};
  holdings.forEach((h) => {
    const label = strategyLabel(h.strategy);
    groups[label] = (groups[label] || 0) + (h.market_value || 0);
  });
  const entries = Object.entries(groups).sort((a, b) => b[1] - a[1]).slice(0, limit);
  const total = entries.reduce((s, [, v]) => s + v, 0);
  $("#" + targetId).innerHTML = entries
    .map(([label, v]) => `
      <div class="chip">
        <div class="c-n" style="color:${strColor(label)}">${esc(label)}</div>
        <div class="c-v">¥${fmtNum(v, 0)}</div>
        <div class="c-d">${total ? ((v / total) * 100).toFixed(1) : 0}%</div>
      </div>`)
    .join("");
}

/* ============================================================
 * 风险审计
 * ========================================================== */

async function renderDb() {
  const body = await api("/api/db/tables", { quiet: true });
  const data = body.data;
  const tables = data.tables_detail || [];
  const dbInfo = data.db_info || {};
  const dbLabel =
    dbInfo.backend === "mysql"
      ? `MySQL（${dbInfo.database || ""}@${dbInfo.host || ""}）`
      : "SQLite";
  const dbDetail = dbInfo.db_path || (dbInfo.host ? `${dbInfo.host}:${dbInfo.port}` : "—");
  const cards = [
    ["数据库", dbLabel, dbDetail],
    ["库大小", fmtNum((data.size_bytes || 0) / 1024, 1) + " KB", ""],
    ...tables.map((t) => [
      t.name,
      fmtNum(t.count, 0) + " 行",
      (t.columns || []).join(", "),
    ]),
  ];
  $("#db-summary").innerHTML = cards
    .map(
      ([k, v, d]) => `
      <div class="card">
        <div class="k">${esc(k)}</div>
        <div class="v" style="font-size:15px">${esc(v)}</div>
        <div class="d">${esc(d)}</div>
      </div>`
    )
    .join("");
  const tableLabels = {
    cache: "cache（结果缓存）",
    positions_snapshots: "positions_snapshots（持仓快照）",
    parse_history: "parse_history（解析历史）",
    api_logs: "api_logs（业务日志）",
    signal_history: "signal_history（信号历史）",
    grid_triggers: "grid_triggers（网格触发）",
    backtest_results: "backtest_results（回测/寻优结果）",
    scheduler_runs: "scheduler_runs（调度历史）",
  };
  const tableSelect = $("#db-table");
  const previous = tableSelect.value;
  tableSelect.innerHTML = tables
    .map(
      (t) =>
        `<option value="${esc(t.name)}">${esc(tableLabels[t.name] || t.name)}</option>`
    )
    .join("");
  if ([...tableSelect.options].some((opt) => opt.value === previous)) {
    tableSelect.value = previous;
  }
  renderDbTable();
  renderDbLogs();
}

function cellValue(value) {
  if (value === null || value === undefined) return '<span class="dim">NULL</span>';
  if (typeof value === "object") {
    const text = JSON.stringify(value, null, 1);
    return `
      <details style="max-width:420px">
        <summary class="dim" style="cursor:pointer">JSON（${text.length} 字符）</summary>
        <pre style="font-size:10.5px;max-height:200px;overflow:auto;background:var(--bg-soft);padding:8px;border-radius:6px">${esc(text)}</pre>
      </details>`;
  }
  const text = String(value);
  if (text.length > 160) {
    return `<details style="max-width:420px">
      <summary class="dim" style="cursor:pointer">${esc(text.slice(0, 80))}…</summary>
      <div style="font-size:11px;word-break:break-all">${esc(text)}</div>
    </details>`;
  }
  return `<span>${esc(text)}</span>`;
}

async function renderDbTable() {
  const table = $("#db-table").value;
  const limit = $("#db-limit").value;
  const offset = state.dbOffset;
  const body = await api(
    `/api/db/table?name=${table}&limit=${limit}&offset=${offset}`,
    { quiet: true }
  );
  const data = body.data;
  const rows = data.rows || [];
  const columns = data.columns || [];
  const colMeta = (data.columns_detail || []).reduce((m, c) => {
    m[c.name] = c;
    return m;
  }, {});
  $("#db-offset").textContent =
    `${table} · 第 ${Math.floor(offset / limit) + 1} 页（每页 ${limit}）· 共 ${data.total ?? rows.length + offset} 行`;
  $("#db-rows").innerHTML = rows.length
    ? `
    <table>
      <thead><tr>${columns
        .map((c) => {
          const comment = colMeta[c] && colMeta[c].comment;
          const tip = comment ? `${esc(c)}（${esc(comment)}）` : esc(c);
          return `<th title="${tip}">${tip}</th>`;
        })
        .join("")}</tr></thead>
      <tbody>
        ${rows
          .map((row) => `<tr>${columns.map((c) => `<td>${cellValue(row[c])}</td>`).join("")}</tr>`)
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">该表暂无数据</div>';
  $("#db-prev").disabled = offset <= 0;
  $("#db-next").disabled = rows.length < Number(limit);
}

async function renderDbLogs() {
  const level = $("#db-log-level").value;
  const limit = $("#db-log-limit").value;
  const body = await api(`/api/logs?limit=${limit}${level ? `&level=${level}` : ""}`, {
    quiet: true,
  });
  const logs = body.data.logs || [];
  $("#db-logs").innerHTML = logs.length
    ? `
    <table>
      <thead><tr><th>时间</th><th>级别</th><th>内容</th></tr></thead>
      <tbody>
        ${logs
          .map((l) => {
            const cls =
              l.level === "ERROR" ? "down" : l.level === "WARN" ? "warn" : "dim";
            return `
            <tr>
              <td class="num">${esc((l.ts || "").slice(0, 19).replace("T", " "))}</td>
              <td class="${cls}">${esc(l.level || "")}</td>
              <td>${esc(l.message || "")}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无日志</div>';
}

async function renderAudit(force) {
  const body = await api(`/api/audit${force ? "?refresh=1" : ""}`, {
    progress: "正在运行风险审计（首次可能需要 1-3 分钟）…",
    timeoutMs: force ? 600000 : 60000,
  });
  state.audit = applyEnvelope(body);
  const au = state.audit;
  const daily = au.daily_metrics || {};
  const ic = au.ic_ir || {};
  const stress = au.stress_test || {};

  $("#dataAsOf").textContent = "风险审计 · RSRS v2.1 四资产池";
  $("#au-note").textContent = `日频样本 ${daily.count || "—"} 天 · IC 样本 ${ic.n_dates || "—"} 期`;

  const metrics = [
    ["年化收益", fmtPct(daily.annual_return_pct, 1, true), "", pctCls(daily.annual_return_pct)],
    ["年化波动", fmtPct(daily.annual_vol_pct, 1), "", ""],
    ["Sharpe", fmtNum(daily.sharpe), "无风险 2.5%", daily.sharpe >= 1 ? "up" : "warn"],
    ["Sortino", fmtNum(daily.sortino), "", ""],
    ["Calmar", fmtNum(daily.calmar), "", ""],
    ["最大回撤", fmtPct(daily.max_dd_pct, 1), `${daily.max_dd_start || ""} ~ ${daily.max_dd_end || ""}`, "down"],
    ["VaR(95%) 日", fmtPct(daily.var_95_daily_pct, 2), "", ""],
    ["VaR(99%) 日", fmtPct(daily.var_99_daily_pct, 2), "", ""],
    ["CVaR(95%) 日", fmtPct(daily.cvar_95_daily_pct, 2), "", ""],
    ["偏度", fmtNum(daily.skewness), "", ""],
    ["超额峰度", fmtNum(daily.kurtosis), "", ""],
    ["日均收益", fmtPct(daily.avg_daily_ret_pct, 3, true), "", pctCls(daily.avg_daily_ret_pct)],
    ["日胜率", fmtPct(daily.win_rate_pct, 1), "", ""],
    ["盈亏比", fmtNum(daily.win_loss_ratio), "", ""],
  ];
  $("#au-daily").innerHTML = metrics
    .map(
      ([k, v, d, cls]) => `
      <div class="metric">
        <div class="m-k">${esc(k)}</div>
        <div class="m-v ${cls}">${esc(v)}</div>
        <div class="m-d">${esc(d)}</div>
      </div>`
    )
    .join("");

  const icRows = [10, 20, 40].map((fwd) => {
    const icV = ic[`ic_${fwd}d`];
    const icStd = ic[`ic_std_${fwd}d`];
    const ir = ic[`ir_${fwd}d`];
    let verdict = "样本不足";
    let cls = "dim";
    if (ir != null) {
      verdict = ir > 0.5 ? "有效" : ir > 0.3 ? "偏弱" : "不足";
      cls = ir > 0.5 ? "up" : ir > 0.3 ? "warn" : "down";
    }
    return `
      <tr>
        <td class="num">${fwd} 日</td>
        <td class="num">${fmtNum(icV, 4)}</td>
        <td class="num">${fmtNum(icStd, 4)}</td>
        <td class="num">${ir != null ? fmtNum(ir, 2) : "—"}</td>
        <td class="${cls}">${esc(verdict)}</td>
      </tr>`;
  }).join("");
  $("#au-ic").innerHTML = `
    <table>
      <thead><tr><th>前向窗口</th><th>IC mean</th><th>IC std</th><th>IR</th><th>判定</th></tr></thead>
      <tbody>
        ${icRows}
        <tr>
          <td class="dim">汇总(20日)</td>
          <td class="num">${fmtNum(ic.ic_mean, 4)}</td>
          <td class="num">${fmtNum(ic.ic_median, 4)}</td>
          <td class="num">${ic.ic_positive_ratio != null ? fmtPct(ic.ic_positive_ratio * 100, 0) : "—"}</td>
          <td class="dim">正比率</td>
        </tr>
      </tbody>
    </table>`;

  const scenarios = stress.scenarios || [];
  $("#au-stress").innerHTML = scenarios.length
    ? `
    <table>
      <thead><tr><th>情景</th><th>期间</th><th>资产回撤</th><th>策略收益</th></tr></thead>
      <tbody>
        ${scenarios
          .map((s) => `
            <tr>
              <td>${esc(s.scenario)}</td>
              <td class="num">${esc(s.period)}</td>
              <td class="num down">${fmtPct(s.asset_dd_pct, 1)}</td>
              <td class="num ${pctCls(s.strategy_return_pct)}">${s.strategy_return_pct != null ? fmtPct(s.strategy_return_pct, 1, true) : "—"}</td>
            </tr>`)
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无压力测试数据</div>';

  const varMetrics = [
    ["VaR 95%", fmtPct(stress.var_95_pct, 2), `¥${fmtNum(stress.var_95_amount, 0)}`],
    ["VaR 99%", fmtPct(stress.var_99_pct, 2), `¥${fmtNum(stress.var_99_amount, 0)}`],
    ["CVaR 95%", fmtPct(stress.cvar_95_pct, 2), `¥${fmtNum(stress.cvar_95_amount, 0)}`],
  ];
  $("#au-var").innerHTML = varMetrics
    .map(
      ([k, v, d]) => `
      <div class="metric">
        <div class="m-k">${esc(k)}</div>
        <div class="m-v down">${esc(v)}</div>
        <div class="m-d">${esc(d)}</div>
      </div>`
    )
    .join("");
}

/* ============================================================
 * Canvas 图表
 * ========================================================== */

function setupCanvas(canvas, height) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const W = Math.max(rect.width || 800, 200);
  canvas.width = W * dpr;
  canvas.height = (height || 300) * dpr;
  canvas.style.height = (height || 300) + "px";
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, height || 300);
  return { ctx, W, H: height || 300 };
}

function drawGrid(ctx, W, H, pad, yTicks, xLabels) {
  ctx.strokeStyle = "rgba(38,50,65,0.45)";
  ctx.lineWidth = 1;
  ctx.font = "10px SF Mono, Menlo, monospace";
  ctx.fillStyle = "#5f6c7d";
  yTicks.forEach(({ y, label }) => {
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(W - pad.r, y);
    ctx.stroke();
    ctx.fillText(label, 4, y + 3);
  });
  xLabels.forEach(({ x, label }) => {
    ctx.fillText(label, x - 16, H - 8);
  });
}

function drawLineChart(canvas, seriesList, opts = {}) {
  const height = opts.height || 340;
  const { ctx, W, H } = setupCanvas(canvas, height);
  const pad = { l: 64, r: 16, t: 12, b: 26 };
  const iw = W - pad.l - pad.r;
  const ih = H - pad.t - pad.b;
  const all = seriesList.flatMap((s) => s.data.map((p) => Number(p.y)));
  const valid = all.filter((v) => Number.isFinite(v));
  if (!valid.length) {
    ctx.fillStyle = "#5f6c7d";
    ctx.font = "13px sans-serif";
    ctx.fillText("暂无数据", W / 2 - 24, H / 2);
    return;
  }
  const minY = Math.min(...valid);
  const maxY = Math.max(...valid);
  const span = maxY - minY || 1;
  const yPad = span * 0.08;
  const yMin = minY - yPad;
  const yMax = maxY + yPad;

  const yTicks = [];
  for (let i = 0; i <= 4; i++) {
    const y = yMax - ((yMax - yMin) * i) / 4;
    yTicks.push({
      y: pad.t + ih - ((y - yMin) / (yMax - yMin)) * ih,
      label: opts.valueFmt ? opts.valueFmt(y) : y.toFixed(0),
    });
  }

  const xLabels = [];
  const maxLen = Math.max(...seriesList.map((s) => s.data.length));
  const labelStep = Math.max(1, Math.ceil(maxLen / 8));
  let prevYear = null;

  seriesList.forEach((s) => {
    if (!s.data.length) return;
    const n = s.data.length;
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.8;
    ctx.beginPath();
    s.data.forEach((p, i) => {
      const x = pad.l + (n === 1 ? iw / 2 : (i / (n - 1)) * iw);
      const y = pad.t + ih - ((Number(p.y) - yMin) / (yMax - yMin)) * ih;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
      const dateStr = String(p.x);
      const thisYear = dateStr.slice(0, 4);
      if (i % labelStep === 0 || i === n - 1) {
        const showYear = thisYear !== prevYear || i === n - 1;
        xLabels.push({ x, label: showYear ? dateStr.slice(2) : dateStr.slice(5) });
      }
      prevYear = thisYear;
    });
    ctx.stroke();
  });

  drawGrid(ctx, W, H, pad, yTicks, xLabels);
  attachHover(canvas, () => {
    const n = Math.max(...seriesList.map((s) => s.data.length));
    const step = iw / Math.max(1, n - 1);
    return {
      n,
      step,
      getAt: (i) =>
        seriesList
          .map((s) => {
            const p = s.data[i];
            return p ? { name: s.name, color: s.color, x: p.x, y: Number(p.y) } : null;
          })
          .filter(Boolean),
      pad,
      yMin,
      yMax,
      ih,
      valueFmt: opts.valueFmt || ((v) => v.toFixed(2)),
      yPadTop: pad.t,
    };
  });
}

function drawKlineChart(canvas, bars, code) {
  const height = 340;
  const { ctx, W, H } = setupCanvas(canvas, height);
  const pad = { l: 58, r: 12, t: 12, b: 60 };
  const iw = W - pad.l - pad.r;
  const ih = H - pad.t - pad.b - 36;
  if (!bars || !bars.length) {
    ctx.fillStyle = "#5f6c7d";
    ctx.fillText("暂无 K 线", W / 2 - 30, H / 2);
    return;
  }

  const n = bars.length;
  const ma20 = movingAvg(bars, 20);
  const ma60 = movingAvg(bars, 60);
  const prices = bars.flatMap((b) => [Number(b.high), Number(b.low)]);
  const maxPrice = Math.max(...prices);
  const minPrice = Math.min(...prices);
  const padPx = (maxPrice - minPrice) * 0.05 || 1;
  const yMin = minPrice - padPx;
  const yMax = maxPrice + padPx;
  const volumeMax = Math.max(...bars.map((b) => Number(b.volume) || 0), 1);

  const y = (v) => pad.t + ih - ((v - yMin) / (yMax - yMin)) * ih;
  const bw = Math.max(2, iw / n * 0.62);
  const step = iw / n;

  bars.forEach((b, i) => {
    const x = pad.l + i * step + step / 2;
    const open = Number(b.open);
    const close = Number(b.close);
    const up = close >= open;
    const color = up ? "#f6465d" : "#0ecb81";
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 1;
    // 影线
    ctx.beginPath();
    ctx.moveTo(x, y(Number(b.high)));
    ctx.lineTo(x, y(Number(b.low)));
    ctx.stroke();
    // 实体
    const bodyTop = y(Math.max(open, close));
    const bodyH = Math.max(1, Math.abs(y(open) - y(close)));
    ctx.fillRect(x - bw / 2, bodyTop, bw, bodyH);
    // 成交量
    const vh = (Number(b.volume) / volumeMax) * 34;
    ctx.globalAlpha = 0.55;
    ctx.fillRect(x - bw / 2, pad.t + ih + 8 + (34 - vh), bw, vh);
    ctx.globalAlpha = 1;
  });

  drawMALine(ctx, ma20, "#4c9aff", pad, step, y, n);
  drawMALine(ctx, ma60, "#f0b90b", pad, step, y, n);

  const yTicks = [];
  for (let i = 0; i <= 4; i++) {
    const v = yMax - ((yMax - yMin) * i) / 4;
    yTicks.push({ y: y(v), label: v.toFixed(3) });
  }
  const xLabels = [];
  const labelStep = Math.max(1, Math.ceil(n / 8));
  let prevYear = null;
  bars.forEach((b, i) => {
    const dateStr = String(b.date);
    const thisYear = dateStr.slice(0, 4);
    if (i % labelStep === 0 || i === n - 1) {
      const showYear = thisYear !== prevYear || i === n - 1;
      xLabels.push({
        x: pad.l + i * step + step / 2,
        label: showYear ? dateStr.slice(2) : dateStr.slice(5),
      });
    }
    prevYear = thisYear;
  });
  drawGrid(ctx, W, H, pad, yTicks, xLabels);

  ctx.fillStyle = "#8b98a9";
  ctx.font = "10px sans-serif";
  ctx.fillText(code + " K线 / MA20 / MA60", pad.l, H - 4);

  attachHover(canvas, () => ({
    n,
    step,
    getAt: (i) => {
      const b = bars[i];
      return b
        ? [{
            name: "OHLC",
            color: "#4c9aff",
            x: b.date,
            y: Number(b.close),
            extra: [
              `开 ${Number(b.open).toFixed(3)}`,
              `高 ${Number(b.high).toFixed(3)}`,
              `低 ${Number(b.low).toFixed(3)}`,
              `收 ${Number(b.close).toFixed(3)}`,
              ma20[i] != null ? `MA20 ${ma20[i].toFixed(3)}` : "",
              ma60[i] != null ? `MA60 ${ma60[i].toFixed(3)}` : "",
              `量 ${fmtNum(Number(b.volume), 0)}`,
            ].filter(Boolean),
          }]
        : [];
    },
    pad,
    yMin,
    yMax,
    ih,
    valueFmt: (v) => v.toFixed(3),
    yPadTop: pad.t,
    kline: true,
  }));
}

function movingAvg(bars, period) {
  const out = [];
  let sum = 0;
  for (let i = 0; i < bars.length; i++) {
    sum += Number(bars[i].close);
    if (i >= period) sum -= Number(bars[i - period].close);
    out.push(i >= period - 1 ? sum / period : null);
  }
  return out;
}

function drawMALine(ctx, values, color, pad, step, y, n) {
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.3;
  ctx.beginPath();
  let started = false;
  values.forEach((v, i) => {
    if (v == null) return;
    const x = pad.l + i * step + step / 2;
    const yy = y(v);
    if (!started) {
      ctx.moveTo(x, yy);
      started = true;
    } else ctx.lineTo(x, yy);
  });
  ctx.stroke();
}

/* ---------- 图表悬浮提示 ---------- */

let hoverState = null;
let hoverCanvas = null;

function attachHover(canvas, getState) {
  const onMove = (ev) => {
    const st = getState();
    hoverState = st;
    hoverCanvas = canvas;
    const rect = canvas.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    const idx = Math.max(0, Math.min(st.n - 1, Math.round((px - st.pad.l) / st.step)));
    const pts = st.getAt(idx);
    if (!pts.length) return;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    // 重绘当前图表
    redrawCurrent(canvas);
    const x = st.pad.l + idx * st.step + (st.kline ? st.step / 2 : 0);
    // 十字线
    ctx.save();
    ctx.strokeStyle = "rgba(140,155,169,0.55)";
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(x, st.pad.t - 6);
    ctx.lineTo(x, st.pad.t + st.ih + 6);
    ctx.stroke();
    ctx.restore();
    // 提示框
    const boxW = st.kline ? 190 : 150;
    const lines = pts.flatMap((p) => [p.name, ...(p.extra || [])]);
    const boxH = lines.length * 15 + 14;
    let bx = x + 12;
    if (bx + boxW > canvas.getBoundingClientRect().width - 8) bx = x - boxW - 12;
    const by = 8;
    ctx.fillStyle = "rgba(13,17,23,0.94)";
    ctx.strokeStyle = "#263241";
    ctx.beginPath();
    ctx.roundRect(bx, by, boxW, boxH, 6);
    ctx.fill();
    ctx.stroke();
    ctx.font = "11px SF Mono, Menlo, monospace";
    let ty = by + 16;
    pts.forEach((p) => {
      ctx.fillStyle = "#8b98a9";
      ctx.fillText(p.x || "", bx + 8, ty);
      ty += 15;
      (p.extra || []).forEach((line) => {
        ctx.fillStyle = "#dce4ee";
        ctx.fillText(line, bx + 8, ty);
        ty += 15;
      });
    });
  };
  const onLeave = () => {
    hoverState = null;
    hoverCanvas = null;
    redrawCurrent(canvas);
  };
  canvas.onmousemove = onMove;
  canvas.onmouseleave = onLeave;
}

function redrawCurrent(canvas) {
  if (canvas === $("#bt-chart") && state.backtest) {
    const nav = state.backtest.daily_nav || [];
    drawLineChart(
      canvas,
      [{ name: "策略净值", color: "#4c9aff", data: nav.map(([d, v]) => ({ x: d, y: v })) }],
      { valueFmt: (v) => "¥" + fmtNum(v, 0) }
    );
  } else if (canvas === $("#sig-chart")) {
    const code = ($("#sig-chart-title").textContent || "").split(" ")[0];
    const data = state.kline.get(code);
    if (data) drawKlineChart(canvas, data.bars, code);
  }
}

/* ============================================================
 * 事件绑定
 * ========================================================== */

function bindEvents() {
  $$(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  $("#globalRefresh").addEventListener("click", async () => {
    trace("action: 刷新数据(" + state.activeTab + ")");
    try {
      $("#globalRefresh").disabled = true;
      await renderTab(state.activeTab, true);
      toast("已强制刷新");
    } catch (err) {
      toast("刷新失败: " + err.message, true);
    } finally {
      $("#globalRefresh").disabled = false;
    }
  });

  $("#sig-run").addEventListener("click", async () => {
    trace("action: 扫描信号");
    try {
      $("#sig-run").disabled = true;
      await renderSignals(true);
      toast("信号扫描完成");
    } catch (err) {
      toast("扫描失败: " + err.message, true);
    } finally {
      $("#sig-run").disabled = false;
    }
  });

  $("#bt-run").addEventListener("click", async () => {
    trace("action: 运行回测");
    try {
      $("#bt-run").disabled = true;
      $("#bt-note").textContent = "回测运行中，首次可能需要 1-3 分钟…";
      await renderBacktest(true);
      toast("回测完成");
    } catch (err) {
      toast("回测失败: " + err.message, true);
    } finally {
      $("#bt-run").disabled = false;
    }
  });

  $("#sc-run-screener").addEventListener("click", async () => {
    trace("action: 四维选品");
    try {
      $("#sc-run-screener").disabled = true;
      $("#sc-note").textContent = "四维选品运行中…";
      await renderScreener(true);
      toast("选品完成");
    } catch (err) {
      toast("选品失败: " + err.message, true);
    } finally {
      $("#sc-run-screener").disabled = false;
      $("#sc-note").textContent = "";
    }
  });

  $("#au-run").addEventListener("click", async () => {
    trace("action: 运行审计");
    try {
      $("#au-run").disabled = true;
      $("#au-note").textContent = "审计运行中，首次可能需要 1-3 分钟…";
      await renderAudit(true);
      toast("审计完成");
    } catch (err) {
      toast("审计失败: " + err.message, true);
    } finally {
      $("#au-run").disabled = false;
    }
  });

  $("#sc-top").addEventListener("change", () => renderScreener(false));
  $("#sc-category").addEventListener("change", () => renderScreener(false));
  $("#sig-custom").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") $("#sig-run").click();
  });
  $("#sig-pool").addEventListener("change", () => {
    state.lastSignalsPool = $("#sig-pool").value;
    syncBacktestPreset(state.lastSignalsPool);
  });
  $("#sig-momentum").addEventListener("change", () => {
    syncBacktestMomentum($("#sig-momentum").value);
  });
  $("#bt-momentum").addEventListener("change", () => {
    syncSignalMomentum($("#bt-momentum").value);
  });
  // 持仓图片解析
  $("#pos-upload-btn").addEventListener("click", () => $("#pos-file").click());
  $("#pos-file").addEventListener("change", async (ev) => {
    const files = [...(ev.target.files || [])];
    if (!files.length) return;
    try {
      $("#pos-parse-status").textContent = `已选择 ${files.length} 张图片，读取中…`;
      state.parseImages = await Promise.all(files.map(fileToBase64));
      await runPositionsParse();
    } catch (err) {
      toast("图片读取失败: " + err.message, true);
      $("#pos-parse-status").textContent = "";
    }
  });
  $("#pos-confirm").addEventListener("click", confirmPositionsUpdate);
  $("#pos-reparse").addEventListener("click", runPositionsParse);
  $("#pos-discard").addEventListener("click", discardParseResult);
  // 网格
  $("#grid-run").addEventListener("click", async () => {
    trace("action: 刷新网格");
    try {
      $("#grid-run").disabled = true;
      await renderGrid(true);
      toast("网格分析已刷新");
    } catch (err) {
      toast("刷新失败: " + err.message, true);
    } finally {
      $("#grid-run").disabled = false;
    }
  });
  $("#grid-opt-run").addEventListener("click", async () => {
    trace("action: 网格参数优化");
    try {
      $("#grid-opt-run").disabled = true;
      await renderGridOptimize(true);
      toast("网格参数优化完成");
    } catch (err) {
      toast("优化失败: " + err.message, true);
    } finally {
      $("#grid-opt-run").disabled = false;
    }
  });
  $("#grid-trigger-add").addEventListener("click", addGridTrigger);
  $("#grid-upload-btn").addEventListener("click", () => $("#grid-trigger-files").click());
  $("#grid-trigger-files").addEventListener("change", async (ev) => {
    const files = [...(ev.target.files || [])];
    if (!files.length) return;
    try {
      $("#grid-trigger-note").textContent = `已选择 ${files.length} 张图片，读取中…`;
      state.gridParseImages = await Promise.all(files.map(fileToBase64));
      await parseGridTriggers();
    } catch (err) {
      toast("图片读取失败: " + err.message, true);
      $("#grid-trigger-note").textContent = "";
    }
  });
  $("#grid-trigger-parse").addEventListener("click", parseGridTriggers);
  $("#grid-trigger-parse-result").addEventListener("click", (ev) => {
    const verifyBtn = ev.target.closest("[data-verify]");
    if (verifyBtn) {
      const tr = verifyBtn.closest("tr");
      if (tr) tr.dataset.verified = "1";
      verifyBtn.textContent = "✓已核实";
      updateGridTriggerHint();
      return;
    }
    const delBtn = ev.target.closest("[data-del]");
    if (!delBtn) return;
    const tr = delBtn.closest("tr");
    if (tr) tr.remove();
    const box = $("#grid-trigger-parse-result");
    const count = box.querySelectorAll("tbody tr").length;
    const btn = $("#grid-trigger-confirm");
    if (btn) btn.textContent = `确认录入 ${count} 条`;
    updateGridTriggerHint();
  });
  $("#grid-trigger-parse-result").addEventListener("input", (ev) => {
    const tr = ev.target.closest("tr");
    if (!tr) return;
    tr.dataset.verified = "0";
    const btn = tr.querySelector("[data-verify]");
    if (btn) btn.textContent = "核实";
    updateGridTriggerHint();
  });
  $("#grid-triggers-toggle").addEventListener("click", () => {
    const collapsed = $("#grid-triggers").style.display === "none";
    $("#grid-triggers").style.display = collapsed ? "" : "none";
    $("#grid-triggers-filter").style.display = collapsed ? "" : "none";
    $("#grid-triggers-toggle").textContent = collapsed ? "收起" : "展开";
  });
  $("#grid-trigger-filter-btn").addEventListener("click", loadGridTriggers);
  $("#grid-trigger-filter-reset").addEventListener("click", () => {
    $("#grid-trigger-filter-code").value = "";
    $("#grid-trigger-filter-type").value = "";
    $("#grid-trigger-filter-start").value = "";
    $("#grid-trigger-filter-end").value = "";
    loadGridTriggers();
  });
  // 数据存储
  $("#db-load").addEventListener("click", renderDbTable);
  $("#db-prev").addEventListener("click", () => {
    state.dbOffset = Math.max(0, state.dbOffset - Number($("#db-limit").value));
    renderDbTable();
  });
  $("#db-next").addEventListener("click", () => {
    state.dbOffset += Number($("#db-limit").value);
    renderDbTable();
  });
  $("#db-table").addEventListener("change", () => {
    state.dbOffset = 0;
    renderDbTable();
  });
  $("#db-log-load").addEventListener("click", renderDbLogs);
  // 信号明细列排序（事件委托，表格每次重渲染后依然生效）
  $("#sig-table").addEventListener("click", (ev) => {
    const th = ev.target.closest("th[data-key]");
    if (th) sortSignals(th.dataset.key);
  });

  window.addEventListener("resize", () => {
    if (state.activeTab === "signals") redrawCurrent($("#sig-chart"));
    if (state.activeTab === "backtest" && state.backtest) redrawCurrent($("#bt-chart"));
  });
}

/* ============================================================
 * 启动
 * ========================================================== */

async function boot() {
  trace("boot start");
  $("#dataAsOf").textContent = "初始化中：连接服务…";
  bindEvents();
  try {
    await ensurePools();
    trace("pools ok");
    setServerStatus(true, "服务正常");
    const initial = (location.hash || "").replace("#", "");
    if (PAGE_TITLES[initial]) switchTab(initial);
    else await renderOverview();
    trace("overview rendered");
  } catch (err) {
    trace("boot error: " + (err && err.message ? err.message : String(err)));
    renderFatalError(err);
  }
}

boot();
