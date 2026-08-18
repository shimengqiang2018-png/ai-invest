-- ============================================================
-- 动量轮动策略仪表盘 - MySQL 表结构 DDL（唯一建表入口）
-- 目标库: ai_invest  (utf8mb4 / utf8mb4_unicode_ci)
-- 说明: 代码不再自动建表/迁移/种子化，新环境先执行本文件，再启动服务。
-- 执行:  mysql -u invest -p ai_invest < schema_mysql.sql
-- 或:    mysql -u root -p
--        mysql> USE ai_invest;
--        mysql> SOURCE /path/to/schema_mysql.sql;
-- ============================================================

CREATE TABLE IF NOT EXISTS cache (
  id         BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  cache_key  VARCHAR(255) NOT NULL                COMMENT '缓存键',
  payload    MEDIUMTEXT   NOT NULL                COMMENT '缓存内容(JSON)',
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_cache_key (cache_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='API结果缓存';

CREATE TABLE IF NOT EXISTS positions_snapshots (
  id         BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  date       VARCHAR(32) DEFAULT NULL            COMMENT '快照日期',
  source     VARCHAR(64) DEFAULT NULL            COMMENT '数据来源',
  payload    MEDIUMTEXT  NOT NULL                COMMENT '完整持仓快照(JSON)',
  created_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  KEY idx_snapshots_date (date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='持仓快照';

CREATE TABLE IF NOT EXISTS holdings_current (
  id            BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  code          VARCHAR(16)  NOT NULL                COMMENT '代码',
  name          VARCHAR(64)  DEFAULT NULL            COMMENT '名称',
  shares        INT          NOT NULL DEFAULT 0      COMMENT '总持仓股数',
  available     INT          DEFAULT NULL            COMMENT '可用股数',
  price         DECIMAL(12,3) DEFAULT NULL           COMMENT '现价(元)',
  cost          DECIMAL(12,3) DEFAULT NULL           COMMENT '成本(元)',
  market_value  DECIMAL(16,3) DEFAULT NULL           COMMENT '市值(元)',
  pnl           DECIMAL(16,3) DEFAULT NULL           COMMENT '浮动盈亏(元)',
  pnl_pct       DECIMAL(8,3)  DEFAULT NULL           COMMENT '盈亏率(%%)',
  daily_pnl     DECIMAL(16,3) DEFAULT NULL           COMMENT '当日盈亏(元)',
  strategy      VARCHAR(32)  DEFAULT NULL            COMMENT '网格/动量/共用/底仓/现金/其他',
  bucket        VARCHAR(32)  DEFAULT NULL            COMMENT '子账户',
  base_shares   INT          NOT NULL DEFAULT 0      COMMENT '底仓股数',
  source        VARCHAR(32)  DEFAULT NULL            COMMENT '来源',
  verified      TINYINT(1)   NOT NULL DEFAULT 0      COMMENT '是否已核实',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_holdings_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='当前持仓';

CREATE TABLE IF NOT EXISTS account_summary_current (
  id               BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键(单行)',
  total_assets     DECIMAL(16,3) DEFAULT NULL COMMENT '总资产(元)',
  securities_value DECIMAL(16,3) DEFAULT NULL COMMENT '证券市值(元)',
  available_cash   DECIMAL(16,3) DEFAULT NULL COMMENT '可用资金(元)',
  withdrawable     DECIMAL(16,3) DEFAULT NULL COMMENT '可取资金(元)',
  position_ratio   DECIMAL(8,3)  DEFAULT NULL COMMENT '仓位(%%)',
  daily_pnl        DECIMAL(16,3) DEFAULT NULL COMMENT '当日盈亏(元)',
  total_pnl        DECIMAL(16,3) DEFAULT NULL COMMENT '总盈亏(元)',
  source           VARCHAR(64)   DEFAULT NULL COMMENT '来源',
  created_at       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='当前账户汇总';

CREATE TABLE IF NOT EXISTS grid_configs (
  id               BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  code             VARCHAR(16)  NOT NULL                COMMENT '证券代码',
  name             VARCHAR(64)  DEFAULT NULL            COMMENT '证券名称',
  strategy_type    VARCHAR(32)  NOT NULL DEFAULT '网格交易' COMMENT '策略类型',
  base_price       DECIMAL(12,3) DEFAULT NULL           COMMENT '基准价(元)',
  spacing_up_pct   DECIMAL(8,3)  DEFAULT NULL           COMMENT '上涨卖出间距(%%)',
  spacing_down_pct DECIMAL(8,3)  DEFAULT NULL           COMMENT '下跌买入间距(%%)',
  price_low        DECIMAL(12,3) DEFAULT NULL           COMMENT '价格区间下限(元)',
  price_high       DECIMAL(12,3) DEFAULT NULL           COMMENT '价格区间上限(元)',
  order_type_sell  VARCHAR(64)  DEFAULT NULL            COMMENT '卖出委托方式(如:限价即时买一价卖出)',
  order_type_buy   VARCHAR(64)  DEFAULT NULL            COMMENT '买入委托方式(如:限价即时卖一价买入)',
  shares_per_grid  INT          DEFAULT NULL            COMMENT '委托数量(份/每笔)',
  base_position    INT          DEFAULT NULL            COMMENT '底仓(份)/持仓区间下限',
  max_position     INT          DEFAULT NULL            COMMENT '最大持仓(份)/持仓区间上限',
  levels_above     INT          DEFAULT NULL            COMMENT '上方卖出层数',
  levels_below     INT          DEFAULT NULL            COMMENT '下方买入层数',
  status           VARCHAR(16)  NOT NULL DEFAULT 'active' COMMENT '状态(active/paused/closed)',
  note             VARCHAR(255) DEFAULT NULL            COMMENT '备注',
  source           VARCHAR(32)  DEFAULT NULL            COMMENT '来源',
  created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_grid_config_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='网格交易配置';

CREATE TABLE IF NOT EXISTS momentum_pools (
  id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  pool_key        VARCHAR(64)  NOT NULL                COMMENT '池标识(recommended/full/回测预设名)',
  pool_type       VARCHAR(16)  NOT NULL DEFAULT 'signal' COMMENT '池类型(signal=信号池/backtest=回测预设)',
  description     VARCHAR(255) DEFAULT NULL            COMMENT '池描述',
  codes           TEXT         NOT NULL                COMMENT '标的代码(逗号分隔)',
  defensive_code  VARCHAR(16)  DEFAULT NULL            COMMENT '防御资产代码(无信号通过时切换)',
  is_recommended  TINYINT      NOT NULL DEFAULT 0      COMMENT '是否默认推荐池(扫描/盘中预测使用)',
  sort_order      INT          NOT NULL DEFAULT 0      COMMENT '展示排序(小在前)',
  enabled         TINYINT      NOT NULL DEFAULT 1      COMMENT '是否启用(0=停用不参与扫描)',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_momentum_pool_key (pool_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='动量信号/回测池配置(信号扫描与盘中预测标的来源)';

CREATE TABLE IF NOT EXISTS parse_history (
  id             BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  parse_updated_at VARCHAR(32) DEFAULT NULL          COMMENT '解析更新时间',
  source         VARCHAR(64) DEFAULT NULL            COMMENT '解析来源',
  holdings_count INT         DEFAULT NULL            COMMENT '持仓数量',
  trades_count   INT         DEFAULT NULL            COMMENT '交易笔数',
  payload        MEDIUMTEXT  DEFAULT NULL            COMMENT '解析结果(JSON)',
  created_at     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI持仓解析历史';

CREATE TABLE IF NOT EXISTS api_logs (
  id         BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  ts         VARCHAR(32) NOT NULL                COMMENT '日志时间(ISO8601)',
  level      VARCHAR(16) DEFAULT NULL            COMMENT '日志级别(INFO/WARN/ERROR)',
  message    VARCHAR(2000) DEFAULT NULL          COMMENT '日志内容',
  created_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  KEY idx_logs_ts (ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='业务日志';

CREATE TABLE IF NOT EXISTS signal_history (
  id            BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  as_of         VARCHAR(32) DEFAULT NULL            COMMENT '信号日期',
  pool          VARCHAR(64) DEFAULT NULL            COMMENT '信号池',
  momentum      INT         DEFAULT NULL            COMMENT 'RSRS动量周期(日)',
  status        VARCHAR(32) DEFAULT NULL            COMMENT '扫描状态',
  items         MEDIUMTEXT  DEFAULT NULL            COMMENT '各标的信号(JSON)',
  selected_code VARCHAR(16) DEFAULT NULL            COMMENT '目标标的代码',
  selected_name VARCHAR(64) DEFAULT NULL            COMMENT '目标标的名称',
  rotation      MEDIUMTEXT  DEFAULT NULL            COMMENT '轮动动作(JSON)',
  payload       MEDIUMTEXT  DEFAULT NULL            COMMENT '完整信号结果(JSON)',
  created_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_signal_day (as_of, pool, momentum),
  KEY idx_signal_pool (pool)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='信号扫描/轮动历史';

CREATE TABLE IF NOT EXISTS grid_triggers (
  id                BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  code              VARCHAR(16)  NOT NULL                COMMENT 'ETF代码',
  name              VARCHAR(64)  DEFAULT NULL            COMMENT 'ETF名称',
  trigger_date      VARCHAR(32)  NOT NULL DEFAULT ''     COMMENT '触发日期时间(YYYY-MM-DD HH:MM:SS)',
  action            VARCHAR(16)  DEFAULT NULL            COMMENT '买入/卖出',
  trigger_type      VARCHAR(16)  NOT NULL DEFAULT 'grid' COMMENT '触发类型(grid/add/reduce/momentum)',
  price             DECIMAL(12,4) DEFAULT NULL           COMMENT '成交价',
  amount            DECIMAL(16,4) NOT NULL DEFAULT 0     COMMENT '成交金额(价格×数量)',
  shares            INT          DEFAULT NULL            COMMENT '数量(份)',
  base_price_before DECIMAL(12,4) DEFAULT NULL           COMMENT '触发前基准价',
  base_price_after  DECIMAL(12,4) DEFAULT NULL           COMMENT '触发后基准价',
  source            VARCHAR(32)  DEFAULT NULL            COMMENT '来源(文件/手工/策略)',
  created_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_trigger (code, trigger_date, amount, shares),
  KEY idx_triggers_code_date (code, trigger_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='网格触发记录';

CREATE TABLE IF NOT EXISTS backtest_results (
  id         BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  kind       VARCHAR(32)  NOT NULL                COMMENT '类型(backtest/enum/screener/grid_opt)',
  params_key VARCHAR(255) NOT NULL                COMMENT '参数指纹(唯一)',
  params     MEDIUMTEXT   DEFAULT NULL            COMMENT '参数(JSON)',
  summary    MEDIUMTEXT   DEFAULT NULL            COMMENT '摘要指标(JSON)',
  payload    MEDIUMTEXT   DEFAULT NULL            COMMENT '完整结果(JSON)',
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_result_kind_key (kind, params_key),
  KEY idx_result_kind (kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='回测/寻优结果';

CREATE TABLE IF NOT EXISTS scheduler_runs (
  id          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  run_type    VARCHAR(32)  DEFAULT NULL            COMMENT '触发类型(schedule/manual)',
  started_at  VARCHAR(32)  DEFAULT NULL            COMMENT '开始时间',
  finished_at VARCHAR(32)  DEFAULT NULL            COMMENT '结束时间',
  duration_ms INT          DEFAULT NULL            COMMENT '耗时(毫秒)',
  result      VARCHAR(32)  DEFAULT NULL            COMMENT '结果(ok/error)',
  detail      MEDIUMTEXT   DEFAULT NULL            COMMENT '执行详情(JSON)',
  email_sent  TINYINT(1)   DEFAULT NULL            COMMENT '邮件是否发送',
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  KEY idx_runs_started (started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='定时任务执行历史';
