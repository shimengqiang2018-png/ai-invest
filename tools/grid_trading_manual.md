# 网格交易管理工具 — 操作手册

**文件**: `tools/grid_trading.py`
**更新**: 2026-07-03
**环境**: Python 3.8+，零外部依赖

---

## 一、概述

独立脚本，覆盖网格交易的全部管理需求：
- 生成网格价格表
- 下单前行情快照（价格、IOPV、溢价率、盘口、成交额）
- 实时状态追踪（自动拉腾讯行情）
- 成交记录录入（动态基准价 + 多格跨越）
- 盈亏统计（FIFO 已实现 + 未实现）
- 风险监控（止损/仓位/亏损三道警戒线）
- 实时 watch 监控（价格接近触发时预警）

**不会做的事**：不连接东方财富账户、不自动下单、不存储密码。

---

## 二、快速开始

### 1. 编辑配置

打开脚本，找到 `CONFIGS` 字典，按实际持仓修改：

```python
CONFIGS = {
    "513180": {
        "name": "恒生科技ETF",
        "base_price": "0.569",          # 初始基准价
        "grid_spacing_pct": "2.5",      # 网格间距(%)
        "levels_above": 5,              # 上方卖出层数
        "levels_below": 5,              # 下方买入层数
        "shares_per_grid": 200,         # 每格股数
        "base_position": 5000,          # 底仓（锁定）
        "grid_position": 3000,          # 网格仓位
        "max_position": 15000,          # 最大持仓上限
        "stop_loss_price": "0.501",      # 止损/暂停网格价（第-5买入层）
    },
}
```

- `base_position` = 不参与网格的长持仓位，卖出不会碰到它
- `grid_position` = 跑网格的仓位
- `grid_spacing_pct` 设为 0 = 该 ETF 仅持有不网格（如 159920）

### 2. 全局风险参数（脚本内可调）

```python
RISK_TOTAL_LOSS_WARN = Decimal("10")   # 总亏损警告线(%)
RISK_TOTAL_LOSS_EXIT = Decimal("15")   # 总亏损清仓线(%)
```

---

## 三、命令详解

### `table` — 网格价格表

```bash
python3 tools/grid_trading.py table              # 所有ETF
python3 tools/grid_trading.py table 513180       # 指定ETF
```

输出包含：每层触发价、操作方向、交易股数、累计持仓、累计成本。
底部汇总：价格区间、仓位范围、最大资金占用、单格往返利润。

### `quote` — 下单前行情快照

```bash
python3 tools/grid_trading.py quote              # 所有已配置ETF
python3 tools/grid_trading.py quote 513180      # 指定ETF
```

显示：当前价、IOPV、估算溢价率、买卖五档、买卖价差、成交额、行情时间。

> 注意：估算溢价率使用公开行情源计算，公开行情可能延迟；下单前必须以东方财富APP的实时盘口、IOPV和成交回报为准。本工具不连接账户、不获取真实持仓、不自动下单。

### `status` — 网格状态

```bash
python3 tools/grid_trading.py status 513180          # 自动拉行情
python3 tools/grid_trading.py status 513180 0.570    # 手动指定价
```

显示：动态基准价、下一买卖触发价及距离百分比、底仓/网格仓/总持仓、浮盈。

> **动态基准价**：初始为配置里的 base_price，每次 `trigger` 录入成交后自动更新为执行价。如果基准价和初始值不同，说明网格已经跑过。

### `trigger` — 录入成交

```bash
python3 tools/grid_trading.py trigger 513180
```

交互流程：
1. 输入成交价
2. 输入日期（回车=今天）
3. 脚本自动判断买入/卖出方向
4. 自动检测是否跳空跨越了多层（倍数委托）
5. 确认后保存，动态基准价自动更新

成交记录保存在 `data/grid_triggers.json`，不会被脚本重启清除。

### `pnl` — 盈亏统计

```bash
python3 tools/grid_trading.py pnl 513180            # 自动拉行情
python3 tools/grid_trading.py pnl 513180 0.570      # 手动指定价
```

- **已实现盈亏**：FIFO 配对——买入依次入队，卖出依次消耗队首，配对成功的价差算已实现
- **未实现盈亏**：(当前价 - 平均成本) × 当前持仓
- 显示最近 5 笔成交记录

### `risk` — 风险检查

```bash
python3 tools/grid_trading.py risk 513180
```

四道检查：

| 检查项 | 触发条件 | 级别 |
|--------|----------|:--:|
| 价格跌破最低买入层 | 当前价 < 最远买入触发价 | 🟡 WARN |
| 仓位接近上限 | 持仓 > 90% max_position | 🟡 WARN |
| 距止损价不足 10% | (现价 - 止损价) / 现价 < 10% | 🟡 WARN |
| 触及止损价 | 现价 ≤ 止损价 | 🔴 CRIT |
| 总亏损达 10% | 浮亏 ≥ 10% | 🟡 WARN |
| 总亏损达 15% | 浮亏 ≥ 15% | 🔴 CRIT |

综合评分：正常 / 🟢低风险 / 🟡中风险 / 🔴高风险。

### `watch` — 实时监控

```bash
python3 tools/grid_trading.py watch 513180          # 30s 刷新
python3 tools/grid_trading.py watch 513180 10        # 10s 刷新
```

每 N 秒自动拉一次实时价，单行显示：

```
[14:59:30] 513180 ¥0.571  卖 ¥0.5832 (2.1%)  买 ¥0.5548 (2.8%)  │ 1.24%
```

三级预警（基于距触发价的距离占网格间距的比例）：

| 图标 | 距离 | 含义 |
|:--:|------|------|
| 正常 | >15% 间距 | 远离触发 |
| ⚡ | 7.5%~15% 间距 | 接近触发 |
| 🔥 | <7.5% 间距 | 即将触发 |
| 🔔 | 已穿越 | 已触发！提示检查条件单 |

按 Ctrl+C 退出。

---

## 四、日常操作流程

```
盘中:
  └─ 开 watch 监控 → 听到/看到 🔔 提醒
      └─ 去东方财富确认条件单状态
          └─ 若已成交 → 跑 trigger 录入

盘后:
  ├─ pnl → 看今天有没有赚钱
  └─ risk → 确认没有风险信号

每周:
  └─ 扫一眼 watch 确认条件单「运行中」

持仓变动时:
  └─ 编辑 CONFIGS 更新 base_position / grid_position
```

---

## 五、风控纪律（来自执行计划）

| 触发条件 | 操作 |
|----------|------|
| 513180 跌破 0.501（第 -5 格） | 暂停网格，等待判断 |
| 跌破下限再跌 5% | 止损 50% |
| 总亏损 ¥700（10%） | 全部暂停 |
| 总亏损 ¥1,050（15%） | 清仓离场 |

`risk` 命令会自动检查 10% 和 15% 两条线。

---

## 六、添加新 ETF

在 CONFIGS 字典里新增一条：

```python
"510300": {
    "name": "沪深300ETF",
    "base_price": "3.850",
    "grid_spacing_pct": "2.0",
    "levels_above": 5,
    "levels_below": 5,
    "shares_per_grid": 100,
    "base_position": 0,
    "grid_position": 1000,
    "max_position": 2000,
    "stop_loss_price": "3.00",
    "note": "Phase 3 入场",
},
```

仅持有不网格的 ETF 设置 `grid_spacing_pct: "0"`，`trigger` 命令会跳过它。

---

## 七、数据文件

- **成交记录**: `data/grid_triggers.json` — 自动创建，按 ETF 代码分类存储
- **配置**: 写入脚本 `CONFIGS` 字典 — 不依赖外部文件，手动编辑

---

## 八、注意事项

- 价格来源：腾讯行情 `qt.gtimg.cn`，延时约 3-5 秒，仅作参考
- 不保证与东方财富条件单的触发逻辑完全一致（委托价类型、滑点等因素）
- `watch` 的 🔔 提示仅作提醒，最终以东方财富 APP 成交记录为准
- 浮盈计算基于配置里的 base_price 和 trigger 记录的成本，与券商实际持仓盈亏可能有差异
- 所有货币计算使用 Decimal 精确计算，无浮点精度问题
