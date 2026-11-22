# -*- coding: utf-8 -*-
"""map_assets.py — 地图前端资源（Leaflet）本地化，让地图不再依赖公网 CDN

问题定位
--------
folium 生成的 HTML 默认从公网加载 10 个资源（leaflet / jquery / bootstrap /
glyphicons / fontawesome / awesome-markers）。其中：
  * leaflet.js 加载不到 → 地图直接白屏，什么都画不出来；
  * leaflet.awesome-markers.js 加载不到 → `L.AwesomeMarkers.icon(...)` 抛错，
    标记点全部消失（folium.Icon 就是靠这个插件画的）。

本项目已经把 Leaflet 下载到 ``libs/``（leaflet.js / leaflet.css / images/），
所以这里做两件事：
  1. 用本地资源替换掉 folium 默认的 CDN 列表，其余用不到的 CDN 依赖全部去掉；
  2. 标记点改用「内联 SVG + DivIcon」，不依赖任何图标字体或图标库，
     这样即使一个外部资源都加载不到，地图和点也能正常显示。

两种使用场景的引用方式不同（见 use_local_assets 的 mode 参数）：
  * ``relative``：静态导出的 HTML，浏览器直接打开，写成 ``libs/leaflet.js``；
  * ``inline``：Streamlit 组件 iframe，拿不到项目目录，写成 ``data:`` URL（自带内容）。

本地文件缺失时自动回退到 folium 默认 CDN，并打印一次警告，而不是直接报错。
"""

from __future__ import annotations

import base64
import functools
import logging
import shutil
from pathlib import Path

import folium

import config

logger = logging.getLogger(__name__)

# 图层配色：与前端表格/图例保持一致（改颜色只改这里）
LAYER_COLORS: dict[str, str] = {
    "bank": "#2b6cb0",       # 蓝：银行网点
    "community": "#2f855a",  # 绿：小区
}
DEFAULT_COLOR = "#4a5568"    # 兜底灰
SELECTED_COLOR = "#e53e3e"   # 选中：红

# 内联 SVG 图钉：点状标记不方便辨认，这里画一个经典水滴形图钉。
# 全部样式写在元素属性里，不依赖任何外部 CSS/字体，离线或内网都能显示。
_PIN_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 18 24">'
    '<path d="M9 0C4.03 0 0 4.03 0 9c0 6.75 9 15 9 15s9-8.25 9-15c0-4.97-4.03-9-9-9z" '
    'fill="{color}" stroke="#ffffff" stroke-width="1.6"/>'
    '<circle cx="9" cy="9" r="3.1" fill="#ffffff"/>'
    "</svg>"
)


def layer_color(layer_key: str) -> str:
    """按图层返回标记颜色；未知图层用兜底色，不抛异常。"""
    return LAYER_COLORS.get(layer_key, DEFAULT_COLOR)


def pin_icon(layer_key: str, selected: bool = False) -> folium.DivIcon:
    """生成一个不依赖外部资源的图钉图标（DivIcon + 内联 SVG）。

    Args:
        layer_key: 图层 key（决定颜色），如 bank / community。
        selected: 是否为「当前选中」记录（红色、略大）。

    Returns:
        folium.DivIcon，可直接作为 Marker 的 icon。
    """
    color = SELECTED_COLOR if selected else layer_color(layer_key)
    width, height = (24, 32) if selected else (18, 24)
    html = _PIN_SVG.format(w=width, h=height, color=color)
    return folium.DivIcon(
        html=html,
        icon_size=(width, height),
        # 图钉尖端对准坐标点，视觉上才不会「漂」
        icon_anchor=(width // 2, height),
    )


def local_assets_available() -> bool:
    """本地 Leaflet 是否齐备（js + css 都在）。"""
    return config.LEAFLET_JS.is_file() and config.LEAFLET_CSS.is_file()


@functools.lru_cache(maxsize=1)
def _inline_urls() -> tuple[str, str]:
    """把本地 leaflet.js / leaflet.css 编码成 data: URL（只编码一次，结果缓存）。

    Streamlit 组件运行在独立 iframe 里，用相对路径拿不到项目文件，
    所以把资源内容直接塞进 URL，做到「零网络请求」。
    """
    js = "data:text/javascript;base64," + base64.b64encode(
        config.LEAFLET_JS.read_bytes()
    ).decode("ascii")
    css = "data:text/css;base64," + base64.b64encode(
        config.LEAFLET_CSS.read_bytes()
    ).decode("ascii")
    return js, css


def use_local_assets(m: folium.Map, mode: str = "relative") -> bool:
    """把这个地图的 JS/CSS 改成本地 Leaflet，并移除其余 CDN 依赖。

    实现方式：覆盖 Map 的 ``default_js`` / ``default_css`` 列表——folium 渲染时
    会把它们写进 HTML 的 <head>（静态导出）或交给 Streamlit 组件注入（内嵌模式）。

    Args:
        m: 已经创建好的 folium.Map。
        mode: "relative" 用 ``libs/leaflet.js`` 相对路径（HTML 与 libs 同级）；
              "inline" 用 data: URL（iframe 内可用，零请求但体积略大）。

    Returns:
        True 表示已切到本地资源；False 表示本地文件缺失、保持 folium 默认 CDN。
    """
    if not local_assets_available():
        logger.warning(
            "未找到本地 Leaflet 资源（%s），本次回退到公网 CDN；"
            "离线环境下地图会显示不出来，请把 leaflet.js / leaflet.css / images 放到该目录。",
            config.LEAFLET_DIR,
        )
        return False

    if mode == "inline":
        js_url, css_url = _inline_urls()
    elif mode == "relative":
        js_url = f"{config.LEAFLET_ASSET_URL}/leaflet.js"
        css_url = f"{config.LEAFLET_ASSET_URL}/leaflet.css"
    else:
        raise ValueError(f"不支持的资源模式：{mode!r}（可选 relative / inline）")

    # 只保留 leaflet：jquery / bootstrap / glyphicons / fontawesome / awesome-markers
    # 在本项目里都用不到（标记点已改成内联 SVG），去掉就少 8 个外部请求
    m.default_js = [("leaflet", js_url)]
    m.default_css = [("leaflet_css", css_url)]
    return True


def ensure_assets_for(output_html: str | Path) -> bool:
    """确保静态地图输出目录旁边有 libs/，否则相对路径的 leaflet 引用会 404。

    导出到项目目录时什么都不做（libs 本来就在旁边）；
    导出到别处（例如交给别人打开）时，把 leaflet 的 js/css/images 复制过去。

    Returns:
        True 表示目标目录已具备本地资源（或本来就存在），False 表示复制失败。
    """
    target_dir = Path(output_html).resolve().parent
    libs_dir = target_dir / config.LEAFLET_ASSET_URL
    if libs_dir.exists():
        return True
    if not local_assets_available():
        return False

    try:
        libs_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config.LEAFLET_JS, libs_dir / config.LEAFLET_JS.name)
        shutil.copy2(config.LEAFLET_CSS, libs_dir / config.LEAFLET_CSS.name)
        if config.LEAFLET_IMAGES_DIR.is_dir():
            shutil.copytree(
                config.LEAFLET_IMAGES_DIR, libs_dir / "images", dirs_exist_ok=True
            )
    except OSError as exc:
        logger.warning("复制 Leaflet 资源到 %s 失败：%s", libs_dir, exc)
        return False
    return True
