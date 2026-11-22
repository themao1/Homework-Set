# -*- coding: utf-8 -*-
"""DataGet.py — 高德 POI 抓取脚本（银行网点 / 小区，可指定城市）

数据管道第 1 环：抓取 → 清洗 → 入库
  - 抓取：高德开放平台 Web 服务 API v5（文本检索）
  - 清洗：clean_row 统一收口（SQLOperation.py）
  - 入库：UPSERT 依赖唯一索引，重复运行不堆积数据
  - 数据库操作全部委托给 SQLOperation，本文件不出现任何 SQL

「哪类数据」由 config.LAYERS 决定：银行（160100）、小区（120300）。
要新增一类数据（学校、医院……），在 config.LAYERS 里加一条即可，本文件不用改。

运行方式（本文件所在目录）：
    python DataGet.py                          # 抓深圳银行（默认，兼容旧用法）
    python DataGet.py --target community        # 抓深圳小区
    python DataGet.py --target all --pages 2    # 两类都抓，各抓前 2 页（调试省配额）
    python DataGet.py --target bank --city 广州  # 换城市只改参数，不改代码

依赖：requirements.txt（requests / python-dotenv / 本项目的 config、SQLOperation）
配置：同目录 .env 写入 AMAP_KEY=<你的高德 Key>
"""

from __future__ import annotations

import argparse
import logging
import time
from contextlib import closing
from typing import Any, Iterable, Sequence

import requests

import config
from SQLOperation import clean_row, create_database, upsert_pois

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# 高德「不可重试」的错误码：Key 无效、配额用尽、权限不足这类错误，重试多少次都没用。
# 把它们和网络抖动区分开，避免在配额已经耗尽时还白白重试 3 次。
FATAL_INFOCODES = {
    "10001",  # Key 不正确或过期
    "10002",  # 没有权限使用相应的服务
    "10003",  # 访问已超出日访问量
    "10005",  # IP 白名单出错
    "10009",  # 用户 Key 与绑定平台不符
    "10044",  # 账号维度日调用量超限
}


def load_api_key() -> str:
    """读取高德 API Key，缺失时当场报错退出（fail fast）。

    Returns:
        有效的 AMAP_KEY 字符串。

    Raises:
        RuntimeError: 环境变量缺失或为空。
    """
    key = (config.AMAP_KEY or "").strip()
    if not key:
        raise RuntimeError(
            "缺少环境变量 AMAP_KEY，请先在 .env 中配置（参考 .env.example）"
        )
    return key


def build_params(
    key: str,
    city: str = config.DEFAULT_CITY,
    keywords: str | None = None,
    amap_types: str | None = None,
    page_size: int = config.PAGE_SIZE,
) -> dict[str, str | int]:
    """组装高德文本检索的公共参数。

    Args:
        key: 高德 API Key。
        city: 检索城市（高德 region 参数）。
        keywords: 检索关键词；None 用调用方给该图层的默认词。
        amap_types: POI 类型编码；None 用图层默认值。
        page_size: 每页条数。

    Returns:
        dict，可直接作为 requests 的 params。
    """
    params: dict[str, str | int] = {
        "key": key,
        "region": city,
        "citylimit": "true" if config.CITY_LIMIT else "false",
        "page_size": int(page_size),
        "extensions": "all",
        "output": "JSON",
    }
    if keywords:
        params["keywords"] = keywords
    if amap_types:
        params["types"] = amap_types
    return params


class _RetryableError(RuntimeError):
    """内部标记：接口报错但值得重试（网络抖动、瞬时限流）。"""


def fetch_page(
    session: requests.Session,
    params: dict[str, Any],
    page_num: int,
) -> list[dict]:
    """抓取高德 POI 接口的第 page_num 页，返回本页原始 POI 列表。

    Args:
        session: 复用的 requests 会话（连接复用，省握手开销）。
        params: build_params 产出的公共参数（本函数会补 page_num）。
        page_num: 页码，从 1 开始。

    Returns:
        本页 POI 的 list[dict]；正常翻到最后一页返回空列表。

    Raises:
        RuntimeError: 接口返回业务错误且不可重试，或重试后仍失败。
        requests.RequestException: 网络层错误（重试后仍失败）。
    """
    last_err: Exception | None = None
    query = dict(params, page_num=page_num)

    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = session.get(
                config.AMAP_TEXT_URL, params=query, timeout=config.REQUEST_TIMEOUT_SEC
            )
            resp.raise_for_status()  # 捕获 HTTP 层异常
            payload = resp.json()
            if payload.get("status") != "1":
                infocode = str(payload.get("infocode") or "")
                message = (
                    f"高德接口业务错误 infocode={infocode} info={payload.get('info')}"
                )
                if infocode in FATAL_INFOCODES:
                    raise RuntimeError(message)  # 不重试，直接上抛
                raise _RetryableError(message)
            return payload.get("pois") or []
        except _RetryableError as exc:
            last_err = exc
        except (requests.RequestException, ValueError) as exc:
            # ValueError 覆盖 JSON 解析失败（接口偶发返回非 JSON）
            last_err = exc
        except RuntimeError as exc:
            # FATAL_INFOCODES 抛出的永久性错误：重试没有意义
            raise

        logger.warning(
            "第 %d 页第 %d/%d 次尝试失败：%s",
            page_num, attempt, config.MAX_RETRIES, last_err,
        )
        if attempt < config.MAX_RETRIES:
            # 指数退避：1s、2s、4s……，且不超过上限
            time.sleep(min(2.0 ** (attempt - 1), 8.0))

    raise RuntimeError(f"第 {page_num} 页连续 {config.MAX_RETRIES} 次失败：{last_err}")


def fetch_all(
    session: requests.Session,
    params: dict[str, Any],
    max_pages: int | None = None,
    label: str = "POI",
) -> list[dict]:
    """分页抓取全部 POI，返回原始记录列表。

    Args:
        session: requests 会话。
        params: 公共检索参数。
        max_pages: 最多抓多少页；None 表示抓到没有数据或触达接口页码上限为止。
        label: 日志里显示的数据类型名称。

    Returns:
        原始 POI 记录列表。

    注意：先把整批数据留在内存再入库，是为了让「抓取失败」和「入库失败」
    两件事解耦；数据量再大时这里应改成边抓边写（回调式），见 on_page 扩展点。
    """
    all_pois: list[dict] = []
    page = 1
    hard_limit = min(config.MAX_PAGE_NUM, max_pages or config.MAX_PAGE_NUM)

    while page <= hard_limit:
        pois = fetch_page(session, params, page)
        if not pois:
            logger.info("[%s] 第 %d 页无数据，抓取结束（共 %d 页）", label, page, page - 1)
            break

        all_pois.extend(pois)
        logger.info("[%s] 已检索 %d 页，累计 %d 条", label, page, len(all_pois))

        if max_pages is not None and page >= max_pages:
            logger.info("[%s] 达到 --pages=%d 上限，停止抓取", label, max_pages)
            break
        page += 1
        time.sleep(config.REQUEST_INTERVAL_SEC)  # 请求节流，别把配额打光

    if page > hard_limit:
        logger.warning(
            "[%s] 已达接口页码上限 %d，后续数据请按区县拆分检索",
            label, hard_limit,
        )
    return all_pois


def pois_to_rows(
    pois: Iterable[dict], source: str = "amap_v5"
) -> tuple[list[dict], int]:
    """把高德原始 POI 清洗成可入库的 dict 列表。

    Args:
        pois: 高德返回的 POI 原始记录。
        source: 数据来源标识，写入 source 列。

    Returns:
        (rows, skipped)：可用记录列表与跳过条数。
    """
    rows: list[dict] = []
    skipped = 0

    for poi in pois:
        location = poi.get("location") or ""
        # 不能让空字符串以 TEXT 形式混进 REAL 列（SQLite 动态类型不会报错）
        if isinstance(location, str) and "," in location:
            lng, lat = location.split(",", 1)
        else:
            lng = lat = None

        # 高德在字段无值时常返回 []，clean_row 里的 _as_text 会统一洗成空串
        cleaned = clean_row(
            poi.get("name"),
            poi.get("address"),
            lng,
            lat,
            poi.get("tel"),
            poi.get("type"),
            source=source,
            city=poi.get("cityname"),
            district=poi.get("adname"),
            adcode=poi.get("adcode"),
        )
        if cleaned is None:
            logger.warning("跳过无效记录（缺名称/坐标）：%s", poi.get("name"))
            skipped += 1
            continue
        rows.append(cleaned)

    return rows, skipped


def save_rows(table: str, rows: list[dict], db_path=None) -> tuple[int, int]:
    """清洗结果增量入库，返回 (新增, 更新)。

    数据库操作全部委托给 SQLOperation，本文件只负责「准备数据」。
    """
    if not rows:
        logger.warning("没有可入库的记录，跳过写库")
        return 0, 0

    with closing(create_database(db_path)) as conn:
        return upsert_pois(conn, table, rows)


def scrape_layer(
    layer: config.Layer,
    city: str = config.DEFAULT_CITY,
    keywords: str | None = None,
    amap_types: str | None = None,
    pages: int | None = None,
    page_size: int = config.PAGE_SIZE,
    db_path=None,
) -> dict[str, Any]:
    """抓取并入库一类 POI，返回本次统计。

    Args:
        layer: 图层配置（决定表名、默认关键词、POI 类型）。
        city: 检索城市。
        keywords: 关键词，缺省用图层默认值。
        amap_types: POI 类型编码，缺省用图层默认值。
        pages: 最多抓取页数；None 抓到底。
        page_size: 每页条数。
        db_path: 覆盖数据库路径（测试用）。

    Returns:
        dict：图层 key/名称、抓取条数、新增、更新、跳过。
    """
    key = load_api_key()
    params = build_params(
        key,
        city=city,
        keywords=keywords or layer.default_keyword,
        amap_types=amap_types or layer.amap_types,
        page_size=page_size,
    )

    logger.info(
        "开始抓取「%s」：城市=%s，关键词=%s，类型=%s，最多 %s 页",
        layer.label, city, params["keywords"], params["types"],
        pages if pages is not None else "不限",
    )

    with requests.Session() as session:
        pois = fetch_all(session, params, max_pages=pages, label=layer.label)

    rows, skipped = pois_to_rows(pois)
    logger.info(
        "清洗完成：可用 %d 条，跳过 %d 条", len(rows), skipped
    )
    inserted, updated = save_rows(layer.table, rows, db_path=db_path)

    return {
        "layer": layer.key,
        "label": layer.label,
        "city": city,
        "fetched": len(pois),
        "usable": len(rows),
        "skipped": skipped,
        "inserted": inserted,
        "updated": updated,
    }


def run(
    targets: Sequence[str] = ("bank",),
    city: str = config.DEFAULT_CITY,
    keywords: str | None = None,
    amap_types: str | None = None,
    pages: int | None = None,
    page_size: int = config.PAGE_SIZE,
    db_path=None,
) -> list[dict[str, Any]]:
    """按图层批量抓取入库（程序入口 main.py 与本文件命令行共用）。

    Args:
        targets: 图层 key 序列，如 ("bank", "community")；写 "all" 表示全部。
        city: 检索城市。
        keywords: 关键词，None 用各图层默认值。
        amap_types: POI 类型编码，None 用各图层默认值。
        pages: 每类最多抓取页数。
        page_size: 每页条数。
        db_path: 覆盖数据库路径。

    Returns:
        每类数据的统计信息列表；某类失败不影响其它类，失败信息写在结果里。

    Raises:
        RuntimeError: 没有指定任何有效图层，或 API Key 缺失。
    """
    keys = list(targets)
    if "all" in keys:
        keys = [layer.key for layer in config.all_layers()]

    layers = [config.get_layer(k) for k in keys]  # 未知 key 直接抛错，避免静默跳过
    if not layers:
        raise RuntimeError("没有指定要抓取的图层，用 --target bank|community|all")

    results: list[dict[str, Any]] = []
    for layer in layers:
        try:
            results.append(
                scrape_layer(
                    layer,
                    city=city,
                    keywords=keywords,
                    amap_types=amap_types,
                    pages=pages,
                    page_size=page_size,
                    db_path=db_path,
                )
            )
        except Exception as exc:  # 一类失败不该把整批任务带崩
            logger.error("抓取「%s」失败：%s", layer.label, exc)
            results.append(
                {"layer": layer.key, "label": layer.label, "error": str(exc)}
            )

    # 汇总日志：一眼看出这次到底入库了多少
    for item in results:
        if "error" in item:
            logger.error("❌ %s：%s", item["label"], item["error"])
        else:
            logger.info(
                "✅ %s：抓取 %d 条，可用 %d 条，新增 %d 条，更新 %d 条，跳过 %d 条",
                item["label"], item["fetched"], item["usable"],
                item["inserted"], item["updated"], item["skipped"],
            )
    return results


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口：python DataGet.py [--target ...] [--city ...] [--pages N]"""
    parser = argparse.ArgumentParser(description="高德 POI 抓取（增量入库）")
    parser.add_argument(
        "--target",
        default="bank",
        help="抓取对象：bank / community / all，或用逗号分隔多个（默认 bank）",
    )
    parser.add_argument(
        "--city", default=config.DEFAULT_CITY, help=f"检索城市（默认 {config.DEFAULT_CITY}）"
    )
    parser.add_argument("--keywords", default=None, help="检索关键词（默认用图层配置）")
    parser.add_argument("--types", default=None, help="高德 POI 类型编码（默认用图层配置）")
    parser.add_argument(
        "--pages", type=int, default=None, help="最多抓取页数（调试用；缺省抓全部页）"
    )
    parser.add_argument(
        "--page-size", type=int, default=config.PAGE_SIZE, help="每页条数（高德最大 25）"
    )
    args = parser.parse_args(argv)

    targets = [t.strip() for t in args.target.split(",") if t.strip()]
    try:
        results = run(
            targets=targets,
            city=args.city,
            keywords=args.keywords,
            amap_types=args.types,
            pages=args.pages,
            page_size=args.page_size,
        )
    except (RuntimeError, KeyError) as exc:
        logger.error("抓取中止：%s", exc)
        return 1

    failed = [item for item in results if "error" in item]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
