"""SQLite 存储层：表结构、连接与身份键工具。

身份规则（全局约定）：
  对象身份 = 实例标识 + 实例代际 + 对象类型 + 实例内编号 + 创建凭据。
  schema 与名称只描述当前位置，不参与身份判定。
  object_key 文本形式：instance|generation|obj_type|obj_number|cred
  无创建凭据的观察 cred 记为空串，且 cred_available=0，永不参与身份匹配。
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS registry (
    object_key    TEXT PRIMARY KEY,
    instance      TEXT NOT NULL,
    generation    INTEGER NOT NULL,
    obj_type      TEXT NOT NULL,
    obj_number    INTEGER NOT NULL,
    cred          TEXT NOT NULL,
    schema_name   TEXT NOT NULL,
    object_name   TEXT NOT NULL,
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    app_id            TEXT PRIMARY KEY,
    object_key        TEXT NOT NULL,
    instance          TEXT NOT NULL,
    generation        INTEGER NOT NULL,
    obj_type          TEXT NOT NULL,
    obj_number        INTEGER NOT NULL,
    cred              TEXT NOT NULL,
    last_known_schema TEXT NOT NULL,
    last_known_name   TEXT NOT NULL,
    applicant         TEXT,
    reason            TEXT,
    submitted_at      TEXT
);

CREATE TABLE IF NOT EXISTS configs (
    version     TEXT PRIMARY KEY,
    declared_at TEXT
);

CREATE TABLE IF NOT EXISTS config_scopes (
    version     TEXT NOT NULL,
    schema_name TEXT NOT NULL,
    obj_type    TEXT NOT NULL,
    PRIMARY KEY (version, schema_name, obj_type)
);

CREATE TABLE IF NOT EXISTS collections (
    collection_id TEXT PRIMARY KEY,
    collected_at  TEXT,
    connector     TEXT,
    accepted      INTEGER NOT NULL,   -- 1=接受 0=拒绝（拒绝的采集不得为退役背书）
    reject_reason TEXT
);

CREATE TABLE IF NOT EXISTS collection_scopes (
    scope_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id TEXT NOT NULL,
    schema_name   TEXT NOT NULL,
    obj_type      TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('complete','restricted','failed')),
    detail        TEXT,
    UNIQUE (collection_id, schema_name, obj_type)
);

CREATE TABLE IF NOT EXISTS sightings (
    sighting_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id  TEXT NOT NULL,
    object_key     TEXT NOT NULL,
    cred_available INTEGER NOT NULL,
    instance       TEXT NOT NULL,
    generation     INTEGER NOT NULL,
    obj_type       TEXT NOT NULL,
    obj_number     INTEGER NOT NULL,
    cred           TEXT,
    schema_name    TEXT NOT NULL,
    object_name    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sightings_key
    ON sightings (collection_id, object_key);
CREATE INDEX IF NOT EXISTS idx_sightings_num
    ON sightings (collection_id, instance, generation, obj_type, obj_number);

-- 每次采集、每个实例观察到哪些代际（导入时预聚合，核验时 O(1) 查询）
CREATE TABLE IF NOT EXISTS collection_generations (
    collection_id TEXT NOT NULL,
    instance      TEXT NOT NULL,
    generation    INTEGER NOT NULL,
    PRIMARY KEY (collection_id, instance, generation)
);

CREATE TABLE IF NOT EXISTS column_lists (
    collection_id TEXT NOT NULL,
    object_key    TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('complete','restricted','failed')),
    PRIMARY KEY (collection_id, object_key)
);

CREATE TABLE IF NOT EXISTS column_items (
    collection_id TEXT NOT NULL,
    object_key    TEXT NOT NULL,
    col_number    INTEGER NOT NULL,
    col_name      TEXT NOT NULL,
    PRIMARY KEY (collection_id, object_key, col_number)
);

-- 核验结果只增不改：同一 (申请, 采集, 配置版本) 重复核验读取历史行
CREATE TABLE IF NOT EXISTS verifications (
    verif_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id         TEXT NOT NULL,
    collection_id  TEXT NOT NULL,
    config_version TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    conclusion     TEXT NOT NULL,
    current_schema TEXT,
    current_name   TEXT,
    detail_json    TEXT NOT NULL,
    UNIQUE (app_id, collection_id, config_version)
);
CREATE INDEX IF NOT EXISTS idx_verif_app ON verifications (app_id, verif_id);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # 离线核验工具的标准生产设置：WAL 允许读写并发，NORMAL 避免每次提交都 fsync
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(path: str) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def make_object_key(instance, generation, obj_type, obj_number, cred) -> str:
    return f"{instance}|{generation}|{obj_type}|{obj_number}|{cred if cred else ''}"
