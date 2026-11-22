# -*- coding: utf-8 -*-
"""bank_map_app.py — 前端页面（Streamlit + st_folium）

功能：
  1. 地图查询：银行网点 / 小区 两类数据的打点、点击看详情、点击空白查周边；
  2. 列表查询：关键词/类型/城市过滤 + 分页 + 排序 + 导出 CSV；
  3. 信息反馈：用户对某条记录反馈「银行/小区信息有错」，写入 feed_back 表，
     状态为「待审核」，由后台人工审核（审核入口是命令行：python main.py review）。

数据访问：本文件**不 import sqlite3、不写 SQL**，所有读写都调用 SQLOperation。
  —— 这是需求里的「权责分明」：界面只负责展示与收集输入，数据怎么存由 DAL 决定。

坐标系：数据来自高德 API，是 GCJ-02（火星坐标），底图必须继续用高德瓦片。
  「底图坐标系 == 数据坐标系」是任何底图/数据源变更的必过检查项。

运行方式（在本文件所在目录执行）：
    python main.py app                      # 推荐：统一入口
    streamlit run bank_map_app.py           # 直接跑也行
"""

from __future__ import annotations

import html
import math
import re
from contextlib import closing, contextmanager
from typing import Any, Iterator

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

import config
import SQLOperation

# ---------------------------------------------------------------------------
# 页面基础设置（必须是第一个 Streamlit 调用）
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title=config.APP_TITLE,
    page_icon=config.APP_ICON,
    layout="wide",
)


# ---------------------------------------------------------------------------
# 数据库会话：一次请求一个短连接，用完即关
# ---------------------------------------------------------------------------


@contextmanager
def db() -> Iterator[Any]:
    """打开一个数据库连接并在用完后关闭。

    不在 Streamlit 里长期缓存连接：SQLite 连接跨线程/跨会话共享容易踩坑，
    每次重跑开一个短连接，对 demo 规模完全够用；
    以后换连接池（PostgreSQL）时只改这个函数。
    """
    with closing(SQLOperation.get_connection()) as conn:
        yield conn


def ensure_database() -> None:
    """确认数据库存在，缺失时给出可执行的提示并停止页面渲染。"""
    try:
        with db():
            return
    except FileNotFoundError as exc:
        st.error(str(exc))
        st.code("python main.py scrape --target all", language="bash")
        st.stop()


@st.cache_data(ttl=120, show_spinner=False)
def cached_types(table: str) -> list[str]:
    """类型下拉框的候选值（缓存 2 分钟，避免每次交互都查一遍库）。"""
    with db() as conn:
        return SQLOperation.list_pois_types(conn, table)


@st.cache_data(ttl=120, show_spinner=False)
def cached_cities(table: str) -> list[str]:
    """城市下拉框的候选值。"""
    with db() as conn:
        return SQLOperation.list_pois_cities(conn, table)


# ---------------------------------------------------------------------------
# 展示工具
# ---------------------------------------------------------------------------


def short_type(raw: str | None) -> str:
    """把高德的长类型串压成好读的短名。

    "金融保险服务;银行;中国建设银行|金融保险服务;自动提款;中国建设银行ATM"
      → "中国建设银行"
    只影响展示，过滤时仍然用原始值。
    """
    if not raw:
        return "未分类"
    first = raw.split("|")[0]
    parts = [part for part in first.split(";") if part]
    return parts[-1] if parts else first


def fmt_value(value: Any, placeholder: str = "（缺失）") -> str:
    """展示层统一处理空值：None / 空串 / "[]" 都显示成占位符。"""
    if value is None:
        return placeholder
    text = str(value).strip()
    return text if text and text != "[]" else placeholder


def map_center(rows: list[dict[str, Any]]) -> tuple[float, float]:
    """用当前数据自身的中心作为地图初始视野。

    这样「以后不只在深圳」时，换城市抓完数据打开页面就自动落在新城市，
    不需要改任何配置；只有一条数据时退化为该点本身。
    """
    points = [(r["lat"], r["lng"]) for r in rows if r.get("lat") and r.get("lng")]
    if not points:
        return config.MAP_CENTER
    lat = sum(p[0] for p in points) / len(points)
    lng = sum(p[1] for p in points) / len(points)
    return (lat, lng)


def build_map(
    rows: list[dict[str, Any]],
    layer: config.Layer,
    center: list[float] | None = None,
    zoom: int | None = None,
    selected_id: int | None = None,
) -> folium.Map:
    """把查询结果渲染成 folium 地图。

    st_folium 点击标记后，返回值里带该标记的 tooltip 文本，所以把 id 编进
    tooltip（"#12 某某银行"），前端就能可靠地知道「点的是 id=12 那条」。
    为什么不直接用点击坐标匹配？——同一栋楼可能有多家网点，坐标不唯一，主键才唯一。

    Args:
        rows: SQLOperation 返回的记录列表（dict）。
        layer: 当前图层（决定标记颜色）。
        center / zoom: 地图视野；None 用数据中心 / 默认缩放。
        selected_id: 当前选中记录的 id，用不同颜色高亮。
    """
    lat, lng = map_center(rows)
    m = folium.Map(
        location=list(center or (lat, lng)),
        zoom_start=zoom or config.MAP_ZOOM,
        tiles=config.AMAP_TILE,
        attr="高德地图",  # 底图版权声明，法律上必须保留
    )

    default_color = "blue" if layer.key == "bank" else "green"

    for row in rows:
        # 数据是不可信的：缺坐标的行直接跳过，不要让 folium 抛错
        if row.get("lat") is None or row.get("lng") is None:
            continue

        # 外部数据（高德返回的名称/地址）拼 HTML 前必须转义，防 XSS
        popup_html = (
            f"<b>{html.escape(fmt_value(row.get('name'), ''))}</b><br>"
            f"{html.escape(fmt_value(row.get('address'), ''))}<br>"
            f"{html.escape(fmt_value(row.get('tel'), ''))}<br>"
            f"<span style='color:#888'>{html.escape(short_type(row.get('type')))}</span>"
        )
        folium.Marker(
            location=[row["lat"], row["lng"]],
            tooltip=f"#{row['id']} {fmt_value(row.get('name'), '')}",
            popup=folium.Popup(popup_html, max_width=260),
            icon=folium.Icon(
                color="red" if row["id"] == selected_id else default_color,
                icon="info-sign",
            ),
        ).add_to(m)
    return m


def parse_poi_id(tooltip: Any) -> int | None:
    """从 st_folium 回传的 tooltip 文本里解析记录 id（"#12 建设银行" → 12）。"""
    if not isinstance(tooltip, str):
        return None
    match = re.match(r"#(\d+)", tooltip)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# 侧边栏：图层与筛选条件（用户的查询意图入口）
# ---------------------------------------------------------------------------


def render_sidebar() -> dict[str, Any]:
    """渲染侧边栏并返回本次查询的过滤条件。"""
    with st.sidebar:
        st.header("筛选")

        layer_key = st.radio(
            "数据类型",
            options=[layer.key for layer in config.all_layers()],
            format_func=lambda key: f"{config.get_layer(key).icon} {config.get_layer(key).label}",
            horizontal=False,
        )
        layer = config.get_layer(layer_key)

        keyword = st.text_input(
            "名称 / 地址关键词",
            placeholder=f"如：{layer.default_keyword}、某某路",
            key=f"kw_{layer.key}",
        )

        all_types = cached_types(layer.table)
        type_options = [config.FILTER_ALL, *all_types]
        poi_type = st.selectbox(
            "类型",
            options=type_options,
            format_func=lambda value: value if value == config.FILTER_ALL else short_type(value),
            key=f"type_{layer.key}",
            help="类型取自高德 POI 分类，展示时做了截短，实际过滤用完整分类值",
        )

        cities = cached_cities(layer.table)
        city_options = [config.FILTER_ALL, *cities, config.FILTER_UNKNOWN]
        city = st.selectbox(
            "城市",
            options=city_options,
            key=f"city_{layer.key}",
            help=f"「{config.FILTER_UNKNOWN}」是尚未回填城市的老数据（重新抓取后自动补齐）",
        )

        radius_m = st.slider(
            "周边查询半径（米）",
            min_value=config.NEARBY_RADIUS_MIN,
            max_value=config.NEARBY_RADIUS_MAX,
            value=config.NEARBY_RADIUS_DEFAULT,
            step=100,
        )

        st.divider()
        if st.button("🔄 刷新数据缓存", width="stretch"):
            st.cache_data.clear()
            st.rerun()

        st.caption(
            "数据来源：高德开放平台 API（GCJ-02 坐标）。\n\n"
            "抓取/更新数据：`python main.py scrape`"
        )

    return {
        "layer": layer,
        "keyword": keyword,
        "poi_type": poi_type,
        "city": city,
        "radius_m": radius_m,
    }


# ---------------------------------------------------------------------------
# 详情卡片
# ---------------------------------------------------------------------------


def render_detail(
    row: dict[str, Any], layer: config.Layer, key_prefix: str
) -> None:
    """渲染单条记录详情，并内嵌「反馈这条记录」的表单。"""
    with st.container(border=True):
        st.subheader(f"{layer.icon} {fmt_value(row.get('name'))}（id={row['id']}）")
        st.markdown(
            f"- **地址**：{fmt_value(row.get('address'))}\n"
            f"- **坐标**：{fmt_value(row.get('lng'))}, {fmt_value(row.get('lat'))}\n"
            f"- **电话**：{fmt_value(row.get('tel'))}\n"
            f"- **类型**：{short_type(row.get('type'))}\n"
            f"- **城市/区县**：{fmt_value(row.get('city'))} / {fmt_value(row.get('district'))}\n"
            f"- **来源 / 更新时间**：{fmt_value(row.get('source'))} / {fmt_value(row.get('updated_at'))}"
        )

        extra = SQLOperation.parse_extra(row.get("extra"))
        if extra:
            # 预留拓展字段：以后小区加了物业费、户数，就能直接在这里展示
            st.caption("拓展字段：" + "，".join(f"{k}={v}" for k, v in extra.items()))

        # 反馈表单内嵌在详情里：用户看到错误就地反馈，不用来回切页面
        with st.expander("✍️ 这条信息有错？点这里反馈"):
            render_feedback_form(
                layer=layer,
                preset_row=row,
                key_prefix=key_prefix,
                compact=True,
            )


# ---------------------------------------------------------------------------
# 反馈表单（地图详情内嵌 + 反馈标签页共用同一个实现）
# ---------------------------------------------------------------------------


def render_feedback_form(
    layer: config.Layer,
    preset_row: dict[str, Any] | None = None,
    key_prefix: str = "fb",
    compact: bool = False,
) -> None:
    """渲染反馈表单，提交后写入 feed_back 表（状态：待审核）。

    Args:
        layer: 反馈针对的数据图层。
        preset_row: 已确定的记录（来自地图点选）；None 时让用户自己从下拉框挑。
        key_prefix: Streamlit 组件 key 前缀，同一个页面出现多个表单时用来区分。
        compact: 紧凑模式（内嵌在详情卡片里时用）。
    """
    field_keys = list(config.FEEDBACK_FIELDS)
    field_labels = config.FEEDBACK_FIELDS

    target_row = preset_row
    if target_row is None:
        # 没有预选记录时，给一个可搜索的下拉框（Streamlit 的 selectbox 支持输入过滤）
        with db() as conn:
            candidates = SQLOperation.list_pois(conn, layer.table, limit=1000)
        if not candidates:
            st.info(f"库里还没有{layer.label}数据，无法反馈。")
            return
        options = {f"#{row['id']} {fmt_value(row.get('name'), '')}": row for row in candidates}
        picked = st.selectbox(
            f"选择要反馈的{layer.label}",
            options=list(options),
            key=f"{key_prefix}_pick_{layer.key}",
        )
        target_row = options[picked]

    with st.form(key=f"{key_prefix}_form_{layer.key}_{target_row['id']}"):
        st.caption(f"反馈对象：**#{target_row['id']} {fmt_value(target_row.get('name'), '')}**")

        field_name = st.selectbox(
            "哪一项信息有误",
            options=field_keys,
            format_func=lambda key: field_labels[key],
        )
        error_desc = st.text_area(
            "错误描述（必填）",
            placeholder="例如：这家网点已经搬迁，地址仍是旧的",
            height=80 if compact else 120,
        )
        suggested_value = st.text_input(
            "正确的内容（选填）",
            placeholder="填了的话，审核通过后可直接回写到记录里；坐标请写「经度,纬度」",
        )
        contact = st.text_input("联系方式（选填）", placeholder="邮箱或手机号，便于回访")

        submitted = st.form_submit_button("提交反馈", type="primary")

        if submitted:
            try:
                with db() as conn:
                    feedback_id = SQLOperation.submit_feedback(
                        conn,
                        target_type=layer.key,
                        field_name=field_name,
                        error_desc=error_desc,
                        target_id=target_row["id"],
                        target_name=fmt_value(target_row.get("name"), ""),
                        suggested_value=suggested_value,
                        contact=contact,
                    )
            except ValueError as exc:
                st.error(f"提交失败：{exc}")
            else:
                st.success(
                    f"已提交，反馈编号 #{feedback_id}。后台人工审核后会更新该记录。"
                )
                st.session_state.setdefault("my_feedback_ids", []).append(feedback_id)


# ---------------------------------------------------------------------------
# 标签页 1：地图查询
# ---------------------------------------------------------------------------


def render_map_tab(filters: dict[str, Any]) -> None:
    """地图打点 + 点击详情 + 周边查询。"""
    layer: config.Layer = filters["layer"]

    with db() as conn:
        total = SQLOperation.count_pois(
            conn, layer.table, filters["keyword"], filters["poi_type"], filters["city"]
        )
        rows = SQLOperation.list_pois(
            conn,
            layer.table,
            keyword=filters["keyword"],
            poi_type=filters["poi_type"],
            city=filters["city"],
            limit=config.MAP_MARKER_LIMIT,  # 浏览器扛不住太多标记，见 config 注释
        )
        library_total = SQLOperation.table_stats(conn, layer.table)["total"]

    c1, c2, c3 = st.columns(3)
    c1.metric("当前筛选结果", f"{total} 条")
    c2.metric(f"库内{layer.label}总数", f"{library_total} 条")
    c3.metric("当前类型数", f"{len({r['type'] for r in rows if r['type']})} 种")

    if not rows:
        st.warning("没有符合条件的数据，请调整筛选条件，或先抓取数据。")
        return
    if total > len(rows):
        st.info(
            f"结果较多，地图只渲染前 {config.MAP_MARKER_LIMIT} 条；"
            f"其余请用「列表查询」分页查看。"
        )

    view_key = f"view_{layer.key}"
    view = st.session_state.get(view_key, {})

    # 选中的记录：两次 rerun 之间记住，避免「点了标记，一闪就没了」
    select_key = f"selected_{layer.key}"
    selected_id = st.session_state.get(select_key)

    map_obj = build_map(
        rows,
        layer,
        center=view.get("center"),
        zoom=view.get("zoom"),
        selected_id=selected_id,
    )

    # returned_objects：只声明关心的回传字段，减小每次 rerun 的 payload
    map_data = st_folium(
        map_obj,
        key=f"map_{layer.key}",
        height=520,
        returned_objects=["last_clicked", "last_object_clicked", "center", "zoom"],
    )

    if map_data:
        st.session_state[view_key] = {
            "center": map_data.get("center"),
            "zoom": map_data.get("zoom"),
        }

    marker_clicked = (map_data or {}).get("last_object_clicked")
    if marker_clicked:
        poi_id = parse_poi_id(marker_clicked.get("tooltip"))
        if poi_id is not None:
            st.session_state[select_key] = poi_id

    # 详情卡片：从库里重新取，保证展示的是「当前最新」的数据，而不是地图渲染时的快照
    if st.session_state.get(select_key):
        with db() as conn:
            row = SQLOperation.get_poi(conn, layer.table, st.session_state[select_key])
        if row:
            render_detail(row, layer, key_prefix="map")
            if st.button("关闭详情", key=f"close_{layer.key}"):
                st.session_state[select_key] = None
                st.rerun()

    # 周边查询：点击地图空白处，按半径查周边同类数据
    last_click = (map_data or {}).get("last_clicked")
    if last_click:
        click_lng, click_lat = last_click["lng"], last_click["lat"]
        st.subheader(f"📍 点击位置周边 {filters['radius_m']} 米内的{layer.label}")
        with db() as conn:
            near = SQLOperation.nearby_pois(
                conn, layer.table, click_lng, click_lat, filters["radius_m"]
            )
        if not near:
            st.info("该范围内没有数据。")
        else:
            show = pd.DataFrame(
                [
                    {
                        "ID": row["id"],
                        "名称": row["name"],
                        "地址": row["address"],
                        "电话": row["tel"],
                        "类型": short_type(row["type"]),
                        "距离(米)": int(round(row["distance_m"])),
                    }
                    for row in near
                ]
            )
            st.dataframe(show, width="stretch", hide_index=True)


# ---------------------------------------------------------------------------
# 标签页 2：列表查询
# ---------------------------------------------------------------------------


def render_list_tab(filters: dict[str, Any]) -> None:
    """表格化查询：分页、排序、导出 CSV。

    与地图页共用侧边栏的过滤条件，避免「同一份筛选要填两遍」。
    """
    layer: config.Layer = filters["layer"]

    sort_options = {
        "id": "按录入顺序",
        "name": "按名称",
        "updated_at": "按最近更新",
    }
    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        sort_key = st.selectbox(
            "排序", options=list(sort_options),
            format_func=lambda key: sort_options[key], key=f"sort_{layer.key}",
        )
    with col2:
        descending = st.checkbox("倒序", value=False, key=f"desc_{layer.key}")
    with col3:
        sizes = list(config.LIST_PAGE_SIZES)
        default_index = (
            sizes.index(config.LIST_PAGE_SIZE_DEFAULT)
            if config.LIST_PAGE_SIZE_DEFAULT in sizes
            else 0
        )
        page_size = st.selectbox(
            "每页条数", options=sizes, index=default_index, key=f"size_{layer.key}"
        )

    with db() as conn:
        total = SQLOperation.count_pois(
            conn, layer.table, filters["keyword"], filters["poi_type"], filters["city"]
        )

    pages = max(1, math.ceil(total / page_size))

    # 筛选条件变了就回到第一页，否则用户会看到「第 3 页但只有 1 页」的空表
    page_key = f"page_{layer.key}"
    signature = (filters["keyword"], filters["poi_type"], filters["city"], sort_key, descending, page_size)
    if st.session_state.get(f"sig_{layer.key}") != signature:
        st.session_state[f"sig_{layer.key}"] = signature
        st.session_state[page_key] = 1
    page = min(max(1, st.session_state.get(page_key, 1)), pages)
    st.session_state[page_key] = page

    with db() as conn:
        rows = SQLOperation.list_pois(
            conn,
            layer.table,
            keyword=filters["keyword"],
            poi_type=filters["poi_type"],
            city=filters["city"],
            limit=page_size,
            offset=(page - 1) * page_size,
            order_by=sort_key,
            descending=descending,
        )

    nav1, nav2, nav3 = st.columns([1, 3, 1])
    with nav1:
        if st.button("⬅️ 上一页", disabled=page <= 1, key=f"prev_{layer.key}", width="stretch"):
            st.session_state[page_key] = page - 1
            st.rerun()
    with nav2:
        st.markdown(
            f"<div style='text-align:center'>第 <b>{page}</b> / {pages} 页，"
            f"共 <b>{total}</b> 条</div>",
            unsafe_allow_html=True,
        )
    with nav3:
        if st.button("下一页 ➡️", disabled=page >= pages, key=f"next_{layer.key}", width="stretch"):
            st.session_state[page_key] = page + 1
            st.rerun()

    if not rows:
        st.warning("没有符合条件的记录。")
        return

    frame = pd.DataFrame(
        [
            {
                "ID": row["id"],
                "名称": row["name"],
                "地址": row["address"],
                "电话": row["tel"],
                "类型": short_type(row["type"]),
                "城市": row["city"],
                "区县": row["district"],
                "更新时间": row["updated_at"],
            }
            for row in rows
        ]
    )
    st.dataframe(frame, width="stretch", hide_index=True)

    # 导出：加 BOM，Excel 打开中文才不乱码
    st.download_button(
        "⬇️ 导出当前筛选结果（CSV）",
        data=frame.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"{layer.table}_{page}.csv",
        mime="text/csv",
        key=f"csv_{layer.key}",
    )
    st.caption("导出的是「当前页」数据；需要全量导出请先缩小筛选范围或提高每页条数。")


# ---------------------------------------------------------------------------
# 标签页 3：信息反馈
# ---------------------------------------------------------------------------


def render_feedback_tab(filters: dict[str, Any]) -> None:
    """独立的反馈入口：也能反馈「地图上找不到的」记录。"""
    layer: config.Layer = filters["layer"]

    st.markdown(
        "发现**银行网点**或**小区**的信息有误（名称、地址、坐标、电话、类型），"
        "在这里提交。反馈会进入 `feed_back` 表，状态为「待审核」，由后台人工核实后修正。"
    )

    with db() as conn:
        stats = SQLOperation.feedback_stats(conn)
    cols = st.columns(len(config.FEEDBACK_STATUS))
    for col, (status, label) in zip(cols, config.FEEDBACK_STATUS.items()):
        col.metric(label, stats.get(status, 0))

    # 反馈对象可以是任意图层，不局限于侧边栏当前选的那类数据
    target_key = st.radio(
        "反馈哪类数据",
        options=[item.key for item in config.all_layers()],
        format_func=lambda key: f"{config.get_layer(key).icon} {config.get_layer(key).label}",
        horizontal=True,
        index=[item.key for item in config.all_layers()].index(layer.key),
        key="fb_target_layer",
    )
    target_layer = config.get_layer(target_key)

    render_feedback_form(layer=target_layer, key_prefix="tab")

    my_ids = st.session_state.get("my_feedback_ids", [])
    if my_ids:
        st.divider()
        st.caption(f"本次会话已提交 {len(my_ids)} 条反馈：" + "、".join(f"#{i}" for i in my_ids))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

ensure_database()

st.title(f"{config.APP_ICON} {config.APP_TITLE}")
st.caption(
    "数据来源：高德地图开放平台 API ｜ 地图：点击标记看详情，点击空白查周边 ｜ "
    "发现错误可在「信息反馈」提交，后台人工审核"
)

filters = render_sidebar()

tab_map, tab_list, tab_feedback = st.tabs(
    ["🗺️ 地图查询", "📋 列表查询", "✍️ 信息反馈"]
)
with tab_map:
    render_map_tab(filters)
with tab_list:
    render_list_tab(filters)
with tab_feedback:
    render_feedback_tab(filters)

st.divider()
st.caption(
    "提示：地图数据为 GCJ-02 坐标系（高德），与高德底图自洽；"
    "如更换 OpenStreetMap 等 WGS84 底图，点位会整体偏移约 500 米。"
)
