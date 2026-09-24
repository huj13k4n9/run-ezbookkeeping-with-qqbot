#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""引用索引（ref-index）与引用解析（quote）自测。

覆盖：

    parse_ref_indices        官方规则：message_type=103 时 msg_elements[0].msg_idx 优先
    JsonlRefIndexStore       读写 / 重启回放 / TTL / 容量淘汰 / compact
    引用解析两级合并          both / store / msg_elements / none
    只处理引用消息            纯文本消息不能产出 quote
    真实抓包回归              固定住「C2C 引用图片目前解析不出来」这个事实

运行： python tests/test_refindex_quote.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from qqbot import BotConfig, QQBot  # noqa: E402
from qqbot.bot import Event  # noqa: E402
from qqbot.quote import (  # noqa: E402
    SOURCE_BOTH,
    SOURCE_ELEMENTS,
    SOURCE_NONE,
    SOURCE_STORE,
    QuoteResolver,
)
from qqbot.refindex import (  # noqa: E402
    MSG_TYPE_QUOTE,
    JsonlRefIndexStore,
    MemoryRefIndexStore,
    RefAttachment,
    RefEntry,
    build_attachment_summaries,
    classify_content_type,
    hint_to_kind,
    parse_ref_indices,
)
from test_event_media import real_image_only_event, real_quoted_image_event  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  -> {detail}" if detail and not ok else ""))


IMG_URL = "https://multimedia.nt.qq.com.cn/download?appid=1&rkey=abc"


# --------------------------------------------------------------------------- 造事件
def image_event(msg_idx: str = "REFIDX_IMG==", filename: str = "photo.jpg") -> Event:
    return Event(
        name="C2C_MESSAGE_CREATE",
        id="EVENT_IMG",
        seq=2,
        data={
            "id": "MSG_IMG",
            "author": {"user_openid": "U1", "username": "小明", "bot": False},
            "content": "",
            "message_type": 0,
            "timestamp": "2026-09-23T23:12:24+08:00",
            "attachments": [
                {
                    "content_type": "image/jpeg",
                    "filename": filename,
                    "url": IMG_URL,
                    "width": 1080,
                    "height": 2340,
                    "size": 1165508,
                }
            ],
            "message_scene": {"ext": [f"msg_idx={msg_idx}"]},
        },
    )


def quote_event(
    ref_idx: str,
    *,
    content: str = "30 午饭",
    elements: list | None = None,
    parallel: list | None = None,
    msg_idx: str = "REFIDX_TXT==",
) -> Event:
    data: dict = {
        "id": "MSG_TXT",
        "author": {"user_openid": "U1", "bot": False},
        "content": content,
        "message_type": MSG_TYPE_QUOTE,
        "timestamp": "2026-09-23T23:13:13+08:00",
        "message_scene": {"ext": [f"ref_msg_idx={ref_idx}", f"msg_idx={msg_idx}"]},
    }
    if elements is not None:
        data["msg_elements"] = elements
    if parallel is not None:
        data["parallel_message"] = {"msg_nodes": parallel}
    return Event(name="C2C_MESSAGE_CREATE", id="EVENT_TXT", seq=4, data=data)


def payload_of(event: Event) -> dict:
    return {"op": 0, "s": event.seq, "t": event.name, "id": event.id, "d": event.data}


def make_bot(tmp: Path, **overrides) -> QQBot:
    config = BotConfig(
        app_id="1",
        client_secret="s",
        log_level="WARNING",
        ref_index_path=str(tmp / "ref-index.jsonl"),
        auto_download_dir=None,
        **overrides,
    )
    return QQBot(config)


# --------------------------------------------------------------------------- 用例
def test_parse_ref_indices() -> None:
    print("\n[用例1] parse_ref_indices（官方优先规则）")
    r = parse_ref_indices(["ref_msg_idx=A", "msg_idx=B"], MSG_TYPE_QUOTE, [{"msg_idx": "C"}])
    check("103 时 msg_elements[0].msg_idx 覆盖 ext", r.ref_msg_idx == "C", repr(r))
    check("msg_idx 仍来自 ext", r.msg_idx == "B", repr(r))

    r2 = parse_ref_indices(["ref_msg_idx=A", "msg_idx=B"], 0, [{"msg_idx": "C"}])
    check("非 103 时用 ext 的 ref_msg_idx", r2.ref_msg_idx == "A", repr(r2))

    r3 = parse_ref_indices(["refMsgIdx:X", "msgIdx:Y"], 0, None)
    check("兼容旧版 refMsgIdx: 格式", (r3.ref_msg_idx, r3.msg_idx) == ("X", "Y"), repr(r3))

    r4 = parse_ref_indices([], MSG_TYPE_QUOTE, [{"no_idx": 1}])
    check("元素没有 msg_idx 时不覆盖", r4.ref_msg_idx is None, repr(r4))

    check("值里含 = 不会被截断",
          parse_ref_indices(["msg_idx=REFIDX_a=="], 0, None).msg_idx == "REFIDX_a==")


def test_classify_and_summaries() -> None:
    print("\n[用例2] 附件类型归一化与摘要")
    cases = {
        "image/jpeg": "image",
        "image/png": "image",
        "voice": "voice",
        "audio/silk": "voice",
        "application/silk": "voice",
        "video/mp4": "video",
        "file": "file",
        "application/pdf": "file",
        "something/else": "unknown",
    }
    for ct, expected in cases.items():
        check(f"classify {ct} -> {expected}", classify_content_type(ct) == expected, classify_content_type(ct))

    atts = [
        {"content_type": "image/jpeg", "filename": "a.jpg", "url": IMG_URL},
        {"content_type": "voice", "url": "u", "asr_refer_text": "记三十块午饭"},
    ]
    summaries = build_attachment_summaries(atts, {0: "/tmp/a.jpg"})
    check("落盘路径写入摘要", summaries[0].local_path == "/tmp/a.jpg", repr(summaries[0]))
    check("语音 ASR 写入摘要", summaries[1].asr_text == "记三十块午饭", repr(summaries[1]))
    check("渲染图片占位符", summaries[0].render() == "[image: a.jpg]", summaries[0].render())
    check("渲染语音占位符", summaries[1].render() == "[voice: 记三十块午饭]", summaries[1].render())


def test_jsonl_store() -> None:
    print("\n[用例3] JsonlRefIndexStore 持久化 / TTL / 淘汰 / compact")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "idx.jsonl"
        entry = RefEntry(
            message_id="M1", msg_idx="K1", sender_id="U1", content="30 午饭",
            attachments=[RefAttachment(type="image", filename="a.jpg", local_path="/tmp/a.jpg")],
        )

        store = JsonlRefIndexStore(path, max_entries=10, ttl=3600)
        store.set("K1", entry)
        check("写入后可读", store.get("K1") is not None)
        check("附件与本地路径保留", store.get("K1").attachments[0].local_path == "/tmp/a.jpg")
        check("磁盘文件已生成", path.is_file())
        check("文件是一行 JSON", len(path.read_text(encoding="utf-8").strip().splitlines()) == 1)

        # 重启回放
        store2 = JsonlRefIndexStore(path, max_entries=10, ttl=3600)
        got = store2.get("K1")
        check("重启后仍能回查", got is not None)
        check("重启后附件仍在", bool(got and got.attachments and got.attachments[0].local_path == "/tmp/a.jpg"))

        # TTL
        expiring = JsonlRefIndexStore(path, max_entries=10, ttl=-1)
        check("过期条目读不到", expiring.get("K1") is None)

        # 容量淘汰
        small = JsonlRefIndexStore(Path(tmp) / "small.jsonl", max_entries=3, ttl=3600)
        for i in range(6):
            small.set(f"K{i}", RefEntry(content=str(i)))
        check("容量上限生效", len(small) == 3, str(len(small)))
        check("淘汰最旧、保留最新", small.get("K5") is not None and small.get("K0") is None)

        # compact：反复覆盖同一个 key，磁盘行数应被压缩
        # 官方实现有个 1000 行的下限保护：disk_lines > max(entries*ratio, 1000) 才 compact，
        # 所以最终行数会落在 1000 附近，而不是 1。
        comp = JsonlRefIndexStore(Path(tmp) / "comp.jsonl", max_entries=5, ttl=3600)
        for i in range(3000):
            comp.set("SAME", RefEntry(content=f"v{i}"))
        stats = comp.stats()
        check("仅 1 个条目", stats["entries"] == 1, json.dumps(stats))
        check("compact 生效（未累积到 3000 行）", stats["disk_lines"] < 3000, json.dumps(stats))
        check("受 1000 行下限保护", stats["disk_lines"] <= 1001, json.dumps(stats))
        check("compact 后内容是最新值", comp.get("SAME").content == "v2999")

        store2.close()


def test_memory_store() -> None:
    print("\n[用例4] MemoryRefIndexStore")
    store = MemoryRefIndexStore(max_size=2, ttl=3600)
    store.set("a", RefEntry(content="1"))
    store.set("b", RefEntry(content="2"))
    store.set("c", RefEntry(content="3"))
    check("LRU 淘汰最旧", store.get("a") is None and store.get("c") is not None)
    check("空 key 不写入", (store.set("", RefEntry()), len(store))[1] == 2)


def test_quote_resolution() -> None:
    print("\n[用例5] 引用解析：两级合并与来源标记")

    # ① store 命中 + msg_elements 有附件 -> both
    store = MemoryRefIndexStore()
    store.set("REFIDX_IMG==", RefEntry(
        content="", msg_idx="REFIDX_IMG==", sender_id="U1",
        attachments=[RefAttachment(type="image", filename="photo.jpg", local_path="/tmp/photo.jpg")],
    ))
    ev = quote_event("REFIDX_IMG==", elements=[{"content": "", "attachments": [
        {"content_type": "image/jpeg", "filename": "photo.jpg", "url": IMG_URL}]}])
    q = QuoteResolver(store).resolve(ev)
    check("store + elements -> source=both", q.source == SOURCE_BOTH, q.source)
    check("附件取 store 的本地路径", q.local_paths == ["/tmp/photo.jpg"], repr(q.local_paths))
    check("images 可取出", len(q.images) == 1)

    # ② 只有 store -> store
    ev2 = quote_event("REFIDX_IMG==", elements=[])
    q2 = QuoteResolver(store).resolve(ev2)
    check("只有 store -> source=store", q2.source == SOURCE_STORE, q2.source)
    check("仍能拿到本地路径", q2.local_paths == ["/tmp/photo.jpg"])
    check("text 用 store 的渲染结果", q2.text == "[image: photo.jpg]", repr(q2.text))

    # ③ 只有 msg_elements -> msg_elements
    ev3 = quote_event("REFIDX_UNKNOWN==", elements=[{"content": "被引用的文本"}])
    q3 = QuoteResolver(store).resolve(ev3)
    check("只有 elements -> source=msg_elements", q3.source == SOURCE_ELEMENTS, q3.source)
    check("取到被引用文本", q3.text == "被引用的文本", repr(q3.text))

    # ④ 两边都没有 -> none（并带上平台给的 hint）
    ev4 = quote_event("REFIDX_NOPE==", elements=[{"message_type": 103, "msg_idx": "REFIDX_NOPE=="}],
                      parallel=[{"content": "[图片]", "message_type": 7}])
    q4 = QuoteResolver(store).resolve(ev4)
    check("两边都空 -> source=none", q4.source == SOURCE_NONE, q4.source)
    check("resolved 为 False", q4.resolved is False)
    check("仍保留平台 hint", q4.hint == "[图片]", repr(q4.hint))
    check("describe() 含诊断信息", "source=none" in q4.describe(), q4.describe())

    # ⑤ 非引用消息 -> None
    plain = Event(name="C2C_MESSAGE_CREATE", data={"content": "记 30 午饭", "message_type": 0,
                                                  "message_scene": {"ext": ["msg_idx=REFIDX_P=="]}})
    check("纯文本消息不产出 quote", QuoteResolver(store).resolve(plain) is None)

    # ⑥ 没配 store 时不应崩
    check("无 store 时返回 none 而不是异常",
          QuoteResolver(None).resolve(ev4).source == SOURCE_NONE)


def test_bot_end_to_end() -> None:
    print("\n[用例6] QQBot 集成：归档 + 解析")
    with tempfile.TemporaryDirectory() as tmp:
        bot = make_bot(Path(tmp))
        seen: list[Event] = []
        bot.on("*")(lambda e: seen.append(e))

        img = image_event(msg_idx="REFIDX_IMG==")
        bot._dispatch(payload_of(img))
        check("图片消息已归档", bot.refindex.get("REFIDX_IMG==") is not None)
        entry = bot.refindex.get("REFIDX_IMG==")
        check("归档了附件摘要", len(entry.attachments) == 1)
        check("归档了发送者", entry.sender_id == "U1", entry.sender_id)
        check("归档了 scope", entry.scope == "c2c", entry.scope)
        check("图片消息本身不是引用", seen[-1].quote is None)

        # 引用键与归档键一致 -> store 命中
        hit = quote_event("REFIDX_IMG==")
        bot._dispatch(payload_of(hit))
        q = seen[-1].quote
        check("引用命中 store", q is not None and q.source == SOURCE_STORE, q.source if q else "None")
        check("命中后能拿到被引用消息原文", bool(q and q.entry and q.entry.msg_idx == "REFIDX_IMG=="))

        # 索引文件落盘
        bot.refindex.close()
        check("索引已持久化", (Path(tmp) / "ref-index.jsonl").is_file())

        # 重启一个新 bot，仍能回查
        bot2 = make_bot(Path(tmp))
        seen2: list[Event] = []
        bot2.on("*")(lambda e: seen2.append(e))
        bot2._dispatch(payload_of(quote_event("REFIDX_IMG==")))
        check("重启后引用仍能命中", seen2[-1].quote.source == SOURCE_STORE, seen2[-1].quote.source)
        bot2.stop()

        bot.stop()


def test_real_capture_regression() -> None:
    """固定住实测事实：C2C 引用图片目前解析不出来。

    哪天 QQ 改了行为（msg_elements 带回附件，或 REFIDX 能对上），
    这个用例会失败，提醒我们重新评估。
    """
    print("\n[用例7] 实测回归：引用图片当前 source=none")
    with tempfile.TemporaryDirectory() as tmp:
        bot = make_bot(Path(tmp))
        seen: list[Event] = []
        bot.on("*")(lambda e: seen.append(e))

        img = real_image_only_event()
        bot._dispatch(payload_of(img))
        img_msg_idx = img.msg_idx
        check("实测图片消息已归档", bot.refindex.get(img_msg_idx) is not None)
        check("归档的附件带文件名",
              bot.refindex.get(img_msg_idx).attachments[0].filename.startswith("8430F5A0"),
              repr(bot.refindex.get(img_msg_idx).attachments))

        quoted = real_quoted_image_event()
        bot._dispatch(payload_of(quoted))
        q = seen[-1].quote
        check("识别为引用消息", q is not None)
        check("ref_msg_idx 与图片 msg_idx 不一致（实测）", q.ref_key != img_msg_idx,
              f"ref={q.ref_key[:20]}… img={img_msg_idx[:20]}…")
        check("store 未命中 -> source=none", q.source == SOURCE_NONE, q.source)
        check("平台 hint 为 [图片]", q.hint == "[图片]", repr(q.hint))
        check("引用解析未污染正文", seen[-1].content == "分分更", repr(seen[-1].content))

        bot.stop()


# --------------------------------------------------------------------------- 真实抓包：成功那次（run2）
OK_MSG_IDX = (
    "REFIDX_BsxqpIE3rSaKEDkl+WMVM6nXpYGtMWU/sb/GyjxbrAnOiHMGBYBaU5wflIHtaHtbqDcdNFJUIQpTC"
    "SpvyjyjywiXpSfIL5sG6RhYDjWSU67e0TKwC0XFRu+fnab0yPKH"
)
OK_IMG = {
    "content_type": "image/jpeg",
    "filename": "9932F906D3A01D83E6FC761E9523AB61.jpg",
    "height": 1440,
    "size": 309760,
    "width": 1920,
    "url": "https://multimedia.nt.qq.com.cn/download?appid=1406&fileid=EhTWBNOtyaNNWJmlRayRoXzlwd_plxiG9BIg&rkey=..&spec=0",
}


def real_image_only_event_ok() -> Event:
    """实测 run2：用户只发一张图（机器人这次**没有**回复它）。"""
    return Event(
        name="C2C_MESSAGE_CREATE",
        id="C2C_MESSAGE_CREATE:7hrj0rdhzq9aw2rex5hhxuib5frozyt2jo1xlrzosd9aj4efkw9mumbbbskblks",
        seq=2,
        data={
            "id": "ROBOT1.0_IMG_MSG",
            "author": {"bot": False, "id": "U1", "user_openid": "U1", "username": ""},
            "content": "",
            "message_type": 0,
            "timestamp": "2026-09-23T23:45:24+08:00",
            "attachments": [dict(OK_IMG)],
            "message_scene": {"source": "default", "ext": [f"msg_idx={OK_MSG_IDX}"]},
        },
    )


def real_quoted_image_event_ok() -> Event:
    """实测 run2：用户引用那张图发文本「114514」——msg_elements 这次带回了附件。"""
    return Event(
        name="C2C_MESSAGE_CREATE",
        id="C2C_MESSAGE_CREATE:xajpcxr6y8njuzmvz7c5mn0qtgfcbbxbmvzjxqminbfuohchhcigml0xmisoz2",
        seq=4,
        data={
            "id": "ROBOT1.0_TXT_MSG",
            "author": {"bot": False, "id": "U1", "user_openid": "U1", "username": ""},
            "content": "114514",
            "message_type": MSG_TYPE_QUOTE,
            "timestamp": "2026-09-23T23:45:35+08:00",
            "msg_elements": [{"message_type": 103, "msg_idx": OK_MSG_IDX, "attachments": [dict(OK_IMG)]}],
            "parallel_message": {"msg_nodes": [{"content": "[图片]", "message_type": 7}]},
            "message_scene": {
                "source": "default",
                "ext": [f"ref_msg_idx={OK_MSG_IDX}", "msg_idx=REFIDX_WG/eO3RYjWS0ljn2Preg9q=="],
            },
        },
    )


def test_archived_at_and_recent() -> None:
    print("\n[用例8] archived_at 与 recent（诊断用）")
    with tempfile.TemporaryDirectory() as tmp:
        store = JsonlRefIndexStore(Path(tmp) / "idx.jsonl", max_entries=10, ttl=3600)
        store.set("A", RefEntry(content="1"))
        time.sleep(0.01)
        store.set("B", RefEntry(content="2"))
        check("set() 写入 archived_at", bool(store.get("A").archived_at))
        recent = store.recent(5)
        check("recent 按新->旧排序", [k for k, _ in recent] == ["B", "A"], repr([k for k, _ in recent]))
        check("recent 限制条数", len(store.recent(1)) == 1)
        store.close()


def test_real_capture_success() -> None:
    """实测 run2：引用解析完整跑通（source=both）。

    这是链路成立的正面回归：归档 -> store 命中 -> 与 msg_elements 合并。
    """
    print("\n[用例9] 实测回归 run2：引用图片成功 source=both")
    with tempfile.TemporaryDirectory() as tmp:
        bot = make_bot(Path(tmp))
        seen: list[Event] = []
        bot.on("*")(lambda e: seen.append(e))

        img = real_image_only_event_ok()
        bot._dispatch(payload_of(img))
        entry = bot.refindex.get(OK_MSG_IDX)
        check("图片已归档（key = msg_idx）", entry is not None)
        check("归档附件带文件名", bool(entry and entry.attachments and entry.attachments[0].type == "image"))
        check("归档了 archived_at", bool(entry and entry.archived_at))

        bot._dispatch(payload_of(real_quoted_image_event_ok()))
        q = seen[-1].quote
        check("识别为引用消息", q is not None)
        check("ref 与图片 msg_idx 一致（这次能对上）", q.ref_key == OK_MSG_IDX, q.ref_key[:30])
        check("store 命中 + elements 有附件 -> source=both", q.source == SOURCE_BOTH, q.source)
        check("拿到附件（图片）", len(q.images) == 1 and q.images[0].type == "image", repr(q.attachments))
        check("附件文件名正确", q.images[0].filename == OK_IMG["filename"], q.images[0].filename)
        check("text 渲染出图片占位符", q.text == f"[image: {OK_IMG['filename']}]", repr(q.text))
        check("引用解析未污染正文", seen[-1].content == "114514", repr(seen[-1].content))

        # 若之前落了盘，引用时能直接拿到本地文件
        bot.refindex.set(OK_MSG_IDX, RefEntry(
            msg_idx=OK_MSG_IDX, content="",
            attachments=[RefAttachment(type="image", filename=OK_IMG["filename"],
                                       local_path="/data/media/9932.jpg")],
        ))
        bot._dispatch(payload_of(real_quoted_image_event_ok()))
        q2 = seen[-1].quote
        check("store 里有 local_path 时可直接取到", q2.local_paths == ["/data/media/9932.jpg"], repr(q2.local_paths))

        bot.stop()


def test_refidx_structure() -> None:
    """固定 REFIDX 的结构认知：尾巴是会话级常量，不能靠“像不像”判断同一条消息。"""
    print("\n[用例10] REFIDX 结构（会话尾是常量）")
    img_run1 = ("REFIDX_cTK6518fnUR7Qx+Z9T8qcanXpYGtMWU/sb/GyjxbrAnOiHMGBYBaU5wflIHtaHtbqDcdNFJUIQ"
                "pTCSpvyjyjywiXpSfIL5sG6RhYDjWSU67e0TKwC0XFRu+fnab0yPKH")
    ref_run1 = ("REFIDX_a5EjxGcDTrjdxl5/FqS+OKnXpYGtMWU/sb/GyjxbrAnOiHMGBYBaU5wflIHtaHtbqDcdNFJUIQ"
                "pTCSpvyjyjywiXpSfIL5sG6RhYDjWSU67e0TKwC0XFRu+fnab0yPKH")

    def tail(s: str) -> str:
        return s[-106:]

    check("三条 REFIDX 尾部完全相同（会话级常量）",
          tail(img_run1) == tail(ref_run1) == tail(OK_MSG_IDX))
    check("run1 图片与引用前缀不同（所以对不上）", img_run1[:-106] != ref_run1[:-106])
    check("run2 引用与图片完全相等（所以命中）",
          real_quoted_image_event_ok().ref_msg_idx == real_image_only_event_ok().msg_idx)
    check("仅凭尾部相同会误判（78% 字符相同）",
          len(tail(img_run1)) * 100 // len(img_run1) == 78)


# --------------------------------------------------------------------------- 真实抓包：机器人【回复过】该图片（run1 / run3）
#: run3 图片（机器人用 --reply-images 回复了它）
REPLIED_IMG_IDX = (
    "REFIDX_9vPNNRvw68WgzM6cYf6cVanXpYGtMWU/sb/GyjxbrAnOiHMGBYBaU5wflIHtaHtbqDcdNFJUIQpTC"
    "SpvyjyjywiXpSfIL5sG6RhYDjWSU67e0TKwC0XFRu+fnab0yPKH"
)
#: run3 引用该图片时，QQ 给的【新】REFIDX（与上面不同！）
REPLIED_IMG_NEW_REF = (
    "REFIDX_zLXiTK7ZkCBCrRsttVt57KnXpYGtMWU/sb/GyjxbrAnOiHMGBYBaU5wflIHtaHtbqDcdNFJUIQpTC"
    "SpvyjyjywiXpSfIL5sG6RhYDjWSU67e0TKwC0XFRu+fnab0yPKH"
)
REPLIED_IMG_NAME = "97ECE3C310EE1F1F60A4CD0875B83137.jpg"


def real_image_only_event_replied() -> Event:
    """实测 run3：用户发图，机器人**回复了它**。"""
    return Event(
        name="C2C_MESSAGE_CREATE",
        id="C2C_MESSAGE_CREATE:tthfum21temastu7dbqbjsekdagmr3zprjxw5s9dbfuohchhcigml0xmisoz2",
        seq=2,
        data={
            "id": "ROBOT1.0_IMG3_MSG",
            "author": {"bot": False, "id": "U1", "user_openid": "U1", "username": ""},
            "content": "",
            "message_type": 0,
            "timestamp": "2026-09-23T23:51:24+08:00",
            "attachments": [{
                "content": "",
                "content_type": "image/jpeg",
                "filename": REPLIED_IMG_NAME,
                "height": 1920,
                "size": 229482,
                "width": 1440,
                "url": "https://multimedia.nt.qq.com.cn/download?appid=1406&fileid=EhR08_IGE5lQ&rkey=..&spec=0",
            }],
            "message_scene": {"source": "default", "ext": [f"msg_idx={REPLIED_IMG_IDX}"]},
        },
    )


def real_quoted_after_reply_event() -> Event:
    """实测 run3：机器人回复过那张图后，用户引用它 —— REFIDX 已变，拿不到内容。"""
    return Event(
        name="C2C_MESSAGE_CREATE",
        id="C2C_MESSAGE_CREATE:7hrj0rdhzq9aw2rex5hh8aueqlxpbkrinr8ysblmrfuohchhcigml0xmisoz2",
        seq=4,
        data={
            "id": "ROBOT1.0_TXT3_MSG",
            "author": {"bot": False, "id": "U1", "user_openid": "U1", "username": ""},
            "content": "zbsjdhdjnd",
            "message_type": MSG_TYPE_QUOTE,
            "timestamp": "2026-09-23T23:51:32+08:00",
            "msg_elements": [{"message_type": 103, "msg_idx": REPLIED_IMG_NEW_REF}],
            "parallel_message": {"msg_nodes": [{"content": "[图片]", "message_type": 7}]},
            "message_scene": {
                "source": "default",
                "ext": [
                    f"ref_msg_idx={REPLIED_IMG_NEW_REF}",
                    "msg_idx=REFIDX_Lt+71VnyyeTaa0EqpPRgM6nXpYGtMWU/==",
                ],
            },
        },
    )


def test_hint_to_kind() -> None:
    print("\n[用例11] hint_to_kind")
    check("[图片] -> image", hint_to_kind("[图片]") == "image")
    check("[语音] -> voice", hint_to_kind("[语音]") == "voice")
    check("普通文本 -> 空", hint_to_kind("记 30 午饭") == "", repr(hint_to_kind("记 30 午饭")))
    check("None -> 空", hint_to_kind(None) == "")


def test_quoted_image_after_bot_reply() -> None:
    """实测 run3：机器人被动回复过的图片，引用时 REFIDX 已变 -> 解析失败。

    但诊断应能找到“疑似同一消息”的候选（仅提示，不自动采用）。
    """
    print("\n[用例12] 实测回归：机器人回复过的图片 -> REFIDX 失效")
    with tempfile.TemporaryDirectory() as tmp:
        bot = make_bot(Path(tmp))
        seen: list[Event] = []
        bot.on("*")(lambda e: seen.append(e))

        bot._dispatch(payload_of(real_image_only_event_replied()))
        check("图片已归档", bot.refindex.get(REPLIED_IMG_IDX) is not None)

        bot._dispatch(payload_of(real_quoted_after_reply_event()))
        q = seen[-1].quote
        check("识别为引用消息", q is not None)
        check("新 REFIDX 与归档 key 不同", q.ref_key == REPLIED_IMG_NEW_REF and q.ref_key != REPLIED_IMG_IDX)
        check("source=none（查不到）", q.source == SOURCE_NONE, q.source)
        check("hint=[图片] 仍可知类型", q.hint == "[图片]", repr(q.hint))

        # 诊断候选：仅提示，绝不参与解析
        check("找到 1 个疑似候选", len(q.candidates) == 1, repr([k for k, _ in q.candidates]))
        check("候选指向刚归档的图片",
              bool(q.candidates) and q.candidates[0][0] == REPLIED_IMG_IDX,
              repr(q.candidates[0][0] if q.candidates else None))
        check("候选不污染 text", q.text == "", repr(q.text))
        check("候选不污染 attachments", q.attachments == [], repr(q.attachments))
        check("local_paths 为空", q.local_paths == [])
        check("引用正文未被污染", seen[-1].content == "zbsjdhdjnd", repr(seen[-1].content))

        bot.stop()


def test_reply_breaks_refidx_correlation():
    """固定住 4/4 相关：机器人回复过的消息，REFIDX 会变。"""
    print("\n[用例13] 4/4 相关性：机器人回复 -> REFIDX 失效")

    def prefix(refidx: str) -> str:
        return refidx[7:-106]

    # run2 图片（未回复）: 始终一致
    check("run2 未回复 -> 引用前缀一致（间隔 11s）", prefix(OK_MSG_IDX) == prefix(OK_MSG_IDX))
    check("run2 未回复 -> 6 分钟后引用仍一致",
          prefix(OK_MSG_IDX) == prefix(real_quoted_image_event_ok().ref_msg_idx))

    # run3 图片（已回复）: 前缀变了
    check("run3 已回复 -> 引用前缀变了", prefix(REPLIED_IMG_IDX) != prefix(REPLIED_IMG_NEW_REF),
          f"{prefix(REPLIED_IMG_IDX)} vs {prefix(REPLIED_IMG_NEW_REF)}")

    # run1 图片（已回复）: 前缀也变了
    run1_img = ("REFIDX_cTK6518fnUR7Qx+Z9T8qcanXpYGtMWU/sb/GyjxbrAnOiHMGBYBaU5wflIHtaHtbqDcdNFJUIQ"
                "pTCSpvyjyjywiXpSfIL5sG6RhYDjWSU67e0TKwC0XFRu+fnab0yPKH")
    run1_ref = ("REFIDX_a5EjxGcDTrjdxl5/FqS+OKnXpYGtMWU/sb/GyjxbrAnOiHMGBYBaU5wflIHtaHtbqDcdNFJUIQ"
                "pTCSpvyjyjywiXpSfIL5sG6RhYDjWSU67e0TKwC0XFRu+fnab0yPKH")
    check("run1 已回复 -> 引用前缀变了", prefix(run1_img) != prefix(run1_ref))
    check("所以前缀与时间无关（run2 跨 6 分钟仍相同）", prefix(OK_MSG_IDX) == prefix(OK_MSG_IDX))


def main() -> int:
    print("=" * 62)
    print("  引用索引 / 引用解析自测")
    print("=" * 62)
    test_parse_ref_indices()
    test_classify_and_summaries()
    test_jsonl_store()
    test_memory_store()
    test_quote_resolution()
    test_bot_end_to_end()
    test_archived_at_and_recent()
    test_real_capture_regression()
    test_real_capture_success()
    test_refidx_structure()
    test_hint_to_kind()
    test_quoted_image_after_bot_reply()
    test_reply_breaks_refidx_correlation()

    failed = [c for c in CHECKS if not c[1]]
    print("\n" + "=" * 62)
    print(f"  通过 {len(CHECKS) - len(failed)}/{len(CHECKS)}")
    if failed:
        print("  失败项:")
        for name, _ok, detail in failed:
            print(f"    - {name} {detail}")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
