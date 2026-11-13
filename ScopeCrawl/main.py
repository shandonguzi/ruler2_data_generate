"""ScopeCrawl 命令行入口。"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import get_args

from scopecrawl.config import ConfigError, load_settings
from scopecrawl.history import DEFAULT_HISTORY_PATH, reset_history
from scopecrawl.models import Language
from scopecrawl.pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ScopeCrawl: 根据 S3/S4 场景检索网页并生成 Markdown 文档",
        epilog="未提供的参数使用 config/config.yaml 中 run 段的对应值。",
    )
    parser.add_argument("--s3", help="大范围场景，例如「天气查询」")
    parser.add_argument("--s4", help="小范围场景，S3 的细化，例如「气象灾害查询」")
    parser.add_argument("--location", help="地点限定，例如「埃及」；可选")
    parser.add_argument(
        "--language",
        choices=get_args(Language),
        help=f"关键词语言：{' / '.join(get_args(Language))}",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="清空跨运行 URL 历史表后重新采集",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    overrides = {
        key: value
        for key, value in vars(args).items()
        if value is not None and key != "restart"
    }

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    # 第三方库的 INFO 日志（HTTP 请求等）会淹没阶段进度，只保留其警告及以上。
    for name in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)

    try:
        settings = load_settings(overrides=overrides)
        if args.restart:
            removed = reset_history(DEFAULT_HISTORY_PATH)
            print("已清空 URL 历史表。" if removed else "URL 历史表为空，无需清空。")
        return run_pipeline(settings)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # 线程池里的在途 HTTP/LLM 请求无法取消，等待线程收尾会拖住退出，直接结束进程。
        print("\n已中断（Ctrl+C），退出。", file=sys.stderr, flush=True)
        sys.stdout.flush()
        os._exit(130)


if __name__ == "__main__":
    raise SystemExit(main())
