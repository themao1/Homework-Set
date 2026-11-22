# -*- coding: utf-8 -*-
"""main.py — 程序统一入口

一个入口管全部操作，避免「要跑哪个脚本、参数怎么传」靠口头传：

    python main.py app                  # 启动前端页面（Streamlit）
    python main.py scrape --target all  # 抓取银行 + 小区数据（高德 API）
    python main.py export-map --layer all   # 导出静态地图 HTML
    python main.py db status            # 查看库结构 / 数据量 / 待审核反馈
    python main.py db migrate           # 建表与结构迁移（幂等）
    python main.py review list          # 查看待审核的用户反馈
    python main.py review show 3        # 看某条反馈详情
    python main.py review approve 3 --apply   # 审核通过（可按建议值回写，需二次确认）
    python main.py review reject 3 --note "证据不足"  # 审核驳回

设计说明：
  - 本文件只做「参数解析 + 调度」，不写 SQL、不 import sqlite3，
    数据操作一律通过 SQLOperation，前端渲染一律在 bank_map_app.py；
  - 每个子命令返回退出码（0 成功、1 失败），方便以后接 CI / 定时任务。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Any, Sequence

import config
import SQLOperation

APP_PATH = config.BASE_DIR / "bank_map_app.py"


# ---------------------------------------------------------------------------
# app：启动前端
# ---------------------------------------------------------------------------


def cmd_app(args: argparse.Namespace) -> int:
    """用 streamlit 启动前端页面。

    通过 `python -m streamlit` 启动而不是裸 `streamlit` 命令：
    保证用的是当前解释器环境里装的那一份，不会因为 PATH 指到别的 Python 而报缺包。
    """
    try:
        import streamlit  # noqa: F401  仅用于提前发现「没装依赖」
    except ImportError:
        print("未安装 streamlit，请先执行：pip install -r requirements.txt")
        return 1

    if not APP_PATH.exists():
        print(f"找不到前端文件：{APP_PATH}")
        return 1

    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(APP_PATH),
        "--server.port",
        str(args.port),
        "--server.address",
        args.address,
        # 首次启动的「输入邮箱」问候对本地 demo 没意义，且没有终端时会直接退出
        "--server.showEmailPrompt=false",
    ]
    if args.no_browser:
        # 不加 --no-browser 时交给 streamlit 自动打开浏览器；服务器/脚本环境用这个开关
        command.append("--server.headless=true")

    print(f"启动前端：http://{args.address}:{args.port}（Ctrl+C 停止）")
    try:
        return subprocess.call(command, cwd=str(config.BASE_DIR))
    except KeyboardInterrupt:  # 用户 Ctrl+C 属于正常退出，不打红叉
        return 0


# ---------------------------------------------------------------------------
# scrape：抓取数据
# ---------------------------------------------------------------------------


def cmd_scrape(args: argparse.Namespace) -> int:
    """抓取高德 POI 并增量入库（委托 DataGet.run）。"""
    import DataGet  # 延迟导入：不抓数据的人不用加载 requests

    targets = [item.strip() for item in args.target.split(",") if item.strip()]
    try:
        results = DataGet.run(
            targets=targets,
            city=args.city,
            keywords=args.keywords,
            amap_types=args.types,
            pages=args.pages,
            page_size=args.page_size,
        )
    except (RuntimeError, KeyError) as exc:
        print(f"抓取中止：{exc}")
        return 1

    print("\n抓取结果：")
    for item in results:
        if "error" in item:
            print(f"  [失败] {item['label']}：{item['error']}")
        else:
            print(
                f"  {item['label']}：抓取 {item['fetched']} 条，"
                f"新增 {item['inserted']} 条，更新 {item['updated']} 条，"
                f"跳过 {item['skipped']} 条"
            )
    return 1 if any("error" in item for item in results) else 0


# ---------------------------------------------------------------------------
# export-map：导出静态地图
# ---------------------------------------------------------------------------


def cmd_export_map(args: argparse.Namespace) -> int:
    """导出静态地图 HTML（委托 folium_map.export）。"""
    import folium_map

    keys = (
        [layer.key for layer in config.all_layers()]
        if args.layer == "all"
        else [args.layer]
    )
    failures = 0
    for key in keys:
        # --out 只对单图层有意义；多层导出时在文件名后加表名，避免互相覆盖
        out = None
        if args.out:
            base = Path(args.out)
            out = (
                base
                if len(keys) == 1
                else base.with_name(f"{base.stem}_{config.get_layer(key).table}{base.suffix}")
            )
        try:
            path = folium_map.export(key, out)
            print(f"已导出 → {path}")
        except (FileNotFoundError, KeyError) as exc:
            print(f"导出「{key}」失败：{exc}")
            failures += 1
        except OSError as exc:
            print(f"写入文件失败（{exc.filename or out}）：{exc.strerror or exc}")
            failures += 1
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# db：库结构与数据概览
# ---------------------------------------------------------------------------


def cmd_db(args: argparse.Namespace) -> int:
    """查看库状态或执行结构迁移（幂等）。"""
    if args.action == "migrate":
        # migrate 允许库不存在（建新库），status 不允许（fail fast）
        with closing(SQLOperation.create_database()) as conn:
            print(SQLOperation.format_status(conn))
        return 0

    if not config.DB_PATH.exists():
        print(f"未找到数据库：{config.DB_PATH}")
        print("   先运行：python main.py scrape --target all")
        return 1
    with closing(SQLOperation.get_connection()) as conn:
        print(SQLOperation.format_status(conn))
    return 0


# ---------------------------------------------------------------------------
# review：人工审核用户反馈
# ---------------------------------------------------------------------------


def _pad(text: str, width: int) -> str:
    """按「显示宽度」补空格。

    中文在等宽终端里占两格，用 str.ljust 会算错列宽导致表格对不齐。
    """
    display = sum(2 if ord(char) > 0x2E80 else 1 for char in text)
    return text + " " * max(0, width - display)


def _feedback_line(item: dict[str, Any]) -> str:
    """把一条反馈压成一行文本（列表展示用）。"""
    layer = config.get_layer(item["target_type"])
    field = config.FEEDBACK_FIELDS.get(item["field_name"], item["field_name"])
    status = config.FEEDBACK_STATUS.get(item["status"], item["status"])
    target = f"#{item['target_id']} {item['target_name'] or ''}".strip()
    desc = (item["error_desc"] or "").replace("\n", " ")[:40]
    return (
        f"{_pad('#' + str(item['id']), 6)}"
        f"{_pad(item['created_at'][5:16], 13)}"
        f"{_pad(layer.label, 10)}"
        f"{_pad(target, 30)}"
        f"{_pad(field, 18)}"
        f"{_pad(status, 9)}"
        f"{desc}"
    )


def cmd_review_list(args: argparse.Namespace) -> int:
    """列出反馈（默认只看待审核）。"""
    status = None if args.status == "all" else args.status
    with closing(SQLOperation.get_connection()) as conn:
        items = SQLOperation.list_feedback(conn, status=status, limit=args.limit)
        stats = SQLOperation.feedback_stats(conn)

    summary = "，".join(
        f"{config.FEEDBACK_STATUS[key]} {count}" for key, count in stats.items()
    )
    print(f"反馈统计：{summary}\n")
    if not items:
        print("（没有符合条件的反馈）")
        return 0
    for item in items:
        print(_feedback_line(item))
    return 0


def cmd_review_show(args: argparse.Namespace) -> int:
    """查看单条反馈的完整内容。"""
    with closing(SQLOperation.get_connection()) as conn:
        item = SQLOperation.get_feedback(conn, args.id)
    if item is None:
        print(f"反馈 #{args.id} 不存在")
        return 1

    layer = config.get_layer(item["target_type"])
    print(f"反馈 #{item['id']}")
    print(f"  状态    ：{config.FEEDBACK_STATUS.get(item['status'], item['status'])}")
    print(f"  提交时间：{item['created_at']}")
    print(f"  对象    ：{layer.label} #{item['target_id']} {item['target_name'] or ''}")
    print(f"  字段    ：{config.FEEDBACK_FIELDS.get(item['field_name'], item['field_name'])}")
    print(f"  错误描述：{item['error_desc']}")
    print(f"  建议值  ：{item['suggested_value'] or '（未填写）'}")
    print(f"  联系方式：{item['contact'] or '（未填写）'}")
    if item["reviewed_at"]:
        print(f"  审核人  ：{item['reviewer'] or '—'}（{item['reviewed_at']}）")
        print(f"  审核备注：{item['review_note'] or '—'}")
    return 0


def _confirm(prompt: str, assume_yes: bool) -> bool:
    """交互式二次确认：非交互环境（管道/定时任务）必须显式加 --yes。"""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("当前不是交互终端，回写前请加 --yes 明确确认")
        return False
    return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}


def cmd_review_approve(args: argparse.Namespace) -> int:
    """审核通过；--apply 时把建议值回写到业务记录（需二次确认）。"""
    with closing(SQLOperation.get_connection()) as conn:
        item = SQLOperation.get_feedback(conn, args.id)
        if item is None:
            print(f"反馈 #{args.id} 不存在")
            return 1
        if item["status"] != config.FEEDBACK_STATUS_DEFAULT:
            print(
                f"反馈 #{args.id} 已经是"
                f"「{config.FEEDBACK_STATUS.get(item['status'], item['status'])}」，不能重复审核"
            )
            return 1

        if args.apply:
            if not (item["suggested_value"] or "").strip():
                print("该反馈没有填写建议值，无法回写；请去掉 --apply，或先人工修正数据")
                return 1
            # 先预检格式再让用户确认，避免「点了确认才发现坐标写错」
            try:
                SQLOperation.validate_feedback_value(
                    item["field_name"], item["suggested_value"]
                )
            except ValueError as exc:
                print(f"建议值无法回写：{exc}")
                return 1
            field = config.FEEDBACK_FIELDS.get(item["field_name"], item["field_name"])
            print(
                f"即将把「{field}」改为：{item['suggested_value']}"
                f"（记录 #{item['target_id']} {item['target_name'] or ''}）"
            )
            if not _confirm("确认回写？", args.yes):
                print("已取消，未做任何修改")
                return 0

        try:
            result = SQLOperation.review_feedback(
                conn,
                args.id,
                status="approved",
                reviewer=args.reviewer,
                note=args.note,
                apply_value=args.apply,
            )
        except (LookupError, ValueError) as exc:
            print(f"审核失败：{exc}")
            return 1

    print(f"反馈 #{args.id} 已通过（审核人：{args.reviewer or '未署名'}）")
    if result["applied"]:
        changes = "，".join(f"{k}={v}" for k, v in result["applied"].items())
        print(f"   已回写到 {result['target_table']}：{changes}")
    else:
        print("   未回写数据（只更新了反馈状态）")
    return 0


def cmd_review_reject(args: argparse.Namespace) -> int:
    """审核驳回：只改状态并记录原因，不改数据。"""
    with closing(SQLOperation.get_connection()) as conn:
        try:
            SQLOperation.review_feedback(
                conn,
                args.id,
                status="rejected",
                reviewer=args.reviewer,
                note=args.note,
                apply_value=False,
            )
        except LookupError:
            print(f"反馈 #{args.id} 不存在")
            return 1
        except ValueError as exc:
            print(f"驳回失败：{exc}")
            return 1
    print(f"反馈 #{args.id} 已驳回（审核人：{args.reviewer or '未署名'}）")
    return 0


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """构建命令行解析器（帮助文本用中文，`python main.py -h` 即可看懂）。"""
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="小区 / 银行信息采集与查询系统 —— 统一入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python main.py app\n"
            "  python main.py scrape --target all --city 深圳 --pages 2\n"
            "  python main.py review list\n"
            "  python main.py review approve 3 --apply --yes --reviewer 张三\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="子命令")

    # app
    p_app = sub.add_parser("app", help="启动前端页面（Streamlit）")
    p_app.add_argument("--port", type=int, default=8501, help="端口（默认 8501）")
    p_app.add_argument(
        "--address", default="localhost", help="监听地址（默认 localhost，仅本机可访问）"
    )
    p_app.add_argument(
        "--no-browser", action="store_true", help="不自动打开浏览器（服务器/无界面环境用）"
    )
    p_app.set_defaults(func=cmd_app)

    # scrape
    p_scrape = sub.add_parser("scrape", help="抓取高德 POI 并增量入库")
    p_scrape.add_argument(
        "--target", default="bank",
        help="抓取对象：bank / community / all，或逗号分隔多个（默认 bank）",
    )
    p_scrape.add_argument("--city", default=config.DEFAULT_CITY, help=f"城市（默认 {config.DEFAULT_CITY}）")
    p_scrape.add_argument("--keywords", default=None, help="检索关键词（默认用图层配置）")
    p_scrape.add_argument("--types", default=None, help="高德 POI 类型编码（默认用图层配置）")
    p_scrape.add_argument("--pages", type=int, default=None, help="最多抓取页数（调试用）")
    p_scrape.add_argument(
        "--page-size", type=int, default=config.PAGE_SIZE, help="每页条数（高德最大 25）"
    )
    p_scrape.set_defaults(func=cmd_scrape)

    # export-map
    p_export = sub.add_parser("export-map", help="导出静态地图 HTML")
    p_export.add_argument("--layer", default="bank", help="bank / community / all（默认 bank）")
    p_export.add_argument("--out", default=None, help="输出路径（仅单层导出时有效）")
    p_export.set_defaults(func=cmd_export_map)

    # db
    p_db = sub.add_parser("db", help="数据库结构/数据概览")
    p_db.add_argument("action", nargs="?", default="status", choices=["status", "migrate"],
                      help="status=查看（默认）；migrate=建表/迁移（幂等）")
    p_db.set_defaults(func=cmd_db)

    # review
    p_review = sub.add_parser("review", help="人工审核用户反馈")
    review_sub = p_review.add_subparsers(dest="review_action", metavar="操作")

    p_list = review_sub.add_parser("list", help="列出反馈（默认待审核）")
    p_list.add_argument("--status", default=config.FEEDBACK_STATUS_DEFAULT,
                        choices=[*config.FEEDBACK_STATUS, "all"],
                        help="按状态过滤，all=全部")
    p_list.add_argument("--limit", type=int, default=50, help="最多显示条数（默认 50）")
    p_list.set_defaults(func=cmd_review_list)

    p_show = review_sub.add_parser("show", help="查看单条反馈详情")
    p_show.add_argument("id", type=int, help="反馈编号")
    p_show.set_defaults(func=cmd_review_show)

    p_approve = review_sub.add_parser("approve", help="审核通过")
    p_approve.add_argument("id", type=int, help="反馈编号")
    p_approve.add_argument("--apply", action="store_true", help="把建议值回写到业务记录")
    p_approve.add_argument("--yes", action="store_true", help="跳过二次确认（非交互环境必填）")
    p_approve.add_argument("--reviewer", default="", help="审核人（留痕）")
    p_approve.add_argument("--note", default="", help="审核备注")
    p_approve.set_defaults(func=cmd_review_approve)

    p_reject = review_sub.add_parser("reject", help="审核驳回")
    p_reject.add_argument("id", type=int, help="反馈编号")
    p_reject.add_argument("--reviewer", default="", help="审核人（留痕）")
    p_reject.add_argument("--note", default="", help="驳回原因")
    p_reject.set_defaults(func=cmd_review_reject)

    p_review.set_defaults(func=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """入口分发：把子命令交给对应函数，返回退出码。"""
    config.make_stdout_safe()  # Windows 控制台是 GBK 时，别让中文输出把脚本打崩
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    # `review` 后面不带具体操作时，打印 help 而不是静默什么都不做
    if args.command == "review" and not getattr(args, "review_action", None):
        parser.parse_args(["review", "-h"])
        return 0

    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
