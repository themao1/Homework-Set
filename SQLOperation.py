# -*- coding: utf-8 -*-
"""
SQLOperation.py — 银行网点数据库访问层（DAL / Data Access Layer）

职责边界：
  项目里所有碰 banks.db 的地方（抓取入库 main.py、静态地图 folium_map.py、
  交互前端 bank_map_app.py）都只通过本模块访问数据库——
    1. SQL 收敛在一处，以后换 PostgreSQL 只改这个文件，其他脚本一行不动；
    2. 建表/迁移逻辑只有一份，三个脚本随便谁先跑，库结构都是对的；
    3. 唯一约束 + UPSERT 在这一层统一实现「增量更新与去重」，

表结构（v2，相对 v1 新增三列，满足赛题「数据版本管理」要求）：
  banks(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    address    TEXT,
    lng        REAL,          -- GCJ-02 坐标系（高德）
    lat        REAL,
    tel        TEXT,
    type       TEXT,
    source     TEXT NOT NULL DEFAULT 'amap_v5',   -- 数据来源
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),  -- 首次入库时间
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))   -- 最近更新时间
  )
  UNIQUE INDEX idx_banks_identity ON (name, lng, lat)
  -- 数据库层面的身份标识：去重交给数据库，而不是某一次运行的内存（见鉴定报告 B1）

用法：
  作为模块导入：
      from SQLOperation import get_connection, upsert_banks, load_banks
  作为命令行工具（自检/手动迁移）：
      python SQLOperation.py status    # 查看库结构与行数
      python SQLOperation.py migrate   # 手动触发建表/迁移（幂等，跑多少次都安全）
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from contextlib import closing
from pathlib import Path#与字符串操作相比，路径路径更可靠
from typing import Any#泛型标注

# ---------------------------------------------------------------------------
# 配置区
# ---------------------------------------------------------------------------

# 与 bank_map_app.py 保持同一个环境变量名，测试/部署时一处配置全局生效
DB_PATH = Path(os.getenv("BANKS_DB", "") or Path(__file__).resolve().parent / "banks.db")

# 当前表结构版本号（写在 SQLite 的 PRAGMA user_version 里，迁移逻辑靠它判断）
SCHEMA_VERSION = 2

# v2 建表语句模板。表名用参数填：常规建表用 "banks"，迁移期间用 "banks_v2"。
_CREATE_TABLE_SQL = """
CREATE TABLE {} (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    address    TEXT,
    lng        REAL,
    lat        REAL,
    tel        TEXT,
    type       TEXT,
    source     TEXT NOT NULL DEFAULT 'amap_v5',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
)
"""

# 命中唯一索引(name,lng,lat)则更新可变字段并刷新 updated_at，
_CREATE_UNIQUE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_banks_identity ON banks (name, lng, lat)"
)

# 未命中则插入。
_UPSERT_SQL = """
INSERT INTO banks (name, address, lng, lat, tel, type, source)
VALUES (:name, :address, :lng, :lat, :tel, :type, :source)
ON CONFLICT (name, lng, lat) DO UPDATE SET
    address    = excluded.address,
    tel        = excluded.tel,
    type       = excluded.type,
    updated_at = datetime('now', 'localtime')
"""


# ---------------------------------------------------------------------------
# 连接与结构管理
# ---------------------------------------------------------------------------


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """打开数据库连接并确保表结构为最新版本。

    可改进：
        1.与create_database存在重复代码，可优化。

    Args:
        db_path: 数据库文件路径；缺省用模块级 DB_PATH（可被 BANKS_DB 环境变量覆盖）。

    Returns:
        sqlite3.Connection，已设置 row_factory=sqlite3.Row，调用方负责 close。

    Raises:
        FileNotFoundError: 库文件不存在（新库应由抓取脚本创建，查询方应给出明确提示）。
    """
    path = Path(db_path) if db_path else DB_PATH
    if not path.exists():
        # 防止SQLite默认创建新库
        raise FileNotFoundError(
            f"未找到数据库文件：{path}。请先运行 main.py 抓取数据，"
            f"或用环境变量 BANKS_DB 指定已有数据库的位置。"
        )
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row  # 按列名取值，
    conn.execute("PRAGMA foreign_keys = ON")# 启用SQLite外键约束
    ensure_schema(conn, allow_create=True)# 结构检查
    return conn


def create_database(db_path: Path | str | None = None) -> sqlite3.Connection:
    """创建全新数据库（抓取脚本第一次运行时用）。

    可改进：
        1.未设PRAGMA foreign_keys = ON，不利于关联表拓展。

    Returns:
        sqlite3.Connection，已建好 v2 表结构，调用方负责 close。
    """
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)# 确保父目录存在
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn, allow_create=True)
    return conn


def ensure_schema(conn: sqlite3.Connection, allow_create: bool = False) -> None:
    """确保 banks 表结构为 v2（幂等）。

    三种情况：
      1. 库里没有 banks 表 → 建 v2 表（需 allow_create=True）；
      2. 有表但缺 v2 新列（source/created_at/updated_at）→ 执行 v1→v2 迁移；
      3. 已是 v2 → 什么都不做（幂等，可以每次连接都调用）。

    Args:
        conn: 数据库连接。
        allow_create: 是否允许新建表。False 时遇到「表不存在」会抛错，
            防止查询类脚本误建空库。

    Raises:
        RuntimeError: 表不存在且 allow_create=False，或迁移中发现无法识别的表结构。
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]

    # 情况 3：已是 v2，只补索引（IF NOT EXISTS 幂等）后返回
    if version >= SCHEMA_VERSION:
        conn.execute(_CREATE_UNIQUE_INDEX)
        return

    # 情况 1：无表 → 建表
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='banks'"
    ).fetchone()
    if not has_table:
        if not allow_create:
            raise RuntimeError("banks 表不存在：请先运行 main.py 抓取数据")
        _create_v2(conn)
        return

    # 情况 2：有表但版本号低于 2 → 迁移
    _migrate_v1_to_v2(conn)


def _create_v2(conn: sqlite3.Connection) -> None:
    """
    建 v2 表并打版本号。

    可改进：
        1._CREATE_UNIQUE_INDEX用参数化表名，无索引，不利于V3拓展。
    """
    with conn:
        conn.execute(_CREATE_TABLE_SQL.format("banks"))
        conn.execute(_CREATE_UNIQUE_INDEX)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """把 v1 旧表（无 source/created_at/updated_at）迁移到 v2。

    可改进：
        1.目前是把旧数据存入内存，不利于大型数据处理。
        2.迁移前未假设v1数据脏（建议先去重）

    标准路径：建新表 → 在 Python 侧逐行清洗后搬数据 → 删旧表 → 改名。
    id 原样保留，前端 tooltip 里的 "#id" 定位不受影响。

    Raises:
        RuntimeError: 旧表结构不符合预期，迁移拒绝执行（保护数据优先）。
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(banks)")}
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
        conn.execute("DROP TABLE IF EXISTS banks_v2")
        conn.execute(_CREATE_TABLE_SQL.format("banks_v2"))
        for r in legacy_rows:
            # CAST/lng/lat 在 clean_row 里完成（str→float），tel 列表皮在这里清掉
            cleaned = clean_row(r["name"], r["address"], r["lng"], r["lat"],
                                 r["tel"], r["type"])
            if cleaned is None:
                continue  # 旧库里的无坐标脏行，正好借迁移淘汰掉
            conn.execute(
                "INSERT OR IGNORE INTO banks_v2 (id, name, address, lng, lat, tel, type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (r["id"], cleaned["name"], cleaned["address"], cleaned["lng"],
                 cleaned["lat"], cleaned["tel"], cleaned["type"]),
            )
        conn.execute("DROP TABLE banks")
        conn.execute("ALTER TABLE banks_v2 RENAME TO banks")
        conn.execute(_CREATE_UNIQUE_INDEX)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


# ---------------------------------------------------------------------------
# 数据清洗（入库前的统一收口，见鉴定报告 A4/A5/B4）
# ---------------------------------------------------------------------------


def normalize_tel(tel: Any) -> str:
    """归一化电话字段为字符串。

    高德 v5 的 tel 可能返回列表（一家银行登记多部电话），
    直接入库会存成 "['0755-8888']" 这种带 Python 列表皮的字符串。

    可改进：
        1.若V1数据已存成字符串无法解包。

    Args:
        tel: 原始 tel 字段（str / list / None 均可）。

    Returns:
        归一化后的字符串，多个号码用分号连接；无电话返回空串。
    """
    if isinstance(tel, list):
        return ";".join(str(t) for t in tel if t)
    return str(tel).strip() if tel else ""


def clean_row(name: Any, address: Any, lng: Any, lat: Any, tel: Any, type_: Any,
              source: str = "amap_v5") -> dict[str, Any] | None:
    """把一条高德 POI 原始记录清洗成可入库的 dict。

    所有「缺失值 / 格式不一致」的处理集中在这一个函数：
      - 坐标缺失或无法转 float → 返回 None，调用方跳过并记日志；
      - tel 列表 → 归一化字符串；
      - name 缺失 → 整行无意义，返回 None。

    可改进：
        1.ing=0、lat=0合法。

    Returns:
        可直接传给 upsert_banks 的 dict；不可用记录返回 None。
    """
    name = (str(name).strip() if name else "")
    if not name:
        return None
    try:
        lng_f = float(lng) if lng not in (None, "") else None
        lat_f = float(lat) if lat not in (None, "") else None
    except (TypeError, ValueError):
        return None
    if lng_f is None or lat_f is None:
        return None
    return {
        "name": name,
        "address": (str(address).strip() if address else ""),
        "lng": lng_f,
        "lat": lat_f,
        "tel": normalize_tel(tel),
        "type": (str(type_).strip() if type_ else ""),
        "source": source,
    }


# ---------------------------------------------------------------------------
# 写入（增量更新 + 去重）
# ---------------------------------------------------------------------------


def upsert_banks(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> tuple[int, int]:
    """批量 UPSERT 银行记录，返回 (新增条数, 更新条数)。

    依赖唯一索引 (name, lng, lat)：重复运行抓取脚本不会堆积数据

    可改进点：
        1.一次UPDATE即使没有值被修改也被计数。
        2.即使是空rows仍开连接并2次count。

    Args:
        conn: 数据库连接（函数内部不开事务，由调用方决定提交时机）。
        rows: clean_row 产出的 dict 列表。

    Returns:
        (inserted, updated) 元组。
    """
    before = conn.execute("SELECT COUNT(*) FROM banks").fetchone()[0]
    changes_before = conn.total_changes
    with conn:  # 事务：一批数据要么全部落库，要么全部回滚
        conn.executemany(_UPSERT_SQL, rows)
    after = conn.execute("SELECT COUNT(*) FROM banks").fetchone()[0]
    inserted = after - before
    updated = (conn.total_changes - changes_before) - inserted
    return inserted, updated


# ---------------------------------------------------------------------------
# 查询（供地图/前端复用）
# ---------------------------------------------------------------------------


def _escape_like(keyword: str) -> str:
    """转义 LIKE 通配符（% _ \\），配合 ESCAPE 子句使用。

    用户输入的 % 会被当成通配符，虽然无害但行为不符合直觉；
    """
    return keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def load_banks(conn: sqlite3.Connection, keyword: str = "",
               bank_type: str = "全部") -> list[sqlite3.Row]:
    """读取银行列表，支持名称关键词模糊匹配与类型精确过滤。

    值一律用 ? 占位符传入。
    """
    sql = ("SELECT id, name, address, lng, lat, tel, type, "
           "source, created_at, updated_at FROM banks WHERE 1=1")
    params: list[Any] = []
    if keyword:
        sql += " AND name LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(keyword)}%")
    if bank_type and bank_type != "全部":
        sql += " AND type = ?"
        params.append(bank_type)
    return conn.execute(sql, params).fetchall()


def get_bank_by_id(conn: sqlite3.Connection, bank_id: int) -> dict[str, Any] | None:
    """按主键查询单条记录（点击地图标记后调用）。"""
    row = conn.execute(
        "SELECT id, name, address, lng, lat, tel, type, "
        "source, created_at, updated_at FROM banks WHERE id = ?",
        (bank_id,),
    ).fetchone()
    return dict(row) if row else None


def count_banks(conn: sqlite3.Connection) -> int:
    """返回 banks 表总行数。"""
    return conn.execute("SELECT COUNT(*) FROM banks").fetchone()[0]


# ---------------------------------------------------------------------------
# CLI：python SQLOperation.py [status|migrate]
# ---------------------------------------------------------------------------


def _cli() -> None:
    parser = argparse.ArgumentParser(description="banks.db 数据库工具")
    parser.add_argument("command", nargs="?", default="status",
                        choices=["status", "migrate"],
                        help="status=查看库状态（默认）；migrate=执行建表/迁移（幂等）")
    args = parser.parse_args()

    if args.command == "migrate":
        # migrate 允许库不存在（建新库），status 不允许（fail fast）
        with closing(create_database(DB_PATH)) as conn:
            print(f"✅ {DB_PATH} 结构已是 v{SCHEMA_VERSION}，当前 {count_banks(conn)} 条记录")
        return

    if not DB_PATH.exists():
        print(f"❌ 未找到 {DB_PATH}")
        raise SystemExit(1)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            print(f"⚠️  表结构为 v{version}，待迁移到 v{SCHEMA_VERSION}（运行 migrate）")
        else:
            print(f"✅ 表结构 v{version}（最新）")
        print(f"记录数：{count_banks(conn)}")
        print(f"来源分布：{dict(conn.execute('SELECT source, COUNT(*) FROM banks GROUP BY source').fetchall())}")
        print(f"最近更新：{conn.execute('SELECT MAX(updated_at) FROM banks').fetchone()[0]}")


if __name__ == "__main__":
    _cli()
