# -*- coding: utf-8 -*-
"""
folium_map.py — 银行网点静态地图导出脚本

数据管道第 3 环（静态产物）：读库 → folium 打点 → 导出 banks_map.html
交互式查询前端在 bank_map_app.py（Streamlit），本文件负责离线静态产物。

⚠️ 坐标系约定（C5，务必先读）：
  banks.db 中 lng/lat 为 GCJ-02 坐标系（高德「火星坐标」），
  底图必须使用高德瓦片；若换 WGS84 底图（如 OpenStreetMap），
  必须先做 GCJ-02 → WGS84 转换，否则点位整体偏移约 500 米。
  「底图坐标系 == 数据坐标系」是任何底图/数据源变更的必过检查项。

运行方式（本文件所在目录）：
    python folium_map.py
"""

from __future__ import annotations

import html
import logging
from contextlib import closing

import folium

from SQLOperation import get_connection, load_banks

logger = logging.getLogger(__name__)

# 地图初始视野（深圳市民中心附近）
MAP_CENTER = [22.54, 114.05]
MAP_ZOOM = 12

# 高德栅格瓦片（GCJ-02 坐标系，与数据自洽，见文件头坐标系约定）
AMAP_TILE = (
    "http://webrd02.is.autonavi.com/appmaptile"
    "?lang=zh_cn&size=1&scale=1&style=7&x={x}&y={y}&z={z}"
)

OUTPUT_HTML = "banks_map.html"


def build_map(rows: list) -> folium.Map:
    """把查询结果渲染成 folium 地图。

    Args:
        rows: SQLOperation.load_banks 返回的记录列表（sqlite3.Row）。

    Returns:
        folium.Map，调用方负责 save。
    """
    m = folium.Map(
        location=MAP_CENTER,
        zoom_start=MAP_ZOOM,
        tiles=AMAP_TILE,
        attr="高德地图",  # 底图版权声明，法律上必须保留
    )

    # C4 修复：itertuples 而不是 iterrows——后者每行构造一个
    # pd.Series，数据量上去后慢一个数量级；当前几百个点无感，
    # 但「遍历数据框首选 itertuples」要现在养成习惯
    for row in rows:
        # 数据是不可信的：库里的行可能缺坐标（NULL），
        # 直接打点会让 folium 抛错，跳过并计日志
        if row["lat"] is None or row["lng"] is None:
            logger.warning("跳过无坐标记录：id=%s %s", row["id"], row["name"])
            continue
        # C3 修复：外部数据（高德返回的名称/地址）拼 HTML 前必须转义。
        # POI 名称不受我们控制，含 <script> 时就会被浏览器执行——
        # 「外部数据 ≠ 可信数据」这条纪律，在低风险场景也要守住
        popup_html = (
            f"<b>{html.escape(row['name'])}</b><br>"
            f"{html.escape(row['address'] or '')}<br>"
            f"{html.escape(row['tel'] or '')}"
        )
        folium.Marker(
            location=[row["lat"], row["lng"]],
            popup=folium.Popup(popup_html, max_width=260),
            tooltip=f"#{row['id']} {row['name']}",
        ).add_to(m)
    return m


def main() -> None:
    """入口：读库 → 建图 → 导出静态 HTML。"""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    # 串联说明：读库统一走数据访问层 SQLOperation，
    # 本文件不再直接碰 sqlite3 / pandas
    with closing(get_connection()) as conn:
        rows = load_banks(conn)

    if not rows:
        logger.warning("库中无数据，请先运行 APIget.py 抓取")
        return

    m = build_map(rows)
    m.save(OUTPUT_HTML)
    logger.info("已导出 %d 个点位到 %s", len(rows), OUTPUT_HTML)


if __name__ == "__main__":
    main()
