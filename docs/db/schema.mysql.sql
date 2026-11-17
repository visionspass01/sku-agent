-- 电商 SKU 监管 Agent —— MySQL 8 建表参考（应用实际以 SQLAlchemy metadata 建表）
-- 关键点：小时快照表按 (stat_hour, sku_id) 建联合索引，支撑「按小时段批量拉取全量 SKU」的巡检查询。

CREATE DATABASE IF NOT EXISTS sku_demo DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS sku (
    sku_id       VARCHAR(32)  PRIMARY KEY,
    style_id     VARCHAR(32)  NOT NULL,
    style_name   VARCHAR(128) NOT NULL,
    category     VARCHAR(32)  NOT NULL,
    season       VARCHAR(16)  NOT NULL,
    color        VARCHAR(16)  NOT NULL,
    size         VARCHAR(8)   NOT NULL,
    warehouse_id VARCHAR(16)  NOT NULL,
    platform     VARCHAR(16)  NOT NULL,
    price        DOUBLE       NOT NULL,
    list_price   DOUBLE       NOT NULL,
    stock        INT          NOT NULL DEFAULT 0,
    return_rate  DOUBLE       NOT NULL DEFAULT 0,
    supplier_id  VARCHAR(16)  NOT NULL,
    status       VARCHAR(16)  NOT NULL DEFAULT '在售',
    list_date    DATE,
    INDEX idx_sku_style (style_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS stat_hour (
    id        BIGINT AUTO_INCREMENT PRIMARY KEY,
    stat_hour DATETIME NOT NULL,
    sku_id    VARCHAR(32) NOT NULL,
    qty       INT NOT NULL DEFAULT 0,
    amt       DOUBLE NOT NULL DEFAULT 0,
    orders    INT NOT NULL DEFAULT 0,
    UNIQUE KEY uq_stat_hour_sku (stat_hour, sku_id),
    KEY idx_stat_hour_sku (stat_hour, sku_id),
    CONSTRAINT fk_stat_hour_sku FOREIGN KEY (sku_id) REFERENCES sku (sku_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS flow (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    sku_id       VARCHAR(32) NOT NULL,
    warehouse_id VARCHAR(16) NOT NULL,
    qty          INT NOT NULL,
    amt          DOUBLE NOT NULL DEFAULT 0,
    ts           DATETIME NOT NULL,
    flow_type    VARCHAR(16) NOT NULL DEFAULT 'OUT',
    order_id     VARCHAR(32) NOT NULL DEFAULT '',
    KEY idx_flow_sku_ts (sku_id, ts)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS anomaly (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    sku_id     VARCHAR(32) NOT NULL,
    hour       DATETIME NOT NULL,
    severity   VARCHAR(8) NOT NULL DEFAULT '中',
    reasons    JSON,
    created_at DATETIME NOT NULL,
    KEY idx_anomaly_hour (hour)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS inspection_run (
    id          VARCHAR(36) PRIMARY KEY,
    kind        VARCHAR(16) NOT NULL,
    status      VARCHAR(16) NOT NULL DEFAULT 'pending',
    params      JSON,
    stats       JSON,
    started_at  DATETIME NOT NULL,
    finished_at DATETIME,
    error       VARCHAR(512)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS inspection_result (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id      VARCHAR(36) NOT NULL,
    sku_id      VARCHAR(32) NOT NULL,
    risk_level  VARCHAR(8) NOT NULL DEFAULT '中',
    plan        JSON,
    citations   JSON,
    verifier    JSON,
    data_bundle JSON,
    degraded    TINYINT(1) NOT NULL DEFAULT 0,
    elapsed_ms  INT NOT NULL DEFAULT 0,
    created_at  DATETIME NOT NULL,
    KEY idx_result_run (run_id),
    KEY idx_result_sku (sku_id)
) ENGINE=InnoDB;

-- 知识库文档元数据（含部门：男装/女装/通用）
CREATE TABLE IF NOT EXISTS kb_doc (
    doc_id          VARCHAR(64) PRIMARY KEY,
    title           VARCHAR(128) NOT NULL,
    department      VARCHAR(16) NOT NULL DEFAULT '通用',
    category        VARCHAR(32) NOT NULL DEFAULT '全部',
    season          VARCHAR(16) NOT NULL DEFAULT '全部',
    warehouse       VARCHAR(32) NOT NULL DEFAULT '全部',
    platform        VARCHAR(16) NOT NULL DEFAULT '全部',
    effective_start DATE,
    effective_end   DATE,
    version         VARCHAR(16) NOT NULL DEFAULT 'v1',
    source          VARCHAR(128) NOT NULL DEFAULT '',
    sha256          VARCHAR(64) NOT NULL DEFAULT '',
    updated_at      DATETIME NOT NULL
) ENGINE=InnoDB;

-- 登录安全记录（仅保留近 4 小时，服务端登录时自动裁剪）
CREATE TABLE IF NOT EXISTS auth_event (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    username   VARCHAR(32) NOT NULL,
    ip         VARCHAR(64) NOT NULL DEFAULT '-',
    user_agent VARCHAR(200) NOT NULL DEFAULT '',
    at         DATETIME NOT NULL,
    KEY idx_auth_event_at (at),
    KEY idx_auth_event_user (username)
) ENGINE=InnoDB;
