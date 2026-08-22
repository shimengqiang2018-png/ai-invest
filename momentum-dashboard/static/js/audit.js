/* ============================================================
 * 风险审计
 * ========================================================== */

import { $, $$, esc, fmtNum, fmtPct, pctCls } from "./utils.js";
import { api, applyEnvelope, state } from "./core.js";

async function renderDb() {
  const body = await api("/api/db/tables", { quiet: true });
  const data = body.data;
  const tables = data.tables_detail || [];
  const dbInfo = data.db_info || {};
  const dbLabel = `MySQL（${dbInfo.database || ""}@${dbInfo.host || ""}）`;
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
      <thead><tr><th data-metric="forward_window">前向窗口 ⓘ</th><th data-metric="ic">IC mean ⓘ</th><th data-metric="ic">IC std ⓘ</th><th data-metric="ir">IR ⓘ</th><th>判定</th></tr></thead>
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
      <thead><tr><th>情景</th><th>期间</th><th data-metric="asset_dd">资产回撤 ⓘ</th><th data-metric="strat_return">策略收益 ⓘ</th></tr></thead>
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

export { renderAudit, renderDb, renderDbLogs, renderDbTable };
