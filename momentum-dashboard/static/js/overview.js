/* ============================================================
 * 总览
 * ========================================================== */

import {
  $,
  $$,
  esc,
  fmtNum,
  fmtPct,
  loadDefaultCombo,
  pctCls,
  statusMeta,
  trace,
} from "./utils.js";
import { api, applyEnvelope, state } from "./core.js";
import { renderPositionChips } from "./positions.js";

// 动量推荐置信度徽章：DSR 显著性 → 绿/黄/红/灰 tag
function dsrConfidenceTag(conf) {
  if (!conf || conf.significance_label == null) return "";
  const label = conf.significance_label;
  const cls = label.startsWith("显著") ? "strong"
    : label.startsWith("边缘") ? "medium"
    : label.startsWith("不显著") ? "unknown" : "none";
  const title = conf.dsr_prob != null
    ? `DSR=${conf.dsr_prob} · 年化${fmtPct(conf.annual_return_pct, 1)} · 超额${fmtPct(conf.excess_return_pct, 1)} · Sharpe${fmtNum(conf.sharpe, 2)}`
    : "";
  return `<span class="tag ${cls}" title="${esc(title)}">显著性 ${esc(label)}</span>`;
}

async function renderOverview(force = false) {
  const positionsP = !state.positions || force
    ? api("/api/positions", { quiet: true })
        .then((pbody) => { state.positions = applyEnvelope(pbody); })
        .catch(() => null)
    : Promise.resolve();
  const combo = loadDefaultCombo();
  const qs = [];
  if (combo && combo.preset) qs.push(`pool=${encodeURIComponent(combo.preset)}`);
  if (combo && combo.switch_buffer) qs.push(`switch_buffer=${encodeURIComponent(combo.switch_buffer)}`);
  if (force) qs.push("refresh=1");
  const body = await api("/api/overview" + (qs.length ? "?" + qs.join("&") : ""), {
    progress: "正在运行策略监测（动量+网格+风险审计）…",
    timeoutMs: force ? 600000 : 120000,
  });
  state.overview = applyEnvelope(body);
  trace("overview data ok");
  const ov = state.overview;
  const ovPoolEl = $("#ov-pool-label");
  if (ovPoolEl) {
    ovPoolEl.textContent = ov.pool_label
      ? `当前组合：${esc(ov.pool_label)}`
      : "";
  }
  const mom = ov.momentum || {};
  const risk = ov.risk || {};
  const gridGroups = ov.grid_groups || {};

  const [momLabel, momCls] = statusMeta(mom.status);
  const momSelected = mom.selected;
  const momConf = mom.confidence || {};
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
        ? `${momSelected.name} · ${momSelected.signal_strength || "none"}${momConf.significance_label ? ` · 显著性 ${momConf.significance_label}` : ""}`
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
    { t: "动量轮动", v: advice.momentum_action || "无正式动作", cls: "momentum", badge: dsrConfidenceTag(momConf) },
    { t: "网格趋势", v: advice.grid_action || "无正式动作", cls: "grid", badge: "" },
  ];
  $("#ov-advice").innerHTML = adviceRows
    .map(
      (a) => `
      <div class="advice-row ${a.cls}">
        <div class="a-t">${esc(a.t)}</div>
        <div>${esc(a.v)}${a.badge}</div>
      </div>`
    )
    .join("");

  const items = (mom.items || []).slice(0, 4);
  $("#ov-signals").innerHTML = items.length
    ? `
    <table>
      <thead><tr>
        <th>代码</th><th>名称</th><th data-metric="rsrs">RSRS ⓘ</th><th data-metric="slope">年化斜率 ⓘ</th>
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

export { dsrConfidenceTag, renderOverview };
