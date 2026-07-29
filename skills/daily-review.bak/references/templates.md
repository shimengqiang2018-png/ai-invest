# HTML 模板参考

三个分屏的 HTML 骨架。`{变量}` 部分按当日数据替换。

## 通用 CSS（三屏共享）

```css
html { color-scheme: only light !important; }
* { margin:0; padding:0; box-sizing:border-box; }
body {
  background: #f2f3f5 !important; color: #1a1a2e !important;
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
  font-size: 14px; line-height: 1.5; padding: 36px 36px 36px;
  max-width: 540px; margin: 0 auto;
  -webkit-font-smoothing: antialiased;
  display:flex; flex-direction:column; min-height:960px; justify-content:space-between;
}
.up { color:#e53e3e; } .down { color:#059669; }
.card {
  background:#ffffff !important; border:1px solid #e5e7eb; border-radius:12px;
  padding:16px 18px; margin-top:12px;
}
.card:first-child { margin-top:0; }
.card-title {
  font-size:11px; font-weight:700; color:#9ca3af;
  text-transform:uppercase; letter-spacing:1px; margin-bottom:12px;
  display:flex; align-items:center; gap:6px;
}
.dot { width:6px; height:6px; border-radius:2px; display:inline-block; flex-shrink:0; }
.footer-text { text-align:center; font-size:9px; color:#d1d5db; }
hr { border:none; border-top:1px solid #e5e7eb; margin:7px 0; }

/* 强制亮色：覆盖系统/浏览器暗黑模式 */
@media (prefers-color-scheme: dark) {
  html, body { background-color: #f2f3f5 !important; }
  .card, .index-item, .hero-stat { background-color: #ffffff !important; }
  body, .card, h1, .index-item .name, .hero-name, .sector-name, .val-header .idx { color: #1a1a2e !important; }
  .hero { background-color: #fffbf0 !important; }
  .hero-stat { background-color: #ffffff !important; }
  .hero-records { background-color: #fffbeb !important; }
  .risk-item { background-color: #fef2f2 !important; }
  .summary-box { background-color: #eff6ff !important; color: #1e40af !important; }
  .logic-num { background-color: #fee2e2 !important; }
}
```

---

## 第一屏：大盘概览

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="color-scheme" content="only light">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A股复盘 ①</title>
<style>
  /* 通用CSS + 以下专用样式 */
  .header { text-align:center; }
  .header .date { font-size:11px; color:#9ca3af; letter-spacing:2px; }
  .header h1 { font-size:26px; font-weight:800; color:#111827; margin:6px 0 2px; }
  .header .sub { font-size:14px; color:#e53e3e; font-weight:500; }
  .index-grid { display:grid; grid-template-columns:1fr 1fr; gap:6px; }
  .index-item {
    background:#f9fafb !important; border-radius:8px; padding:10px 12px;
    display:flex; justify-content:space-between; align-items:center;
  }
  .index-item .name { font-size:13px; font-weight:600; color:#1f2937; }
  .index-item .price { font-size:15px; font-weight:700; color:#111827; }
  .index-item .change { font-size:12px; font-weight:700; }
  .right { text-align:right; }
  .pulse-row { display:flex; justify-content:space-around; text-align:center; }
  .pulse-item .val { font-size:28px; font-weight:800; line-height:1.15; }
  .pulse-item .label { font-size:11px; color:#6b7280; margin-top:2px; }
  .split2 { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .stat-box { border-radius:10px; padding:14px 16px; text-align:center; }
  .stat-box .v { font-size:26px; font-weight:800; line-height:1.15; }
  .stat-box .t { font-size:11px; color:#6b7280; margin-bottom:4px; }
  .stat-box .sub { font-size:10px; color:#6b7280; margin-top:2px; }
  .macro-row { display:flex; justify-content:space-around; text-align:center; margin-top:12px; padding-top:10px; border-top:1px solid #e5e7eb; }
  .macro-item .m-val { font-size:20px; font-weight:800; }
  .macro-item .m-label { font-size:10px; color:#6b7280; margin-top:1px; }
</style>
</head>
<body>

<div class="header">
  <div class="date">{YYYY} / {MM} / {DD} · 星期{X}</div>
  <h1>A股市场复盘</h1>
  <div class="sub">{当日核心主题，如：长鑫科技首日登顶 · 两市成交破两万亿}</div>
</div>

<div class="card">
  <div class="card-title"><span class="dot" style="background:#e53e3e"></span>主要指数行情</div>
  <div class="index-grid">
    <!-- 8个指数，中证1000背景#fef2f2，上证50背景#ecfdf5 -->
  </div>
  <div style="font-size:10px;color:#6b7280;margin-top:6px;text-align:center">
    最强指数 vs 最弱指数 剪刀差 <span style="color:#e53e3e;font-weight:700">{x.xx}%</span> · {风格结论}
  </div>
</div>

<div class="card">
  <div class="card-title"><span class="dot" style="background:#059669"></span>市场情绪</div>
  <div class="pulse-row">
    <!-- 上涨家数(红) 下跌家数(黑) 成交额(蓝) 涨停家数(橙) -->
  </div>
  <div style="display:flex;justify-content:space-between;font-size:11px;color:#6b7280;margin-top:10px;padding-top:10px;border-top:1px solid #e5e7eb">
    <span>较上日放量/缩量 <span style="color:#e53e3e;font-weight:700">±{x,xxx}亿</span></span>
    <span>跌停 {x}家</span>
    <span>封板率 {x}%</span>
  </div>
</div>

<div class="card">
  <div class="card-title"><span class="dot" style="background:#d97706"></span>风格特征 &amp; 宏观速览</div>
  <div class="split2">
    <!-- 最强指数卡片（红底 #fef2f2） + 最弱指数卡片（绿底 #ecfdf5） -->
  </div>
  <div style="font-size:13px;color:#1f2937;text-align:center;margin-top:10px;font-weight:700">
    风格剪刀差 <span style="color:#e53e3e">{x.xx}%</span> · {小盘碾压大盘 / 大盘防御占优 / etc}
  </div>
  <div class="macro-row">
    <div class="macro-item"><div class="m-val" style="color:#d97706">{xx.x}%</div><div class="m-label">6月 PMI · {描述}</div></div>
    <div class="macro-item"><div class="m-val" style="color:#111827">{x.xx}</div><div class="m-label">USD/CNY · {描述}</div></div>
    <div class="macro-item"><div class="m-val" style="color:#9ca3af">{日期}</div><div class="m-label">{下月}PMI公布</div></div>
  </div>
</div>

<div class="footer-text">
  数据来源：腾讯行情 · 蛋卷基金 PE/PB 分位（近10年）· 东方财富 · 证券时报 &nbsp;|&nbsp; 仅供参考，不构成投资建议 &nbsp;|&nbsp; 1/3
</div>
</body>
</html>
```

---

## 第二屏：焦点 + 板块

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="color-scheme" content="only light">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A股复盘 ②</title>
<style>
  /* 通用CSS + 以下专用样式 */
  .hero {
    background:#fffbf0 !important; border:2px solid #fbbf24; border-radius:12px;
    padding:18px 20px;
  }
  .hero-header { display:flex; align-items:center; gap:10px; margin-bottom:4px; }
  .hero-name { font-size:22px; font-weight:800; color:#111827; }
  .hero-tag { display:inline-block; background:#fbbf24; color:#78350f; font-size:10px; font-weight:700; padding:3px 8px; border-radius:4px; }
  .hero-code { font-size:11px; color:#6b7280; font-weight:400; }
  .hero-change { font-size:46px; font-weight:900; color:#e53e3e; line-height:1.1; margin:6px 0 4px; }
  .hero-price-line { font-size:12px; color:#6b7280; margin-bottom:10px; }
  .hero-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }
  .hero-stat {
    background:#ffffff !important; border:1px solid #fde68a; border-radius:8px;
    padding:12px 6px; text-align:center;
  }
  .hero-stat .v { font-size:20px; font-weight:800; color:#b45309; }
  .hero-stat .l { font-size:10px; color:#6b7280; margin-top:2px; line-height:1.3; }
  .hero-records {
    margin-top:10px; font-size:11px; color:#92400e; line-height:1.8;
    background:#fffbeb !important; border-radius:8px; padding:10px 12px;
  }
  .sector-row {
    display:flex; justify-content:space-between; align-items:center;
    padding:7px 0; border-bottom:1px solid #f3f4f6;
  }
  .sector-row:last-child { border-bottom:none; }
  .sector-name { font-size:13px; font-weight:500; color:#1f2937; }
  .sector-pct { font-weight:700; font-size:13px; min-width:56px; text-align:right; }
</style>
</head>
<body>

<!-- 今日主角（如有重大个股事件，没有则跳过） -->
<div class="hero">
  <div class="card-title"><span class="dot" style="background:#d97706"></span>今日主角</div>
  <div class="hero-header">
    <span class="hero-name">{股票名称}</span>
    <span class="hero-tag">{行业标签}</span>
  </div>
  <div class="hero-code">{代码} · {板块}</div>
  <div class="hero-change">{+xxx.xx%}</div>
  <div class="hero-price-line">
    发行价 <span style="color:#111827;font-weight:600">{x.xx}</span> → 开盘 <span style="color:#e53e3e;font-weight:700">{xx.xx}</span> → 收盘 <span style="color:#e53e3e;font-weight:700">{xx.xx}元</span>
    &nbsp;|&nbsp; 最高 <span style="color:#e53e3e;font-weight:700">{xx.xx}</span>（+{xxx}%）
  </div>
  <div class="hero-grid">
    <div class="hero-stat"><div class="v">{x,xxx}<span style="font-size:11px">亿</span></div><div class="l">成交额 · {纪录描述}</div></div>
    <div class="hero-stat"><div class="v">{x.xx}<span style="font-size:11px">万亿</span></div><div class="l">总市值 · {排名描述}</div></div>
    <div class="hero-stat"><div class="v">{xx.x}<span style="font-size:11px">%</span></div><div class="l">换手率 · {描述}</div></div>
  </div>
  <div class="hero-records">
    <!-- 破纪录列表，每行一个 ✅ -->
  </div>
</div>

<div class="card">
  <div class="card-title"><span class="dot" style="background:#2563eb"></span>板块红黑榜</div>
  <div style="font-size:11px;font-weight:700;color:#e53e3e;margin-bottom:4px">🟢 领涨板块</div>
  <!-- 6个领涨板块，sector-row -->
  <hr>
  <div style="font-size:11px;font-weight:700;color:#059669;margin-bottom:4px">🔴 领跌 / 滞涨</div>
  <!-- 4个领跌/滞涨板块，油气开采用红底 #fef2f2 -->
  <!-- 风格切换方向：旧能源→新经济 | 大金融→科技消费 | 红利防御→小盘成长 -->
</div>

<div class="footer-text">
  数据来源：腾讯行情 · 东方财富 · 证券时报 · 蛋卷基金 &nbsp;|&nbsp; 仅供参考，不构成投资建议 &nbsp;|&nbsp; 2/3
</div>
</body>
</html>
```

---

## 第三屏：估值 + 逻辑 + 风险

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="color-scheme" content="only light">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A股复盘 ③</title>
<style>
  /* 通用CSS + 以下专用样式 */
  .val-row { margin-bottom:10px; }
  .val-row:last-child { margin-bottom:0; }
  .val-header { display:flex; justify-content:space-between; align-items:baseline; font-size:12px; margin-bottom:3px; }
  .val-header .idx { font-weight:600; color:#1f2937; min-width:60px; }
  .val-header .detail { color:#6b7280; font-size:11px; }
  .val-header .pct { font-weight:700; font-size:12px; }
  .val-bar { height:8px; border-radius:4px; background:#e5e7eb; overflow:hidden; }
  .val-bar-fill { height:100%; border-radius:4px; }
  .val-note {
    font-size:11px; color:#6b7280; margin-top:8px; padding:10px 12px;
    background:#fef2f2; border-radius:8px; border-left:3px solid #e53e3e; line-height:1.5;
  }
  .logic-list { list-style:none; }
  .logic-list li { padding:10px 0; border-bottom:1px solid #f3f4f6; display:flex; gap:10px; align-items:flex-start; }
  .logic-list li:last-child { border-bottom:none; }
  .logic-num {
    background:#fee2e2; color:#e53e3e; width:22px; height:22px; border-radius:6px;
    display:flex; align-items:center; justify-content:center;
    font-size:12px; font-weight:800; flex-shrink:0;
  }
  .logic-text strong { font-size:14px; color:#111827; }
  .logic-text .desc { font-size:11px; color:#6b7280; display:block; margin-top:2px; line-height:1.4; }
  .risk-grid { display:grid; gap:8px; grid-template-columns:1fr 1fr; }
  .risk-item {
    background:#fef2f2 !important; border:1px solid #fecaca; border-radius:8px;
    padding:10px 12px; font-size:11px; display:flex; gap:8px; align-items:flex-start;
    color:#1f2937; line-height:1.4;
  }
  .risk-num {
    background:#e53e3e; color:#ffffff; width:16px; height:16px; border-radius:50%;
    display:flex; align-items:center; justify-content:center;
    font-size:9px; font-weight:800; flex-shrink:0;
  }
  .summary-box {
    background:#eff6ff !important; border:2px solid #93c5fd; border-radius:12px;
    padding:14px 18px; text-align:center; font-size:15px; font-weight:700;
    color:#1e40af; line-height:1.6; margin-top:12px;
  }
</style>
</head>
<body>

<div class="card">
  <div class="card-title"><span class="dot" style="background:#d97706"></span>指数估值水位 · 近10年PE分位</div>
  <!-- 4-5个指数PE分位进度条，沪深300/上证50/中证500 红色，创业板指 橙色 -->
  <!-- 每个指数显示：名称 + PE xx.x · PB x.xx · 股息 x.xx% + PE分位百分比 -->
  <div class="val-note">
    ⚠️ {整体估值判断}。上涨由<strong>流动性+情绪</strong>驱动，非盈利驱动{，安全边际不足}。
  </div>
</div>

<div class="card">
  <div class="card-title"><span class="dot" style="background:#2563eb"></span>上涨逻辑</div>
  <ol class="logic-list">
    <!-- 3条逻辑，每条：编号 + 标题(strong) + 解释(.desc) -->
  </ol>
</div>

<div class="card" style="border-color:#fecaca">
  <div class="card-title"><span class="dot" style="background:#e53e3e"></span>风险提示</div>
  <div class="risk-grid">
    <!-- 4个风险，2×2网格 -->
  </div>
</div>

<div class="summary-box">
  📢 {一句话定性判断}<br>
  {关键短语} · {关键短语} · {关键短语}<br>
  <span style="font-size:12px;font-weight:500">{总结建议}</span>
</div>

<div class="footer-text">
  数据来源：腾讯行情 · 蛋卷基金 PE/PB 分位（近10年）· 东方财富 · 证券时报 &nbsp;|&nbsp; 仅供参考，不构成投资建议 &nbsp;|&nbsp; 3/3
</div>
</body>
</html>
```

---

## 变量说明

| 变量 | 来源 | 示例 |
|------|------|------|
| 指数点位/涨跌幅 | `ashare_data.py market` | 3,858.25 +1.15% |
| PE/PB/分位/股息 | `ashare_data.py index CODE` | PE 14.4 PB 1.46 分位86.6% 股息2.52% |
| USD/CNY | `ashare_data.py fx` | 6.78 |
| PMI | WebSearch 国家统计局 | 50.3% |
| 成交额/涨跌家数 | WebSearch 交叉验证 | 2.08万亿 5195涨 |
| 板块涨跌幅 | WebSearch 申万行业 | 建筑材料 +5.44% |
| 个股事件 | WebSearch 当日重大IPO/异动 | 长鑫科技 +465.82% |
| 剪刀差 | 计算: 最强涨幅 - 最弱涨幅 | 3.53% |
