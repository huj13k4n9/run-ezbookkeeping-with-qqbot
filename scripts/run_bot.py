#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QQ 记账机器人的常驻服务入口（systemd / docker 拉起）。

做的事只有三件：

1. 连上网关，收单聊消息
2. 把有文本的消息交给 pi agent（后台线程，不阻塞接收循环）
3. 把 agent 的回复发回 QQ

纯图片消息**不会**启 agent、也不会回复 —— 它只被归档落盘，
等用户引用它并给出说明时才处理（见 agent/AGENTS.md）。

运行：

    python scripts/run_bot.py              # 正常常驻
    python scripts/run_bot.py --check      # 只做自检，不连网关
    python scripts/run_bot.py --version

注意：以前这里没有任何参数解析，随手敲的 `--check` 会被**静默忽略**并真的
把机器人拉起来。现在未知参数会直接报错退出。
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqbot import BotConfig, Event, QQBot, __version__  # noqa: E402

log = logging.getLogger("run_bot")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_bot.py",
        description="QQ 记账机器人（单聊）。不传参数就是常驻运行。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  %(prog)s                常驻运行\n"
            "  %(prog)s --check        只做自检（不连 QQ，不花 token）\n"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只加载配置并做自检，打印摘要后退出；不连网关、不启 agent",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        metavar="PATH",
        help="指定 .env 路径（默认 ./.env）；传空字符串表示不读文件",
    )
    parser.add_argument("--version", action="version", version=f"qqbot {__version__}")
    return parser


def _print_startup(bot: QQBot) -> list[str]:
    cfg = bot.config
    print("=" * 64)
    print("  QQ 记账机器人")
    print("=" * 64)
    print(f"  接口地址     : {cfg.api_base}")
    print(f"  引用索引     : {cfg.ref_index_path if cfg.ref_index_enabled else '已禁用'}")
    print(f"  附件落盘     : {cfg.auto_download_dir or '不落盘（引用回查拿不到本地文件）'}")
    print(f"  agent        : {'启用' if cfg.agent_enabled else '禁用'}")
    if cfg.agent_enabled:
        print(f"    cwd        : {bot.agent.cwd}")
        print(f"    session    : {cfg.session_prefix}-<hash>-<{cfg.session_rotation}>")
        print(f"    工具       : {cfg.agent_tools}")
        print(f"    模型       : {cfg.agent_model or '(pi 默认)'}")
        print(f"    并发/超时  : {cfg.agent_max_concurrency} / {cfg.agent_timeout:.0f}s")
        print(f"    确认阈值   : {cfg.confirm_amount_threshold or '关闭'}")
    print(f"  白名单       : {cfg.allowed_openids or '不限制'}")
    print("=" * 64)

    # QQ bot 代码与 agent 目录通常不在一起，配错很常见 —— 这里直接报到脸上
    problems: list[str] = []
    if cfg.agent_enabled:
        problems = bot.agent.validate()
        if problems:
            for item in problems:
                print(f"  [警告] {item}")
            print("  [警告] agent 将无法正常工作，请修正上面的配置\n")
        else:
            print("  [自检] agent 运行环境就绪\n")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        # env_file="" / None 交给 from_env 当“不读文件”处理
        bot = QQBot(env_file=args.env_file or None)
    except Exception as exc:  # noqa: BLE001
        # 配置错误是最常见的首跑故障，给一句人话而不是 traceback
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    problems = _print_startup(bot)

    if args.check:
        bot.stop()
        if problems:
            log.error("自检未通过（%d 项）", len(problems))
            return 1
        log.info("自检通过")
        return 0

    # ---------------------------------------------------------- 事件处理
    @bot.on("C2C_MESSAGE_CREATE")
    def on_c2c(event: Event) -> None:
        # 非阻塞：内部丢到线程池，网关接收循环立刻返回
        bot.dispatch_to_agent(event)

    @bot.on("*")
    def on_any(event: Event) -> None:
        if event.name == "C2C_MESSAGE_CREATE":
            return
        # 只订阅了单聊事件；万一有别的类型过来，记一笔方便排查
        log.info("收到未处理事件: %s seq=%s", event.name, event.seq)

    # ---------------------------------------------------------- 优雅退出
    shutting_down = threading.Event()

    def handle_signal(signum, _frame) -> None:
        if shutting_down.is_set():
            return
        shutting_down.set()
        log.info("收到信号 %s，开始关闭…", signum)
        bot.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):  # 非主线程 / 平台不支持
            log.debug("注册信号 %s 失败", sig)

    # ---------------------------------------------------------- 主循环
    try:
        bot.run()  # 阻塞，内部断线自动重连
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C")
    except Exception as exc:  # noqa: BLE001
        log.exception("机器人异常退出: %s", exc)
        return 1
    finally:
        bot.stop()
    log.info("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
