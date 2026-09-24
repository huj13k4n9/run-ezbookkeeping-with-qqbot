#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QQ 机器人接入测试工具。

覆盖四条链路，建议按顺序验证：

    1) python scripts/qqbot_test.py token          # 鉴权：能否拿到 access_token
    2) python scripts/qqbot_test.py gateway        # 鉴权 + 网关接入点 + /users/@me
    3) python scripts/qqbot_test.py listen         # WebSocket 消息订阅（发消息给机器人看是否收到）
    4) python scripts/qqbot_test.py send-c2c <openid> "Hello"   # 主动发送单聊消息

listen 模式下默认会自动回复收到的消息，等于同时验证了「订阅」和「被动回复」两条链路。

每条收到的消息都会详细打印：message_type、文本、附件（content_type/文件名/大小/尺寸/URL）、
图片、语音（ASR 文本 + WAV 地址）、引用内容。

    --raw                 额外打印完整原始 JSON（用来验证「引用图片是否回传附件」）
    --download-dir DIR    把附件落盘到 DIR 并打印路径

配置来源：命令行参数 > 环境变量 > .env 文件（默认读取当前目录 .env）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time

# 让 Windows 控制台也能正常输出中文/emoji
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from qqbot import (  # noqa: E402
    BotConfig,
    Event,
    QQBot,
    QQBotAPI,
    QQBotAPIError,
    QQBotAuthError,
    describe,
)

log = logging.getLogger("qqbot.test")


def build_config(args) -> BotConfig:
    overrides = {
        "sandbox": True if args.sandbox else None,
        "log_level": "DEBUG" if args.debug else None,
        # --download-dir 同时驱动自动落盘，落盘后的路径会写入引用索引
        "auto_download_dir": getattr(args, "download_dir", None),
    }
    if args.intents is not None:
        overrides["intents"] = args.intents
    return BotConfig.from_env(args.env_file, **overrides)


def banner(text: str) -> None:
    print("\n" + "=" * 62)
    print(f"  {text}")
    print("=" * 62)


# --------------------------------------------------------------------------- 1) token
def cmd_token(args) -> int:
    banner("鉴权测试：获取 access_token")
    config = build_config(args)
    api = QQBotAPI(config)
    try:
        token = api.get_token(force=True)
    except QQBotAuthError as exc:
        print(f"\n[失败] {exc}")
        return 1

    from qqbot import mask

    print(f"AppID        : {config.app_id}")
    print(f"接口地址     : {config.token_url}")
    print(f"access_token : {mask(token)}  (长度 {len(token)})")
    print(f"剩余有效期   : {int(api.tokens.expires_in)} 秒")
    print("\n[通过] 鉴权链路正常")
    return 0


# --------------------------------------------------------------------------- 2) gateway
def cmd_gateway(args) -> int:
    banner("网关测试：鉴权 + /gateway + /users/@me")
    config = build_config(args)
    api = QQBotAPI(config)

    try:
        token = api.get_token(force=True)
    except QQBotAuthError as exc:
        print(f"\n[失败] 鉴权失败: {exc}")
        return 1
    print(f"[1/3] 鉴权通过，token 长度 {len(token)}")

    url = api.get_gateway()
    print(f"[2/3] 网关接入点: {url}")

    try:
        me = api.me()
    except QQBotAPIError as exc:
        print(f"[3/3] /users/@me 调用失败（不影响网关连接）: {exc}")
    else:
        print(f"[3/3] 机器人身份: {me.get('username')} (id={me.get('id')})")
        print(f"      已验证: {me.get('verify')}  私域: {me.get('private')}")

    print(f"\n本次订阅 intents = {config.intents} ({describe(config.intents)})")
    print("\n[通过] 网关接入点获取正常")
    return 0


# --------------------------------------------------------------------------- 3) listen
#: message_type 取值 -> 可读名称
MESSAGE_TYPE_NAMES = {
    0: "纯文本",
    3: "ARK卡片",
    101: "并行消息",
    102: "聊天记录",
    103: "引用消息",
}


def _fmt_size(size) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _brief_url(url, limit: int = 96) -> str:
    text = str(url or "")
    if not text:
        return "(无)"
    return text if len(text) <= limit else f"{text[:limit]}…(+{len(text) - limit})"


#: REFIDX 的尾部长度 —— 实测这是**会话级常量**，跨消息/跨时间都相同，
#: 因此不能用「字符串很像」判断是不是同一条消息。
REFIDX_TAIL_LEN = 106


def _split_refidx(value) -> tuple[str, str]:
    """把 REFIDX 拆成 (消息相关前缀, 会话级尾巴)。"""
    text = str(value or "")
    if text.startswith("REFIDX_") and len(text) > 7 + REFIDX_TAIL_LEN:
        cut = len(text) - REFIDX_TAIL_LEN
        return text[:cut], text[cut:]
    return text, ""


def _dump_attachment(att: dict, index: int, *, indent: str = "      ") -> list[str]:
    """把一个附件展开成多行日志。"""
    content_type = att.get("content_type") or "?"
    name = att.get("filename") or "(无文件名)"
    pad = indent + "    "
    lines = [
        f"{indent}[{index}] content_type={content_type}  filename={name}  size={_fmt_size(att.get('size'))}"
    ]

    if att.get("width") or att.get("height"):
        lines.append(f"{pad}width/height: {att.get('width')}x{att.get('height')}")
    lines.append(f"{pad}url         : {_brief_url(att.get('url'))}")
    if att.get("content"):
        lines.append(f"{pad}content     : {att['content']!r}")
    if att.get("voice_wav_url"):
        lines.append(f"{pad}voice_wav   : {_brief_url(att.get('voice_wav_url'))}")
    if att.get("asr_refer_text"):
        lines.append(f"{pad}asr_text    : {att['asr_refer_text']!r}")

    # 把没识别到的字段也打出来，便于发现新字段
    known = {
        "content_type", "filename", "size", "width", "height", "url",
        "content", "voice_wav_url", "asr_refer_text",
    }
    others = {k: v for k, v in att.items() if k not in known}
    if others:
        lines.append(f"{pad}其它字段     : {others}")
    return lines


def format_event(event: Event, *, show_raw: bool = False) -> None:
    """打印一条事件的完整信息：类型 + 文本 + 附件/图片/语音内容。"""
    message_type = event.message_type
    type_name = MESSAGE_TYPE_NAMES.get(message_type, "未知")

    print("\n" + "-" * 72)
    print(f"[事件] {event.name}   seq={event.seq}   event_id={event.id}")
    print(f"  message_type : {message_type} ({type_name})")
    print(f"  msg_id       : {event.msg_id}   (被动回复用)")
    if event.msg_idx:
        print(f"  msg_idx      : {event.msg_idx}")
    if event.ref_msg_idx:
        print(f"  ref_msg_idx  : {event.ref_msg_idx}")
    if event.auth_token:
        print(f"  auth_token   : {event.auth_token}")

    who = event.author.get("username") or "(无昵称)"
    print(f"  发送者        : {who}   openid={event.user_openid}")
    if event.group_openid:
        print(f"  group_openid : {event.group_openid}")
    print(f"  时间          : {event.timestamp}")
    print(f"  文本内容      : {event.content!r}")

    # ---- 附件 ----
    attachments = event.attachments
    if attachments:
        print(f"  附件 ({len(attachments)}):")
        for index, att in enumerate(attachments):
            for line in _dump_attachment(att, index):
                print(line)
    else:
        print("  附件         : 无")

    # ---- 分类汇总 ----
    print("  分类统计      : "
          f"图片={len(event.images)}  语音={1 if event.voice else 0}  "
          f"其它文件={len(event.files)}  引用图片={len(event.quoted_images)}")

    # ---- 语音专有字段 ----
    if event.voice:
        print("  语音内容      :")
        print(f"      asr_text    : {event.asr_text!r}")
        print(f"      voice_wav   : {_brief_url(event.voice.get('voice_wav_url'))}")

    # ---- 引用内容 ----
    if event.is_quoted or event.quoted_attachments or event.msg_elements or event.parallel_nodes:
        print(f"  引用内容 (msg_elements={len(event.msg_elements)}, parallel_nodes={len(event.parallel_nodes)}):")
        print(f"      quoted_text   : {event.quoted_text!r}")
        print(f"      quoted 摘要   : {event.quoted_summary!r}")
        kinds = event.quoted_kinds
        print(f"      引用媒体类型  : {sorted(kinds) if kinds else '无'}")
        if event.parallel_nodes:
            for index, node in enumerate(event.parallel_nodes):
                print(f"      node[{index}]      : {node}")
        if event.msg_elements:
            first = event.msg_elements[0]
            print(f"      elements[0] 类型 : {first.get('message_type')}  "
                  f"（0=文本 3=卡片 7=媒体 103=引用）")
        if event.quoted_attachments:
            print(f"      quoted 附件 ({len(event.quoted_attachments)}):")
            for index, att in enumerate(event.quoted_attachments):
                for line in _dump_attachment(att, index, indent="          "):
                    print(line)
        else:
            print("      quoted 附件  : 无（实测：引用图片不回传附件，只能拿到占位符）")

    # ---- 落盘 ----
    if event.local_attachments:
        for index, path in sorted(event.local_attachments.items()):
            print(f"  [已落盘] [{index}] {path}")

    # ---- 引用解析（关键诊断：哪一级命中）----
    if event.quote is not None:
        quote = event.quote
        prefix, tail = _split_refidx(quote.ref_key)
        print("  引用解析      :")
        print(f"      ref 前缀     : {_brief_url(prefix, 60)}   ← 消息相关")
        print(f"      ref 会话尾   : {_brief_url(tail, 40)}   ← 会话级常量，跨消息相同")
        print(f"      source       : {quote.source}   ← both/store/msg_elements/none")
        print(f"      text         : {quote.text!r}")
        if quote.entry is not None and quote.entry.archived_at:
            import time as _time
            print(f"      归档时间     : {_time.time() - quote.entry.archived_at:.0f}s 前")
        if quote.attachments:
            for index, att in enumerate(quote.attachments):
                local = att.local_path or "(未落盘)"
                asr = f"  asr={att.asr_text!r}" if att.asr_text else ""
                print(f"      [{index}] type={att.type}  filename={att.filename or '-'}")
                print(f"          local_path : {local}{asr}")
        elif not quote.resolved:
            print(f"      ⚠ 解析失败（hint={quote.hint!r}）—— 引用内容回查不到")
            print("         最常见原因：该消息被机器人被动回复过，QQ 重新生成了 REFIDX")
            print("         其次：引用了机器人自己发的消息（不入站，永远不会归档）")
            if quote.candidates:
                import time as _time
                print("         疑似同一消息（仅提示，未自动采用）:")
                for key, entry in quote.candidates:
                    age = f"{_time.time() - entry.archived_at:.0f}s" if entry.archived_at else "?"
                    print(f"           key={_brief_url(key, 34)}  age={age}  "
                          f"attachments={[a.filename for a in entry.attachments]}")

    # ---- 原始 JSON ----
    if show_raw:
        print("  原始数据 :")
        for line in json.dumps(event.raw, ensure_ascii=False, indent=2).splitlines():
            print(f"      {line}")


def cmd_listen(args) -> int:
    banner("WebSocket 单聊消息订阅测试")
    config = build_config(args)
    bot = QQBot(config)

    print(f"接口地址 : {config.api_base}")
    print(f"intents  : {config.intents} ({describe(config.intents)})")
    print(f"shard    : {config.shard}")
    print(f"自动回复 : {'开启' if args.reply else '关闭'}（纯图片消息默认跳过，除非 --reply-images）")
    print(f"打印原文 : {'是' if args.raw else '否'}")
    print(f"附件落盘 : {args.download_dir or '不落盘（引用回查将拿不到本地文件）'}")
    print(f"引用索引 : {config.ref_index_path}（TTL {config.ref_index_ttl_days} 天）")
    print("\n现在用手机 QQ 私聊机器人发消息，Ctrl+C 退出。\n")

    def _show(event: Event) -> None:
        format_event(event, show_raw=args.raw)

    @bot.on("C2C_MESSAGE_CREATE")
    def _on_c2c(event: Event) -> None:
        _show(event)

        # 纯图片消息（无文本、无引用）：按设计只记录、不回复
        #
        # 实测（2026-09）发现：机器人**被动回复过**的消息，其 REFIDX 会变，
        # 用户之后引用这条消息会直接 miss。所以对图片不回复不仅是 UX 选择，
        # 而是保证「引用图片」可用的前提。
        is_image_only = bool(event.images) and not event.content.strip() and not event.is_quoted
        if is_image_only:
            if not args.reply_images:
                print("  [跳过] 纯图片消息，只记录不回复（--reply-images 可强制回复）")
                return
            print("  [注意] --reply-images 已开启：被动回复会让该消息的 REFIDX 失效，"
                  "之后引用这张图会解析失败")

        if not args.reply:
            return
        try:
            resp = bot.reply(event, f"已收到你的消息：{event.content}\n— 记账机器人连接测试")
            print(f"  [OK] 已回复 message_id={resp.get('id')}")
        except QQBotAPIError as exc:
            print(f"  [FAIL] 回复失败: {exc}")

    @bot.on("FRIEND_ADD")
    def _on_friend_add(event: Event) -> None:
        print("\n" + "-" * 72)
        print(f"[事件] FRIEND_ADD   用户 {event.user_openid} 添加了机器人")

    @bot.on("*")
    def _on_any(event: Event) -> None:
        """其它事件也打出来，便于发现自己没订阅到/多订阅了哪些事件。"""
        if event.name in ("C2C_MESSAGE_CREATE", "FRIEND_ADD"):
            return
        format_event(event, show_raw=args.raw)

    thread = threading.Thread(target=_run_bot, args=(bot,), name="qqbot-main", daemon=True)
    thread.start()

    started = time.time()
    try:
        while thread.is_alive():
            thread.join(timeout=1)
            if args.duration and time.time() - started >= args.duration:
                print(f"\n到达 --duration {args.duration}s，退出")
                break
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在关闭…")
    finally:
        bot.stop()
        thread.join(timeout=3)

    print("[结束] 订阅测试结束")
    return 0


def _run_bot(bot: QQBot) -> None:
    try:
        bot.run()
    except Exception as exc:  # noqa: BLE001
        print(f"\n[错误] 网关运行失败: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- 4) send
def cmd_send_c2c(args) -> int:
    banner("主动发送单聊消息")
    config = build_config(args)
    api = QQBotAPI(config)
    try:
        resp = api.send_c2c_message(
            args.openid,
            content=args.content,
            msg_id=args.msg_id,
            msg_seq=args.msg_seq,
        )
    except (QQBotAPIError, QQBotAuthError) as exc:
        print(f"\n[失败] {exc}")
        return 1

    print(f"收件人 openid : {args.openid}")
    print(f"消息内容      : {args.content!r}")
    print(f"message_id    : {resp.get('id')}")
    print(f"timestamp     : {resp.get('timestamp')}")
    print("\n[通过] 单聊消息发送成功")
    return 0


def cmd_echo_reply(args) -> int:
    """监听一条消息后被动回复，用于在没有 openid 的情况下做端到端验证。"""
    args.reply = True
    args.duration = args.duration or 120
    return cmd_listen(args)


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="qqbot_test",
        description="QQ 机器人鉴权 / WebSocket 订阅 / 消息发送测试工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--env-file", default=".env", help="配置文件路径，默认 .env")
    parser.add_argument("--sandbox", action="store_true", help="使用沙箱环境")
    parser.add_argument("--intents", type=lambda v: int(v, 0), default=None, help="覆盖 intents 位图")
    parser.add_argument("--debug", action="store_true", help="输出 DEBUG 日志")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("token", help="只测试鉴权（获取 access_token）").set_defaults(func=cmd_token)
    sub.add_parser("gateway", help="测试鉴权 + 网关接入点 + /users/@me").set_defaults(func=cmd_gateway)

    p_listen = sub.add_parser("listen", help="连接 WebSocket 接收单聊事件（详细打印消息与附件）")
    p_listen.add_argument("--reply", action="store_true", help="收到消息后自动回复（默认开）")
    p_listen.add_argument("--no-reply", dest="reply", action="store_false", help="只打印不回复")
    p_listen.add_argument("--reply-images", action="store_true", help="对纯图片消息也回复（默认不回复）")
    p_listen.add_argument("--duration", type=float, default=0, help="运行多少秒后自动退出，0 表示不限")
    p_listen.add_argument("--raw", action="store_true", help="额外打印事件的完整原始 JSON")
    p_listen.add_argument(
        "--download-dir",
        default=None,
        metavar="DIR",
        help="把收到的附件落盘到该目录并打印路径（默认不落盘）",
    )
    p_listen.set_defaults(func=cmd_listen, reply=True, raw=False, download_dir=None, reply_images=False)

    p_c2c = sub.add_parser("send-c2c", help="主动发送单聊消息")
    p_c2c.add_argument("openid", help="用户 OpenID")
    p_c2c.add_argument("content", help="消息内容")
    p_c2c.add_argument("--msg-id", default=None, help="被动回复时传入事件中的 msg_id")
    p_c2c.add_argument("--msg-seq", type=int, default=None, help="被动回复序号，默认 1")
    p_c2c.set_defaults(func=cmd_send_c2c)

    p_echo = sub.add_parser("echo-reply", help="监听并被动回复（限时，便于端到端验证）")
    p_echo.add_argument("--duration", type=float, default=120, help="运行秒数，默认 120")
    p_echo.add_argument("--raw", action="store_true", help="额外打印事件的完整原始 JSON")
    p_echo.add_argument("--reply-images", action="store_true", help="对纯图片消息也回复")
    p_echo.add_argument(
        "--download-dir",
        default=None,
        metavar="DIR",
        help="把收到的附件落盘到该目录并打印路径",
    )
    p_echo.set_defaults(func=cmd_echo_reply, reply=True, raw=False, download_dir=None, reply_images=False)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2

    python_log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=python_log_level,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"\n[异常] {type(exc).__name__}: {exc}")
        if args.debug:
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
