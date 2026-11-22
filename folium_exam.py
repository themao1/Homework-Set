# -*- coding: utf-8 -*-
"""folium_exam.py — folium 打点学习/验证脚本（非正式功能代码）

用途：早期验证「高德瓦片 + GCJ-02 坐标」组合能否正常打点的小实验，
取库中前 10 条银行数据导出 test_map.html，用于排查坐标偏移问题。
正式的静态地图导出在 folium_map.py，交互查询在 bank_map_app.py。

数据访问同样走 SQLOperation：连学习脚本也不绕过数据访问层，
这样「只有 SQLOperation 直接操作数据库」这条规矩才是真的成立。

运行：python folium_exam.py
"""

from contextlib import closing

import folium

import config
import map_assets
from SQLOperation import get_connection, list_pois


def main() -> int:
    """读前 10 条银行数据，打点并保存到 test_map.html。"""
    with closing(get_connection()) as conn:
        rows = list_pois(conn, "banks", limit=10)

    if not rows:
        print("库中没有数据，请先运行：python main.py scrape --target bank")
        return 0

    m = folium.Map(
        location=list(config.MAP_CENTER),
        zoom_start=config.MAP_ZOOM,
        tiles=config.AMAP_TILE,
        attr="高德地图",
    )
    map_assets.use_local_assets(m, mode="relative")

    # 暴力打点测试（不用自定义图标，就用内联图钉）
    for row in rows:
        lat, lng = row.get("lat"), row.get("lng")
        if lat is None or lng is None:
            print(f"第 {row.get('id')} 条数据缺坐标，跳过")
            continue
        folium.Marker(
            location=[float(lat), float(lng)],
            popup=row.get("name") or "",
            icon=map_assets.pin_icon("bank"),
        ).add_to(m)

    m.save(str(config.BASE_DIR / "test_map.html"))
    print("✅ 测试地图已保存：test_map.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
