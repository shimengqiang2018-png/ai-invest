/* ============================================================
 * 信号扫描
 * ========================================================== */

import {
  $,
  $$,
  esc,
  fmtNum,
  fmtPct,
  loadDefaultCombo,
  pctCls,
  setSelectValue,
  statusMeta,
  trace,
} from "./utils.js";
import { api, applyEnvelope, state } from "./core.js";
import { loadKlineChart } from "./charts.js";
import {
  ensurePools,
  populateSignalPools,
  syncBacktestMomentum,
  syncBacktestPreset,
  syncSignalMomentum,
  syncSignalPool,
} from "./backtest.js";

async function renderSignals(force) {
  const pools = await ensurePools();
  populateSignalPools(pools);
  if (!state.sigDefaultsApplied) {
    // 回测历史选中的默认组合：仅在首次渲染时应用，之后保留页面手动选择
    state.sigDefaultsApplied = true;
    const combo = loadDefaultCombo();
    if (combo) {
      syncSignalPool(combo.preset);
      syncSignalMomentum(String(combo.momentum ?? ""));
      setSelectValue("#sig-switch-buffer", combo.switch_buffer);
    }
  }
  const pool = $("#sig-pool").value;
  const momentum = $("#sig-momentum").value;
  const custom = $("#sig-custom").value.trim();
  const holding = ($("#sig-holding") || {}).value?.trim().toUpperCase() || "";
  const sb = ($("#sig-switch-buffer") || {}).value || "1.5";
  state.lastSignalsPool = pool;
  const base = custom && /^\d{6}(,\d{6})*$/.test(custom)
    ? `pool=${encodeURIComponent(custom)}&momentum=${momentum}`
    : `pool=${pool}&momentum=${momentum}`;
  const query = base
    + `&switch_buffer=${encodeURIComponent(sb)}`
    + (holding ? `&holding=${encodeURIComponent(holding)}` : "");
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
  const intradayBadge = sig.intraday_prediction
    ? '<span class="tag" style="background:#f59e0b22;color:#d97706;margin-left:8px">⏱ 14:30 盘中预测（当日临时信号）</span>'
    : "";
  $("#dataAsOf").innerHTML = `动量信号日期: ${esc(sig.as_of || "—")}${intradayBadge}`;
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
    const rotAction = sig.rotation.action || "—";
    const rotTarget = sig.rotation.target
      ? `→ ${esc(sig.rotation.target.code)} ${esc(sig.rotation.target.name)}`
      : "";
    const rotReason = sig.rotation.reason
      ? `<div class="dim" style="font-size:11.5px;margin-top:2px">${esc(sig.rotation.reason)}</div>`
      : "";
    const actionColor = { hold: "warn", switch: "up", buy: "up", liquidate: "down", none: "" }[rotAction] || "";
    adviceHtml += `
      <div class="advice-item">
        <div class="a-k">轮动决策${holding ? `（持仓 ${esc(holding)}·迟滞 ${esc(sb)}）` : "（未指定持仓）"}</div>
        <div class="a-v"><span class="tag ${actionColor}">${esc(rotAction)}</span> ${rotTarget}
          ${rotReason}</div>
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
  renderWalkForwardHint();

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

async function renderWalkForwardHint() {
  const box = $("#sig-advice");
  if (!box) return;
  let wf = null;
  try {
    const body = await api("/api/walk-forward/latest", { quiet: true });
    wf = body.data;
  } catch {
    wf = null;
  }
  const item = document.createElement("div");
  item.className = "advice-item";
  if (!wf || !(wf.candidates || []).length) {
    item.innerHTML = `
      <div class="a-k">样本外验证（Walk-Forward）</div>
      <div class="a-v dim">尚未运行 · 在「回测分析 → 样本外验证」生成后，这里会自动显示结论</div>`;
    box.appendChild(item);
    return;
  }
  const follow = wf.follow_strategy || {};
  const top = [...(wf.candidates || [])].sort(
    (a, b) => (a.oos_rank ?? 999) - (b.oos_rank ?? 999)
  )[0];
  const excess = follow.excess_pct;
  const cls = excess != null && excess > 0 ? "up" : "down";
  item.innerHTML = `
    <div class="a-k">样本外验证（Walk-Forward）</div>
    <div class="a-v">
      跟随策略样本外 <b class="${cls}">${fmtPct(follow.oos_total_pct, 1, true)}</b>
      vs 等权基准 ${fmtPct(follow.benchmark_total_pct, 1, true)}
      · 超额 ${fmtPct(excess, 1, true)}
      · Sharpe ${follow.oos_sharpe != null ? fmtNum(follow.oos_sharpe) : "—"}
      <div class="dim" style="font-size:12px">
        ${top ? `样本外排名 #1：${esc(top.label)}（年化 ${top.oos_annual_pct != null ? fmtPct(top.oos_annual_pct, 1, true) : "—"}）` : ""}
        · 验证于 ${(wf.generated_at || "").slice(0, 10) || "—"}
      </div>
    </div>`;
  box.appendChild(item);
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

export {
  SIG_COLUMNS,
  compareSignals,
  loadRealtimeIntoTable,
  renderRealtimeCells,
  renderSignals,
  renderSignalsTable,
  renderWalkForwardHint,
  sigSortValue,
  sortSignals,
};
