# GitHub 投资知识体系 — 深度学习笔记

> 2026-07-18 从 GitHub 开源社区系统学习投资技术知识
> 研读 25+ 仓库，分两轮深度探索

---

## 一、研读仓库全景图

### 第一轮（直接克隆研读源码）

| 仓库 | Stars | 语言 | 核心价值 |
|------|-------|------|---------|
| [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) | 53K | Python | 大师 Agent 提示词 + AlphaModel 架构 |
| [TradingAgents](https://github.com/TauricResearch/TradingAgents) | 71K | Python | LangGraph 多 Agent 编排 + 多空辩论 |
| [valueinvest](https://github.com/wangzhe3224/valueinvest) | 新 | Python | 24 种估值方法 + 11 项会计红旗 + 护城河引擎 |
| [Value-Investing-Agent](https://github.com/danielchu97/Value-Investing-Agent) | 新 | TypeScript | MCP 服务器模式 + Graham/Buffett 分析 |
| [company-analyst](https://github.com/Kevin-XXX/company-analyst) | 新 | Python | 三方法混合估值 + 结构化研报生成 + A/H 股支持 |
| [Microsoft Qlib](https://github.com/microsoft/qlib) | 44K | Python | AI 量化全流程平台 |

### 第二轮（WebSearch 深度学习）

| 仓库 | Stars | 核心价值 |
|------|-------|---------|
| [FinGPT](https://github.com/AI4Finance-Foundation/FinGPT) | 20K | 金融 LLM 五层架构 + LoRA 微调 |
| [FinRobot](https://github.com/AI4Finance-Foundation/FinRobot) | 7.2K | 多 Agent 金融 CoT 推理 |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | 15K | 深度强化学习交易 + GRPO/DAPO |
| [Freqtrade](https://github.com/freqtrade/freqtrade) | 51K | 策略→回测→超参优化→实盘一体化 |
| [vectorbt](https://github.com/polakowo/vectorbt) | ~4K | GPU 加速向量化回测 |
| [Riskfolio-Lib](https://github.com/dcajasn/Riskfolio-Lib) | 4.2K | 24 种凸风险度量的组合优化 |
| [PyPortfolioOpt](https://github.com/robertmartin8/PyPortfolioOpt) | 4.6K | 模块化四层组合优化 |
| [FinBERT2](https://github.com/ValueSimplex/FinBERT2) | 新 | 32B token 中文金融语料预训练 |
| [FinNLP](https://github.com/AI4Finance-Foundation/FinNLP) | 1.5K | 全栈金融 NLP + 中英文数据源 |
| [VeighNa/vnpy](https://github.com/vnpy/vnpy) | ~35K | 国产第一量化全流程框架 |
| [Lumibot](https://github.com/Lumiwealth/lumibot) | 4K | 最广资产覆盖的回测+实盘 |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | ~23K | Rust 原生纳秒级回测引擎 |
| [AKShare](https://github.com/akfamily/akshare) | 10K+ | A 股全品类免费数据接口 |
| [Backtrader](https://github.com/mementum/backtrader) | 22K | 事件驱动回测入门首选 |
| [awesome-quant-ai](https://github.com/leoncuhk/awesome-quant-ai) | 资源合集 | 量化 AI 资源索引 |
| [awesome-systematic-trading2](https://github.com/Big-Lu-Qi/awesome-systematic-trading2) | 资源合集 | 系统化交易工具大全 |

---

## 二、架构设计精髓

### 2.1 AlphaModel 统一接口（ai-hedge-fund，53K⭐）

这是整个 GitHub 投资开源生态中最优雅的设计之一。

```python
class AlphaModel(ABC):
    """所有分析师共享的接口。LLM Agent 和量化模型都是 AlphaModel。"""
    @abstractmethod
    def predict(ticker, date, data_client) -> Signal:
        """形成 point-in-time 观点。返回 Signal(value: [-1,1], reasoning: str)"""
```

**五个不可妥协的设计原则**：

1. **界面统一，能力各异** — 系统不知道也不关心分析师是 LLM 还是数学公式，只认 Signal 接口。新分析师只需实现 `predict()`。

2. **LLM 永远不触碰交易** — "The LLM never touches the trade." LLM 只形成观点和叙事。仓位管理、风险限制、下单执行全部由确定性代码完成。这是个硬性边界。

3. **Point-in-Time 诚实** — "在任何模拟日期，基金只能用当时已公开的数据。绝不偷看未来。这是让回测有意义的前提。"

4. **失败契约** — 数据层错误：传播（fail loud，不允许用坏数据静默生成信号）。LLM 调用失败：弃权（abstain，承认不知道比假装知道好）。LLM 解析失败：弃权（保留原始响应用于调试）。

5. **LLM 决定被缓存和记录** — 相同快照+相同提示词，永远不重复调用 LLM。每个决策的完整提示词+响应被持久化，用于审计和调试。

### 2.2 基金的三层嵌套结构（ai-hedge-fund）

```
FUND     = Allocator (CIO) 管理 Strategies（资本分配）
STRATEGY = Portfolio Policy 管理 Analysts（一个"pod"，如 Value/Event/Macro）
ANALYST  = AlphaModel → Signal（观点 + 确信度）
```

**一条 pipeline，三种模式**：
```
BACKTEST = 历史时钟 + 模拟券商 → 回测
PAPER    = 实时时钟 + 纸券商   → 模拟盘
LIVE     = 实时时钟 + 真实券商 → 实盘
```

三种模式共享同一条 `run_cycle` 代码路径 — "回测时跑的代码就是实盘时跑的代码"，不存在研究环境和生产环境的代码分叉。

**研究实验室**：生产基金持续运行的同时，实验室在回测候选策略（新分析师、新组合策略、新分配器）。胜出的策略通过验证门（CPCV/PBO）升入实盘。

### 2.3 多 Agent 协作流水线（TradingAgents，71K⭐）

```
数据层 → 分析师层（并行4个）→ 辩论层（多空对抗）→ 交易层 → 风控辩论层 → 决策层

分析师层（并行启动）:
├─ 市场分析师 → 技术面报告（只能调用价格/指标工具）
├─ 社媒分析师 → 情绪报告（只能调用 Reddit/StockTwits）
├─ 新闻分析师 → 事件报告（只能调用新闻/内部交易/宏观工具）
└─ 基本面分析师 → 财务报告（只能调用财报工具）

辩论层:
  多头研究员 ⇄ 空头研究员（最多 N 轮对抗）
        ↓
  研究经理裁决 → 投资论点

风控辩论层（三方视角）:
  激进派: "市场时机好，全力执行"
  保守派: "风险过高，减仓或观望"
  中性派: "折中方案"
        ↓ (三方辩论)
  风控裁判: 风险调整后的投资计划

决策层:
  组合经理 → 最终交易决策
```

**三大核心设计特色**：

1. **对抗式辩论而非单向分析** — 一个人的分析容易有盲点。多空双方互驳，暴露逻辑漏洞。这不是装饰，是质量控制机制。

2. **工具绑定防止幻觉** — 每个 Agent 只能调用分配给它的工具。基本面分析师不能碰价格数据。每个 Agent 启动时注入 `resolve_instrument_identity()` 的确定性结果（公司名/行业/交易所），防止 Agent 从价格图表"模式识别"出错误的公司。

3. **记忆与反思系统** — 过去的决策和实际回报被记录。下次分析同一股票时，`memory_log.get_past_context()` 注入历史教训。

### 2.4 Freqtrade 的统一接口设计（51K⭐）

```python
class IStrategy:
    """一个策略类在回测、超参优化、模拟盘、实盘四种模式下运行同一套代码"""
    timeframe = "1h"
    minimal_roi = {"60": 0.01, "30": 0.02, "0": 0.04}
    stoploss = -0.10

    def populate_indicators(self, dataframe, metadata):
        """计算指标 — 所有模式共用"""
    def populate_entry_trend(self, dataframe, metadata):
        """入场信号 — 所有模式共用"""
    def populate_exit_trend(self, dataframe, metadata):
        """离场信号 — 所有模式共用"""
```

**核心教训**：回测代码和实盘代码的分叉是量化系统最大的隐性 bug 来源。Freetrade 强制同一条代码路径跑所有模式 — 回测的承诺就是实盘的承诺。

### 2.5 VectorBT 的向量化范式（~4K⭐）

传统回测框架是事件驱动的：逐 bar 循环，一个策略一个策略地跑。VectorBT 的思路完全不同：

```python
# 测试 10,000 个双均线参数组合，一次运行
windows = np.arange(2, 101)
fast_ma, slow_ma = vbt.MA.run_combs(price, window=windows, r=2)
entries = fast_ma.ma_crossed_above(slow_ma)
exits = fast_ma.ma_crossed_below(slow_ma)
pf = vbt.Portfolio.from_signals(price, entries, exits)
# 所有(fast_window, slow_window)组合同时回测完毕
```

**原理**：把参数组合打包成多维 NumPy 数组，Numba JIT 编译热路径，一次矩阵运算代替 N 次循环。小时级的网格搜索变成秒级。

---

## 三、估值方法体系

### 3.1 24种估值方法（valueinvest）

#### 绝对估值
| 方法 | 公式/原理 | 适用场景 | 关键参数 |
|------|----------|---------|---------|
| **Graham Number** | √(22.5 × EPS × BVPS) | 防御型投资者，稳定蓝筹 | BVPS < $10 视为不适用（轻资产公司） |
| **Graham Formula** | EPS × (8.5 + 2g) × 4.4 / Y | 适度增长 (5-15%) 成熟企业 | g 上限 20%，下限 0% |
| **NCAV (Net-Net)** | (流动资产 - 总负债 - 优先股) / 股数 | 深度价值，烟蒂股 | 安全边际 = 2/3 NCAV |
| **DCF** | Σ FCF/(1+WACC)^t + 终值/(1+WACC)^n | 有稳定 FCF 的企业 | WACC 通过 CAPM 动态校准 |
| **Reverse DCF** | 反推当前股价隐含的增长率 | 检验市场预期合理性 | 隐含增长率 vs 历史增长率差距 |
| **EPV** | (调整后盈利 - 维持 CapEx) / WACC | 无增长/低增长企业 | 维持 CapEx ≠ 总 CapEx |
| **DDM** | DPS / (r - g) | 稳定派息企业 | 要求 r > g |
| **Two-Stage DDM** | 高增长期 + 稳定期 DPS 折现 | 成长转成熟派息企业 | 两阶段增长率差 |
| **Owner Earnings** | (经营利润 + D&A - 维持 CapEx) / WACC | 巴菲特偏好的方法 | CapEx 分解是关键 |
| **Residual Income** | BV + Σ (ROE - r) × BV/(1+r)^t | 银行/金融股 | 终值 ROE 收敛到 r |
| **PB Valuation** | BV × (ROE - g) / (r - g) | 银行/金融股 | 要求 ROE > g |
| **SOTP** | Σ 各分部估值 - 持股公司折扣 - 少数股东权益 | 多元化集团 | 持股公司折扣 10-30% |

#### 相对估值
| 方法 | 指标 | 适用场景 |
|------|------|---------|
| **P/E Relative** | vs 历史 PE 百分位 | 盈利稳定的公司 |
| **P/B Relative** | vs 历史 PB 百分位 | 资产重的公司 |
| **EV/EBITDA** | 同行中位数比较 | 跨资本结构比较 |
| **PEG** | PE / 盈利增长率 | 成长股的估值合理性 |
| **GARP** | PEG + DCF 混合 | 合理价格成长 |

#### 质量/健康评分
| 方法 | 评分维度 | 用途 |
|------|---------|------|
| **Piotroski F-Score** | 9 项（盈利 4 + 财务 3 + 运营 2） | 财务健康度，0-9 分 |
| **Altman Z-Score** | 5 项财务比率 | 破产风险预测 |
| **Beneish M-Score** | 8 项财务报表比率 | 盈利操纵检测 |
| **Magic Formula** | ROC 排名 + 盈利收益率排名 | 质优价廉筛选 |
| **Rule of 40** | 收入增长% + FCF 利润率% ≥ 40 | SaaS/软件公司 |

### 3.2 自动方法推荐逻辑

```python
# 根据公司特征自动选择估值方法组合
if is_bank:
    → pb, residual_income, ddm, altman_z
elif has_dividend and not is_growth:
    → ddm, two_stage_ddm, graham_number, owner_earnings
elif is_growth and has_fcf:
    → dcf, reverse_dcf, peg, garp, ev_ebitda
elif is_value and has_positive_earnings:
    → graham_number, graham_formula, epv, owner_earnings
else:
    → graham_formula, epv
```

**对 ai-invest 的启示**：不同公司不应该用同一套方法。银行用 PB/RI，成长股用 DCF/PEG，价值股用 Graham — 方法选择本身就是估值判断。

### 3.3 三方法混合估值（company-analyst）

```
公允价值 = 50% DCF Base + 30% Peer Comps + 20% DCF Bear
```

三个组件各司其职：
- **DCF Base**（50%权重）：用分析师一致预期增长率，通过 CAPM 动态校准 WACC，给未来 5 年建模
- **Peer Comps**（30%权重）：用同业 PE/EV-EBITDA/PS 中位数反推隐含价格。这个方法的精妙之处在于它不是直接比较 PE，"这个便宜那个贵"，而是反推：**"如果这只股票以同行中位数估值交易，股价应该是多少？"**
- **DCF Bear**（20%权重）：Base 增长率的 70% + WACC 上浮 5%。这是下限保护。

> 设计了但故意不用 Buy/Sell/Hold 标签。核心理念："贵不代表会跌，便宜不代表会涨。估值是锚，不是预测。"

### 3.4 周期股专用方法

周期股的陷阱：景气高点 PE 很低（显得便宜），景气低点 PE 很高（显得贵）— 传统估值完全失效。

解决方案：**不用当年财务数据，用完整周期（10年）平均值。**
- Cyclical PB：10 年平均 ROE → 推断合理 PB
- Cyclical PE：10 年平均 EPS → 推断合理 PE（Shiller CAPE 的思路）
- Cyclical FCF：10 年平均 FCF → DCF

---

## 四、财务红旗检测系统

### 4.1 11项会计红旗（valueinvest）

| 类别 | 红旗 | 检测逻辑 | 致命阈值 |
|------|------|---------|---------|
| **盈利质量** | CFO vs NI | OCF/NI 比率 | < 0.2 → 严重 |
| **盈利质量** | Sloan Accrual | (NI-OCF)/总资产 | > 10% → 严重 |
| **盈利质量** | Earnings Persistence | NI 与 OCF 方向一致性 | **NI>0 且 OCF<0 → 致命** |
| **收入确认** | AR vs Revenue | 隐含 DSO 天数 | > 90 天 → 严重，> 120 → 致命 |
| **收入确认** | Revenue Quality | OCF/Revenue 比率 | < 5% → 警示，< 0 → 致命 |
| **资产效率** | Inventory Buildup | 隐含 DIO 天数 | > 120 天 → 警示 |
| **资产效率** | Working Capital | 流动比率趋势 | 大幅下降 |
| **资产效率** | CapEx vs Depreciation | CapEx/折旧 | < 0.4 → 严重（资产饥渴） |
| **资本结构** | Debt Trend | 负债率趋势 | 上升 > 10pp → 严重 |
| **资本结构** | SBC/Revenue | 股权激励占收入 | > 5% → 警示，**SBC>NI → 致命** |
| **资本结构** | FCF Quality | FCF/NI 比率 | **NI>0 且 FCF≤0 → 致命** |

### 4.2 三个致命红旗

1. **NI>0 但 OCF<0**：利润全部是应收账款，没有现金进来
2. **NI>0 但 FCF≤0**：赚了利润但全投回 CapEx，自由现金流为零
3. **SBC > Net Income**：股权激励比净利润还大，股东在被隐性稀释

这三个信号中的任何一个都应该触发"不要投资"的判断。

---

## 五、护城河量化分析框架

### 5.1 五维度加权评分（valueinvest）

```
护城河评分 (0-100)
├─ 盈利能力   (30%)  — ROE 持续性、毛利率水平与趋势
├─ 效率       (20%)  — ROIC vs WACC、资产周转率
├─ 成长       (20%)  — 收入 CAGR、盈利 CAGR
├─ 市场地位   (15%)  — 市场份额、定价权证据
└─ 财务堡垒   (15%)  — 负债率、利息覆盖倍数、FCF 稳定性
```

### 5.2 护城河强度分级

| 分数 | 分类 | 含义 |
|------|------|------|
| 75-100 | **Wide Moat** 宽护城河 | 持久竞争优势，难以复制 |
| 55-74 | **Narrow Moat** 窄护城河 | 有一定优势但非牢不可破 |
| 35-54 | **No Moat** 无护城河 | 无明显竞争优势 |
| 0-34 | **Eroding** 护城河侵蚀 | 竞争地位在恶化 |

**对 ai-invest 的启示**：护城河分析不应该是定性描述（"该公司有品牌优势"），而应该是量化评分。11 个具体信号支撑 5 个维度的加权综合。

---

## 六、大师 Agent 系统提示词设计

ai-hedge-fund 的设计哲学：**Agent = 名字 + 系统提示词。** 所有机制在基类（LLMAgent），子类只定义 `name` 和 `get_system_prompt()`。

### 6.1 四个 Agent 的完整框架

#### 巴菲特 Agent
```
分析清单：
1. 能力圈 — 从提供的数据能否理解这个生意？
2. 竞争护城河 — ROE 是否持续高位？利润率稳定/改善？
3. 管理层质量 — 账面价值复利、合理杠杆、持续 FCF
4. 财务实力 — 低负债、健康流动比率、持续盈利
5. 估值 — 相对质量和增长，价格合理吗？
      好公司+合理价格 > 普通公司+好价格
6. 长期前景 — 愿意持有十年吗？

信号规则：
- bullish:  强韧的好生意 + 合理或更好的价格
- bearish:  弱化/恶化的生意，或价格要求完美
- neutral:  混合证据，或好生意但价格明显过高

硬约束：只基于提供的数据推理。把最新财报日当做今天。
        禁止使用财报日后发生的任何知识。禁止发明数字。
```

#### 芒格 Agent
```
分析清单：
1. 反转 — 这笔投资怎么失败？利润率恶化？杠杆上升？ROE 下降？
2. 质量 — 好生意年复一年高资本回报，不要只看一年
3. 激励 — 账面价值在复利吗？FCF 真实且增长吗？
4. 价格 — 好生意合理价格可接受。愚蠢价格不行。
5. 太难堆 — 数字不清晰就放进去。大多数东西都属于太难堆。

信号规则：
- bullish:  明显的好生意 + 不愚蠢的价格
- bearish:  平庸/恶化生意、不诚实的数字、需要相信蠢事的估值
- neutral:  太难堆，或好质量但不值得付的价格

硬约束：直言不讳。数字说什么就说什么，不粉饰。
```

#### 格雷厄姆 Agent
```
分析清单：
1. 安全边际 — 价格相对已证明的盈利能力和账面价值是否足够低？
      比较 PE 和 PB 与保守标准。PE>>15-20 需要非凡理由——很少批准
2. 财务实力 — 流动比率 > 1.5，适度负债。弱资产负债表直接否定
3. 盈利稳定性 — 全记录期正盈利，无剧烈波动。投机性增长一文不值
4. 增长溢价 — 深度怀疑为预测增长付费。未来不确定，资产负债表不不确定

信号规则：
- bullish:  健全生意 + 强健资产负债表 + 真正的安全边际
- bearish:  弱财务、不稳定盈利、或价格资本化了希望而非事实结果
      高估本身就是看空事实
- neutral:  健全企业，但安全边际不足
```

#### 彼得·林奇 Agent
```
分析清单：
1. 分类 — 快速成长(20%+)/稳健(10-12%)/慢速/反转？
2. PEG 测试 — PE 远低于可见增长率→有吸引力；PE 远高于→在为故事付钱
3. 故事验证 — 收入增长→盈利增长，利润率向好，EPS 一路上行
4. 资产负债表 — 避免高负债。强健资产负债表让成长故事挺过坏年头
5. 盈利驱动股价 — 长期来看这是全部游戏。忽略其他。

信号规则：
- bullish:  真实可见的盈利增长 + PE 未透支 (PEG 舒适)
- bearish:  增长减速 + 高估值倍数 — 这就是亏钱的方式
- neutral:  好公司，价格已充分反映

硬约束：用简单的话说。解释不清楚故事就 neutral。
```

### 6.2 Agent 设计的五条教训

1. **Agent = 系统提示词，不是代码** — 基类管所有机制（LLM 调用、解析、缓存、弃权），子类只定义 voice
2. **强制结构化输出** — 必须返回 `{signal, confidence: 0-100, reasoning}` JSON
3. **置信度量化** — 不仅判断方向，还给出确信度（90-100 = 异乎寻常，70-89 = 可靠，40-69 = 混合，10-39 = 弱）
4. **失败兜底** — LLM 调用/解析失败 → abstain（signal=0, metadata.abstained=True），不假装有观点
5. **反幻觉约束** — "只基于提供的数据。把最新财报日当做今天。禁止发明数字。"

---

## 七、金融 AI 与 NLP

### 7.1 FinGPT（20K⭐）— 五层架构

```
第1层 数据源      — 新闻、股价、财报电话会、SEC 文件、社交媒体
第2层 数据工程    — 实时 NLP 处理，解决时序敏感性和低信噪比
第3层 LLMs       — 开源基座模型 (Llama-2, Falcon, ChatGLM2, Qwen)
                   通过 LoRA/QLoRA 微调，成本从 $3M→$300/次
第4层 任务        — 情绪分析、股价预测、报告分析、NER、关系抽取
第5层 应用        — FinGPT Forecaster、FinGPT RAG、财报分析
```

**核心设计**：数据驱动 + 轻量适配。不训练巨型金融模型，而是在小型开源模型上用金融数据做 LoRA 微调。成本降低 10,000 倍。

### 7.2 FinRobot（7.2K⭐）— 金融 CoT 推理

三阶段金融思维链：
```
Data-CoT Agent   → 聚合多源金融数据
Concept-CoT Agent → 产生分析洞见，模仿人类分析师思维
Thesis-CoT Agent  → 综合为连贯的投资报告
```

Smart Scheduler：Director Agent 根据任务特征动态选择最合适的 LLM，任意组合多个模型。

### 7.3 FinBERT2 — 中文金融 NLP

- 32B token 中文金融语料预训练：260 万份分析师报告 + 20 年公司公告 + 财经新闻
- 在 5 个金融分类任务上 **超越 GPT-4/Claude-3.5/Qwen2-72B 9.7%-12.3%** F1
- 专门的中文金融领域模型，不是通用 LLM

### 7.4 FinNLP（1.5K⭐）— 全栈金融数据

```
pip install finnlp
```

**中文支持数据源**：新浪财经、东方财富、微博、巨潮资讯（公司公告）、AShare 数据集
**美股支持数据源**：Finnhub、Stocktwits、Reddit、SEC 文件

---

## 八、组合优化与风险管理

### 8.1 Riskfolio-Lib（4.2K⭐） vs PyPortfolioOpt（4.6K⭐）

| 维度 | Riskfolio-Lib | PyPortfolioOpt |
|------|-------------|----------------|
| **优化引擎** | CVXPY (LP到SDP全覆盖) | CVXPY + CLA |
| **风险度量** | **24 种凸风险度量** | ~4 种 |
| **Black-Litterman** | 资产级 + **因子模型**（BLB、ABL） | 仅资产级 |
| **层次化方法** | **HRP + HERC + NCO** | 仅 HRP |
| **风险平价** | 12 种风险度量支持 | 通过 EfficientFrontier |
| **因子模型** | 完整支持，因子载荷矩阵 | 不直接支持 |
| **协方差估计** | 基础 (历史, EWMA) | **丰富** (Ledoit-Wolf, OAS, MCD, 半协方差) |
| **设计哲学** | 厨房水槽 — 什么都有 | **四层模块化** — 每层可替换 |

**选择建议**：
- 需要数学上最复杂的模型 → Riskfolio-Lib
- 需要最干净的 API 和最灵活的协方差估计 → PyPortfolioOpt

**对 ai-invest 的启示**：portfolio-review skill 目前是手工分析。应该引入至少基础的组合优化 — 哪怕只是有效前沿可视化。

### 8.2 FinRL（15K⭐）— 深度强化学习交易

三层架构：Applications（策略）→ Agents（DRL算法）→ Environment（市场模拟+数据）

2025 年前沿：**DAPO-SR 算法**（FinRL Contest 第 2 名）：
- GRPO 替代 PPO：去掉 critic 网络，内存从 120GB → 15GB
- 非对称裁剪：`ε_low ≠ ε_high`，允许更灵活的策略更新
- LLM 信号注入：`r' = r × (Sentiment^α) / (Risk^β)` — LLM 情绪和风险作为奖励修改器
- 230% 累计收益，训练时间从 8h → 2.5h

---

## 九、可立即应用到 ai-invest 的改进

### 9.1 高优先级（立即可做）

| # | 改进 | 来源 | 效果 |
|---|------|------|------|
| 1 | **Signal 标准化** | ai-hedge-fund | 每个大师输出 `{方向, 确信度0-100, 推理}` JSON，Team Lead 加权综合 |
| 2 | **会计红旗扫描** | valueinvest | 11 项检测，致命红旗直接触发"不建议" |
| 3 | **估值方法自动推荐** | valueinvest | 银行业用 PB/RI，成长股用 DCF/PEG，不再一套方法套所有 |
| 4 | **强制反向论点** | TradingAgents | 每次分析生成 bull case + bear case，Team Lead 评判 |
| 5 | **三方法混合估值** | company-analyst | 50% DCF + 30% Comps + 20% DCF Bear 替代单点估值 |

### 9.2 中优先级（需要一定开发）

| # | 改进 | 来源 | 效果 |
|---|------|------|------|
| 6 | **多空辩论环节** | TradingAgents | 在 investment-team 中加入 bull vs bear 对抗辩论 |
| 7 | **估值 Range 替代单点** | valueinvest | 所有 DCF 输出 low/base/high 三值，诚实呈现不确定性 |
| 8 | **护城河量化评分** | valueinvest | 五维度 0-100 分量表 + 11 个具体信号 |
| 9 | **决策记忆/跟踪** | TradingAgents | 记录每次分析 → 定期回顾准确率，形成反馈闭环 |
| 10 | **Point-in-time 标注** | ai-hedge-fund | 所有数据显式标注时间戳，防止用未来数据分析过去 |

### 9.3 低优先级（长期探索）

| # | 改进 | 来源 | 效果 |
|---|------|------|------|
| 11 | **组合优化可视化** | Riskfolio-Lib | 有效前沿、BL 模型、HRP — 让 portfolio-review 有数学支撑 |
| 12 | **周期股专用路径** | valueinvest | 10 年平均数据替代当年数据做估值 |
| 13 | **MCP 服务化** | Value-Investing-Agent | 估值引擎做成 MCP Server，Claude Code 直接调用 |
| 14 | **中文金融 NLP** | FinBERT2 + FinNLP | 情绪分析、新闻摘要、财报关键句提取 |
| 15 | **回测框架** | vectorbt / Freqtrade | 对历史推荐做批量回测，跟踪推荐准确率 |

---

## 十、A 股/港股数据源速查

| 数据源 | 费用 | 注册 | 覆盖 | 特点 |
|--------|------|------|------|------|
| **AKShare** | 免费 | 不需要 | A股/期货/期权/外汇/加密货币 | 最全面，封装东方财富等平台 |
| **Tushare** | 部分免费 | 需要 Token | A股全市场 | 质量高，付费版无限制 |
| **Baostock** | 免费 | 不需要 | A股历史行情/财务 | 纯本地化，稳定，财务数据强 |
| **东方财富** | 免费 | 不需要 | A股实时 | AKShare 底层数据源之一 |
| **yfinance** | 免费 | 不需要 | 全球 | A股支持有限，港股/美股好 |

---

## 十一、核心设计原则汇总

从所有这些仓库中提炼的共性原则：

1. **界面统一 > 实现多样** — 所有组件共享接口。系统不知道也不关心谁在分析。
2. **观点形成 ≠ 仓位管理 ≠ 交易执行 ≠ 风险控制** — 四个独立层，关注点分离。
3. **LLM = 分析师，不是交易员** — LLM 输出观点和推理。代码管钱。
4. **失败必须先设计** — 数据不够怎么办？LLM 挂了怎么办？不是异常处理，是核心功能。
5. **诚实 > 聪明** — Point-in-time 诚实、不能偷看未来、不知道就说不知道。
6. **对抗性验证** — 一个人的分析容易偏，多方辩论暴露盲点。
7. **代码同一条路径** — 回测和实盘跑同一段代码。不允许"研究环境"和"生产环境"的分叉。
8. **可解释性留存** — 每个决策保留完整的推理链，不是黑箱输出。
9. **参数优化必须有防范过拟合机制** — 样本外验证、walk-forward、限制同时优化参数数量。
10. **工具绑定防止幻觉** — Agent 只能调用分配给它的数据工具，不能"知道一切"。
