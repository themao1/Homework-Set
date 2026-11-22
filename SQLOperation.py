# -*- coding: utf-8 -*-
"""SQLOperation.py — 全项目唯一的数据库访问层（DAL / Data Access Layer）

责任边界（对应需求「权责分明」）：
  项目里**只有本文件** import sqlite3、只有本文件出现 SQL 字面量。
  抓取脚本（DataGet.py）、地图/前端（bank_map_app.py）、静态地图（folium_map.py）、
  程序入口（main.py）都只调用本模块的函数：
    1. SQL 收敛在一处：以后换 PostgreSQL / MySQL，只改这个文件，其它脚本一行不动；
    2. 建表/迁移逻辑只有一份：谁先跑，库结构都是对的；
    3. 唯一约束 + UPSERT 在这一层统一实现「增量更新与去重」。

表结构（v3）：
  banks / communities  —— 两类 POI 共用同一套列，新增一类数据只需在 config.LAYERS 里注册
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    address    TEXT,
    lng        REAL,          -- GCJ-02 坐标系（高德）
    lat        REAL,
    tel        TEXT,
    type       TEXT,          -- 高德 POI 类型（可能形如 "金融保险服务;银行;中国建设银行"）
    city       TEXT,          -- 所属城市（为「以后不只在深圳」预留，可筛选、可统计）
    district   TEXT,          -- 所属区县（高德 adname）
    adcode     TEXT,          -- 行政区划编码（高德 adcode）
    source     TEXT NOT NULL DEFAULT 'amap_v5',
    extra      TEXT,          -- 预留：JSON 文本，放未来才有的业务字段（如小区物业费/户数）
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
  UNIQUE INDEX idx_<table>_identity ON <table> (name, lng, lat)
    —— 身份标识交给数据库，重复抓取不会堆积数据

  feed_back  —— 用户在前端反馈的信息错误，等待人工审核
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type    TEXT NOT NULL,   -- 图层 key：bank / community（对应 config.LAYERS）
    target_id      INTEGER,         -- 被反馈记录的 id，可为空（例如反馈「缺少某个网点」）
    target_name    TEXT,            -- 提交时的名称快照（记录被改动后仍能对照）
    field_name     TEXT NOT NULL,   -- 出错字段：name/address/lnglat/tel/type/other
    error_desc     TEXT NOT NULL,   -- 用户描述的错误
    suggested_value TEXT,           -- 用户给出的正确值（审核通过时可回写）
    contact        TEXT,            -- 可选联系方式
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected
    reviewer       TEXT,            -- 审核人
    review_note    TEXT,            -- 审核备注
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    reviewed_at    TEXT

用法：
  作为模块导入：
      from SQLOperation import get_connection, list_pois, submit_feedback
  作为命令行工具（自检/手动迁移）：
      python SQLOperation.py status    # 查看库结构、各表行数、待审核反馈数
      python SQLOperation.py migrate   # 手动触发建表/迁移（幂等，跑多少次都安全）
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

import config

# ---------------------------------------------------------------------------
# 配置区
# ---------------------------------------------------------------------------

# 与其它模块共用同一份配置：默认同目录 banks.db，可用环境变量 BANKS_DB 覆盖
DB_PATH = config.DB_PATH

# 当前表结构版本号（写在 SQLite 的 PRAGMA user_version 里，迁移逻辑靠它判断）
# v1：只有 banks 七列；v2：+ source/created_at/updated_at；v3：+ 小区表、反馈表、城市列
SCHEMA_VERSION = 3

# 允许被操作的表（SQL 标识符白名单，来自 config.LAYERS，防止拼接表名时注入）
ALLOWED_TABLES: set[str] = config.layer_table_names()

# POI 表共用的列（不含 id 与时间戳）：建表、迁移、UPSERT 都从这里推导，
# 以后加业务字段只需要改这一处 + 补一条 ALTER 迁移
POI_COLUMNS: tuple[str, ...] = (
    "name",
    "address",
    "lng",
    "lat",
    "tel",
    "type",
    "city",
    "district",
    "adcode",
    "source",
    "extra",
)

# 建表语句模板。表名由 config.LAYERS 提供并经过白名单校验，不接受外部任意输入
_CREATE_POI_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    address    TEXT,
    lng        REAL,
    lat        REAL,
    tel        TEXT,
    type       TEXT,
    city       TEXT,
    district   TEXT,
    adcode     TEXT,
    source     TEXT NOT NULL DEFAULT 'amap_v5',
    extra      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
)
"""

# 老表缺列时用 ALTER TABLE ADD COLUMN 补齐。
# ⚠️ SQLite 的 ADD COLUMN 不接受非常量默认值，所以时间戳列只作为兜底（无默认值）
# 出现在这里；正常 v2 库不会缺这两列。
_POI_ADD_COLUMN_DDL: dict[str, str] = {
    "city": "TEXT",
    "district": "TEXT",
    "adcode": "TEXT",
    "extra": "TEXT",
    "source": "TEXT NOT NULL DEFAULT 'amap_v5'",
    "created_at": "TEXT",
    "updated_at": "TEXT",
}

# 命中唯一索引 (name,lng,lat) 则更新可变字段并刷新 updated_at，未命中则插入。
# extra 用 COALESCE 保住旧值：新抓取的记录没有该字段时，别把人工补的拓展字段冲掉
_UPSERT_POI_SQL = """
INSERT INTO {table} (name, address, lng, lat, tel, type, city, district, adcode, source, extra)
VALUES (:name, :address, :lng, :lat, :tel, :type, :city, :district, :adcode, :source, :extra)
ON CONFLICT (name, lng, lat) DO UPDATE SET
    address    = excluded.address,
    tel        = excluded.tel,
    type       = excluded.type,
    city       = COALESCE(excluded.city, {table}.city),
    district   = COALESCE(excluded.district, {table}.district),
    adcode     = COALESCE(excluded.adcode, {table}.adcode),
    extra      = COALESCE(excluded.extra, {table}.extra),
    updated_at = datetime('now', 'localtime')
"""

_CREATE_FEEDBACK_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS feed_back (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type     TEXT NOT NULL,
    target_id       INTEGER,
    target_name     TEXT,
    field_name      TEXT NOT NULL,
    error_desc      TEXT NOT NULL,
    suggested_value TEXT,
    contact         TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    reviewer        TEXT,
    review_note     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    reviewed_at     TEXT
)
"""

# 反馈表索引：按状态+时间取待审核列表、按目标记录查历史反馈，都是高频路径
_FEEDBACK_INDEX_SQLS = (
    "CREATE INDEX IF NOT EXISTS idx_feed_back_status ON feed_back (status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_feed_back_target ON feed_back (target_type, target_id)",
)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _quote_table(table: str) -> str:
    """校验并返回可安全拼进 SQL 的表名。

    表名来自 config.LAYERS（代码内常量），但仍然过一遍白名单：
    「永远不要相信字符串能安全地拼进 SQL」这条纪律，在只读场景也要守住。

    Raises:
        ValueError: 表名不在白名单里。
    """
    if table not in ALLOWED_TABLES:
        raise ValueError(
            f"不允许操作的表：{table!r}，可用表：{sorted(ALLOWED_TABLES)}"
        )
    return table


def _user_version(conn: sqlite3.Connection) -> int:
    """读取 SQLite 的 PRAGMA user_version（表结构版本号）。"""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """判断表是否存在。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """返回表的列名集合（用 PRAGMA，不解析建表语句文本）。"""
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({_quote_table(table)})")}


# 老版本把空值 str() 后留下的字面量，读出来时统一当空串处理
_EMPTY_ARTIFACTS = {"[]", "{}", "None", "null", "NULL"}


def _as_text(value: Any) -> str:
    """把高德返回值（可能是 str / list / None / dict）统一成入库用的字符串。

    高德接口在「无值」时常常返回空列表 ``[]``，直接 str() 会存成字面量 "[]"，
    这是脏数据的典型来源，所以统一在这里洗掉；历史库里已经被存成 "[]" 的值
    也一并还原成空串。
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ";".join(_as_text(item) for item in value if _as_text(item))
    if isinstance(value, dict):
        return ""
    text = str(value).strip()
    # 历史脏值：老版本把空列表/空 dict 直接 str() 存了进来
    return "" if text in _EMPTY_ARTIFACTS else text


# ---------------------------------------------------------------------------
# 连接与结构管理
# ---------------------------------------------------------------------------


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """打开数据库连接并确保表结构为最新版本。

    Args:
        db_path: 数据库文件路径；缺省用模块级 DB_PATH（可被 BANKS_DB 环境变量覆盖）。

    Returns:
        sqlite3.Connection，已设置 row_factory=sqlite3.Row，调用方负责 close。

    Raises:
        FileNotFoundError: 库文件不存在。查询方应给出明确提示，
            而不是让 sqlite3 静默新建一个空库再报「表不存在」的迷惑错误。
    """
    path = Path(db_path) if db_path else DB_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"未找到数据库文件：{path}。请先运行 python main.py scrape 抓取数据，"
            f"或用环境变量 BANKS_DB 指定已有数据库的位置。"
        )
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row  # 按列名取值，避免魔法下标 row[3]
    conn.execute("PRAGMA foreign_keys = ON")  # 启用外键约束，便于以后加关联表
    ensure_schema(conn, allow_create=True)  # 结构检查（幂等）
    return conn


def create_database(db_path: Path | str | None = None) -> sqlite3.Connection:
    """创建（或打开）数据库并保证结构最新，抓取脚本第一次运行时用。

    Returns:
        sqlite3.Connection，已建好 v3 表结构，调用方负责 close。
    """
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)  # 确保父目录存在
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn, allow_create=True)
    return conn


def ensure_schema(conn: sqlite3.Connection, allow_create: bool = False) -> None:
    """确保库结构为 v3（幂等）。

    四种情况：
      1. 库里连 banks 都没有 → allow_create=True 时建全套表，否则抛错
         （防止查询脚本误建空库）；
      2. 有 v1 旧表（无 source 等列）→ 整表重建迁移到 v3；
      3. 有 v2 表（缺 city 等列）→ 补列 + 建小区表/反馈表；
      4. 已是 v3 → 只补索引（IF NOT EXISTS），不做任何数据改动。

    Args:
        conn: 数据库连接。
        allow_create: 是否允许新建表。

    Raises:
        RuntimeError: 表不存在且 allow_create=False，或发现无法识别的表结构。
    """
    version = _user_version(conn)
    has_banks = _table_exists(conn, "banks")

    # 情况 1：空库
    if not has_banks and not allow_create:
        raise RuntimeError(
            "banks 表不存在：请先运行 python main.py scrape --target bank 抓取数据"
        )

    # 情况 2：v1 旧表 → 一步迁到 v3（建新表搬数据，id 原样保留）
    if has_banks and version < 2:
        _migrate_v1_to_v3(conn)

    # 情况 3/4：补列、补表、补索引
    _ensure_v3(conn)


def _ensure_v3(conn: sqlite3.Connection) -> None:
    """把结构补齐到 v3：所有图层的表、缺失的列、反馈表与索引。"""
    with conn:  # 事务：结构变更要么全成，要么全回滚
        for layer in config.all_layers():
            table = _quote_table(layer.table)
            if not _table_exists(conn, table):
                conn.execute(_CREATE_POI_TABLE_SQL.format(table=table))
            else:
                _add_missing_poi_columns(conn, table)
            # 唯一索引：身份标识交给数据库（新表建索引，旧表补索引）
            conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_identity "
                f"ON {table} (name, lng, lat)"
            )
            # 常见过滤条件的辅助索引：城市筛选、名称前缀排序（数据量大时才有意义）
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_city ON {table} (city)"
            )

        conn.execute(_CREATE_FEEDBACK_TABLE_SQL)
        for sql in _FEEDBACK_INDEX_SQLS:
            conn.execute(sql)

        if _user_version(conn) < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _add_missing_poi_columns(conn: sqlite3.Connection, table: str) -> None:
    """给老表补齐 POI 共用列（v2 → v3 的 city/district/adcode/extra）。"""
    existing = _table_columns(conn, table)
    for column, ddl in _POI_ADD_COLUMN_DDL.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate_v1_to_v3(conn: sqlite3.Connection) -> None:
    """把 v1 旧表（无 source/created_at/updated_at）迁移到 v3。

    标准路径：建新表 → 在 Python 侧逐行清洗后搬数据 → 删旧表 → 改名。
    id 原样保留，前端 tooltip 里的 "#id" 定位不受影响。

    Raises:
        RuntimeError: 旧表结构不符合预期，迁移拒绝执行（保护数据优先）。
    """
    cols = _table_columns(conn, "banks")
    required_v1 = {"id", "name", "address", "lng", "lat", "tel", "type"}
    if not required_v1.issubset(cols):
        raise RuntimeError(
            f"banks 表结构无法识别（现有列：{sorted(cols)}），"
            f"迁移中止以免损坏数据。请人工检查 {DB_PATH}。"
        )

    with conn:  # 事务：迁移中途失败则整体回滚，不留半成品
        legacy_rows = conn.execute(
            "SELECT id, name, address, lng, lat, tel, type FROM banks"
        ).fetchall()
        conn.execute("DROP TABLE IF EXISTS banks__migrating")
        conn.execute(_CREATE_POI_TABLE_SQL.format(table="banks__migrating"))
        for r in legacy_rows:
            # 旧库里的无坐标脏行，正好借迁移淘汰掉
            cleaned = clean_row(r["name"], r["address"], r["lng"], r["lat"],
                                r["tel"], r["type"])
            if cleaned is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO banks__migrating "
                "(id, name, address, lng, lat, tel, type, city, district, adcode, source, extra) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["id"], cleaned["name"], cleaned["address"], cleaned["lng"],
                 cleaned["lat"], cleaned["tel"], cleaned["type"], cleaned["city"],
                 cleaned["district"], cleaned["adcode"], cleaned["source"],
                 cleaned["extra"]),
            )
        conn.execute("DROP TABLE banks")
        conn.execute("ALTER TABLE banks__migrating RENAME TO banks")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_banks_identity "
            "ON banks (name, lng, lat)"
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


# ---------------------------------------------------------------------------
# 数据清洗（入库前的统一收口）
# ---------------------------------------------------------------------------


def normalize_tel(tel: Any) -> str:
    """归一化电话字段为字符串。

    高德 v5 的 tel 可能返回列表（一家银行登记多部电话），
    直接入库会存成 "['0755-8888']" 这种带 Python 列表皮的字符串。

    Args:
        tel: 原始 tel 字段（str / list / None 均可）。

    Returns:
        归一化后的字符串，多个号码用分号连接；无电话返回空串。
    """
    return _as_text(tel)


# 城市名归一：高德返回「深圳市」，而命令行 --city 深圳、人工录入的往往不带「市」。
# 只去掉这一个常见后缀，不臆测其它行政区划写法（「恩施土家族苗族自治州」等保持原样）
_CITY_SUFFIXES = ("市",)


def normalize_city(city: Any) -> str:
    """归一化城市名，让不同来源的同一城市能对上（深圳市 → 深圳）。

    这是「数据一致性」问题而不是展示问题：城市下拉框、按城市统计都依赖它。
    """
    text = _as_text(city)
    for suffix in _CITY_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return text


def clean_row(
    name: Any,
    address: Any,
    lng: Any,
    lat: Any,
    tel: Any,
    type_: Any,
    source: str = "amap_v5",
    city: Any = None,
    district: Any = None,
    adcode: Any = None,
    extra: dict[str, Any] | str | None = None,
) -> dict[str, Any] | None:
    """把一条高德 POI 原始记录清洗成可入库的 dict。

    所有「缺失值 / 格式不一致」的处理集中在这一个函数里：
      - 坐标缺失、无法转 float、或超出经纬度合法范围 → 返回 None，调用方跳过并记日志；
      - tel / address 返回列表（高德常见）→ 用 ; 连接成字符串；
      - extra 允许传 dict，统一序列化成 JSON 文本（数据库里只存 TEXT）。

    Args:
        name/address/lng/lat/tel/type_: 高德 POI 字段原值。
        source: 数据来源标识，写入 source 列。
        city/district/adcode: 行政区划信息（高德 cityname/adname/adcode）。
        extra: 预留的拓展字段，dict 会被序列化成 JSON 文本。

    Returns:
        可直接传给 upsert_pois 的 dict；不可用记录返回 None。
    """
    name_text = _as_text(name)
    if not name_text:
        return None

    try:
        lng_f = float(lng) if lng not in (None, "") else None
        lat_f = float(lat) if lat not in (None, "") else None
    except (TypeError, ValueError):
        return None

    # 坐标合法范围校验：缺坐标或明显错位的记录不入库。
    # 注意 0,0 在数学上合法（几内亚湾），只是业务上几乎不会出现，这里不特殊处理
    if lng_f is None or lat_f is None:
        return None
    if not (-180.0 <= lng_f <= 180.0 and -90.0 <= lat_f <= 90.0):
        return None

    return {
        "name": name_text,
        "address": _as_text(address),
        "lng": lng_f,
        "lat": lat_f,
        "tel": normalize_tel(tel),
        "type": _as_text(type_),
        "city": normalize_city(city) or None,
        "district": _as_text(district) or None,
        "adcode": _as_text(adcode) or None,
        "source": source,
        "extra": _dump_extra(extra),
    }


def _dump_extra(extra: dict[str, Any] | str | None) -> str | None:
    """把拓展字段序列化成 JSON 文本；空值返回 None（不写 "{}" 制造噪音）。"""
    if extra is None or extra == "":
        return None
    if isinstance(extra, str):
        return extra
    try:
        return json.dumps(extra, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return None


def parse_extra(text: Any) -> dict[str, Any]:
    """把 extra 列里的 JSON 文本解析成 dict，解析不了就返回空 dict。

    展示层不该因为一条脏 JSON 抛异常——数据不可信，解析要容错。
    """
    if not text:
        return {}
    if isinstance(text, dict):
        return text
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# 写入（增量更新 + 去重）
# ---------------------------------------------------------------------------


def upsert_pois(
    conn: sqlite3.Connection, table: str, rows: Iterable[dict[str, Any]]
) -> tuple[int, int]:
    """批量 UPSERT 一类 POI 记录，返回 (新增条数, 更新条数)。

    依赖唯一索引 (name, lng, lat)：重复运行抓取脚本不会堆积数据。

    Args:
        conn: 数据库连接（函数内部开事务，由调用方决定连接何时关闭）。
        table: 目标表名（必须在 config.LAYERS 注册）。
        rows: clean_row 产出的 dict 列表。

    Returns:
        (inserted, updated) 元组；空列表返回 (0, 0)，不产生任何 SQL。
    """
    table = _quote_table(table)
    rows = list(rows)
    if not rows:
        return 0, 0

    before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    changes_before = conn.total_changes
    with conn:  # 事务：一批数据要么全部落库，要么全部回滚
        conn.executemany(_UPSERT_POI_SQL.format(table=table), rows)
    after = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    inserted = after - before
    updated = (conn.total_changes - changes_before) - inserted
    return inserted, updated


# 兼容旧调用名（外部脚本可能还在用），内部统一走 upsert_pois
def upsert_banks(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """批量写入银行网点（upsert_pois 的语义化包装）。"""
    return upsert_pois(conn, "banks", rows)


def upsert_communities(
    conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]
) -> tuple[int, int]:
    """批量写入小区（upsert_pois 的语义化包装）。"""
    return upsert_pois(conn, "communities", rows)


# ---------------------------------------------------------------------------
# 查询（供地图/列表/前端复用）
# ---------------------------------------------------------------------------


def _escape_like(keyword: str) -> str:
    """转义 LIKE 通配符（% _ \\），配合 ESCAPE 子句使用。

    用户输入的 % 会被当成通配符，虽然无害但行为不符合直觉。
    """
    return keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# 允许排序的列白名单：排序列不能用 ? 占位符（SQL 标识符），只能走白名单
_SORTABLE_COLUMNS = ("id", "name", "city", "type", "updated_at", "created_at")


def _build_where(
    keyword: str = "",
    poi_type: str = config.FILTER_ALL,
    city: str = config.FILTER_ALL,
    district: str | None = None,
) -> tuple[str, list[Any]]:
    """拼装 WHERE 子句与参数，所有值一律用 ? 占位符传入。

    「全部」「未标注」是界面用的哨兵值，不当作真实的过滤值参与比较。
    """
    clauses = ["1=1"]
    params: list[Any] = []

    if keyword:
        like = f"%{_escape_like(keyword.strip())}%"
        # 名称和地址都搜：用户往往只记得「某某路」而不是完整网点名
        clauses.append("(name LIKE ? ESCAPE '\\' OR address LIKE ? ESCAPE '\\')")
        params.extend([like, like])

    if poi_type and poi_type != config.FILTER_ALL:
        clauses.append("type = ?")
        params.append(poi_type)

    if city and city != config.FILTER_ALL:
        if city == config.FILTER_UNKNOWN:
            clauses.append("(city IS NULL OR city = '')")
        else:
            clauses.append("city = ?")
            params.append(city)

    if district:
        clauses.append("district = ?")
        params.append(district)

    return " AND ".join(clauses), params


def list_pois(
    conn: sqlite3.Connection,
    table: str,
    keyword: str = "",
    poi_type: str = config.FILTER_ALL,
    city: str = config.FILTER_ALL,
    district: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    order_by: str = "id",
    descending: bool = False,
) -> list[dict[str, Any]]:
    """按条件查询 POI 列表（银行 / 小区共用同一套逻辑）。

    Args:
        conn: 数据库连接。
        table: 目标表名（config.LAYERS 注册过）。
        keyword: 名称/地址关键词，模糊匹配；空串表示不过滤。
        poi_type: 类型精确过滤；config.FILTER_ALL 表示不过滤。
        city: 城市过滤；config.FILTER_ALL 不过滤，config.FILTER_UNKNOWN 只取未标注的。
        district: 区县过滤（可选）。
        limit: 最多返回条数；None 表示不限制（地图需要全量）。
        offset: 跳过多少条（分页用）。
        order_by: 排序列，必须在白名单内。
        descending: 是否倒序。

    Returns:
        list[dict]，无数据时返回空列表。

    Raises:
        ValueError: 排序字段不在白名单内。
    """
    table = _quote_table(table)
    if order_by not in _SORTABLE_COLUMNS:
        raise ValueError(
            f"不允许按 {order_by!r} 排序，可选：{_SORTABLE_COLUMNS}"
        )

    where, params = _build_where(keyword, poi_type, city, district)
    sql = (
        "SELECT id, name, address, lng, lat, tel, type, city, district, adcode, "
        "source, extra, created_at, updated_at "
        f"FROM {table} WHERE {where} "
        f"ORDER BY {order_by} {'DESC' if descending else 'ASC'}"
    )
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])

    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def count_pois(
    conn: sqlite3.Connection,
    table: str,
    keyword: str = "",
    poi_type: str = config.FILTER_ALL,
    city: str = config.FILTER_ALL,
    district: str | None = None,
) -> int:
    """返回符合条件的记录总数（分页用；与 list_pois 共用同一套过滤条件）。"""
    table = _quote_table(table)
    where, params = _build_where(keyword, poi_type, city, district)
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {where}", params
    ).fetchone()[0]


def get_poi(conn: sqlite3.Connection, table: str, poi_id: int) -> dict[str, Any] | None:
    """按主键查询单条记录（点击地图标记后调用）。"""
    table = _quote_table(table)
    row = conn.execute(
        "SELECT id, name, address, lng, lat, tel, type, city, district, adcode, "
        "source, extra, created_at, updated_at "
        f"FROM {table} WHERE id = ?",
        (poi_id,),
    ).fetchone()
    return dict(row) if row else None


# 向后兼容：老代码直接调 load_banks(conn, keyword, bank_type)
def load_banks(
    conn: sqlite3.Connection, keyword: str = "", bank_type: str = config.FILTER_ALL
) -> list[dict[str, Any]]:
    """读取银行列表（get_poi/list_pois 的语义化包装）。"""
    return list_pois(conn, "banks", keyword=keyword, poi_type=bank_type)


def list_pois_types(conn: sqlite3.Connection, table: str, city: str = config.FILTER_ALL) -> list[str]:
    """返回某类数据里出现过的所有类型值（供前端「类型」下拉框）。

    高德的 type 可能是 "金融保险服务;银行;中国建设银行" 这种长串，
    这里原样返回：过滤必须用原始值，展示层再决定怎么截短。
    """
    table = _quote_table(table)
    where, params = _build_where(city=city)
    rows = conn.execute(
        f"SELECT DISTINCT type FROM {table} WHERE {where} AND type IS NOT NULL AND type <> '' "
        "ORDER BY type",
        params,
    ).fetchall()
    return [row[0] for row in rows]


def list_pois_cities(conn: sqlite3.Connection, table: str) -> list[str]:
    """返回某类数据里出现过的城市（支持「以后不只在深圳」）。"""
    table = _quote_table(table)
    rows = conn.execute(
        f"SELECT DISTINCT city FROM {table} "
        "WHERE city IS NOT NULL AND city <> '' ORDER BY city"
    ).fetchall()
    return [row[0] for row in rows]


def table_stats(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
    """返回某张表的概览：总行数、城市数、类型数、最近更新时间。

    一次 SQL 取完，避免界面为了三个指标打三次查询。
    """
    table = _quote_table(table)
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "COUNT(DISTINCT city) AS cities, "
        "COUNT(DISTINCT type) AS types, "
        "MAX(updated_at) AS last_updated "
        f"FROM {table}"
    ).fetchone()
    return {
        "total": row["total"] or 0,
        "cities": row["cities"] or 0,
        "types": row["types"] or 0,
        "last_updated": row["last_updated"],
    }


def nearby_pois(
    conn: sqlite3.Connection,
    table: str,
    lng: float,
    lat: float,
    radius_m: int,
    limit: int = config.NEARBY_LIMIT,
) -> list[dict[str, Any]]:
    """查询某坐标点周边 radius_m 米内的 POI，按真实距离升序返回。

    两步走：
      1. bounding box 粗筛：把经纬度矩形范围下推到 SQL（能用上索引，
         换 PostgreSQL + PostGIS 时可进一步下推为 ST_DWithin）；
      2. Haversine 精算：只对粗筛幸存者算球面距离，再排序、截断。

    「距离」是纯数学计算，不依赖具体数据库，因此留在本层没有引入耦合；
    以后数据量再大，也只需把第 1 步换成空间索引查询。

    Args:
        conn: 数据库连接。
        table: 目标表名。
        lng / lat: 点击坐标（GCJ-02）。
        radius_m: 查询半径（米）。
        limit: 最多返回条数（防止大范围检索把页面拖垮）。

    Returns:
        list[dict]，每条多一个 distance_m 字段（米，浮点）。
    """
    table = _quote_table(table)
    if radius_m <= 0:
        return []

    # 1 度纬度 ≈ 111.32 km；经度距离随纬度收缩，需除以 cos(lat)
    d_lat = radius_m / 111_320.0
    cos_lat = max(math.cos(math.radians(lat)), 1e-6)  # 防止高纬度除零
    d_lng = radius_m / (111_320.0 * cos_lat)

    # 含 5% 边界缓冲，防舍入误差在边界上漏点
    min_lng, max_lng = lng - d_lng * 1.05, lng + d_lng * 1.05
    min_lat, max_lat = lat - d_lat * 1.05, lat + d_lat * 1.05

    rows = conn.execute(
        "SELECT id, name, address, lng, lat, tel, type, city, district, adcode, "
        "source, extra, created_at, updated_at "
        f"FROM {table} "
        "WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ? "
        "AND lat IS NOT NULL AND lng IS NOT NULL",
        (min_lat, max_lat, min_lng, max_lng),
    ).fetchall()

    if not rows:
        return []

    # Haversine 球面距离公式：d = 2R·asin(√a)
    lat0 = math.radians(lat)
    results: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        lat_r = math.radians(d["lat"])
        sin_dlat = math.sin((lat_r - lat0) / 2)
        sin_dlon = math.sin((math.radians(d["lng"]) - math.radians(lng)) / 2)
        a = sin_dlat ** 2 + math.cos(lat_r) * math.cos(lat0) * sin_dlon ** 2
        distance_m = 2 * 6_371_000 * math.asin(min(1.0, math.sqrt(a)))
        if distance_m <= radius_m:
            d["distance_m"] = distance_m
            results.append(d)

    results.sort(key=lambda item: item["distance_m"])
    return results[:limit]


# ---------------------------------------------------------------------------
# 用户反馈（写 feed_back，等人工审核）
# ---------------------------------------------------------------------------


def submit_feedback(
    conn: sqlite3.Connection,
    target_type: str,
    field_name: str,
    error_desc: str,
    target_id: int | None = None,
    target_name: str = "",
    suggested_value: str = "",
    contact: str = "",
) -> int:
    """写入一条用户反馈，状态为「待审核」。

    Args:
        conn: 数据库连接。
        target_type: 图层 key（config.LAYERS 的键，如 bank / community）。
        field_name: 出错字段，必须是 config.FEEDBACK_FIELDS 的键。
        error_desc: 用户描述的错误，必填。
        target_id: 被反馈记录的主键；反馈「缺少某某」时可留空。
        target_name: 提交时的名称快照。
        suggested_value: 用户建议的正确值，审核通过时可回写。
        contact: 可选联系方式。

    Returns:
        新建反馈的 id。

    Raises:
        ValueError: 图层/字段不合法，或错误描述为空。
    """
    if target_type not in config.LAYERS:
        raise ValueError(
            f"未知的反馈对象：{target_type!r}，可选：{sorted(config.LAYERS)}"
        )
    if field_name not in config.FEEDBACK_FIELDS:
        raise ValueError(
            f"未知的反馈字段：{field_name!r}，可选：{sorted(config.FEEDBACK_FIELDS)}"
        )
    desc = _as_text(error_desc)
    if not desc:
        raise ValueError("错误描述不能为空")

    with conn:
        cursor = conn.execute(
            "INSERT INTO feed_back "
            "(target_type, target_id, target_name, field_name, error_desc, "
            " suggested_value, contact, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                target_type,
                target_id,
                _as_text(target_name) or None,
                field_name,
                desc,
                _as_text(suggested_value) or None,
                _as_text(contact) or None,
                config.FEEDBACK_STATUS_DEFAULT,
            ),
        )
    return int(cursor.lastrowid)


def list_feedback(
    conn: sqlite3.Connection,
    status: str | None = config.FEEDBACK_STATUS_DEFAULT,
    limit: int = config.FEEDBACK_LIST_LIMIT,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """按状态读取反馈列表（待审核列表按提交时间正序，先到先审）。

    Args:
        status: 状态值；None 表示所有状态。
        limit / offset: 分页。
    """
    sql = "SELECT * FROM feed_back WHERE 1=1"
    params: list[Any] = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def get_feedback(conn: sqlite3.Connection, feedback_id: int) -> dict[str, Any] | None:
    """按主键读取一条反馈（审核前看详情）。"""
    row = conn.execute("SELECT * FROM feed_back WHERE id = ?", (feedback_id,)).fetchone()
    return dict(row) if row else None


def count_feedback(conn: sqlite3.Connection, status: str | None = None) -> int:
    """统计反馈条数；status 为 None 表示全部。"""
    if status:
        return conn.execute(
            "SELECT COUNT(*) FROM feed_back WHERE status = ?", (status,)
        ).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM feed_back").fetchone()[0]


def feedback_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """返回各状态反馈数量（沿用 config.FEEDBACK_STATUS，缺的状态补 0）。"""
    stats = {status: 0 for status in config.FEEDBACK_STATUS}
    for status, total in conn.execute(
        "SELECT status, COUNT(*) FROM feed_back GROUP BY status"
    ).fetchall():
        stats[status] = total
    return stats


def _feedback_value_updates(
    field_name: str, suggested_value: str
) -> dict[str, Any]:
    """把「反馈字段 + 建议值」翻译成一条记录的列更新。

    只接受白名单字段，坐标支持 "经度,纬度" 一次性改两列。

    Raises:
        ValueError: 字段不支持回写，或建议值格式不合法。
    """
    value = _as_text(suggested_value)

    if field_name == "name":
        if not value:
            raise ValueError("名称不能为空")
        return {"name": value}
    if field_name in {"address", "tel", "type"}:
        return {field_name: value}
    if field_name == "lnglat":
        parts = [p.strip() for p in value.replace("，", ",").split(",")]
        if len(parts) != 2:
            raise ValueError(f"坐标格式应为「经度,纬度」，收到：{value!r}")
        try:
            lng, lat = float(parts[0]), float(parts[1])
        except ValueError:
            raise ValueError(f"坐标必须是数字，收到：{value!r}") from None
        if not (-180.0 <= lng <= 180.0 and -90.0 <= lat <= 90.0):
            raise ValueError(f"坐标超出合法范围：{lng}, {lat}")
        return {"lng": lng, "lat": lat}

    # other 及未知字段只改反馈状态，不碰业务数据
    raise ValueError(f"字段 {field_name!r} 不支持自动回写，请人工处理")


def validate_feedback_value(
    field_name: str, suggested_value: str
) -> dict[str, Any]:
    """预检「建议值能否回写」，不写库。

    命令行审核在动手之前先跑一次，避免「确认了才发现坐标格式不对」。
    校验规则与回写完全共用同一份实现（_feedback_value_updates），不会出现两套标准。

    Returns:
        将写入的列与值，如 {"lng": 114.1, "lat": 22.5}。

    Raises:
        ValueError: 字段不支持回写，或建议值格式不合法。
    """
    return _feedback_value_updates(field_name, suggested_value)


def review_feedback(
    conn: sqlite3.Connection,
    feedback_id: int,
    status: str,
    reviewer: str = "",
    note: str = "",
    apply_value: bool = False,
) -> dict[str, Any]:
    """人工审核一条反馈：改状态，可选把建议值回写到业务记录。

    回写与改状态在同一个事务里完成：不会出现「状态已通过但数据没改」的中间态。

    Args:
        conn: 数据库连接。
        feedback_id: 反馈主键。
        status: 目标状态，必须是 approved / rejected。
        reviewer: 审核人（写进 reviewer 列，留痕）。
        note: 审核备注。
        apply_value: 是否回写建议值（仅 approved 且建议值非空时生效）。

    Returns:
        dict：feedback_id、status、applied（回写的列与值）、target_table。

    Raises:
        LookupError: 反馈不存在。
        ValueError: 状态非法、已审核过、或无法回写。
    """
    if status not in {"approved", "rejected"}:
        raise ValueError(f"审核状态只能是 approved / rejected，收到：{status!r}")

    with conn:  # 事务：回写业务表 + 改反馈状态，要么都成要么都不成
        row = conn.execute(
            "SELECT * FROM feed_back WHERE id = ?", (feedback_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"反馈 #{feedback_id} 不存在")
        if row["status"] != config.FEEDBACK_STATUS_DEFAULT:
            raise ValueError(
                f"反馈 #{feedback_id} 已经是「{config.FEEDBACK_STATUS.get(row['status'], row['status'])}」，"
                "不能重复审核"
            )

        applied: dict[str, Any] = {}
        target_table = config.get_layer(row["target_type"]).table

        if apply_value and status == "approved" and (row["suggested_value"] or "").strip():
            if row["target_id"] is None:
                raise ValueError(
                    f"反馈 #{feedback_id} 没有关联具体记录（target_id 为空），无法自动回写"
                )
            updates = _feedback_value_updates(row["field_name"], row["suggested_value"])
            set_sql = ", ".join(f"{column} = ?" for column in updates)
            conn.execute(
                f"UPDATE {target_table} SET {set_sql}, "
                "updated_at = datetime('now','localtime') WHERE id = ?",
                (*updates.values(), row["target_id"]),
            )
            applied = updates

        conn.execute(
            "UPDATE feed_back SET status = ?, reviewer = ?, review_note = ?, "
            "reviewed_at = datetime('now','localtime') WHERE id = ?",
            (status, _as_text(reviewer) or None, _as_text(note) or None, feedback_id),
        )

    return {
        "feedback_id": feedback_id,
        "status": status,
        "applied": applied,
        "target_table": target_table,
    }


# ---------------------------------------------------------------------------
# CLI：python SQLOperation.py [status|migrate]
# ---------------------------------------------------------------------------


def format_status(conn: sqlite3.Connection) -> str:
    """生成库结构/数据概览文本（status 子命令与 main.py 共用）。"""
    version = _user_version(conn)
    lines = [
        f"数据库：{DB_PATH}",
        f"表结构版本：v{version}"
        + ("（最新）" if version >= SCHEMA_VERSION else f"（待迁移到 v{SCHEMA_VERSION}，运行 migrate）"),
    ]
    for layer in config.all_layers():
        if _table_exists(conn, layer.table):
            stats = table_stats(conn, layer.table)
            lines.append(
                f"  {layer.label}（{layer.table}）：{stats['total']} 条，"
                f"城市 {stats['cities']} 个，类型 {stats['types']} 种，"
                f"最近更新 {stats['last_updated'] or '—'}"
            )
        else:
            lines.append(f"  {layer.label}（{layer.table}）：表不存在")

    if _table_exists(conn, "feed_back"):
        stats = feedback_stats(conn)
        detail = "，".join(
            f"{config.FEEDBACK_STATUS[key]} {count}" for key, count in stats.items()
        )
        lines.append(f"  用户反馈（feed_back）：共 {sum(stats.values())} 条（{detail}）")
    return "\n".join(lines)


def _cli() -> None:
    """命令行自检工具：查看状态或手动触发迁移。"""
    config.make_stdout_safe()
    parser = argparse.ArgumentParser(description="数据库工具（银行/小区/反馈）")
    parser.add_argument(
        "command",
        nargs="?",
        default="status",
        choices=["status", "migrate"],
        help="status=查看库状态（默认）；migrate=执行建表/迁移（幂等）",
    )
    args = parser.parse_args()

    if args.command == "migrate":
        # migrate 允许库不存在（建新库），status 不允许（fail fast）
        with closing(create_database(DB_PATH)) as conn:
            print(format_status(conn))
        return

    if not DB_PATH.exists():
        print(f"未找到 {DB_PATH}")
        raise SystemExit(1)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row  # 版本检查也要能按列名取值
        print(format_status(conn))


if __name__ == "__main__":
    _cli()
