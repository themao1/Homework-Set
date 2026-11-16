# -*- coding: utf-8 -*-
"""
main.py — 高德银行 POI 抓取脚本

数据管道第 1 环：抓取 → 清洗 → 入库（banks.db）
  - 抓取：高德开放平台 Web 服务 API v5
  - 清洗：clean_row 统一收口（SQLOperation.py）
  - 入库：UPSERT 依赖唯一索引，重复运行不堆积数据

运行方式（本文件所在目录）：
    python main.py            # 全量分页抓取深圳银行并增量入库
    python main.py --pages 2  # 只抓前 2 页（调试用，省配额）

依赖：requirements.txt（requests / python-dotenv / SQLOperation.py 本地模块）
配置：同目录 .env 写入 AMAP_KEY=<你的高德 Key>
"""

from __future__ import annotations#可以不使用（开发版本python3.14）

import argparse#CLI构建模块
import logging
import os
import time
from contextlib import closing#close资源兜底

import requests
from dotenv import load_dotenv

from SQLOperation import clean_row, create_database, upsert_banks

# ---------------------------------------------------------------------------
# 日志：print 不是日志——没有级别、没有时间戳、出事后无法回溯。
# 数据管道项目最常问的「昨晚定时任务跑没跑、跑到哪一步挂了」全靠它回答。
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置区：所有「可能变的东西」集中一处
# ---------------------------------------------------------------------------
#可改进：keywords、region、types可做成CLI参数或环境变量。

URL = "https://restapi.amap.com/v5/place/text"

DEFAULT_PARAMS: dict[str, str | int] = {
    "keywords": "银行",
    "types": "160100",  # 160100=银行  120300=住宅区（小区数据源扩展位）
    "region": "深圳",
    "citylimit": "true",
    "page_size": 25,
    "page_num": 1,
    "extensions": "all",
    "output": "JSON",
}

# 每页请求间隔（秒）：请求节流，
REQUEST_INTERVAL_SEC = 1.0

# 响应体 status != "1" 时最多重试次数（网络抖动兜底）
MAX_RETRIES = 3


def load_api_key() -> str:
    """读取高德 API Key，缺失时当场报错退出（fail fast）。

    Returns:
        有效的 AMAP_KEY 字符串。

    Raises:
        RuntimeError: 环境变量缺失或为空。
    """
    load_dotenv()  # 加载同目录 .env
    key = (os.getenv("AMAP_KEY") or "").strip()
    if not key:
        raise RuntimeError("缺少环境变量 AMAP_KEY，请先在 .env 中配置（参考 .env.example）")
    return key


def fetch_page(session: requests.Session, key: str, page_num: int) -> list[dict]:
    """抓取高德 POI 接口的第 page_num 页，返回本页原始 POI 列表。

    可调整事项：
        1.线性退避可改成指数退避。
        2.将可重试错误与永久性错误区分开。
        3.没有JSONDecodeError接口。

    Args:
        session: 复用的 requests 会话（连接复用，对目标服务更友好）。
        key: 高德 API Key。
        page_num: 页码，从 1 开始。

    Returns:
        本页 POI 的 list[dict]；正常翻到最后一页返回空列表。

    Raises:
        RuntimeError: 接口返回业务错误（重试后仍失败）。
        requests.RequestException: 网络层错误（重试后仍失败）。
    """
    params = dict(DEFAULT_PARAMS, key=key, page_num=page_num)
    last_err: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(URL, params=params, timeout=10)
            resp.raise_for_status()  #捕获HTTP层异常
            payload = resp.json()
            if payload.get("status") != "1":
                raise RuntimeError(
                    f"高德接口业务错误 infocode={payload.get('infocode')} "
                    f"info={payload.get('info')}"
                )
            return payload.get("pois") or []
        except (requests.RequestException, RuntimeError) as exc:
            last_err = exc
            logger.warning("第 %d 页第 %d/%d 次尝试失败：%s",
                           page_num, attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(REQUEST_INTERVAL_SEC * attempt)  # 递增退避

    raise RuntimeError(f"第 {page_num} 页连续 {MAX_RETRIES} 次失败：{last_err}")


def fetch_all(session: requests.Session, key: str, max_pages: int | None = None) -> list[dict]:
    """分页抓取全部银行 POI，返回原始记录列表。

    可调整事项：
        1.可改为边抓边写以避免在产品检索范围扩张时爆内存。

    Args:
        max_pages: 最多抓多少页；None 表示抓到没有数据为止。调试时限制页数可省配额。
    """
    all_pois: list[dict] = []
    page = 1
    while True:
        pois = fetch_page(session, key, page)
        if not pois:
            logger.info("第 %d 页无数据，抓取结束（共 %d 页）", page, page - 1)
            break
        all_pois.extend(pois)
        logger.info("已检索 %d 页，累计 %d 条", page, len(all_pois))
        if max_pages is not None and page >= max_pages:
            logger.info("达到 --pages=%d 上限，停止抓取", max_pages)
            break
        page += 1
        time.sleep(REQUEST_INTERVAL_SEC)  # 请求节流
    return all_pois


def save_banks(pois: list[dict]) -> tuple[int, int]:
    """清洗一批原始 POI 并增量入库，返回 (新增, 更新)。

    可改进处：
        1.rows为空可提前返回。

    数据库操作全部委托给 SQLOperation，

    Returns:
        (inserted, updated) 元组。
    """
    rows: list[dict] = []
    skipped = 0
    for p in pois:
        location = p.get("location") or ""
        # 不能让空字符串以 TEXT 形式混进 REAL 列（SQLite 动态类型不会报错）
        if "," not in location:
            logger.warning("跳过无坐标记录：%s", p.get("name"))
            skipped += 1
            continue
        lng, lat = location.split(",", 1)
        #清洗收口：坐标转 float、tel 归一化、空值处理全在 clean_row 里
        cleaned = clean_row(
            p.get("name"), p.get("address"), lng, lat, p.get("tel"), p.get("type")
        )
        if cleaned is None:
            logger.warning("跳过无效记录：%s", p.get("name"))
            skipped += 1
            continue
        rows.append(cleaned)

    logger.info("清洗完成：可用 %d 条，跳过 %d 条", len(rows), skipped)

    # contextlib.closing：无论 rows 是否为空、中途是否异常，连接都保证释放（A6）
    with closing(create_database()) as conn:
        inserted, updated = upsert_banks(conn, rows)
    return inserted, updated


def main(max_pages: int | None = None) -> None:
    """入口：抓取深圳银行 POI 并增量入库。"""
    key = load_api_key()
    with requests.Session() as session:
        pois = fetch_all(session, key, max_pages=max_pages)

    if not pois:
        # 接口正常但 0 条——和「接口挂了」在这里显式区分开（A3 的另一半）
        logger.warning("本次抓取结果为 0 条，请检查关键词/区域参数或数据源配额")
        return

    inserted, updated = save_banks(pois)
    logger.info("入库完成：新增 %d 条，更新 %d 条（重复数据被唯一约束挡下）",
                inserted, updated)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="高德银行 POI 抓取（增量入库）")
    parser.add_argument("--pages", type=int, default=None,
                        help="最多抓取页数（调试用；缺省抓全部页）")
    args = parser.parse_args()
    main(max_pages=args.pages)
