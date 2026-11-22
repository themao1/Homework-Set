# -*- coding: utf-8 -*-
"""folium_map.py — 静态地图导出脚本（银行网点 / 小区）

数据管道第 3 环（静态产物）：读库 → folium 打点 → 导出 HTML。
交互式查询前端在 bank_map_app.py（Streamlit），本文件负责离线静态产物
（例如放进报告、发给别人直接双击打开）。

⚠️ 坐标系约定（务必先读）：
  banks.db 中 lng/lat 为 GCJ-02 坐标系（高德「火星坐标」），
  底图必须使用高德瓦片；若换 WGS84 底图（如 OpenStreetMap），
  必须先做 GCJ-02 → WGS84 转换，否则点位整体偏移约 500 米。
  「底图坐标系 == 数据坐标系」是任何底图/数据源变更的必过检查项。

运行方式（本文件所在目录）：
    python folium_map.py                       # 导出银行的 banks_map.html（兼容旧用法）
    python folium_map.py --layer community      # 导出小区
    python folium_map.py --layer all            # 两类各导出一份
    python main.py export-map --layer all       # 统一入口的写法
"""

from __future__ import annotations

import argparse
import html
import logging
from contextlib import closing
from pathlib import Path
from typing import Any, Sequence

import folium

import config
import map_assets
from SQLOperation import get_connection, list_pois

logger = logging.getLogger(__name__)


def data_center(rows: list[dict[str, Any]]) -> tuple[float, float]:
    """用数据自身的中心作为地图初始视野（换城市后不用改配置）。

    与 bank_map_app.map_center 逻辑一致：静态导出与前端页面看到的位置相同。
    """
    points = [(r["lat"], r["lng"]) for r in rows if r.get("lat") and r.get("lng")]
    if not points:
        return config.MAP_CENTER
    return (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points),
    )


def build_map(rows: list[dict[str, Any]], layer: config.Layer) -> folium.Map:
    """把查询结果渲染成 folium 地图。

    Args:
        rows: SQLOperation.list_pois 返回的记录列表（dict）。
        layer: 当前图层（决定标记颜色）。

    Returns:
        folium.Map，调用方负责 save。
    """
    lat, lng = data_center(rows)
    m = folium.Map(
        location=[lat, lng],
        zoom_start=config.MAP_ZOOM,
        tiles=config.AMAP_TILE,
        attr="高德地图",  # 底图版权声明，法律上必须保留
    )
    # 关键：把 leaflet 的 js/css 指到本地 libs/，并去掉 jquery/bootstrap 等用不到的 CDN
    # ——否则离线或 CDN 不通时，导出的 HTML 打开就是一片空白
    map_assets.use_local_assets(m, mode="relative")

    for row in rows:
        # 数据是不可信的：库里的行可能缺坐标（NULL），直接打点会让 folium 抛错
        if row.get("lat") is None or row.get("lng") is None:
            logger.warning("跳过无坐标记录：id=%s %s", row["id"], row["name"])
            continue
        # 外部数据（高德返回的名称/地址）拼 HTML 前必须转义：
        # POI 名称不受我们控制，含 <script> 时就会被浏览器执行
        folium.Marker(
            location=[row["lat"], row["lng"]],
            # 用 tooltip 而不是 Popup：Popup 内部依赖 jQuery，离线时会抛错中断脚本
            tooltip=map_assets.tooltip_html(
                row["id"],
                html.escape(row["name"] or ""),
                html.escape(row["address"] or ""),
                html.escape(row["tel"] or ""),
                html.escape(row["type"] or ""),
            ),
            # 内联 SVG 图钉：不依赖 awesome-markers / 图标字体，离线也画得出来
            icon=map_assets.pin_icon(layer.key),
        ).add_to(m)
    return m


def export(layer_key: str = "bank", output: str | Path | None = None) -> Path:
    """导出某一图层的静态地图。

    Args:
        layer_key: 图层 key（config.LAYERS 的键）。
        output: 输出文件路径；None 时用 <表名>_map.html。

    Returns:
        实际写出的文件路径。

    Raises:
        FileNotFoundError: 数据库不存在。
    """
    layer = config.get_layer(layer_key)
    out_path = Path(output) if output else config.BASE_DIR / f"{layer.table}_map.html"

    # 读库统一走数据访问层 SQLOperation，本文件不碰 sqlite3
    with closing(get_connection()) as conn:
        rows = list_pois(conn, layer.table)

    if not rows:
        logger.warning("库中没有「%s」数据，请先运行 python main.py scrape", layer.label)
        return out_path

    # 导出到别的目录时，把 leaflet 资源一起带过去，否则 HTML 里的 libs/... 会 404
    if not map_assets.ensure_assets_for(out_path):
        logger.warning(
            "输出目录 %s 旁没有 libs/ 且无法复制本地 Leaflet，"
            "该 HTML 打开后地图将无法显示", out_path.parent
        )

    build_map(rows, layer).save(str(out_path))
    logger.info("已导出 %d 个「%s」点位到 %s", len(rows), layer.label, out_path)
    return out_path


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口：python folium_map.py [--layer ...] [--out ...]"""
    config.make_stdout_safe()
    parser = argparse.ArgumentParser(description="导出静态地图 HTML")
    parser.add_argument(
        "--layer", default="bank",
        help="导出哪类数据：bank / community / all（默认 bank）",
    )
    parser.add_argument("--out", default=None, help="输出文件路径（仅单层导出时有效）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    keys = [item.key for item in config.all_layers()] if args.layer == "all" else [args.layer]
    failures = 0
    for key in keys:
        out = None
        if args.out:
            base = Path(args.out)
            # 多层导出时在文件名后加表名，避免后一份覆盖前一份
            out = (
                base
                if len(keys) == 1
                else base.with_name(f"{base.stem}_{config.get_layer(key).table}{base.suffix}")
            )
        try:
            export(key, out)
        except (FileNotFoundError, KeyError) as exc:
            logger.error("导出「%s」失败：%s", key, exc)
            failures += 1
        except OSError as exc:
            logger.error("写入文件失败（%s）：%s", exc.filename or out, exc.strerror or exc)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
