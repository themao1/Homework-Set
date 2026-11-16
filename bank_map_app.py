# -*- coding: utf-8 -*-
"""
bank_map_app.py — 银行网点地图查询前端（Streamlit + st_folium 教案式单文件版）

  - 数据来源：main.py 抓取并写入的 banks.db（表 banks: id/name/address/lng/lat/tel/type）
  - 坐标系：数据来自高德 API，是 GCJ-02 坐标（火星坐标系）。
    底图因此必须继续用高德瓦片 —— 底图坐标系和数据坐标系必须一致，

运行方式（在本文件所在目录执行，banks.db 需在同一目录）：
    pip install -r requirements.txt
    streamlit run bank_map_app.py

交互设计（st_folium 的核心机制）：
  st_folium 不是"一次性渲染"，而是每次地图交互（点击/缩放）都会返回一个 dict，
  并触发 Streamlit 整页 rerun。从返回 dict 里"读出"用户点了哪里，
  再拿着坐标/ID 回数据库查询
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

# ---------------------------------------------------------------------------
# 配置区：
# ---------------------------------------------------------------------------

# 默认读同目录的 banks.db，也允许用环境变量 BANKS_DB 覆盖路径（便于测试/部署）
DB_PATH = Path(os.getenv("BANKS_DB", "") or Path(__file__).resolve().parent / "banks.db")

# 地图初始视野（深圳市民中心附近）
MAP_CENTER = [22.54, 114.05]
MAP_ZOOM = 12

# 高德栅格瓦片（GCJ-02 坐标系，与数据自洽）
AMAP_TILE = (
    "http://webrd02.is.autonavi.com/appmaptile"
    "?lang=zh_cn&size=1&scale=1&style=7&x={x}&y={y}&z={z}"
)


# ---------------------------------------------------------------------------
'''数据访问层
需要改进：
1.
越过SQLOperation操作数据库。
'''
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    """打开数据库连接，并启用"行 → dict"的行工厂。

    可改进：
        1.能运行是因为调用的列V1也有。

    Returns:
        sqlite3.Connection

    Raises:
        FileNotFoundError: banks.db 不存在时抛出，让调用方给出明确提示，
            而不是让 sqlite3 静默新建一个空库再报"表不存在"的迷惑错误。
            （fail fast：错误越早暴露、信息越明确，排查成本越低）
    """
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"未找到数据库文件：{DB_PATH}。"
            f"请先运行 main.py 抓取数据，或把 banks.db 放到本文件同目录。"
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 查询结果可按列名取值，避免魔法下标 row[3]
    return conn


def load_banks(
    keyword: str = "", bank_type: str = "全部"
) -> pd.DataFrame:
    """读取银行列表，支持按名称关键词、类型筛选。

    可改进：
        1.没有像SQLOperation.load_banks那样将输入转义。

    Args:
        keyword: 名称关键词，模糊匹配（LIKE），空串表示不过滤。
        bank_type: 网点类型；"全部" 表示不过滤。

    Returns:
        pd.DataFrame，列同 banks 表；无数据时返回空 DataFrame。
    """
    sql = "SELECT id, name, address, lng, lat, tel, type FROM banks WHERE 1=1"
    params: list[Any] = []

    if keyword:
        sql += " AND name LIKE ?"
        params.append(f"%{keyword}%")
    if bank_type != "全部":
        sql += " AND type = ?"
        params.append(bank_type)

    # 只读操作用 try/finally
    conn = _connect()
    try:
        df = pd.read_sql(sql, conn, params=params)
    finally:
        conn.close()
    return df


def get_bank_by_id(bank_id: int) -> dict[str, Any] | None:
    """按主键查询单个银行详情（点击地图标记后调用）。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id, name, address, lng, lat, tel, type FROM banks WHERE id = ?",
            (bank_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 地理计算：SQLite 没有空间函数，半径查询在应用层算
# ---------------------------------------------------------------------------


def nearby_banks(
    df: pd.DataFrame, lng: float, lat: float, radius_m: int
) -> pd.DataFrame:
    """查询某坐标点周边 radius_m 米内的银行，按距离升序返回。

    可改进：
        1.d_lng爆炸问题（如若全球或大范围应用）

    两步走的性能思路：
      1. bounding box 粗筛：经纬度矩形范围过滤掉 99% 的无关点，纯数值比较极快；
      2. Haversine 精算：只对粗筛幸存者算球面距离，然后按真实距离排序/截断。
    如果数据库换成 PostgreSQL + PostGIS，这一步可以下推为
    ST_DWithin 空间索引查询 —— 这就是"数据访问层收敛 SQL"带来的替换自由。

    Args:
        df: 当前筛选后的银行 DataFrame。
        lng / lat: 点击坐标（GCJ-02）。
        radius_m: 查询半径（米）。
    """
    if df.empty:
        return df

    km = radius_m / 1000.0
    # 1 度纬度 ≈ 111.32 km；经度距离随纬度收缩，需除以 cos(lat)
    d_lat = km / 111.32
    d_lng = km / (111.32 * math.cos(math.radians(lat)))

    # 粗筛：与点击点的经纬度差都在矩形半径内（含 5% 边界缓冲，防舍入误差漏点）
    box = df[(df["lat"] - lat).abs() <= d_lat * 1.05]
    box = box[(box["lng"] - lng).abs() <= d_lng * 1.05]

    if box.empty:
        return box

    # 精算：Haversine 球面距离公式（d = 2R·asin(√a)）
    lat0, lng0 = math.radians(lat), math.radians(lng)
    lat_rad = box["lat"].map(math.radians)
    lng_rad = box["lng"].map(math.radians)
    sin_dlat = ((lat_rad - lat0) / 2).map(math.sin)
    sin_dlon = ((lng_rad - lng0) / 2).map(math.sin)
    a = sin_dlat.pow(2) + lat_rad.map(math.cos) * math.cos(lat0) * sin_dlon.pow(2)
    # clip 防止浮点误差把 a 推到 1 以上，导致 asin 定义域报错
    box = box.assign(
        distance_m=2 * 6_371_000 * a.clip(upper=1.0).pow(0.5).map(math.asin)
    )
    box = box[box["distance_m"] <= radius_m]

    return box.sort_values("distance_m")


# ---------------------------------------------------------------------------
# 地图构建
# ---------------------------------------------------------------------------


def build_map(
    df: pd.DataFrame,
    center: list[float] | None = None,
    zoom: int | None = None,
) -> folium.Map:
    """把 DataFrame 渲染成 folium 地图。

    可改进：
        1.pd.isna挡不住空字符串。
        2.外部数据拼HTML未实际转义。

    关键机制：st_folium 点了哪个标记，返回值里就带哪个标记的
    tooltip/popup 文本。所以我们在 tooltip 里编入 id（"#12 网点名"），
    前端就能可靠地知道"用户点的是数据库里 id=12 的那条记录"。
    为什么不直接用点击坐标去匹配数据库？
      —— 不同网点可能共享同一坐标（同栋楼的银行），坐标不唯一，主键才唯一。
    """
    m = folium.Map(
        location=center or MAP_CENTER,
        zoom_start=zoom or MAP_ZOOM,
        tiles=AMAP_TILE,
        attr="高德地图",  # 底图版权声明。
    )

    # iterrows 慢，但几百上千个点完全无感；数据量上 10 万再考虑 CircleMarker 批量方案
    for _, row in df.iterrows():
        # 数据是不可信的：API 抓回来的行可能缺 lat/lng，
        if pd.isna(row["lat"]) or pd.isna(row["lng"]):
            continue
        folium.Marker(
            location=[float(row["lat"]), float(row["lng"])],
            tooltip=f"#{row['id']} {row['name']}",  # id 编码进 tooltip 供回传解析
            popup=folium.Popup(
                # 外部数据拼 HTML 前先转义，防 XSS（名称里带 <script> 时页面会被注入）
                folium.Html(
                    f"<b>{row['name']}</b><br>{row['address']}<br>{row['tel']}",
                    script=True,
                ),
                max_width=260,
            ),
        ).add_to(m)
    return m


def parse_bank_id(tooltip: Any) -> int | None:
    """从 st_folium 回传的 tooltip 文本里解析银行 id（如 "#12 建设银行..." → 12）。"""
    if not isinstance(tooltip, str):
        return None
    m = re.match(r"#(\d+)", tooltip)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# 页面（Streamlit UI 层）
# ---------------------------------------------------------------------------

st.set_page_config(page_title="银行网点地图查询", page_icon="🏦", layout="wide")
st.title("🏦 银行网点地图查询")
st.caption("数据来源：高德地图 API → banks.db ｜ 点击标记查看详情，点击地图查询周边网点")

# ---- 侧边栏：筛选条件（用户的查询意图入口） ----
with st.sidebar:
    st.header("筛选")
    keyword = st.text_input("网点名称关键词", placeholder="如：建设银行")
    try:
        all_banks = load_banks()
    except FileNotFoundError as exc:
        st.error(str(exc))  # 把 fail fast 的错误翻译成人话给用户
        st.stop()
    all_types = ["全部"] + sorted(
        all_banks["type"].dropna().astype(str).unique().tolist()
    )
    bank_type = st.selectbox("网点类型", all_types)
    radius_m = st.slider("周边查询半径（米）", 200, 3000, 1000, step=100)

# ---- 主区：读数据 + 统计 ----
banks = load_banks(keyword, bank_type)

if banks.empty:
    st.warning("没有符合条件的网点，请调整筛选条件，或先运行 main.py 抓取数据。")
    st.stop()


#可改进：若拓展后数据量大要考虑st.cache_date缓存
c1, c2, c3 = st.columns(3)
c1.metric("当前筛选结果", f"{len(banks)} 家")
c2.metric("库内总网点数", f"{len(all_banks)} 家")
c3.metric("类型数", f"{banks['type'].nunique()} 种")

# ---- 地图 + 交互回传 ----
if "map_view" not in st.session_state:
    st.session_state.map_view = {}
view = st.session_state.map_view

m = build_map(banks, center=view.get("center"), zoom=view.get("zoom"))

# returned_objects：只声明我们关心的回传字段，减小每次 rerun 的 payload
# key：给组件固定标识；debug：开发期想看完整回传可改成 debug=True
map_data = st_folium(
    m,
    key="bank_map",
    height=520,
    returned_objects=["last_clicked", "last_object_clicked", "center", "zoom"],
)

if map_data:
    st.session_state.map_view = {
        "center": map_data.get("center"),
        "zoom": map_data.get("zoom"),
    }

# Streamlit 每次交互都整页重跑，这里用 session_state 记住"用户当前选中的银行"
if "selected_bank" not in st.session_state:
    st.session_state.selected_bank = None

marker_clicked = (map_data or {}).get("last_object_clicked")
if marker_clicked:
    bank_id = parse_bank_id(marker_clicked.get("tooltip"))
    if bank_id is not None:
        st.session_state.selected_bank = get_bank_by_id(bank_id)

# ---- 详情卡片：点击标记后展示数据库里的完整记录 ----
if st.session_state.selected_bank:
    b = st.session_state.selected_bank
    with st.container(border=True):
        st.subheader(f"{b['name']}（id={b['id']}）")
        st.markdown(
            f"- **地址**：{b['address'] or '（缺失）'}\n"
            f"- **坐标**：{b['lng']}, {b['lat']}\n"
            f"- **电话**：{b['tel'] or '（缺失）'}\n"
            f"- **类型**：{b['type'] or '（缺失）'}"
        )
        if st.button("关闭详情"):
            st.session_state.selected_bank = None

# ---- 周边查询：点击地图空白处，按半径查周边网点 ----
last_click = (map_data or {}).get("last_clicked")
if last_click:
    lng, lat = last_click["lng"], last_click["lat"]
    st.subheader(f"📍 点击位置周边 {radius_m} 米内的网点")
    near = nearby_banks(banks, lng, lat, radius_m)
    if near.empty:
        st.info("该范围内没有符合条件的网点。")
    else:
        # 表格里 distance_m 保留 0 位小数更友好 —— 展示层的格式化不要污染数据层
        show = near[["id", "name", "address", "tel", "type", "distance_m"]].copy()
        show["distance_m"] = show["distance_m"].round(0).astype(int)
        show.columns = ["ID", "名称", "地址", "电话", "类型", "距离(米)"]
        st.dataframe(show, use_container_width=True, hide_index=True)

st.divider()
st.caption(
    "提示：地图数据为 GCJ-02 坐标系（高德），与高德底图自洽；"
    "如更换 OpenStreetMap 等 WGS84 底图，点位会整体偏移。"
)
