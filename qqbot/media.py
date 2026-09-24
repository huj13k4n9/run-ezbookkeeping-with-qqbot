"""接收侧附件处理：解析 + 落盘。

事件里的附件结构（C2C_MESSAGE_CREATE / GROUP_AT_MESSAGE_CREATE）::

    {
        "content_type": "image/jpeg",   # voice=语音, image/*, video/mp4, file=群文件
        "filename": "photo.jpg",
        "url": "https://multimedia.nt.qq.com.cn/download?appid=..&fileid=..&rkey=..",
        "width": 1920, "height": 1080,  # 非图片无此字段
        "size": 256000,
        "voice_wav_url": "...",         # 仅语音：SILK 转好的 WAV
        "asr_refer_text": "..."         # 仅语音：ASR 参考文本
    }

注意：下载 URL 带 ``rkey`` 等签名参数，很可能有有效期，
所以收到后应尽快落盘，不要只存 URL。
"""

from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path
from typing import Any, Iterable, Optional

import requests

log = logging.getLogger("qqbot.media")

#: content_type -> 默认扩展名
EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "video/mp4": ".mp4",
    "voice": ".silk",
}

#: 只保留安全字符，防止 filename 里带路径穿越
_UNSAFE_CHARS = re.compile(r"[^0-9A-Za-z._\u4e00-\u9fff\-]+")


def is_image(attachment: dict) -> bool:
    return str(attachment.get("content_type") or "").lower().startswith("image/")


def is_voice(attachment: dict) -> bool:
    return str(attachment.get("content_type") or "").lower() == "voice"


def guess_filename(attachment: dict, *, index: int = 0) -> str:
    """给附件推断一个安全的文件名。"""
    raw_name = str(attachment.get("filename") or "").strip()
    if raw_name:
        # 去掉目录部分，再过滤非法字符
        raw_name = raw_name.replace("\\", "/").rsplit("/", 1)[-1]
        safe = _UNSAFE_CHARS.sub("_", raw_name).strip("._")
        if safe:
            return safe

    content_type = str(attachment.get("content_type") or "").lower()
    ext = EXT_BY_CONTENT_TYPE.get(content_type) or mimetypes.guess_extension(content_type) or ""
    return f"attachment_{index}{ext}"


def _unique_path(directory: Path, filename: str) -> Path:
    """避免重名覆盖：a.jpg -> a_1.jpg -> a_2.jpg"""
    path = directory / filename
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for n in range(1, 10000):
        candidate = directory / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"{directory} 下同名文件过多: {filename}")


def download_attachment(
    attachment: dict,
    dest_dir: str | Path,
    *,
    filename: Optional[str] = None,
    index: int = 0,
    session: Optional[requests.Session] = None,
    timeout: float = 30.0,
    headers: Optional[dict] = None,
    max_bytes: Optional[int] = None,
) -> Path:
    """把附件下载到 ``dest_dir``，返回落盘路径。

    :param headers: 需要时自行传鉴权头（事件里的 ``auth_token`` 见
        :attr:`qqbot.Event.auth_token`）；默认按 URL 自带的签名参数直接下载。
    :param max_bytes: 超过该大小直接报错，避免被塞大文件。
    """
    url = str(attachment.get("url") or "")
    if not url:
        raise ValueError("attachment 缺少 url，无法下载")

    directory = Path(dest_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = _unique_path(directory, filename or guess_filename(attachment, index=index))

    http = session or requests
    log.info("下载附件 %s -> %s", url.split("?")[0], target)
    resp = http.get(url, headers=headers, timeout=timeout, stream=True)
    resp.raise_for_status()

    declared = attachment.get("size")
    if max_bytes and isinstance(declared, int) and declared > max_bytes:
        raise ValueError(f"附件大小 {declared} 超过 max_bytes={max_bytes}")

    written = 0
    try:
        with target.open("wb") as fp:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if max_bytes and written > max_bytes:
                    raise ValueError(f"附件实际大小超过 max_bytes={max_bytes}")
                fp.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        resp.close()

    log.info("附件已保存 %s (%d 字节)", target, written)
    return target


def collect_attachments(elements: Iterable[dict], *, recursive: bool = True) -> list[dict]:
    """从 ``msg_elements``（引用消息的嵌套结构）里递归取出所有附件。"""
    found: list[dict] = []
    for element in elements or []:
        if not isinstance(element, dict):
            continue
        for attachment in element.get("attachments") or []:
            if isinstance(attachment, dict):
                found.append(attachment)
        if recursive:
            found.extend(collect_attachments(element.get("msg_elements") or []))
    return found


def parse_scene_ext(ext: Any) -> dict[str, str]:
    """把 ``message_scene.ext`` 的 ``["k=v", ...]`` 解析成 dict。

    形如 ``msg_idx=REFIDX_xxx==``，值里可能含 ``=``，所以只按第一个 ``=`` 切。
    """
    result: dict[str, str] = {}
    if not ext:
        return result
    items = ext if isinstance(ext, (list, tuple)) else [ext]
    for item in items:
        text = str(item)
        key, sep, value = text.partition("=")
        if sep:
            result[key.strip()] = value.strip()
    return result
