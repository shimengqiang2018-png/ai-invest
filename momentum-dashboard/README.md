# 动量轮动策略台（Momentum Dashboard）

把仓库里动量轮动策略涉及的脚本（信号扫描、策略监测、回测、审计、选品）
封装成一套带前端页面的本地 Web 应用。零第三方依赖（Python 标准库 + 原生 JS），
离线可用（数据来自 `data/cache` 缓存，网络失败自动回退）。

## 快速开始

```bash
python3 momentum-dashboard/server.py
```

浏览器打开 http://127.0.0.1:8765

可选参数：

```bash
python3 momentum-dashboard/server.py --port 9000 --host 127.0.0.1
python3 momentum-dashboard/server.py --online   # 页面加载时也允许联网刷新数据
```

服务日志同时输出到终端和 `momentum-dashboard/server-YYYYMMDD.log`（按天命名，
自动保留最近 7 天并删除更早文件），可用
`tail -f momentum-dashboard/server-$(date +%Y%m%d).log` 观察每次请求的处理情况。

### 日志内容

每类关键事件都有独立标记（`[INFO]` / `[WARN]` / `[ERROR]`）：

| 标记 | 含义 | 示例 |
|------|------|------|
| `[BIZ]` | 业务摘要：信号/轮动/组合/回测/审计/选品/持仓/止损 | `[BIZ] SIGNAL >> 目标 159920 恒生ETF strength=strong` |
| `START/END` | 每次 API 请求的开始与结束（含耗时） | `END /api/overview [cache] 3ms` |
| `CACHE` | 缓存命中/过期/强制重算/回退旧缓存 | `CACHE 命中 [signals\|518880,...] age=4s` |
| `RUN` | 脚本执行开始/完成/失败（含耗时与输出大小） | `RUN 完成 strategy_monitor.py --json exit=0 (20.4s, stdout=12836B)` |
| `DATA` | K 线读取来源（本地缓存或回退加载） | `DATA 518880 使用本地缓存 sina 2000 根` |
| `FILE` | 本地数据文件读取（持仓/枚举/全市场） | `FILE positions 快照 2026-07-29 (8 只持仓)` |
| `TRACE` | 浏览器前端启动轨迹（用于定位页面卡点） | `TRACE overview rendered` |
| `400/404/500` | 参数错误/未找到/服务异常（含堆栈） | `500 /api/x: ...` |

## 数据模式（为什么不会卡）

- **默认离线缓存模式**：页面加载时只读 `data/cache` 本地缓存（秒开）。
  只有点「刷新数据」按钮或「扫描/回测/审计/选品」按钮时才允许脚本联网更新 K 线。
- **联网模式**：`--online` 启动后，页面加载也会尝试联网刷新数据。

> 说明：`data/cache` 中超过 3 天的 K 线属于“陈旧缓存”。如果每次加载都联网刷新，
> 在网络慢或接口限流时会一次等几分钟，页面看起来像卡住。
> 离线模式避免了这一点；需要最新行情时手动点「刷新数据」即可。

底层数据工具支持 `ETF_DATA_OFFLINE=1` 环境变量（只读缓存、不联网），
仪表盘在调用脚本时自动注入，也可在命令行直接使用：

```bash
ETF_DATA_OFFLINE=1 python3 tools/momentum_signal.py --pool 518880,513100,159915,159920 --json
```

## 页面功能

| 页面 | 内容 | 后端数据源 |
|------|------|-----------|
| 总览 | 动量状态、RSRS 目标、风险指标、网格分组、操作建议、信号 Top4、组合速览 | `tools/strategy_monitor.py --json` + `data/positions_*.json` |
| 信号扫描 | 五条件明细（RSRS/MA20/波动/量能/RSI）、信号强度、轮动决策、点击行查看 K 线（MA20/MA60） | `tools/momentum_signal.py --json` |
| 网格 | 网格标的趋势分析（评分/BB/MA/判断）、买卖触发价 vs 实时价、触发记录、网格参数自动寻优 | `tools/grid_trading.py` + MySQL `grid_triggers` 表 |
| 回测分析 | 收益/风险卡片、净值曲线、枚举组合 Top10（v3.0 25日/MA20 口径）、最近交易 | `tools/momentum_etf_backtest.py --json` + `data/enum_backtest_veteran_c3_25d.json` |
| 选品池 | 全市场动量适配评分（可筛选排序）、四维选品推荐、组合相关性矩阵 | `data/etf_backtest_results.json` + `tools/etf_screener.py --json` |
| 持仓 | 账户汇总、持仓明细、策略归属分组 | MySQL `holdings_current` / `account_summary_current` 表 |
| 风险审计 | 日频风险指标、RSRS 因子 IC/IR、压力测试情景、VaR 解读 | `tools/strategy_audit.py --json` |

## 多厂商模型交互

系统内置模型调用层（`models.py`，零依赖），支持两类协议：

- **OpenAI 兼容**：DeepSeek / 通义千问（DashScope）/ 智谱 GLM / Moonshot / Ollama 本地等
- **Anthropic Messages API**：Claude

厂商配置在 [model_config.json](model_config.json)，API Key 一律从环境变量读取（不落盘）：

```bash
export DASHSCOPE_API_KEY=sk-xxx      # 通义千问视觉
export ZHIPU_API_KEY=xxx             # 智谱 GLM-4V
export ANTHROPIC_API_KEY=sk-ant-xxx  # Claude
```

`GET /api/models` 列出厂商（标注是否已配置 Key、是否支持视觉）；`POST /api/model/chat`
提供通用文本对话能力。

## 交易时段定时任务

服务内置定时调度（`scheduler.py`，`threading.Timer` 链式实现）：

- **调度规则**：每个交易日（周一至周五）09:07-11:57、13:07-14:27 每 10 分钟一次；
  14:30-15:27 每 5 分钟一次（用当天实时价做盘中信号预测）；
- **任务内容**：刷新信号（联网运行 `strategy_monitor.py` 并更新信号页缓存）
  + 发送监测邮件（复用 `tools/monitor_alert.py` 的 HTML 模板与 QQ SMTP）；
- **默认启用**，可用 `--no-scheduler` 或环境变量 `MOMENTUM_SCHEDULER=0` 关闭；
- **接口**：`GET /api/scheduler` 查看状态与下次调度时间；
  `POST /api/scheduler/run` 手动立即执行一次。

## 数据存储（MySQL + 分层抽象）

系统数据统一写入 **MySQL**（本地 `ai_invest` 库，MySQL 8.4，已随服务启动）。存储按
两层抽象，方便后续切换底层实现：

- **`db.py` 数据访问层**：唯一包含 SQL / 直接操作数据库的代码层，对外只暴露函数
  接口。仅支持 MySQL（SQLAlchemy + PyMySQL），连接参数读取 `.env` 的 `DB_*`；
  表结构由 `schema_mysql.sql` 管理，代码不自动建表。
- **`cache.py` 缓存抽象层**：只暴露 `get / set / cached / delete_expired /
  flush` 与后端切换 `configure()`。默认后端 `db`（走 `db.py` 的 cache 表，即
  MySQL 持久化），另有 `memory` 仅用于单元测试。

四张表：

| 表 | 内容 |
|----|------|
| `cache` | API 结果缓存（带 TTL） |
| `positions_snapshots` | 持仓快照（每次 AI 解析/确认更新写入，含完整 payload） |
| `parse_history` | AI 持仓解析历史（解析更新时间/来源/持仓数/交易数） |
| `api_logs` | 业务日志（与日志文件同步镜像，自动清理 7 天前数据，可查询） |
| `signal_history` | 信号扫描/轮动历史（每天每池每条，可复盘当时为何换仓） |
| `grid_triggers` | 网格触发记录（完整保留每笔买入/卖出，替代被覆盖的 JSON 文件） |
| `backtest_results` | 回测/寻优结果（backtest/enum/screener/grid_opt，按参数复用避免重算） |
| `scheduler_runs` | 定时任务执行历史（调度/手动，含邮件状态，重启不丢） |

每张表均包含：`id BIGINT` 主键（自增）、`created_at` 创建时间、
`updated_at` 更新时间，且所有字段带中文注释。建表 DDL 见
[`schema_mysql.sql`](schema_mysql.sql)：

```bash
# 手动建表（ai_invest 库需先创建）
mysql -u invest -p ai_invest < momentum-dashboard/schema_mysql.sql
```

代码不再自动建表、迁移或种子化默认数据；表结构与初始数据（如
`momentum_pools` 动量池、`grid_configs` 网格配置）需按上述 DDL 建表后人工维护。

### 可视化

侧栏「数据存储」页提供数据库可视化：库统计卡片（行数 / 大小 / 最新快照日期）、
四张表浏览（分页、行内容 JSON 折叠）、日志浏览（`INFO/WARN/ERROR` 级别筛选）。
对应接口：`GET /api/db/stats`、`GET /api/db/tables`、
`GET /api/db/table?name=cache&limit=100&offset=0`（白名单表，分页读取）、
`GET /api/logs?limit=100&level=INFO`。

持仓全部存 MySQL（`holdings_current` / `positions_snapshots`），不再使用本地 JSON 文件。

### 网格触发录入

网格页「触发记录录入」支持两种方式，录入后写 MySQL `grid_triggers` 表
（`data/grid_triggers.json` 已废弃，触发记录一律从数据库读取）：

- **手动录入**：选择标的/日期/动作/价格/数量，基准价可自动取当前配置，重复记录自动去重；
- **截图识别**：上传券商成交记录截图，视觉模型直接看图，或本地 OCR + 文本模型
  （如 DeepSeek）结构化，识别结果三级核验（通过 / ⚠核实 / ✗错误）后可编辑修正，
  支持**增删行**后确认批量入库；识别时会与历史触发记录**比对重复**（同
  代码+日期+动作+价格+数量标记「重复」），确认时自动去重。

识别过程日志（通道 / OCR 文本片段 / 模型返回 / 逐条核验结果）会打印到
`server-YYYYMMDD.log` 与 `api_logs`，便于排查识别偏差。

## 持仓图片解析（第一个应用场景）

持仓页支持上传券商/银行 App 截图，由视觉模型解析出**持仓明细 + 交易记录**，
逐项核验后确认写入系统。解析口径完全遵循 `/fund-screenshot-ocr` skill：

1. **解析**：支持两条通道——
   - **视觉模型**（qwen-vl-max / glm-4v-plus / Claude 等）直接看图；
   - **本地 OCR + 文本模型**（如 DeepSeek）：tesseract 本地识别（放大2倍+增强对比度+锐化）
     后由文本模型按 skill 规范结构化，适合只有 DeepSeek Key 的场景；
   输出结构化
   `account_summary / holdings / trades`，并按 skill 的平台决策树标注来源
   （tiantianfund / eastmoney_position / eastmoney_transaction / cmb_list / cmb_detail）；
2. **核验**（三级：通过 / ⚠ 核实 / ✗ 错误）：代码合法性、市值≈股数×现价、
   盈亏≈(现价-成本)×股数、盈亏率勾稽、证券市值与持仓合计、总资产与市值+资金、
   **成交记录反推持仓数量/成本交叉校验**（偏差>3% 标记核实），并与腾讯实时行情比对；
3. **更新**：存在硬错误时拒绝写入；警告项由用户确认后仍可写入；
   写入 MySQL（`holdings_current` + `positions_snapshots`），同时按 skill 规范生成
   `reports/ETF/持仓数据-{YYYYMMDD}.xlsx` 和组合文件 `reports/portfolio-latest.md`。

接口：`POST /api/positions/parse`（上传图片 JSON）、`POST /api/positions/update`（写入）。

### 配置 DeepSeek（文本模型走本地 OCR）

```bash
# 写入项目根目录 .env（已 gitignore，不会入库）
echo "DEEPSEEK_API_KEY=sk-xxx" >> .env
```

服务启动时自动加载 `.env`。`/api/models` 中 `deepseek configured=true` 后，
持仓页解析模型选择 DeepSeek 即走「本地 OCR + 模型」通道。

## API

全部为 `GET`，返回 `{ok, data, cached, server_time}`。

| 接口 | 参数 | 缓存 |
|------|------|------|
| `/api/pools` | — | 无 |
| `/api/signals` | `pool=recommended\|full\|回测预设(best4 等)\|自定义代码`，`momentum=25`（RSRS 周期 5-120），`refresh=1` | 10 分钟 |
| `/api/overview` | `refresh=1` | 15 分钟 |
| `/api/backtest` | `preset=best4`（或 `pool=信号池名/自定义代码`），`momentum=25`，`freq=biweekly`，`start=full\|1y\|3y\|5y\|日期`，`commission=0.00025`（费率），`min_commission=0\|5`（0=免5）；无效参数返回 400 | 2 小时 |
| `/api/audit` | `refresh=1` | 2 小时 |
| `/api/screener` | `refresh=1` | 24 小时 |
| `/api/positions` | — | 无 |
| `/api/kline` | `code=518880`，`count=300`；无数据代码返回 400 | 1 小时 |
| `/api/stoploss` | `code=159920`，`entry=1.502` | 5 分钟 |
| `/api/enum` | — | 无 |
| `/api/etf-scan` | `top=100`，`category=跨境ETF` | 无 |
| `/api/models` | 厂商列表（GET） | 无 |
| `/api/model/chat` | `{provider, messages}`（POST） | 无 |
| `/api/positions/parse` | `{provider, images:[{mime,data_b64}]}`（POST，1-6 张） | 无 |
| `/api/positions/update` | `{verified, source}`（POST，核验通过后写入） | 无 |
| `/api/grid` | 网格标的分析 + 分组 + 触发记录，`refresh=1` | 5 分钟 |
| `/api/grid/optimize` | `codes=512010,512880`（留空=全部），`capital`，`start`；内置候选（间距 1-5%、层数 3-8、每格金额 200-5000 元折算股数）自动寻优，多标的多线程并行 | 每组 24 小时 |
| `/api/grid/triggers` | `{code,date,action,price,shares,base_price_before?,base_price_after?}`（POST）手动录入网格触发，写 MySQL `grid_triggers` 表 | 无 |
| `/api/grid/triggers/parse` | `{provider,images:[{mime,data_b64}]}`（POST）截图识别成交记录（视觉模型或 OCR+模型），返回待核验记录 | 无 |
| `/api/grid/triggers/confirm` | `{records:[已核验记录]}`（POST）批量确认录入，重复自动去重 | 无 |
| `/api/db/stats` | 库统计（各表行数 / 大小 / 最新快照） | 无 |
| `/api/db/tables` | 四张业务表清单（名称 / 行数 / 列名） | 无 |
| `/api/db/table` | `name=表名&limit=100&offset=0` 分页读表（白名单） | 无 |
| `/api/logs` | `limit=100&level=INFO/WARN/ERROR` 查询业务日志 | 无 |

`refresh=1` 强制重跑脚本；重跑失败时自动回退到旧缓存并标记 `stale`。

## 目录结构

```text
momentum-dashboard/
├── server.py        # 本地 API 服务（封装工具脚本；数据走 db/cache 层）
├── bizlog.py        # 业务日志 / 子进程执行器（server.log 按天轮转）
├── market_tools.py  # 行情 / 技术面工具（K 线、实时报价、指标、网格评分）
├── db.py            # 数据访问层（唯一含 SQL；SQLAlchemy + MySQL）
├── cache.py         # 缓存抽象层（默认 db 后端=MySQL 持久化，可切换 Redis 等）
├── services/        # 业务服务层（signal / position / grid）
│   ├── signal_service.py     # 信号池配置、盘中预测、定时任务
│   ├── position_service.py   # 持仓快照、策略归属
│   └── grid_service.py       # 网格配置、触发分析、持仓构建
├── schema_mysql.sql # MySQL 建表 DDL（id BIGINT 主键 + 创建/更新时间 + 字段注释）
├── models.py        # 多厂商模型客户端（OpenAI 兼容 / Anthropic）
├── scheduler.py     # 交易时段定时刷新（threading.Timer 链式）
├── positions_parser.py  # 持仓截图 AI 解析 + 三级核验 + 落库
├── holdings_strategy.json  # 持仓策略归属配置（网格/动量双策略口径）
├── static/
│   ├── index.html   # 单页仪表盘
│   ├── app.js       # 页面逻辑 + Canvas 图表（零依赖）
│   └── style.css    # 深色金融风格
```

## 说明

- 服务只监听 `127.0.0.1`，不对外暴露；无任何写操作接口。
- 页面展示的数据全部来自仓库内既有脚本的结构化输出，未重写策略逻辑。
- 首次点「刷新数据」会联网更新 K 线，耗时 1-3 分钟属正常，页面会显示进度。
- 本项目用于学习与研究，不构成投资建议。
