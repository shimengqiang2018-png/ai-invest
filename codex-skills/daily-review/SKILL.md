---
name: daily-review
description: "AI Invest skill: A股每日市场复盘 · 抖音发布. Source: skills/daily-review.md."
---

## Codex adapter note

This skill is generated from `skills/daily-review.md` so Claude Code and Codex users share one canonical workflow.

- Treat `$ARGUMENTS` as the user's request in the current Codex thread.
- When the source mentions Claude-only surfaces such as Task, Agent, WebSearch, Bash, Read, or Write, use the closest Codex capability available in this session: subagents when available, web search when needed, shell commands for local tools, and normal file edits for workspace files.
- Use shared project tools from `tools/` in this repository. Prefer running commands from the repository root with paths like `python3 tools/financial_rigor.py ...`; if the current thread starts outside the repo, locate the actual checkout path first instead of assuming a fixed home-directory path.
- Before starting research, run the `date` command to confirm today's date; treat it as the baseline for "latest" data and state the data cutoff date in the report header. Never assume the current date from training data.
- Preserve the research quality rules from `AGENTS.md`: cross-check financial data, use exact arithmetic tools for valuation/math, and clearly label uncertainty and source gaps.

# A股每日市场复盘 · 抖音发布

> 触发词：复盘、今日大盘、今日市场、市场复盘、每日复盘、发抖音、A股复盘、大盘分析。只要是当天市场回顾相关，即使没有明确说"复盘"二字也使用此skill。

## 流程总览

```
数据获取 ──→ 交叉核对 ──→ 【阻断】数据溯源表验收 ──→ 生成3个分屏HTML ──→ Chrome Headless 转 PNG ──→ 输出抖音标题+标签
```

**关键变更：交叉核对完成后，必须输出「数据溯源表」并得到用户确认，才能进入 HTML 生成。**

---

## 一、数据获取

**必须使用以下命令并行获取数据，禁止跳过脚本直接用 WebSearch 获取行情和估值数据。**

```bash
# 指数行情（必须）
python tools/ashare_data.py market

# 指数估值 PE/PB 分位（必须，至少获取沪深300/上证50/中证500/创业板指/中证1000/科创50）
python tools/ashare_data.py index 000300
python tools/ashare_data.py index 000016
python tools/ashare_data.py index 000905
python tools/ashare_data.py index 399006
python tools/ashare_data.py index 000852
python tools/ashare_data.py index 000688

# 汇率（必须）
python tools/ashare_data.py fx
```

**WebSearch 辅助获取（脚本无法覆盖的数据）：**
- 搜索当日领涨/领跌板块（申万行业分类）
- 搜索当日特殊事件（如重大IPO、政策发布、龙头异动）
- 搜索最新 PMI / 宏观经济数据
- 搜索上涨家数、下跌家数、成交额、涨停跌停家数、北向资金

---

## 二、交叉核对 + 数据溯源表（强制阻断点）

### 2.1 数据来源矩阵

每个数据点至少需要 **两个独立来源**，只有一个来源的数据必须标注 `[单源]`，零来源的不得使用。

| 数据类型 | 必选来源 | 辅助来源 | 容差 |
|---------|---------|---------|------|
| 指数点位/涨跌幅 | `ashare_data.py market` | WebSearch（证券时报/东方财富/新浪财经） | 点位±5，涨跌幅±0.05% |
| PE/PB分位/股息 | `ashare_data.py index CODE` | 蛋卷基金/且慢/天天基金 | PE分位±3% |
| 汇率 | `ashare_data.py fx` | 新浪财经/中国外汇交易中心 | ±0.01 |
| 成交额 | WebSearch A | WebSearch B | ±500亿 |
| 上涨/下跌家数 | WebSearch A | WebSearch B | ±200家 |
| 涨停家数 | WebSearch A | WebSearch B | ±10家 |
| 跌停家数 | WebSearch A | WebSearch B | ±5家 |
| 北向资金净流入 | WebSearch A | WebSearch B | ±20亿 |
| 板块涨跌幅 | WebSearch A | WebSearch B | ±0.3% |
| PMI/宏观数据 | 国家统计局官网 | 财新/东方财富 | 精确一致 |
| 个股涨跌幅/事件 | WebSearch A | WebSearch B | 精确一致 |

### 2.2 WebSearch 检索规范

每个 WebSearch 数据点必须使用**两次独立搜索**，搜索词不能相同（避免同一搜索引擎返回同一来源的不同镜像）：

```
# 成交额 — 两次独立搜索
搜索1: "A股 YYYY年MM月DD日 成交额 收盘"
搜索2: "YYYY年MM月DD日 A股 两市成交"

# 涨停家数 — 两次独立搜索
搜索1: "A股 YYYY年MM月DD日 涨停 家数"
搜索2: "YYYY年MM月DD日 涨停板 复盘"

# 板块涨跌幅 — 搜索当日领涨领跌
搜索1: "A股 YYYY年MM月DD日 领涨板块 申万"
搜索2: "YYYY年MM月DD日 板块涨跌 行业"
```

### 2.3 强制阻断点：数据溯源表

**交叉核对完成后、生成 HTML 之前，必须输出以下表格并等待用户确认：**

```
## 数据溯源表 · YYYY-MM-DD

| 数据项 | 值 | 来源1 | 来源2 | 状态 |
|--------|-----|-------|-------|------|
| 上证指数 | 3,820.52 (-0.98%) | ashare_data.py | 证券时报 ✅ | ✅ 双源 |
| 创业板指 | 3,397.97 (-5.37%) | ashare_data.py | 东方财富 ✅ | ✅ 双源 |
| 上涨家数 | ~3,100 | 证券时报 10:28 | 待核实 ⚠️ | ⚠️ 单源/待补 |
| 涨停家数 | ? | 未找到 | 未找到 | ❌ 缺失 |
| ... | ... | ... | ... | ... |

数据质量评估:
- ✅ 双源确认: X/Y 项
- ⚠️ 单源待补: X/Y 项
- ❌ 缺失: X/Y 项
```

**阻断规则：**
- 任何 `❌ 缺失` 的数据点，**不得填入 HTML**。对应位置标注"N/A"或使用已验证的替代数据
- 任何 `⚠️ 单源` 的数据点，在 HTML 中必须加 `*` 标注，页脚注明"* 单源数据，未经交叉验证"
- 用户未确认溯源表前，**禁止生成 HTML**

### 2.4 核对差异处理

若两源差异超过容差范围：
- 标注差异值
- 优先采用脚本数据（指数/估值）或更权威来源（官方 > 财经媒体 > 自媒体）
- 差异超过容差 2 倍以上的，标记为 `⚠️ 争议`，报告中同时列出两个值

### 2.5 硬约束

- **绝对禁止编造任何数字**。宁可空着写"数据待核实"，不能填假数
- 涨停/跌停/封板率/北向资金如果当天无法从两个来源确认，**直接跳过**，不要用估算凑数
- 盘中复盘（午盘）的数据天然不完整——标注"截至午盘"，成交额注明"半日"

---

## 三、内容结构（三屏分割）

### 第一屏：大盘概览

1. **页眉**：日期 + 标题"A股市场复盘" + 当日核心主题（一句话）
2. **主要指数行情**：8个指数网格（上证/深证/创业板/沪深300/中证1000/上证50/中证500/科创50），点位+涨跌幅，高亮最强和最弱指数
3. **市场情绪**：
   - **必须项**：上涨家数、下跌家数、成交额（三项至少双源确认）
   - **可选项**：涨停家数、跌停家数、封板率、北向资金（只有双源确认后才展示，否则跳过整个可选项）
   - **附注**：放量/缩量判断基于成交额同比变化
4. **风格特征 + 宏观速览**：最强vs最弱指数对比卡片 + PMI/汇率/重要事件（PMI无当月数据时用上月，标注月份）

### 第二屏：焦点 + 板块

1. **今日主角**（如有重大个股事件）：名称、代码、关键数据（涨幅/成交/市值/换手率）、创下的纪录、基本面亮点
2. **板块红黑榜**：领涨板块（6个）+ 领跌/滞涨板块（4个），标注风格切换方向

### 第三屏：估值 + 逻辑 + 风险

1. **指数估值水位**：4-5个主要指数的PE分位进度条，附PE/PB/股息数据，整体判断
2. **上涨逻辑**：3条核心逻辑，每条带简短解释
3. **风险提示**：4个关键风险，2×2网格
4. **总结**：一句话总结框

---

## 四、HTML 规范

### 通用标准

| 属性 | 值 |
|------|-----|
| 背景色 | `#f2f3f5` |
| 卡片色 | `#ffffff` |
| 文字色 | `#1a1a2e` |
| 涨色 | `#e53e3e` |
| 跌色 | `#059669` |
| 字体 | PingFang SC, Microsoft YaHei, sans-serif |
| 基准字号 | 14px |
| 行距 | 1.5 |
| 四边留白 | 36px |
| 卡片间距 | 12px |
| 卡片内边距 | 16px 18px |
| 卡片圆角 | 12px |
| **最大宽度（强制）** | **540px** — 缺失会导致移动端撑满屏幕边缘 |

### 必须包含的元标签

```html
<meta name="color-scheme" content="only light">
```

### 必须包含的 CSS

```css
html { color-scheme: only light !important; }
body {
  display: flex;
  flex-direction: column;
  min-height: 960px;
  justify-content: space-between;
  max-width: 540px;  /* 绝对必须，防止内容撑到边缘 */
  margin: 0 auto;     /* 居中 */
}
@media (prefers-color-scheme: dark) {
  /* 强制覆盖所有元素为亮色 */
  html, body, .card, .index-item, .hero, .hero-stat, .risk-item, .summary-box {
    background-color: #ffffff !important;
  }
  html, body { background-color: #f2f3f5 !important; }
  body, h1, .index-item .name, .hero-name, .sector-name { color: #1a1a2e !important; }
}
```

### 文件命名

```
reports/大盘复盘-YYYYMMDD-p1.html
reports/大盘复盘-YYYYMMDD-p2.html
reports/大盘复盘-YYYYMMDD-p3.html
```

---

## 五、PNG 转换

使用 Chrome Headless，每个分屏单独截取：

```bash
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
OUT="reports"

for i in 1 2 3; do
  "$CHROME" --headless \
    --screenshot="$OUT/大盘复盘-YYYYMMDD-p${i}.png" \
    --window-size=540,960 \
    --default-background-color=0xf2f3f5 \
    --force-device-scale-factor=2 \
    "file://$(pwd)/$OUT/大盘复盘-YYYYMMDD-p${i}.html" 2>&1 | tail -1
done
```

**参数说明：**
- `--window-size=540,960`：视口尺寸，乘以 `--force-device-scale-factor=2` = 1080×1920（抖音标准）
- `--default-background-color=0xf2f3f5`：确保背景色正确
- 文件路径必须用 `file://` 协议 + 绝对路径

**验证**：截取后检查每张图尺寸是否为 1080×1920：
```bash
sips -g pixelWidth -g pixelHeight reports/大盘复盘-YYYYMMDD-p1.png
```

---

## 六、抖音发布物料

PNG 生成完成后，必须输出以下发布物料：

### 6.1 标题建议

提供 **2-3 个**不同风格的标题供选择，每个标注适用场景：

| 风格 | 特征 | 适用场景 |
|------|------|---------|
| **反差流** | 用一个反常数据点制造钩子（如"指数暴跌但涨多跌少"） | 当日有明显反差信号时首选 |
| **震惊流** | 数字+情绪词（如"暴跌5%""崩盘""涨停潮"） | 单边极端行情 |
| **逻辑流** | 一句话讲清因果关系 | 适合懂行的观众，涨粉质量高 |

**标题规则：**
- 必须包含当日最核心的一个数字（涨跌幅/家数比）
- 不超过 40 字（抖音标题截断线）
- 避免"震惊！""突发！"等过度标题党
- 从当日的实际数据中提取钩子，不编造冲突

### 6.2 标签建议

提供 5-8 个标签，覆盖：
- **大盘标签**（必选）：`#A股` `#财经` `#股票` `#复盘`
- **行情标签**（按当日涨跌选）：`#创业板` `#AI算力` `#光刻机` 等
- **风格标签**（可选）：`#国产替代` `#风格切换`

### 6.3 输出格式

```
## 📱 抖音发布物料

### 标题
1. 【反差流】{标题}
2. 【震惊流】{标题}
3. 【逻辑流】{标题}

> 推荐：第 X 个，理由：{一句话}

### 标签
#A股 #财经 #股票 #复盘 #{当日热点1} #{当日热点2} #{当日热点3}
```

---

## 七、交互原则

1. **先拉数据，再分析**。不预判结果。
2. **数据差异主动汇报**。核对中发现不一致时，列出差异，让用户决定。
3. **客观表述**。用"数据显示"而非"我认为"。
4. **反面论据**。每个核心判断附带反向观点。
5. **简洁**。这是抖音用图，文字精炼，不写长段落。
6. **宁缺毋滥**。宁可图里少一个数据卡片，也绝不填一个假数字。

### 6.1 反面案例（2026-07-28 事故）

**问题**：涨停46家、跌停11家、封板率73%、PMI 50.5%——这四个数字没有任何来源，凭空编造。

**根因**：HTML 模板写了"涨停家数"卡片，执行者为了填满模板而编造数据。

**教训**：
- 模板是**参考骨架**，不是强制清单。拿不到的数据就删掉对应卡片
- 市场情绪卡片只保证 **上涨家数、下跌家数、成交额** 三项必须项
- 任何数字写入 HTML 前，必须能在溯源表中找到对应行

---

## 八、发布清单

生成完成后确认：
- [ ] 三张图均为 1080×1920
- [ ] 亮色主题，无暗黑残留
- [ ] 数据溯源表中所有显示的数据至少双源确认（单源数据已标注 `*`）
- [ ] 数据溯源表 `❌ 缺失` 项未出现在 HTML 中
- [ ] 页脚有数据来源 + 免责声明
- [ ] 单源数据已在页脚注明 `* 部分数据仅有单一来源`
- [ ] `⚠️ 争议` 数据已在报告中列出两个值
- [ ] 已输出 2-3 个抖音标题 + 标签建议
- [ ] 文件路径: `reports/大盘复盘-YYYYMMDD-p{1,2,3}.png`

### 8.1 数据质量自检（生成 HTML 前必查）

在写 HTML 之前，逐项回答：
1. 有没有数字是我凭感觉填的？→ 有就删掉
2. 每个数字能不能在 30 秒内找到来源？→ 不能就标注或删除
3. PMI/宏观数据是哪个月的？→ 必须注明月份
4. 成交额是全天的还是半日的？→ 盘中复盘必须标注"半日"
5. 涨停/跌停家数是两个来源交叉验证的吗？→ 不是就去掉

---

*免责声明：本复盘基于公开数据和量化模型，仅供研究参考，不构成投资建议。*

## 参考模板

完整的 HTML 模板参考见 `skills/daily-review/references/templates.md`，包含三个分屏的完整 HTML 骨架代码。
