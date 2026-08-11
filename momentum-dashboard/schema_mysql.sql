-- ============================================================
-- 动量轮动策略仪表盘 - MySQL 表结构 DDL
-- 目标库: ai_invest  (utf8mb4 / utf8mb4_unicode_ci)
-- 执行:  mysql -u invest -p ai_invest < schema_mysql.sql
-- 或:    mysql -u root -p
--        mysql> USE ai_invest;
--        mysql> SOURCE /path/to/schema_mysql.sql;
-- ============================================================

-- 1. API 结果缓存（cache.py / db.py 共用）
CREATE TABLE IF NOT EXISTS `cache` (
  `id`         BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  `cache_key`  VARCHAR(255) NOT NULL                COMMENT '缓存键',
  `payload`    MEDIUMTEXT   NOT NULL                COMMENT '缓存内容(JSON)',
  `created_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_cache_key` (`cache_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='API结果缓存';

-- 2. 持仓快照（每次 AI 解析/确认更新写入，含完整 payload）
CREATE TABLE IF NOT EXISTS `positions_snapshots` (
  `id`         BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  `date`       VARCHAR(32) DEFAULT NULL            COMMENT '快照日期',
  `source`     VARCHAR(64) DEFAULT NULL            COMMENT '数据来源',
  `payload`    MEDIUMTEXT  NOT NULL                COMMENT '完整持仓快照(JSON)',
  `created_at` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `idx_snapshots_date` (`date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='持仓快照';

-- 3. AI 持仓解析历史
CREATE TABLE IF NOT EXISTS `parse_history` (
  `id`             BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  `parse_updated_at` VARCHAR(32) DEFAULT NULL          COMMENT '解析更新时间',
  `source`         VARCHAR(64) DEFAULT NULL            COMMENT '解析来源',
  `holdings_count` INT         DEFAULT NULL            COMMENT '持仓数量',
  `trades_count`   INT         DEFAULT NULL            COMMENT '交易笔数',
  `payload`        MEDIUMTEXT  DEFAULT NULL            COMMENT '解析结果(JSON)',
  `created_at`     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at`     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI持仓解析历史';

-- 4. 业务日志（与 server.log 同步镜像）
CREATE TABLE IF NOT EXISTS `api_logs` (
  `id`         BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  `ts`         VARCHAR(32) NOT NULL                COMMENT '日志时间(ISO8601)',
  `level`      VARCHAR(16) DEFAULT NULL            COMMENT '日志级别(INFO/WARN/ERROR)',
  `message`    VARCHAR(2000) DEFAULT NULL          COMMENT '日志内容',
  `created_at` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `idx_logs_ts` (`ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='业务日志';

-- 5. 信号扫描/轮动历史（每天每池一条，可复盘当时为何换仓）
CREATE TABLE IF NOT EXISTS `signal_history` (
  `id`            BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  `as_of`         VARCHAR(32) DEFAULT NULL            COMMENT '信号日期',
  `pool`          VARCHAR(64) DEFAULT NULL            COMMENT '信号池',
  `momentum`      INT         DEFAULT NULL            COMMENT 'RSRS动量周期(日)',
  `status`        VARCHAR(32) DEFAULT NULL            COMMENT '扫描状态',
  `items`         MEDIUMTEXT  DEFAULT NULL            COMMENT '各标的信号(JSON)',
  `selected_code` VARCHAR(16) DEFAULT NULL            COMMENT '目标标的代码',
  `selected_name` VARCHAR(64) DEFAULT NULL            COMMENT '目标标的名称',
  `rotation`      MEDIUMTEXT  DEFAULT NULL            COMMENT '轮动动作(JSON)',
  `payload`       MEDIUMTEXT  DEFAULT NULL            COMMENT '完整信号结果(JSON)',
  `created_at`    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at`    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_signal_day` (`as_of`, `pool`, `momentum`),
  KEY `idx_signal_pool` (`pool`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='信号扫描/轮动历史';

-- 6. 网格触发记录（完整保留每笔买入/卖出，替代被覆盖的 grid_triggers.json）
CREATE TABLE IF NOT EXISTS `grid_triggers` (
  `id`                BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  `code`              VARCHAR(16)  NOT NULL                COMMENT 'ETF代码',
  `name`              VARCHAR(64)  DEFAULT NULL            COMMENT 'ETF名称',
  `trigger_date`      VARCHAR(32)  NOT NULL DEFAULT ''     COMMENT '触发日期时间(YYYY-MM-DD HH:MM:SS)',
  `action`            VARCHAR(16)  DEFAULT NULL            COMMENT '买入/卖出',
  `trigger_type`      VARCHAR(16)  NOT NULL DEFAULT 'grid' COMMENT '触发类型(grid/add/reduce/momentum)',
  `price`             DECIMAL(12,4) DEFAULT NULL           COMMENT '成交价',
  `amount`            DECIMAL(16,4) NOT NULL DEFAULT 0     COMMENT '成交金额(价格×数量)',
  `shares`            INT          DEFAULT NULL            COMMENT '数量(份)',
  `base_price_before` DECIMAL(12,4) DEFAULT NULL           COMMENT '触发前基准价',
  `base_price_after`  DECIMAL(12,4) DEFAULT NULL           COMMENT '触发后基准价',
  `source`            VARCHAR(32)  DEFAULT NULL            COMMENT '来源(文件/手工/策略)',
  `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_trigger` (`code`, `trigger_date`, `amount`, `shares`),
  KEY `idx_triggers_code_date` (`code`, `trigger_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='网格触发记录';

-- 7. 回测/寻优结果（enum/screener/grid_opt 等，按参数复用避免重算）
CREATE TABLE IF NOT EXISTS `backtest_results` (
  `id`         BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  `kind`       VARCHAR(32)  NOT NULL                COMMENT '类型(backtest/enum/screener/grid_opt)',
  `params_key` VARCHAR(255) NOT NULL                COMMENT '参数指纹(唯一)',
  `params`     MEDIUMTEXT   DEFAULT NULL            COMMENT '参数(JSON)',
  `summary`    MEDIUMTEXT   DEFAULT NULL            COMMENT '摘要指标(JSON)',
  `payload`    MEDIUMTEXT   DEFAULT NULL            COMMENT '完整结果(JSON)',
  `created_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_result_kind_key` (`kind`, `params_key`),
  KEY `idx_result_kind` (`kind`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='回测/寻优结果';

-- 8. 定时任务执行历史（调度/手动，含邮件状态；重启后不丢失）
CREATE TABLE IF NOT EXISTS `scheduler_runs` (
  `id`          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  `run_type`    VARCHAR(32)  DEFAULT NULL            COMMENT '触发类型(schedule/manual)',
  `started_at`  VARCHAR(32)  DEFAULT NULL            COMMENT '开始时间',
  `finished_at` VARCHAR(32)  DEFAULT NULL            COMMENT '结束时间',
  `duration_ms` INT          DEFAULT NULL            COMMENT '耗时(毫秒)',
  `result`      VARCHAR(32)  DEFAULT NULL            COMMENT '结果(ok/error)',
  `detail`      MEDIUMTEXT   DEFAULT NULL            COMMENT '执行详情(JSON)',
  `email_sent`  TINYINT(1)   DEFAULT NULL            COMMENT '邮件是否发送',
  `created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `idx_runs_started` (`started_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='定时任务执行历史';

-- 可选：建完后验证
-- SHOW CREATE TABLE cache\G
-- SELECT table_name, table_comment FROM information_schema.tables
--   WHERE table_schema = 'ai_invest';
