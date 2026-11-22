# -*- coding: utf-8 -*-
"""config.py — 全局配置与「数据图层」注册表

设计原则（对应需求「不要把代码写死」「预留拓展接口」）：
  1. 所有会随时间、地区、部署环境变化的值集中在这里，业务代码里不再出现字面量；
  2. 每一项都可用环境变量覆盖，部署时改 .env 或系统环境变量即可，不必改代码；
  3. 新增一类数据（学校、医院、加油站……）只需在 LAYERS 里加一条，
     抓取脚本、地图、列表查询、反馈表都会自动支持，不需要改动业务代码。

懒加载说明：本模块在导入时读取 .env，但不会连接数据库、不发起网络请求。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# 基础路径
# ---------------------------------------------------------------------------

# 项目根目录（本文件所在目录），所有相对路径都以它为基准，避免受启动目录影响
BASE_DIR = Path(__file__).resolve().parent

# 可选依赖：python-dotenv 缺失时退化为「只读系统环境变量」，不影响运行
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except Exception:  # pragma: no cover - 仅在没有安装依赖时触发
    pass


def env_str(name: str, default: str = "") -> str:
    """读取字符串环境变量，去掉首尾空白；未设置或为空时返回默认值。"""
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def env_int(name: str, default: int) -> int:
    """读取整数环境变量；非法值静默回退默认值（配置错误不该让程序崩在启动阶段）。"""
    try:
        return int(env_str(name, str(default)))
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    """读取布尔环境变量，接受 1/0、true/false、yes/no、on/off。"""
    value = env_str(name, "").lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


def make_stdout_safe() -> None:
    """让命令行输出在任意终端编码下都不崩。

    Windows 控制台常见 GBK（cp936）或 CP437，打印中文/符号时 Python 会抛
    ``UnicodeEncodeError`` 直接中断脚本——别人 clone 下来跑第一条命令就崩，很难受。
    这里把错误处理降级为 replace：实在编码不了的字符显示成 ``?``，但程序继续跑。

    各命令行入口（main.py / DataGet.py / folium_map.py / SQLOperation.py）启动时调用。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            # 非标准流（被重定向成 StringIO 等）没有 reconfigure，忽略即可
            pass


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------

# 数据库文件路径：默认同目录 banks.db，可用环境变量 BANKS_DB 指向别处
# （测试、部署、以后换成远程库时只改这一处）
DB_PATH = Path(env_str("BANKS_DB", str(BASE_DIR / "banks.db")))

# ---------------------------------------------------------------------------
# 高德开放平台（Web 服务 API）
# ---------------------------------------------------------------------------

AMAP_TEXT_URL = env_str(
    "AMAP_TEXT_URL", "https://restapi.amap.com/v5/place/text"
)

# 密钥只从环境变量/.env 读，绝不写进代码或提交到仓库
AMAP_KEY = env_str("AMAP_KEY", "")

# 默认检索城市。以后要抓别的城市：命令行 --city 或环境变量 DEFAULT_CITY
DEFAULT_CITY = env_str("DEFAULT_CITY", "深圳")

# 只在本城市范围内检索（true 时不会把邻近城市的数据混进来）
CITY_LIMIT = env_bool("AMAP_CITY_LIMIT", True)

# 高德单页最大 25 条
PAGE_SIZE = env_int("AMAP_PAGE_SIZE", 25)

# 高德 v5 文本检索的页码上限（超过会被接口拒绝，抓取循环以此收尾）
MAX_PAGE_NUM = env_int("AMAP_MAX_PAGE_NUM", 100)

# 每次请求之间的节流间隔（秒）与失败重试次数
REQUEST_INTERVAL_SEC = float(env_str("AMAP_REQUEST_INTERVAL", "1.0"))
MAX_RETRIES = env_int("AMAP_MAX_RETRIES", 3)
REQUEST_TIMEOUT_SEC = env_int("AMAP_REQUEST_TIMEOUT", 10)

# ---------------------------------------------------------------------------
# 地图与查询默认值
# ---------------------------------------------------------------------------

# 初始视野中心与缩放：数据来自哪个城市就大致落在哪里即可，
# 前端在拿到数据后会用「数据自身的中心」覆盖它（见 bank_map_app.py）
MAP_CENTER = (
    float(env_str("MAP_CENTER_LAT", "22.54")),
    float(env_str("MAP_CENTER_LNG", "114.05")),
)
MAP_ZOOM = env_int("MAP_ZOOM", 12)

# 高德栅格瓦片（GCJ-02 坐标系）。⚠️ 底图坐标系必须与数据坐标系一致
AMAP_TILE = env_str(
    "AMAP_TILE",
    "http://webrd02.is.autonavi.com/appmaptile"
    "?lang=zh_cn&size=1&scale=1&style=7&x={x}&y={y}&z={z}",
)

# ---------------------------------------------------------------------------
# 地图前端资源（Leaflet）本地化
# ---------------------------------------------------------------------------
# folium 默认从公网 CDN 拉 leaflet/jquery/bootstrap/awesome-markers，
# 内网、离线或 CDN 被墙时地图就是一片空白。这里指向项目内的本地副本。
LEAFLET_DIR = Path(env_str("LEAFLET_DIR", str(BASE_DIR / "libs")))
LEAFLET_JS = LEAFLET_DIR / "leaflet.js"
LEAFLET_CSS = LEAFLET_DIR / "leaflet.css"
LEAFLET_IMAGES_DIR = LEAFLET_DIR / "images"

# 相对引用前缀：静态 HTML 与 libs/ 同级时用 "libs/leaflet.js" 即可
LEAFLET_ASSET_URL = env_str("LEAFLET_ASSET_URL", LEAFLET_DIR.name or "libs").rstrip("/")

# 前端内嵌地图的引用方式：
#   inline（默认）= 把 leaflet 内容编码成 data: URL 塞进组件，零网络请求，离线可用；
#   cdn           = 回退 folium 默认 CDN（个别浏览器策略拦截 data: 时用这个开关应急）。
# 静态导出的 HTML 固定用相对路径（libs/leaflet.js），便于缓存与查看源码。
MAP_ASSET_MODE = env_str("MAP_ASSET_MODE", "inline").lower()

# 周边查询：默认半径、可调上下限（米）
NEARBY_RADIUS_DEFAULT = env_int("NEARBY_RADIUS_DEFAULT", 1000)
NEARBY_RADIUS_MIN = env_int("NEARBY_RADIUS_MIN", 200)
NEARBY_RADIUS_MAX = env_int("NEARBY_RADIUS_MAX", 5000)

# 单次周边查询最多返回多少条（防止大范围检索把页面拖垮）
NEARBY_LIMIT = env_int("NEARBY_LIMIT", 300)

# 地图上最多渲染多少个标记点：浏览器扛不住十万个 DOM 节点，
# 数据量大了以后应该换成聚合图层 / 按视野范围请求（这是明确的拓展点）
MAP_MARKER_LIMIT = env_int("MAP_MARKER_LIMIT", 5000)

# 列表分页：每页条数选项与默认值
LIST_PAGE_SIZES = (10, 20, 50, 100)
LIST_PAGE_SIZE_DEFAULT = env_int("LIST_PAGE_SIZE", 20)

# 下拉框里「不做过滤」的哨兵值。用显式常量而不是魔法字符串，改文案只改这里
FILTER_ALL = "全部"

# 城市筛选里「未标注城市」的哨兵值：老数据没有 city 列，迁移后为空，
# 界面单独列一项，避免用户以为数据丢了
FILTER_UNKNOWN = "未标注"

# ---------------------------------------------------------------------------
# 数据图层注册表：新增一类 POI 数据只需要在这里加一条
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Layer:
    """一类可检索、可反馈的数据图层。

    Attributes:
        key: 图层标识（存进 feed_back.target_type，不要随便改）。
        table: 数据库表名（必须是 SQLOperation 白名单里的表）。
        label: 中文名称，用于页面标题、按钮文案。
        icon: 展示用图标。
        amap_types: 高德 POI 类型编码（如 160100=银行，120300=住宅区）。
        default_keyword: 抓取时默认关键词。
        description: 一句话说明，给配置的人和界面看。
    """

    key: str
    table: str
    label: str
    icon: str
    amap_types: str
    default_keyword: str
    description: str


LAYERS: dict[str, Layer] = {
    "bank": Layer(
        key="bank",
        table="banks",
        label="银行网点",
        icon="🏦",
        amap_types="160100",       # 金融保险服务 > 银行
        default_keyword="银行",
        description="高德 POI 类型 160100（金融保险服务>银行）",
    ),
    "community": Layer(
        key="community",
        table="communities",
        label="小区",
        icon="🏘️",
        amap_types="120300",       # 商务住宅 > 住宅区
        default_keyword="小区",
        description="高德 POI 类型 120300（商务住宅>住宅区）",
    ),
}


def get_layer(key: str) -> Layer:
    """按 key 取图层配置。

    Raises:
        KeyError: key 不在 LAYERS 里，错误信息直接列出所有可用图层，
            避免调用方拿着难懂的 KeyError 去猜。
    """
    try:
        return LAYERS[key]
    except KeyError:
        raise KeyError(
            f"未知的数据图层：{key!r}，可用图层：{sorted(LAYERS)}"
        ) from None


def get_layer_by_table(table: str) -> Layer:
    """按表名反查图层（反馈审核回写时用）。"""
    for layer in LAYERS.values():
        if layer.table == table:
            return layer
    raise KeyError(f"表 {table!r} 没有对应的图层配置，请检查 config.LAYERS")


def all_layers() -> list[Layer]:
    """返回全部图层的列表（顺序即注册顺序，界面按此展示）。"""
    return list(LAYERS.values())


def layer_table_names() -> set[str]:
    """返回允许被 SQLOperation 操作的表名集合（SQL 标识符白名单）。"""
    return {layer.table for layer in LAYERS.values()}


# ---------------------------------------------------------------------------
# 用户反馈
# ---------------------------------------------------------------------------

# 反馈涉及的可修改字段：字段名 → 中文说明（界面下拉、审核回写白名单共用）
FEEDBACK_FIELDS: dict[str, str] = {
    "name": "名称",
    "address": "地址",
    "lnglat": "坐标（经度,纬度）",
    "tel": "电话",
    "type": "类型",
    "other": "其他（仅文字说明）",
}

# 审核状态：状态值 → 中文说明（数据库里存英文，界面和命令行走中文）
FEEDBACK_STATUS: dict[str, str] = {
    "pending": "待审核",
    "approved": "已通过",
    "rejected": "已驳回",
}

FEEDBACK_STATUS_DEFAULT = "pending"

# 单次审核列表最多取多少条
FEEDBACK_LIST_LIMIT = env_int("FEEDBACK_LIST_LIMIT", 200)

# ---------------------------------------------------------------------------
# 应用信息
# ---------------------------------------------------------------------------

APP_TITLE = env_str("APP_TITLE", "小区 / 银行信息采集与查询系统")
APP_ICON = "🏙️"
