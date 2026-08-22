/* ============================================================
 * Canvas 图表
 * ========================================================== */

import { $, esc, fmtNum } from "./utils.js";
import { api, state, toast } from "./core.js";

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

export {
  attachHover,
  drawGrid,
  drawKlineChart,
  drawLineChart,
  drawMALine,
  loadKlineChart,
  movingAvg,
  redrawCurrent,
  setupCanvas,
};
