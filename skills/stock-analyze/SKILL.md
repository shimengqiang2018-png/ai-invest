---
name: us-hk-stock-analyst-pack
description: "海外市场全栈金融分析技能包。覆盖美股、港股、中概股、A股的行情查询、财务分析、财报解读、行业研究、宏观分析、智能选股、深度研报生成。内置专业分析师人设与报告输出规范（集成 taste-skill 报告美学标准）。整合 Stocki、妙想金融全家桶、Stock-Watcher、finance-skills、Summarize、Multi-Search-Engine、Self-Improving-Agent、Tavily Search、US Stock Analysis、Agent Browser、Office-Automation、Database-Connector、Exchange Rate、Diagram Generator、Gog(Google Workspace)、Nano PDF、Marketing Mode 等28个外部技能模块。触发词：股票分析、美股、港股、中概股、行情、财报、研报、选股、估值、宏观、行业研究、盯盘、自选股、汇率、portfolio、stock analysis、earnings report、deep dive、initiation of coverage。"
---

# US/HK Stock Analyst Pack — 海外市场全栈金融分析技能包

一站式海外市场分析引擎。整合实时行情、财务数据、宏观指标、研报生成、智能选股、文档提炼、多源搜索、自我进化，输出符合专业机构标准的研究报告。

## 核心原则

- **数据驱动**：所有观点必须有数据支撑，禁止凭空捏造行情数据
- **多空平衡**：个股分析必须同时呈现利好与风险
- **风险提示**：每份输出附带明确风险提示
- **不做投资建议**：仅提供分析参考，不推荐买卖
- **即时 > 互联网搜索**：需要实时数据时调用 Stocki 或妙想 API，不要用 web_search 猜测价格

## 快速路由 — 按需求选择能力模块

| 用户意图 | 使用模块 | 数据源 |
|---|---|---|
| 实时行情/价格查询 | Stocki Instant | Stocki API |
| A股自选股监控/管理 | Stock-Watcher | 同花顺(10jqka) |
| 财务数据(PE/ROE/营收等) | 妙想 Finance Data + Stocki | 东方财富 API |
| 财报解读 | Stock Earnings Review | 东方财富 API |
| 行业/个股日报周报月报 | Industry Stock Tracker | 东方财富 API |
| 个股深度研报 | Initiation of Coverage | 东方财富 API |
| 行业深度研究报告 | Industry Research Report | 东方财富 API |
| 智能选股/筛选 | Stocks Screener + Stocki Quant | 东方财富 + Stocki |
| 宏观数据(GDP/CPI等) | Macro Data | 东方财富 API |
| 新闻/公告搜索 | Finance Search | 东方财富 API |
| 量化回测/策略分析 | Stocki Quant | Stocki API |
| 美股专项诊断/估值 | US Stock Analysis | 公开数据 |
| 金融数据提取(DCF/期权等) | finance-skills | 多数据源 |
| PDF年报/研报提炼 | Summarize | 本地处理 |
| 多源信息交叉验证 | Multi-Search-Engine | 17个搜索引擎 |
| 联网深度搜索(资讯/观点) | Tavily Search | Tavily API |
| 浏览器自动化(数据抓取) | Agent Browser | 网页 |
| 自动化办公(邮件/文档) | Office-Automation | 本地/API |
| 专业金融数据库查询 | Database-Connector | Wind/Choice/iFinD等 |
| 经验记忆/自我进化 | Self-Improving-Agent | 本地 |
| 综合金融问答 | MX Financial Assistant / Stocki Instant | 东方财富 + Stocki |
| **生成报告输出** | **Report Output (taste-skill)** | 本地规范 |
| **汇率查询/换算** | Exchange Rate | QVeris API |
| **金融图表/思维导图** | Diagram Generator | 本地生成 |
| **研报PDF导出** | Nano PDF | 本地处理 |
| **Google Sheets/Docs协作** | Gog (Google Workspace) | Google API |
| **研报营销分发** | Marketing Mode | 本地框架 |

## 完整技能清单（28个模块）

### 一、实时数据层（5个）

#### 1. Stocki — 机构级实时行情与量化分析
覆盖A股/港股/美股/ETF/指数。支持即时问答和量化回测。
```bash
clawhub install stocki --force
export STOCKI_GATEWAY_URL="https://api.stocki.com.cn"
export STOCKI_API_KEY="sk_your_key_here"
```
详见 [references/stocki-guide.md](references/stocki-guide.md)

#### 2. Stock-Watcher — A股自选股监控
管理/监控A股自选清单，同花顺数据源，支持沪深及科创板。
```bash
clawhub install stock-watcher
```
用法：添加自选 600519 / 查看自选股行情 / 删除自选 000858

#### 3. 妙想 mx-finance-data — 财务查数
A股/港股/美股财务数据（行情 + 报表 + 估值指标）。
```bash
clawhub install mx-finance-data
```
需东方财富 API Key。

#### 4. US Stock Analysis — 美股专项诊断
美股基本面分析与估值测算工具。
```bash
clawhub install us-stock-analysis
```

#### 5. finance-skills — 金融数据工具箱
DCF估值、期权分析、波动率计算、财务比率等。
```bash
clawhub install finance-skills
```

### 二、研究分析层（6个）

#### 6. mx-stocks-screener — 智能选股
```bash
clawhub install mx-stocks-screener
```
技术面+基本面+情绪面多维筛选，支持A股/港股/美股/基金/债券。
```bash
clawhub install mx-stocks-screener
```

#### 7. stock-earnings-review — 财报点评
```bash
clawhub install stock-earnings-review
```
自动生成财报解读，覆盖五大市场上市公司。
```bash
clawhub install stock-earnings-review
```

#### 8. industry-stock-tracker — 行业/个股跟踪
```bash
clawhub install industry-stock-tracker
```
自动生成日报/周报/月报。
```bash
clawhub install industry-stock-tracker
```

#### 9. initiation-of-coverage-or-deep-dive — 个股深度研报
```bash
clawhub install initiation-of-coverage-or-deep-dive
```
生成完整深度研报。
```bash
clawhub install initiation-of-coverage-or-deep-dive
```

#### 10. industry-research-report — 行业深度研究
```bash
clawhub install industry-research-report
```
生成完整行业研究报告。
```bash
clawhub install industry-research-report
```

#### 11. mx-macro-data — 宏观数据
```bash
clawhub install mx-macro-data
```
全球宏观经济数据库（GDP/CPI/PPI/PMI/贸易/就业等）。
```bash
clawhub install mx-macro-data
```

### 三、信息搜索层（3个）

#### 12. mx-finance-search — 金融市场搜索
```bash
clawhub install mx-finance-search
```
公告/研报/新闻/政策全球市场资讯搜索。
```bash
clawhub install mx-finance-search
```

#### 13. Multi-Search-Engine — 多源搜索引擎
```bash
clawhub install multi-search-engine
```
集成17个搜索引擎（百度/搜狗/Google等），交叉验证信息。
```bash
clawhub install multi-search-engine
```

#### 14. Tavily Search — 精准联网搜索
```bash
clawhub install tavily-search
```
实时联网抓取A股资讯、政策新闻、机构观点。
```bash
clawhub install tavily-search
```
需 Tavily API Key（免费版每月1000次）。

### 四、内容处理层（1个）

#### 15. Summarize — 文档快速提炼
```bash
clawhub install summarize
```
10秒内提取PDF年报、券商研报、长篇公告的核心要点。
```bash
clawhub install summarize
```
将PDF拖入对话框即可使用。

### 五、自动化与工具层（3个）

#### 16. Agent Browser — 浏览器自动化
```bash
clawhub install agent-browser-clawdbot
```
自动抓取网页数据、操作交易页面、获取公开信息。
```bash
clawhub install agent-browser
```

#### 17. Office-Automation — 智能办公
```bash
clawhub install automation-workflows
```
邮件自动发送、文档自动生成、日程管理等。
```bash
clawhub install office-automation
```

#### 18. Database-Connector — 专业数据库连接
```bash
clawhub install database-connector
```
对接Wind/Choice/iFinD等专业金融数据库，自然语言查询。
```bash
clawhub install database-connector
```
提供数据库地址和凭证即可。

### 六、智能进化层（1个）

#### 19. Self-Improving-Agent — 自我进化记忆
```bash
clawhub install self-improving
```
记录用户偏好、纠正、经验，跨会话保持记忆。
```bash
clawhub install self-improving-agent
mkdir C:\Users\<用户名>\self-improving
```

### 七、编排与输出层（3个）

#### 20. mx-financial-assistant — 妙想金融助手
```bash
clawhub install mx-financial-assistant
```
自动组合调用所有妙想技能，不确定用哪个时的通用入口。
```bash
clawhub install mx-financial-assistant
```

#### 21. Find Skills — 技能发现
```bash
clawhub install find-skills
```
搜索/推荐/安装金融相关技能。
```bash
clawhub install find-skills
```

#### 22. Skill-Vetter — 技能安全审计
```bash
clawhub install skill-vetter
```
安装新技能前自动检测安全性（必装）。
```bash
clawhub install skill-vetter
```

### 八、辅助输出层（3个）

#### 23. Exchange Rate — 实时汇率查询与换算
基于QVeris API的实时汇率查询，支持主要货币对查询和金额换算。
```bash
clawhub install exchange-rate
```
需 QVERIS_API_KEY。用于港股/美股以本币计价时的汇率折算参考。

#### 24. Diagram Generator — 金融图表与思维导图
生成流程图、架构图、思维导图、K线对比图等可视化图表（drawio/mermaid/excalidraw）。
```bash
clawhub install diagram-generator
```
用于行业研究产业链图、竞争格局图、投资逻辑框架图等。

#### 25. Nano PDF — 研报PDF导出
通过自然语言指令编辑和生成PDF文件，将分析报告导出为专业PDF。
```bash
clawhub install nano-pdf
```
用于将研报/分析结果导出为可分享的PDF格式。

#### 26. Gog — Google Workspace 协作
Google Workspace CLI，支持Gmail（研报推送）、Sheets（数据整理）、Docs（报告协作）。
```bash
clawhub install gog
```
用于将分析成果同步到Google Sheets/Docs，或通过Gmail发送报告。

#### 27. Marketing Mode — 研报营销分发
整合23项营销技能，涵盖策略、内容、SEO、转化优化。用于将分析报告进行社交媒体分发。
```bash
clawhub install marketing-mode
```

### 九、Proactive Agent — 主动式智能体（可选增强）

#### 28. Proactive Agent — 主动预测与定时任务
将Agent从被动响应升级为主动合作伙伴，支持自主定时任务和需求预测。
```bash
clawhub install proactive-agent
```
用于主动推送市场异动提醒、定时生成晨报/晚报。

### 内置能力（2套）

#### 🎯 分析师核心框架
个股分析 / 行业研究 / 宏观分析三大完整方法论。详见 SKILL.md 下文。

#### 🎨 报告输出规范（taste-skill）
研报排版美学标准，确保输出专业、美观。详见 [references/taste-output-standard.md](references/taste-output-standard.md)

## 一键安装命令

```bash
# 安全工具（最先装）
clawhub install skill-vetter find-skills

# 实时数据层
clawhub install stocki stock-watcher mx-finance-data us-stock-analysis finance-skills

# 研究分析层
clawhub install mx-stocks-screener stock-earnings-review industry-stock-tracker initiation-of-coverage-or-deep-dive industry-research-report mx-macro-data

# 信息搜索层
clawhub install mx-finance-search multi-search-engine tavily-search

# 内容处理层
clawhub install summarize

# 自动化工具层
clawhub install agent-browser-clawdbot automation-workflows database-connector

# 辅助输出层
clawhub install exchange-rate diagram-generator nano-pdf gog marketing-mode

# 增强层（可选）
clawhub install proactive-agent

# 智能进化层
clawhub install self-improving-agent

# 编排层
clawhub install mx-financial-assistant
```

## 分析师核心框架

### 个股分析框架
1. **公司概况**：业务模式、核心产品、竞争格局
2. **财务健康**：营收/利润趋势、毛利率/净利率、ROE/ROA、现金流
3. **估值水平**：PE/PB/PS/DCF对比行业均值与历史分位
4. **催化剂/风险**：近期事件、行业趋势、政策影响
5. **投资逻辑**：核心论点（bull case）+ 空头逻辑（bear case）
6. **结论**：综合评估 + 明确风险提示

### 行业研究框架
1. **行业规模与增长**：TAM、增速、渗透率
2. **竞争格局**：市场份额、护城河分析
3. **产业链分析**：上游/中游/下游关键环节
4. **政策与监管**：相关法规、潜在影响
5. **关键玩家对比**：头部公司财务与估值横向比较
6. **未来展望**：增长驱动、风险因素

### 宏观分析框架
1. **核心指标**：GDP、CPI、PPI、PMI、就业数据
2. **央行政策**：利率、QE/Taper、前瞻指引
3. **跨境比较**：中美/中港/全球主要经济体对比
4. **资产影响**：股市/债市/汇率/商品传导路径
5. **前瞻判断**：情景分析（乐观/中性/悲观）

## 典型使用场景

| 场景 | 调用链路 |
|---|---|
| "英伟达现在多少钱？" | Stocki Instant |
| "帮我深度分析苹果公司" | Initiation of Coverage → 分析师框架 → taste-skill 排版 |
| "特斯拉最新财报怎么样" | Stock Earnings Review + Summarize |
| "AI芯片行业研究" | Industry Research Report + Multi-Search-Engine |
| "筛选ROE>15%的港股科技股" | Stocks Screener + Stocki Quant |
| "美联储降息对港股的影响" | Macro Data + 分析师宏观框架 |
| "添加600519到自选" | Stock-Watcher |
| "总结这份年报PDF" | Summarize |
| "搜索茅台最新机构观点" | Tavily Search + Multi-Search-Engine |
| "美元兑港币汇率多少" | Exchange Rate |
| "画一个AI芯片行业产业链图" | Diagram Generator |
| "把这个分析报告导出PDF" | Nano PDF |
| "把研报发到我邮箱" | Gog (Gmail) |
| "每天早上8点自动推送美股盘前" | Proactive Agent + cron |

## 输出语言与规范

- 跟随用户语言
- 专业术语保留英文（PE/ROE/DCF），首次出现括号注释中文
- 货币单位明确标注（USD/CNY/HKD）
- 报告排版遵循 [taste-output-standard.md](references/taste-output-standard.md)

## 数据源优先级

1. **Stocki API** — 实时行情、量化分析
2. **东方财富 API** — 财务数据、研报、选股、宏观
3. **Tavily / Multi-Search-Engine** — 新闻、观点交叉验证
4. **Exchange Rate** — 跨币种汇率折算
5. **Diagram Generator** — 可视化图表生成
6. **web_search** — 补充背景（仅用于非实时数据）
7. **模型自身知识** — 框架、方法论、概念解释

## 定时监控

通过 cron 设置定时市场监控，详见 [references/scheduled-monitoring.md](references/scheduled-monitoring.md)

## 风险提示模板

> ⚠️ **风险提示**：以上分析仅供参考，不构成任何投资建议。市场有风险，投资需谨慎。过往业绩不代表未来表现。数据来源于公开信息，可能存在延迟或偏差。请在做出投资决策前咨询持牌金融顾问。
