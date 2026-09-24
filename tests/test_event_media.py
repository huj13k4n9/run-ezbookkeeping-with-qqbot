#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""接收侧附件解析 + 落盘自测（不需要真实机器人 / 外网）。

覆盖：

    图片消息      -> event.images / message_type / scene_ext
    引用消息      -> event.quoted_images / quoted_text（递归 msg_elements）
    语音消息      -> event.voice / event.asr_text
    附件下载      -> 本地 HTTP 服务，真实下载验证落盘、重名、大小限制

运行： python tests/test_event_media.py
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqbot.bot import Event  # noqa: E402
from qqbot.media import download_attachment, guess_filename, parse_scene_ext  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  -> {detail}" if detail and not ok else ""))


# --------------------------------------------------------------------------- 事件样本
IMAGE_URL = "https://multimedia.nt.qq.com.cn/download?appid=1&fileid=2&rkey=abc&spec=0"


def image_event() -> Event:
    """用户单聊只发了一张图（无文本）。"""
    return Event(
        name="C2C_MESSAGE_CREATE",
        id="EVENT1",
        seq=11,
        data={
            "id": "ROBOT1.0_MSGID_IMG",
            "author": {"id": "U1", "user_openid": "U1", "username": "", "bot": False},
            "content": " ",
            "message_type": 0,
            "timestamp": "2026-07-21T10:05:00+08:00",
            "attachments": [
                {
                    "content_type": "image/jpeg",
                    "filename": "photo.jpg",
                    "url": IMAGE_URL,
                    "width": 1920,
                    "height": 1080,
                    "size": 256000,
                }
            ],
            "message_scene": {"source": "default", "ext": ["msg_idx=REFIDX_img=="]},
        },
    )


def quoted_image_event() -> Event:
    """用户引用那张图，发文本「30 午饭」。"""
    return Event(
        name="C2C_MESSAGE_CREATE",
        id="EVENT2",
        seq=12,
        data={
            "id": "ROBOT1.0_MSGID_TEXT",
            "author": {"user_openid": "U1", "bot": False},
            "content": "30 午饭",
            "message_type": 103,
            "timestamp": "2026-07-21T10:06:00+08:00",
            "msg_elements": [
                {
                    "msg_idx": "REFIDX_img==",
                    "message_type": 0,
                    "content": "",
                    "attachments": [
                        {
                            "content_type": "image/png",
                            "filename": "receipt.png",
                            "url": "https://example.com/receipt.png",
                            "size": 1024,
                        }
                    ],
                    "msg_elements": [
                        {
                            "msg_idx": "REFIDX_nested==",
                            "content": "嵌套文本",
                            "attachments": [
                                {"content_type": "image/webp", "url": "https://example.com/nested.webp"}
                            ],
                        }
                    ],
                }
            ],
            "message_scene": {
                "source": "default",
                "ext": ["ref_msg_idx=REFIDX_img==", "msg_idx=REFIDX_text==", "auth_token=TOK123"],
            },
        },
    )


def voice_event() -> Event:
    return Event(
        name="C2C_MESSAGE_CREATE",
        id="EVENT3",
        seq=13,
        data={
            "id": "ROBOT1.0_MSGID_VOICE",
            "author": {"user_openid": "U1"},
            "content": "",
            "message_type": 0,
            "attachments": [
                {
                    "content_type": "voice",
                    "filename": "voice.silk",
                    "url": "https://example.com/voice.silk",
                    "voice_wav_url": "https://example.com/voice.wav",
                    "asr_refer_text": "记三十块午饭",
                }
            ],
            "message_scene": {"ext": ["msg_idx=REFIDX_voice=="]},
        },
    )


# --------------------------------------------------------------------------- 用例
def test_image_event() -> None:
    print("\n[用例1] 图片消息解析")
    event = image_event()
    check("message_type = 0", event.message_type == 0, repr(event.message_type))
    check("识别为含附件", bool(event.attachments))
    check("images 取到 1 张图", len(event.images) == 1, repr(event.images))
    check("不是纯文本", event.is_text_only is False)
    check("不是引用消息", event.is_quoted is False)
    check("files 为空（图片不算文件）", event.files == [], repr(event.files))
    check("content 是空白", event.content.strip() == "")
    check("msg_id 可用于被动回复", event.msg_id == "ROBOT1.0_MSGID_IMG", repr(event.msg_id))
    check("scene_ext 解析 msg_idx", event.msg_idx == "REFIDX_img==", repr(event.msg_idx))
    check("quoted_images 为空", event.quoted_images == [])
    print(f"  图片尺寸: {event.images[0]['width']}x{event.images[0]['height']}")


def test_quoted_image_event() -> None:
    print("\n[用例2] 引用消息解析（含嵌套 msg_elements）")
    event = quoted_image_event()
    check("message_type = 103", event.message_type == 103, repr(event.message_type))
    check("识别为引用消息", event.is_quoted is True)
    check("本消息无附件", event.attachments == [])
    check("引用文本 = 30 午饭", event.content == "30 午饭", repr(event.content))

    quoted = event.quoted_images
    check("递归取到 2 张被引用图片", len(quoted) == 2, repr([q.get("content_type") for q in quoted]))
    check("第 1 张是 receipt.png", quoted[0]["filename"] == "receipt.png", repr(quoted[0]))
    check("第 2 张来自嵌套层级", quoted[1]["url"].endswith("nested.webp"), repr(quoted[1]))
    check("quoted_text 取到嵌套文本", "嵌套文本" in event.quoted_text, repr(event.quoted_text))
    check("ref_msg_idx 正确", event.ref_msg_idx == "REFIDX_img==", repr(event.ref_msg_idx))
    check("auth_token 解析正确", event.auth_token == "TOK123", repr(event.auth_token))
    check("all_images 合并本消息+引用", len(event.all_images) == 2)
    check("is_text_only = False（引用带图）", event.is_text_only is False)


def test_voice_event() -> None:
    print("\n[用例3] 语音消息解析")
    event = voice_event()
    check("识别到语音附件", event.voice is not None)
    check("asr_text 取到识别文本", event.asr_text == "记三十块午饭", repr(event.asr_text))
    check("images 为空", event.images == [])
    check("files 为空（语音不算文件）", event.files == [], repr(event.files))
    check("voice_wav_url 可访问", str(event.voice.get("voice_wav_url")).endswith(".wav"))


def test_filename_and_ext() -> None:
    print("\n[用例4] 文件名推断与 scene_ext 解析")
    check("优先用 filename", guess_filename({"filename": "a b!.jpg"}) == "a_b_.jpg",
          guess_filename({"filename": "a b!.jpg"}))
    check("filename 去掉路径穿越", guess_filename({"filename": "../../etc/passwd"}) == "passwd",
          guess_filename({"filename": "../../etc/passwd"}))
    check("无 filename 按 content_type 补扩展名",
          guess_filename({"content_type": "image/png"}) == "attachment_0.png",
          guess_filename({"content_type": "image/png"}))
    check("voice 默认扩展名", guess_filename({"content_type": "voice"}) == "attachment_0.silk")

    ext = parse_scene_ext(["msg_idx=REFIDX_xxx==", "auth_token=abc=", "garbage"])
    check("按第一个 = 切分", ext.get("msg_idx") == "REFIDX_xxx==", repr(ext))
    check("忽略没有 = 的项", "garbage" not in ext, repr(ext))
    check("空输入返回空 dict", parse_scene_ext(None) == {})


def test_download() -> None:
    print("\n[用例5] 附件真实下载（本地 HTTP 服务）")
    with tempfile.TemporaryDirectory() as serve_dir, tempfile.TemporaryDirectory() as dest_dir:
        root = Path(serve_dir)
        (root / "photo.jpg").write_bytes(b"FAKE-JPEG-DATA" * 100)
        (root / "receipt.png").write_bytes(b"PNG")

        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
        with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{port}"

            try:
                # 1) 正常下载，用 filename
                att = {"url": f"{base}/photo.jpg", "filename": "photo.jpg", "size": 1400}
                path = download_attachment(att, dest_dir)
                check("落盘成功", path.is_file(), repr(path))
                check("内容正确", path.read_bytes() == (root / "photo.jpg").read_bytes())
                check("文件名沿用附件 filename", path.name == "photo.jpg", path.name)

                # 2) 重名不覆盖
                path2 = download_attachment(att, dest_dir)
                check("重名自动加序号", path2.name == "photo_1.jpg", path2.name)

                # 3) 无 filename 时按 content_type 补扩展名
                att3 = {"url": f"{base}/receipt.png", "content_type": "image/png"}
                path3 = download_attachment(att3, dest_dir)
                check("按 content_type 推断扩展名", path3.suffix == ".png", path3.name)

                # 4) 超过 max_bytes 报错且不留残文件
                att4 = {"url": f"{base}/photo.jpg", "filename": "big.jpg"}
                try:
                    download_attachment(att4, dest_dir, max_bytes=10)
                except ValueError as exc:
                    check("max_bytes 生效", "超过" in str(exc), str(exc))
                else:
                    check("max_bytes 生效", False, "未抛异常")
                check("失败不留残文件", not (Path(dest_dir) / "big.jpg").exists())

                # 5) 缺 url 报错
                try:
                    download_attachment({}, dest_dir)
                except ValueError:
                    check("缺 url 抛 ValueError", True)
                else:
                    check("缺 url 抛 ValueError", False)
            finally:
                httpd.shutdown()


# --------------------------------------------------------------------------- 真实抓包回归
# 以下两个事件来自 2026-09-23 单聊实测（REFIDX 等长串为便于阅读做了截断）
IMAGE_ONLY_MSG_IDX = "REFIDX_cTK6518fnUR7Qx+Z9T8qcanXpYGtMWU/sb/GyjxbrAnOiHMGBYBaU5w"
QUOTE_REF_MSG_IDX = "REFIDX_a5EjxGcDTrjdxl5/FqS+OKnXpYGtMWU/sb/GyjxbrAnOiHMGBYBaU5w"


def real_image_only_event() -> Event:
    """实测：用户只发一张图，不带任何文本。

    重点：``content`` 为空串，图片全在 ``attachments`` 里，
    且 attachment 多了一个未文档化的 ``content`` 字段。
    """
    return Event(
        name="C2C_MESSAGE_CREATE",
        id="C2C_MESSAGE_CREATE:kiimdgafrtjyzs38lbmdtnsycsdf1m33oaxwnvorqffuohchhcigml0xmisoz2",
        seq=2,
        data={
            "attachments": [
                {
                    "content": "",
                    "content_type": "image/jpeg",
                    "filename": "8430F5A09A9B442F19B5013B7B148710.jpg",
                    "height": 2340,
                    "size": 1165508,
                    "url": (
                        "https://multimedia.nt.qq.com.cn/download?appid=1406"
                        "&fileid=EhTp_ulUqT-pvkkvav5tWeh8h6U-7hjEkUcg&rkey=CAISMKmR7rP-me25gwzIBqjlWdB&spec=0"
                    ),
                    "width": 1080,
                }
            ],
            "author": {
                "bot": False,
                "id": "593CBC924722A699FB58056798027289",
                "union_openid": "",
                "user_openid": "593CBC924722A699FB58056798027289",
                "username": "",
            },
            "content": "",
            "id": "ROBOT1.0_eBGxvSabUiywWCteQjpFvQAct8RxLP.txXGPK7TVAGPVAjhFUw6a96th57t67JRzRAIfTqo0pA3or6ujC959rvpYgaf4C5XyaLdtdKVzqgg!",
            "message_scene": {"ext": [f"msg_idx={IMAGE_ONLY_MSG_IDX}"], "source": "default"},
            "message_type": 0,
            "timestamp": "2026-09-23T23:12:24+08:00",
        },
    )


def real_quoted_image_event() -> Event:
    """实测：用户发文本「分分更」并引用那张图。

    重点：``msg_elements`` 里**没有 attachments**，只有 ``parallel_message``
    的 ``[图片]`` 占位符，且 ``ref_msg_idx`` 与图片事件的 ``msg_idx`` 不一致。
    """
    return Event(
        name="C2C_MESSAGE_CREATE",
        id="C2C_MESSAGE_CREATE:hryui0nzjjnod32ksyyt1bcdzia4yyio3qjay15kndfuohchhcigml0xmisoz2",
        seq=4,
        data={
            "author": {
                "bot": False,
                "id": "593CBC924722A699FB58056798027289",
                "union_openid": "",
                "user_openid": "593CBC924722A699FB58056798027289",
                "username": "",
            },
            "content": "分分更",
            "id": "ROBOT1.0_eBGxvSabUiywWCteQjpFveSxZIrA-01LI308Mlva6ZaKRm453TzyjEKwBBv1tOBmoMw-SdGMaHXEvgvwVvx0MfpYgaf4C5XyaLdtdKVzqgg!",
            "message_scene": {
                "ext": [
                    f"ref_msg_idx={QUOTE_REF_MSG_IDX}",
                    "msg_idx=REFIDX_Nu3P658JTHw8IsJPdAdGHKnXpYGtMWU/sb/GyjxbrAnOiHMGBYBaU5w",
                ],
                "source": "default",
            },
            "message_type": 103,
            "msg_elements": [{"message_type": 103, "msg_idx": QUOTE_REF_MSG_IDX}],
            "parallel_message": {"msg_nodes": [{"content": "[图片]", "message_type": 7}]},
            "timestamp": "2026-09-23T23:13:13+08:00",
        },
    )


def test_real_image_only() -> None:
    print("\n[用例6] 实测回归：单独发图")
    event = real_image_only_event()
    check("content 为空串", event.content == "", repr(event.content))
    check("识别出 1 张图", len(event.images) == 1)
    check("不是纯文本消息", event.is_text_only is False)
    check("attachment 多了 content 字段", event.images[0].get("content") == "", repr(event.images[0]))
    check("尺寸解析正确", (event.images[0]["width"], event.images[0]["height"]) == (1080, 2340))
    check("文件名解析正确", event.images[0]["filename"].endswith(".jpg"))
    check("可用于被动回复的 msg_id 存在", event.msg_id.startswith("ROBOT1.0_"))

    # 这是 Skill 判断“纯图片、需静默处理”的判据
    is_image_only = bool(event.images) and not event.content.strip() and not event.is_quoted
    check("可判定为「纯图片消息」", is_image_only is True)


def test_real_quoted_image() -> None:
    print("\n[用例7] 实测回归：引用图片 + 文本")
    event = real_quoted_image_event()
    check("message_type = 103", event.message_type == 103)
    check("识别为引用消息", event.is_quoted is True)
    check("文本内容 = 分分更", event.content == "分分更")
    check("本消息无附件", event.attachments == [])

    # ★ 实测结论：引用不携带图片附件
    check("msg_elements 里没有 attachments", event.quoted_attachments == [], repr(event.quoted_attachments))
    check("quoted_text 为空", event.quoted_text == "")
    check("parallel_nodes 有 1 个节点", len(event.parallel_nodes) == 1)
    check("节点是 [图片] 占位符", event.quoted_summary == "[图片]", repr(event.quoted_summary))
    check("能判断「引用的是图片」", event.quoted_is_image is True)
    check("quoted_kinds = {'image'}", event.quoted_kinds == {"image"}, repr(event.quoted_kinds))
    check("拿不到图片 URL（quoted_images 为空）", event.quoted_images == [])

    # ★ ref_msg_idx 与图片事件的 msg_idx 不一致 -> 无法用引用索引精确匹配
    check(
        "ref_msg_idx 与图片 msg_idx 不一致（不能精确匹配）",
        event.ref_msg_idx != real_image_only_event().msg_idx,
        repr((event.ref_msg_idx, real_image_only_event().msg_idx)),
    )


def main() -> int:
    print("=" * 62)
    print("  接收侧附件解析 / 落盘自测")
    print("=" * 62)
    test_image_event()
    test_quoted_image_event()
    test_voice_event()
    test_filename_and_ext()
    test_download()
    test_real_image_only()
    test_real_quoted_image()

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
