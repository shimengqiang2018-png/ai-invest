/* ============================================================
 * 回测分析
 * ========================================================== */

import {
  $,
  $$,
  esc,
  fmtNum,
  fmtPct,
  loadDefaultCombo,
  pctCls,
  saveDefaultCombo,
  setSelectValue,
  shortName,
} from "./utils.js";
import { api, applyEnvelope, state, toast } from "./core.js";
import { drawLineChart } from "./charts.js";

let _historyItems = [];
let _historyLoaded = false;
let _historyPage = 0;
let _historyPageSize = 10;
let _historyTotal = 0;

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
    const combo = loadDefaultCombo();
    presetSel.value =
      combo && combo.preset && pools.backtest_presets[combo.preset]
        ? combo.preset
        : state.lastSignalsPool && pools.backtest_presets[state.lastSignalsPool]
          ? state.lastSignalsPool
          : "best4";
    presetSel.addEventListener("change", () => syncSignalPool(presetSel.value));
    if (combo) {
      setSelectValue("#bt-momentum", combo.momentum);
      setSelectValue("#bt-freq", combo.freq);
      setSelectValue("#bt-start", combo.start);
      setSelectValue("#bt-commission", combo.commission);
      setSelectValue("#bt-min-commission", combo.min_commission);
      setSelectValue("#bt-switch-buffer", combo.switch_buffer);
    }
  }
  const preset = presetSel.value;
  const momentum = $("#bt-momentum").value;
  const freq = $("#bt-freq").value;
  const start = $("#bt-start").value;
  const commission = $("#bt-commission").value;
  const minCommission = $("#bt-min-commission").value;
  const switchBuffer = $("#bt-switch-buffer").value;
  const body = await api(
    `/api/backtest?preset=${preset}&momentum=${momentum}&freq=${freq}` +
      `&start=${start}&commission=${commission}&min_commission=${minCommission}` +
      `&switch_buffer=${switchBuffer}` +
      `${force ? "&refresh=1" : ""}`,
    {
      progress: "正在运行回测（首次可能需要 1-3 分钟）…",
      timeoutMs: force ? 600000 : 60000,
    }
  );
  state.backtest = applyEnvelope(body);
  if (!_historyLoaded) {
    _historyLoaded = true;
    loadBacktestHistory();
  }
  const bt = state.backtest;
  const perf = bt.performance || {};
  const period = bt.period || {};

  $("#dataAsOf").textContent = `回测区间: ${period.start || "—"} ~ ${period.end || "—"} · ${period.years || "—"} 年`;
  const startLabel = { full: "全部", "10y": "近10年", "7y": "近7年", "5y": "近5年", "3y": "近3年", "2y": "近2年", "1y": "近1年" }[start] || start;
  const commissionBp = Number(commission) * 10000;
  const commissionLabel =
    commissionBp === 0
      ? "免佣"
      : `佣金万${Number.isInteger(commissionBp) ? commissionBp : commissionBp.toFixed(1)}`;
  $("#bt-note").textContent =
    `${preset} · RSRS ${momentum}日 · ${freq} · 区间 ${startLabel} · ` +
    `${commissionLabel} · ${minCommission === "0" ? "免5" : "最低5元"} · ` +
    `迟滞 ${switchBuffer} · ` +
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
    ["胜率", fmtNum(perf.trade_win_rate_pct, 1) + "%", `${perf.trade_win_count || 0}/${perf.trade_sell_count || 0} 笔`, (perf.trade_win_rate_pct || 0) >= 50 ? "up" : "warn"],
    ["盈亏比", fmtNum(perf.profit_loss_ratio, 2), "平均盈利/平均亏损", (perf.profit_loss_ratio || 0) >= 1 ? "up" : "warn"],
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
  renderWalkForwardFromLatest();
}

async function renderWalkForwardFromLatest() {
  const panel = $("#wf-panel");
  if (!panel) return;
  try {
    const body = await api("/api/walk-forward/latest", { quiet: true });
    const wf = body.data;
    if (wf && (wf.candidates || []).length) {
      renderWalkForward(wf);
    }
  } catch {
    /* 未运行过或接口异常时保持面板隐藏 */
  }
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
        <th>#</th><th>组合</th><th data-metric="annual">年化 ⓘ</th><th data-metric="total">总收益 ⓘ</th>
        <th data-metric="max_dd">最大回撤 ⓘ</th><th data-metric="sharpe">夏普 ⓘ</th><th data-metric="calmar">卡玛 ⓘ</th><th data-metric="dsr">DSR ⓘ</th><th data-metric="win_rate">胜率 ⓘ</th><th data-metric="trades">交易 ⓘ</th>
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
              <td class="num">${r.dsr != null ? r.dsr.toFixed(2) : "—"}</td>
              <td class="num">${fmtNum(r.wr, 1)}%</td>
              <td class="num">${r.trades}</td>
            </tr>`)
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无枚举数据</div>';
}

async function pollJob(jobId, onMessage) {
  for (;;) {
    const body = await api(`/api/job?id=${encodeURIComponent(jobId)}`, {
      quiet: true,
      timeoutMs: 30000,
    });
    const job = body.data || {};
    if (onMessage && job.message) onMessage(job.message);
    if (job.status === "done") return job.result;
    if (job.status === "error") throw new Error(job.error || job.message || "任务失败");
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
}

async function recalcEnum() {
  const btn = $("#enum-recalc");
  const note = $("#enum-note");
  if (!btn) return;
  btn.disabled = true;
  note.textContent = "后台枚举中（约数分钟），请稍候…";
  try {
    const body = await api(
      "/api/enum/recalc?universe=veteran&min=3&max=5&momentum=25&top=30",
      { quiet: true, timeoutMs: 30000 }
    );
    const jobId = (body.data || {}).job_id;
    if (!jobId) throw new Error("服务未返回任务 ID");
    await pollJob(jobId, (msg) => (note.textContent = msg));
    // 任务完成即落盘 + 写 MySQL；这里清掉内存缓存，重新从 /api/enum 拉取归一化结果，
    // 避免直接使用任务原始字段（annual_pct/…）导致表格空值。
    state.enumData = null;
    await renderEnumTable();
    toast("枚举重算完成");
  } catch (err) {
    toast("枚举失败: " + err.message, true);
  } finally {
    btn.disabled = false;
    note.textContent = "";
  }
}

function renderWalkForward(wf) {
  const panel = $("#wf-panel");
  if (!panel) return;
  panel.hidden = false;
  const cands = wf.candidates || [];
  const follow = wf.follow_strategy || {};
  const folds = wf.folds || [];
  panel.innerHTML = `
    <div class="panel-title">样本外验证（Walk-Forward · ${wf.n_folds || 0} 折）</div>
    <div class="muted" style="margin-bottom:8px">
      跟随策略样本外 <b>${fmtPct(follow.oos_total_pct, 1, true)}</b>
      vs 等权基准 ${fmtPct(follow.benchmark_total_pct, 1, true)}
      · 超额 ${fmtPct(follow.excess_pct, 1, true)}
      · Sharpe ${follow.oos_sharpe != null ? fmtNum(follow.oos_sharpe) : "—"}
    </div>
    ${cands.length ? `
    <table>
      <thead><tr>
        <th>样本外#</th><th>组合</th><th data-metric="oos_total">样本外总收益 ⓘ</th>
        <th data-metric="oos_annual">样本外年化 ⓘ</th><th data-metric="oos_sharpe">样本外Sharpe ⓘ</th><th data-metric="selected">被选中 ⓘ</th>
      </tr></thead>
      <tbody>${cands.map((c) => `
        <tr>
          <td class="dim">${c.oos_rank ?? "—"}</td>
          <td class="num">${esc(c.label || c.combo)}</td>
          <td class="num ${(c.oos_total_pct || 0) >= 0 ? "up" : "down"}">${fmtPct(c.oos_total_pct, 1, true)}</td>
          <td class="num">${c.oos_annual_pct != null ? fmtPct(c.oos_annual_pct, 1, true) : "—"}</td>
          <td class="num">${c.oos_sharpe != null ? fmtNum(c.oos_sharpe) : "—"}</td>
          <td class="dim">${c.selected_count ?? 0}/${c.n_folds ?? 0}</td>
        </tr>`).join("")}</tbody>
    </table>` : '<div class="empty">无样本外数据</div>'}
    <div class="muted" style="margin-top:8px">选参：${folds.map((f) => `${f.train}→${f.test}=${f.selected}`).join(" · ")}</div>
  `;
}

async function recalcWalkForward() {
  const btn = $("#walk-forward");
  const note = $("#enum-note");
  if (!btn) return;
  btn.disabled = true;
  note.textContent = "样本外验证运行中（较慢），请稍候…";
  try {
    const body = await api(
      "/api/walk-forward?top=20&train-months=24&test-months=12&step-months=12&metric=sharpe",
      { quiet: true, timeoutMs: 30000 }
    );
    const jobId = (body.data || {}).job_id;
    if (!jobId) throw new Error("服务未返回任务 ID");
    const result = await pollJob(jobId, (msg) => (note.textContent = msg));
    renderWalkForward(result);
    toast("样本外验证完成");
  } catch (err) {
    toast("样本外验证失败: " + err.message, true);
  } finally {
    btn.disabled = false;
    note.textContent = "";
  }
}

async function recalcScan() {
  const btn = $("#sc-recalc");
  const note = $("#sc-note");
  if (!btn) return;
  btn.disabled = true;
  note.textContent = "全市场扫描运行中（约数分钟），请稍候…";
  try {
    const minSizeYi = parseFloat($("#sc-min-size") ? $("#sc-min-size").value : "") || 0;
    const minTurnoverYi =
      parseFloat($("#sc-min-turnover") ? $("#sc-min-turnover").value : "") || 0;
    const body = await api(
      `/api/etf-scan/recalc?top=640&min-days=500` +
        `&min-size=${minSizeYi * 1e8}&min-turnover=${minTurnoverYi}`,
      { quiet: true, timeoutMs: 30000 }
    );
    const jobId = (body.data || {}).job_id;
    if (!jobId) throw new Error("服务未返回任务 ID");
    await pollJob(jobId, (msg) => (note.textContent = msg));
    await renderScreener(false);
    toast("全市场扫描完成");
  } catch (err) {
    toast("扫描失败: " + err.message, true);
  } finally {
    btn.disabled = false;
    note.textContent = "";
  }
}

function renderTradesTable(trades) {
  const rows = [...trades].reverse().slice(0, 20);
  const buy = (t) => (t.action || "").includes("买入");
  const sell = (t) => (t.action || "").includes("卖出");
  $("#bt-trades").innerHTML = rows.length
    ? `
    <table>
      <thead><tr>
        <th>日期</th><th>动作</th><th>代码</th><th>名称</th>
        <th>价格</th><th>数量</th><th>金额</th><th>盈亏</th><th>原因</th>
      </tr></thead>
      <tbody>
        ${rows
          .map((t) => {
            const pnlTxt = sell(t) && t.pnl != null
              ? `${t.pnl >= 0 ? "+" : ""}${fmtNum(t.pnl, 0)} (${fmtPct(t.pnl_pct, 1, true)})`
              : "—";
            const pnlCls = sell(t) && t.pnl != null ? (t.pnl >= 0 ? "up" : "down") : "dim";
            return `
            <tr>
              <td class="num">${esc(t.date)}</td>
              <td class="${buy(t) ? "up" : "down"}">${esc(t.action)}</td>
              <td>${esc(t.code)}</td>
              <td>${esc(shortName(t.name, t.code))}</td>
              <td class="num">${fmtNum(t.price)}</td>
              <td class="num">${fmtNum(t.shares, 0)}</td>
              <td class="num">${fmtNum(t.amount, 0)}</td>
              <td class="num ${pnlCls}">${pnlTxt}</td>
              <td class="dim">${esc(t.reason || "")}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无交易记录</div>';
}

async function renderPresetPoolState() {
  const sub = $("#pp-sub");
  const box = $("#pp-current");
  if (!sub && !box) return;
  try {
    const body = await api("/api/preset-pool", { quiet: true });
    const data = body.data || {};
    const presets = data.presets || [];
    const scanGen = data.scan && data.scan.generated_at;
    if (sub) {
      sub.textContent = presets.length
        ? `${presets.length} 个预设池 · 全市场扫描 ${scanGen ? String(scanGen).slice(0, 10) : "—"}`
        : "尚未保存预设池";
    }
    if (box) {
      box.innerHTML = presets.length
        ? `
        <table>
          <thead><tr><th>池名</th><th>描述</th><th>标的（名称 + 代码）</th><th>状态</th></tr></thead>
          <tbody>
            ${presets
              .map(
                (p) => `
                <tr>
                  <td>${esc(p.pool_key)}</td>
                  <td class="dim">${esc(p.description || "")}</td>
                  <td>${(p.codes || [])
                    .map((c, i) => `${esc((p.names || [])[i] || c)}(${esc(c)})`)
                    .join("、")}</td>
                  <td class="dim">${p.enabled ? "启用" : "停用"}</td>
                </tr>`
              )
              .join("")}
          </tbody>
        </table>`
        : '<span class="muted">暂无预设池（先点「生成预设池」，预览后保存）</span>';
    }
  } catch {
    /* 服务不可用时不打扰页面 */
  }
}

function renderPresetCandidates(result) {
  const box = $("#pp-candidates");
  if (!box) return;
  const cands = (result || {}).candidates || [];
  if (!cands || !cands.length) {
    box.innerHTML = '<div class="empty">未生成候选</div>';
    return;
  }
  const maxScore = Math.max(...cands.map((c) => c.composite_score || 0));
  box.innerHTML = `
    <table>
      <thead><tr>
        <th>#</th><th>代码</th><th>名称</th><th>类别</th>
        <th data-metric="composite">综合分 ⓘ</th><th>平均相关</th><th>四维分</th>
        <th data-metric="annual_vol">年化波动 ⓘ</th><th data-metric="annual">年化收益 ⓘ</th><th data-metric="sharpe">Sharpe ⓘ</th><th data-metric="max_dd">MaxDD ⓘ</th>
      </tr></thead>
      <tbody>
        ${cands
          .map(
            (c, i) => `
              <tr>
                <td class="dim">${i + 1}</td>
                <td>${esc(c.code)}</td>
                <td>${esc(c.name)}</td>
                <td class="dim">${esc(c.category)}</td>
                <td class="num ${c.composite_score === maxScore ? "up" : ""}">${fmtNum(c.composite_score, 1)}</td>
                <td class="num ${c.avg_corr != null && c.avg_corr > 0.6 ? "warn" : ""}">${c.avg_corr != null ? c.avg_corr.toFixed(2) : "—"}${c.corr_override ? " ⚠" : ""}</td>
                <td class="num">${c.screener_total != null ? c.screener_total + "/20" : "—"}</td>
                <td class="num">${fmtPct((c.volatility || 0) * 100, 1)}</td>
                <td class="num ${(c.annual_return || 0) >= 0 ? "up" : "down"}">${fmtPct((c.annual_return || 0) * 100, 1, true)}</td>
                <td class="num">${fmtNum(c.sharpe_ratio)}</td>
                <td class="num down">${fmtPct((c.max_drawdown || 0) * 100, 1)}</td>
              </tr>`
          )
          .join("")}
      </tbody>
    </table>
    <div class="toolbar" style="margin-top:8px">
      <button class="btn ghost" id="pp-apply">保存为预设池</button>
      <span class="muted">保存后写入 MySQL momentum_pools，枚举/回测/扫描自动使用该池</span>
    </div>`;
  const removedCorr = (result || {}).removed_corr || [];
  const removedNoData = (result || {}).removed_no_data || [];
  if (removedCorr.length || removedNoData.length) {
    box.innerHTML += `
      <div class="muted" style="margin-top:8px;font-size:12.5px">
        ${removedCorr.length ? `<div>⚠ 相关性超限剔除：${removedCorr.map((d) => `${d.code} ${d.name}（与 ${d.with_code} 相关 ${d.max_corr}）`).join("；")}</div>` : ""}
        ${removedNoData.length ? `<div>⚠ 无本地行情剔除：${removedNoData.map((d) => `${d.code} ${d.name}`).join("；")}（请先联网重新扫描全市场）</div>` : ""}
      </div>`;
  }
  const applyBtn = $("#pp-apply");
  if (applyBtn) applyBtn.addEventListener("click", applyPresetPool);
}

async function buildPresetPool() {
  const btn = $("#pp-build");
  const note = $("#pp-note");
  if (!btn) return;
  btn.disabled = true;
  note.textContent = "正在从全市场评分生成候选…";
  try {
    const minBars = $("#pp-exclude-new") && $("#pp-exclude-new").checked ? 1000 : 0;
    const minSizeYi = parseFloat($("#pp-min-size") ? $("#pp-min-size").value : "") || 0;
    const minTurnoverYi = parseFloat($("#pp-min-turnover") ? $("#pp-min-turnover").value : "") || 0;
    const body = await api(
      `/api/preset-pool/build?target-size=10&min-bars=${minBars}` +
        `&min-size=${minSizeYi * 1e8}&min-turnover=${minTurnoverYi * 1e8}`,
      {
      quiet: true,
      timeoutMs: 30000,
      }
    );
    const jobId = (body.data || {}).job_id;
    if (!jobId) throw new Error("服务未返回任务 ID");
    const result = await pollJob(jobId, (msg) => (note.textContent = msg));
    renderPresetCandidates(result || {});
    toast(`已生成 ${((result || {}).candidates || []).length} 只候选，可预览后保存`);
  } catch (err) {
    toast("生成失败: " + err.message, true);
  } finally {
    btn.disabled = false;
    note.textContent = "";
  }
}

async function applyPresetPool() {
  const rows = [...$$("#pp-candidates tbody tr")];
  const codes = rows
    .map((tr) => (tr.cells[1] ? tr.cells[1].textContent.trim() : ""))
    .filter(Boolean);
  const poolKey = ($("#pp-key").value || "veteran").trim();
  if (!codes.length) {
    toast("没有可保存的候选", true);
    return;
  }
  try {
    const body = await api("/api/preset-pool/apply", {
      method: "POST",
      body: {
        pool_key: poolKey,
        codes,
        description: `预设池 ${poolKey}（${codes.length} 只）`,
      },
      timeoutMs: 30000,
    });
    toast(`预设池 ${body.data.pool_key} 已保存到 MySQL（${codes.length} 只）`);
    await renderPresetPoolState();
  } catch (err) {
    toast("保存失败: " + err.message, true);
  }
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
        <th data-metric="annual">年化 ⓘ</th><th data-metric="max_dd">最大回撤 ⓘ</th><th data-metric="sharpe">夏普 ⓘ</th><th data-metric="annual_vol">波动 ⓘ</th>
        <th data-metric="signal_wr10">信号胜率10d ⓘ</th><th data-metric="signal_wr20">信号胜率20d ⓘ</th><th data-metric="composite">综合评分 ⓘ</th>
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
  renderPresetPoolState();
}

function renderScreenerRecommend(sc) {
  const rec = sc.recommended || [];
  $("#sc-recommend").innerHTML = rec.length
    ? `
    <table>
      <thead><tr>
        <th>代码</th><th>名称</th><th>类别</th><th>方向</th><th>流动</th>
        <th>独立</th><th data-metric="annual_vol">波动 ⓘ</th><th data-metric="composite">总分 ⓘ</th><th data-metric="cagr">CAGR ⓘ</th><th data-metric="amount">日均额 ⓘ</th>
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
 * 回测历史：展示 + 选中设为默认组合（首页/信号扫描联动）
 * ========================================================== */

function comboFromParams(p) {
  return {
    preset: p.preset || p.pool || "",
    momentum: p.momentum ?? 25,
    freq: p.freq || "monthly",
    start: p.start || "full",
    commission: p.commission ?? 0.00025,
    min_commission: p.min_commission ?? 0,
    switch_buffer: p.switch_buffer ?? 1.0,
  };
}

function currentBacktestCombo() {
  const presetSel = $("#bt-preset");
  return {
    preset: presetSel ? presetSel.value : "",
    momentum: $("#bt-momentum") ? $("#bt-momentum").value : 25,
    freq: $("#bt-freq") ? $("#bt-freq").value : "monthly",
    start: $("#bt-start") ? $("#bt-start").value : "full",
    commission: $("#bt-commission") ? $("#bt-commission").value : 0.00025,
    min_commission: $("#bt-min-commission") ? $("#bt-min-commission").value : 0,
    switch_buffer: $("#bt-switch-buffer") ? $("#bt-switch-buffer").value : 1.0,
  };
}

function comboMatches(a, b) {
  return (
    String(a.preset || a.pool || "") === String(b.preset || b.pool || "") &&
    String(a.momentum ?? "") === String(b.momentum ?? "") &&
    String(a.freq ?? "") === String(b.freq ?? "") &&
    String(a.start ?? "") === String(b.start ?? "") &&
    String(a.commission ?? "") === String(b.commission ?? "") &&
    String(a.min_commission ?? "") === String(b.min_commission ?? "") &&
    String(a.switch_buffer ?? "") === String(b.switch_buffer ?? "")
  );
}

const FREQ_LABEL = { daily: "日频", weekly: "周频", biweekly: "双周", monthly: "月频" };
const START_LABEL = {
  full: "全部",
  "10y": "近10年",
  "7y": "近7年",
  "5y": "近5年",
  "3y": "近3年",
  "2y": "近2年",
  "1y": "近1年",
};

function formatComboParams(p) {
  const parts = [];
  parts.push(FREQ_LABEL[p.freq] || p.freq || "月频");
  parts.push(START_LABEL[p.start] || p.start || "全部");
  const commissionBp = Number(p.commission ?? 0.00025) * 10000;
  parts.push(
    commissionBp === 0
      ? "免佣"
      : `万${Number.isInteger(commissionBp) ? commissionBp : commissionBp.toFixed(1)}`
  );
  parts.push(String(p.min_commission ?? 0) === "0" ? "免5" : "最低5元");
  if (p.switch_buffer != null && Number(p.switch_buffer) > 1) {
    parts.push(`迟滞${p.switch_buffer}`);
  }
  return parts.join(" · ");
}

async function loadBacktestHistory() {
  const box = $("#bt-history");
  if (!box) return;
  try {
    const body = await api(
      `/api/backtest/history?limit=${_historyPageSize}&offset=${_historyPage * _historyPageSize}`,
      { quiet: true }
    );
    const data = body.data || {};
    _historyItems = (data.items || []).filter(
      (it) => it.params && (it.params.preset || it.params.pool)
    );
    _historyTotal = data.total ?? _historyItems.length;
    const totalPages = Math.max(1, Math.ceil(_historyTotal / _historyPageSize));
    if (_historyPage >= totalPages && _historyPage > 0) {
      _historyPage = totalPages - 1;
      loadBacktestHistory();
      return;
    }
    renderBacktestHistory();
  } catch (err) {
    box.innerHTML = `<div class="empty">历史加载失败：${esc(err.message)}</div>`;
  }
}

function renderBacktestHistory() {
  const box = $("#bt-history");
  if (!box) return;
  if (!_historyItems.length) {
    box.innerHTML = '<div class="empty">暂无历史（运行回测后自动记录）</div>';
    return;
  }
  const combo = loadDefaultCombo();
  const totalPages = Math.max(1, Math.ceil(_historyTotal / _historyPageSize));
  box.innerHTML = `
    <div class="toolbar" style="margin-bottom:8px">
      <span class="muted">共 ${_historyTotal} 条 · 第 ${_historyPage + 1}/${totalPages} 页</span>
      <label>每页
        <select id="bt-history-size">
          ${[10, 20, 50]
            .map(
              (n) =>
                `<option value="${n}"${n === _historyPageSize ? " selected" : ""}>${n} 条</option>`
            )
            .join("")}
        </select>
      </label>
      <button class="btn mini" id="bt-history-prev" ${_historyPage === 0 ? "disabled" : ""}>上一页</button>
      <button class="btn mini" id="bt-history-next" ${_historyPage >= totalPages - 1 ? "disabled" : ""}>下一页</button>
    </div>
    <table>
      <thead><tr>
        <th>组合</th><th>参数</th><th>年化</th><th>最大回撤</th><th>Sharpe</th><th>交易</th><th>时间</th>
      </tr></thead>
      <tbody>
        ${_historyItems.map((it, i) => historyRowHtml(it, i, combo)).join("")}
      </tbody>
    </table>`;
  const sizeSel = $("#bt-history-size");
  if (sizeSel) {
    sizeSel.addEventListener("change", (ev) => {
      _historyPageSize = Number(ev.target.value);
      _historyPage = 0;
      loadBacktestHistory();
    });
  }
  const prevBtn = $("#bt-history-prev");
  if (prevBtn) {
    prevBtn.addEventListener("click", () => {
      if (_historyPage > 0) {
        _historyPage -= 1;
        loadBacktestHistory();
      }
    });
  }
  const nextBtn = $("#bt-history-next");
  if (nextBtn) {
    nextBtn.addEventListener("click", () => {
      if (_historyPage < totalPages - 1) {
        _historyPage += 1;
        loadBacktestHistory();
      }
    });
  }
  box.querySelectorAll("button[data-select]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const item = _historyItems[Number(btn.dataset.select)];
      if (item) selectBacktestHistory(item);
    });
  });
}

function historyRowHtml(it, i, combo) {
  const p = it.params || {};
  const s = it.summary || {};
  const label = p.preset || p.pool || "自定义";
  const isDefault = !!(combo && comboMatches(p, combo));
  const sel = isDefault ? ' class="sel"' : "";
  return `<tr data-i="${i}"${sel}>
    <td>${esc(label)}</td>
    <td class="dim">${esc(formatComboParams(p))} · ${esc(String(p.momentum ?? 25))}日</td>
    <td class="${pctCls(s.annual_return_pct)}">${fmtPct(s.annual_return_pct, 1, true)}</td>
    <td class="down">${fmtPct(s.max_dd_pct, 1)}</td>
    <td>${fmtNum(s.sharpe)}</td>
    <td class="num">${fmtNum(s.num_trades, 0)}</td>
    <td class="dim num">${esc(String(it.updated_at || "").slice(0, 16))}</td>
    <td><button class="btn mini" data-select="${i}" ${isDefault ? "disabled" : ""}>${isDefault ? "✓ 当前默认" : "设为默认"}</button></td>
  </tr>`;
}

function selectBacktestHistory(item) {
  const combo = comboFromParams(item.params || {});
  saveDefaultCombo(combo);
  const presetSel = $("#bt-preset");
  if (combo.preset && presetSel && ![...presetSel.options].some((o) => o.value === combo.preset)) {
    const opt = document.createElement("option");
    opt.value = combo.preset;
    opt.textContent = `${combo.preset} — 历史组合`;
    presetSel.appendChild(opt);
  }
  if (combo.preset) presetSel.value = combo.preset;
  setSelectValue("#bt-momentum", combo.momentum);
  setSelectValue("#bt-freq", combo.freq);
  setSelectValue("#bt-start", combo.start);
  setSelectValue("#bt-commission", combo.commission);
  setSelectValue("#bt-min-commission", combo.min_commission);
  setSelectValue("#bt-switch-buffer", combo.switch_buffer);
  // 信号扫描联动
  syncSignalPool(combo.preset);
  setSelectValue("#sig-momentum", combo.momentum);
  setSelectValue("#sig-switch-buffer", combo.switch_buffer);
  renderBacktestHistory();
  toast(`已选择组合 ${combo.preset || "自定义"}，设为首页/信号扫描默认`);
  renderBacktest(false);
}

export {
  applyPresetPool,
  buildPresetPool,
  currentBacktestCombo,
  ensurePools,
  pollJob,
  populateSignalPools,
  recalcEnum,
  recalcScan,
  recalcWalkForward,
  renderBacktest,
  renderCorrMatrix,
  renderEnumTable,
  renderPresetCandidates,
  renderPresetPoolState,
  renderScreener,
  renderScreenerRecommend,
  renderTradesTable,
  renderWalkForward,
  renderWalkForwardFromLatest,
  syncBacktestMomentum,
  syncBacktestPreset,
  syncSignalMomentum,
  syncSignalPool,
};
