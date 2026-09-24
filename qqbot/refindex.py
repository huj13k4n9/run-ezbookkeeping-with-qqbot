"""引用索引（ref-index）：本地缓存已收到的消息，供引用消息回查。

对标官方 Node SDK 的设计（`@tencent-connect/qqbot-nodejs`）：

* `protocol/utils/text-parsing.ts` 的 `parseRefIndices` / `buildAttachmentSummaries`
* `protocol/utils/ref-index-store.ts` 的 `JsonlRefIndexStore`

要点：

1. **每条入站消息**都按 ``msg_idx``（回退 ``message_id``）记录一条精简摘要，
   这样后面收到的「引用消息」才能通过 ``ref_msg_idx`` 回查原文与附件。
2. 附件摘要里带 **``local_path``** —— 官方设计就是「收到即落盘 + 记录本地路径」，
   因为附件的 ``url`` 带 ``rkey`` 签名参数，有时效，不能事后回查。
3. 持久化用 JSONL 追加写（写便宜、启动回放、体积膨胀后 compact），
   TTL 默认 7 天，容量默认 50000 条。

官方文档里没有的规则（从 SDK 源码里挖出来的）：

    message_type == 103（引用消息）时，``msg_elements[0].msg_idx``
    **优先于** ``message_scene.ext`` 里的 ``ref_msg_idx`` —— 元素级索引更权威。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

log = logging.getLogger("qqbot.refindex")

#: 引用消息的 message_type
MSG_TYPE_QUOTE = 103

#: 默认 TTL：7 天
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
#: 默认容量
DEFAULT_MAX_ENTRIES = 50_000
#: 磁盘行数 / 内存条数 超过该比例时 compact
DEFAULT_COMPACT_RATIO = 2


#: 被引用内容摘要里的占位符 -> 媒体类型
#: （来自 parallel_message.msg_nodes[].content）
QUOTED_HINT_KINDS = {
    "[图片]": "image",
    "[视频]": "video",
    "[语音]": "voice",
    "[文件]": "file",
    "[表情]": "unknown",
}


def hint_to_kind(hint: Any) -> str:
    """把平台给的引用摘要（如 ``[图片]``）映射成媒体类型。"""
    return QUOTED_HINT_KINDS.get(str(hint or "").strip(), "")


# ============================================================ 数据结构
def classify_content_type(content_type: Any) -> str:
    """把 ``content_type`` 归一化成 image / voice / video / file / unknown。"""
    ct = str(content_type or "").lower()
    if ct.startswith("image/"):
        return "image"
    if ct in ("voice", "audio") or ct.startswith("audio/") or "silk" in ct or "amr" in ct:
        return "voice"
    if ct.startswith("video/"):
        return "video"
    if ct.startswith(("application/", "text/")) or ct in ("file",):
        return "file"
    return "unknown"


@dataclass
class RefAttachment:
    """一条附件摘要。``local_path`` 是收到时落盘的本地文件路径。"""

    type: str = "unknown"
    filename: str = ""
    content_type: str = ""
    local_path: Optional[str] = None
    asr_text: str = ""
    url: str = ""

    @classmethod
    def from_raw(cls, attachment: dict, local_path: Optional[str] = None) -> "RefAttachment":
        return cls(
            type=classify_content_type(attachment.get("content_type")),
            filename=str(attachment.get("filename") or ""),
            content_type=str(attachment.get("content_type") or ""),
            local_path=local_path,
            asr_text=str(attachment.get("asr_refer_text") or ""),
            url=str(attachment.get("url") or ""),
        )

    @classmethod
    def from_dict(cls, data: dict) -> "RefAttachment":
        known = {f for f in ("type", "filename", "content_type", "local_path", "asr_text", "url")}
        return cls(**{k: v for k, v in data.items() if k in known})

    def render(self) -> str:
        """渲染成人/模型可读的单行文本。"""
        if self.type == "voice":
            return f"[voice: {self.asr_text}]" if self.asr_text else "[voice]"
        if self.type == "image":
            return f"[image: {self.filename}]" if self.filename else "[image]"
        if self.type == "video":
            return f"[video: {self.filename}]" if self.filename else "[video]"
        return f"[file: {self.filename or 'untitled'}]"


@dataclass
class RefEntry:
    """一条已记录消息的摘要。"""

    message_id: str = ""
    msg_idx: str = ""
    sender_id: str = ""
    sender_name: str = ""
    content: str = ""
    timestamp: str = ""
    is_bot: bool = False
    scope: str = ""
    #: 归档时间（unix 秒），用于诊断「引用跨度多久」
    archived_at: float = 0.0
    attachments: list[RefAttachment] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "RefEntry":
        payload = dict(data)
        payload["attachments"] = [
            RefAttachment.from_dict(a) for a in (payload.get("attachments") or []) if isinstance(a, dict)
        ]
        known = {
            "message_id", "msg_idx", "sender_id", "sender_name", "content",
            "timestamp", "is_bot", "scope", "archived_at", "attachments",
        }
        return cls(**{k: v for k, v in payload.items() if k in known})

    @property
    def text(self) -> str:
        """正文 + 附件占位符，拼成一段可读文本。"""
        parts: list[str] = []
        if self.content.strip():
            parts.append(self.content.strip())
        parts.extend(a.render() for a in self.attachments)
        return "\n".join(parts)


# ============================================================ ext 解析
@dataclass
class RefIndices:
    ref_msg_idx: Optional[str] = None
    msg_idx: Optional[str] = None


def parse_ref_indices(
    ext: Any,
    message_type: Optional[int] = None,
    msg_elements: Optional[list] = None,
) -> RefIndices:
    """从 ``message_scene.ext`` 解析引用索引。

    兼容两种前缀格式：

    * ``ref_msg_idx=`` / ``msg_idx=``（平台原生格式）
    * ``refMsgIdx:`` / ``msgIdx:``（旧版内部格式）

    并且：``message_type == 103`` 时，``msg_elements[0].msg_idx`` 覆盖 ext 里的
    ``ref_msg_idx``（官方 SDK 明确「元素级索引更权威」）。
    """
    result = RefIndices()
    items = ext if isinstance(ext, (list, tuple)) else ([ext] if ext else [])
    for item in items:
        if not isinstance(item, str):
            continue
        if item.startswith("ref_msg_idx="):
            result.ref_msg_idx = item[len("ref_msg_idx="):].strip()
        elif item.startswith("msg_idx="):
            result.msg_idx = item[len("msg_idx="):].strip()
        elif item.startswith("refMsgIdx:"):
            result.ref_msg_idx = item[len("refMsgIdx:"):].strip()
        elif item.startswith("msgIdx:"):
            result.msg_idx = item[len("msgIdx:"):].strip()

    if message_type == MSG_TYPE_QUOTE and msg_elements:
        first = msg_elements[0]
        if isinstance(first, dict) and first.get("msg_idx"):
            result.ref_msg_idx = str(first["msg_idx"]).strip()
    return result


def collect_flat_attachments(msg_elements: Optional[list]) -> list[dict]:
    """取 ``msg_elements[0].attachments``（官方只认第 0 个元素，这里保持兼容）。"""
    if not msg_elements:
        return []
    first = msg_elements[0]
    if not isinstance(first, dict):
        return []
    raw = first.get("attachments")
    return [a for a in raw if isinstance(a, dict)] if isinstance(raw, list) else []


def build_attachment_summaries(
    attachments: list[dict], local_paths: Optional[dict[int, str]] = None
) -> list[RefAttachment]:
    """把事件里的 ``attachments`` 转成摘要，带上落盘后的本地路径。"""
    local_paths = local_paths or {}
    return [RefAttachment.from_raw(att, local_paths.get(i)) for i, att in enumerate(attachments or [])]


# ============================================================ Store
class RefIndexStore(Protocol):
    """可插拔的引用索引存储。多实例部署时可用 Redis / SQL 实现。"""

    def get(self, key: str) -> Optional[RefEntry]:  # pragma: no cover - 协议
        ...

    def set(self, key: str, entry: RefEntry) -> None:  # pragma: no cover - 协议
        ...


class MemoryRefIndexStore:
    """内存 LRU + TTL，进程重启即丢（测试/临时用）。"""

    def __init__(self, max_size: int = DEFAULT_MAX_ENTRIES, ttl: float = DEFAULT_TTL_SECONDS):
        self.max_size = max_size
        self.ttl = ttl
        self._data: dict[str, tuple[float, RefEntry]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[RefEntry]:
        if not key:
            return None
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            created, entry = item
            if self.ttl and time.time() - created > self.ttl:
                self._data.pop(key, None)
                return None
            return entry

    def set(self, key: str, entry: RefEntry) -> None:
        if not key:
            return
        with self._lock:
            self._data.pop(key, None)
            if len(self._data) >= self.max_size:
                oldest = next(iter(self._data), None)
                if oldest is not None:
                    self._data.pop(oldest, None)
            self._data[key] = (time.time(), entry)

    def recent(self, limit: int = 5) -> list[tuple[str, RefEntry]]:
        """最近归档的条目（新→旧）。"""
        with self._lock:
            items = sorted(self._data.items(), key=lambda kv: kv[1][0], reverse=True)
            return [(k, v) for k, (_ts, v) in items[:limit]]

    def __len__(self) -> int:
        return len(self._data)


class JsonlRefIndexStore:
    """JSONL 追加写 + 启动回放 + compact 的持久化存储。

    对标官方 ``JsonlRefIndexStore``：进程重启后引用回查依然可用。
    """

    def __init__(
        self,
        file_path: str | Path,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl: float = DEFAULT_TTL_SECONDS,
        compact_ratio: int = DEFAULT_COMPACT_RATIO,
    ):
        self.file_path = Path(file_path)
        self.max_entries = max_entries
        self.ttl = ttl
        self.compact_ratio = compact_ratio
        self._data: dict[str, tuple[float, RefEntry]] = {}
        self._disk_lines = 0
        self._lock = threading.Lock()
        self._load()

    # -------------------------------------------------- 读写
    def get(self, key: str) -> Optional[RefEntry]:
        if not key:
            return None
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            created, entry = item
            if self.ttl and time.time() - created > self.ttl:
                self._data.pop(key, None)
                return None
            return entry

    def set(self, key: str, entry: RefEntry) -> None:
        if not key:
            return
        now = time.time()
        entry.archived_at = now
        with self._lock:
            self._data.pop(key, None)
            self._data[key] = (now, entry)
            self._evict_locked()
            self._append_locked(key, entry, now)
            if self._should_compact_locked():
                self._compact_locked()

    def recent(self, limit: int = 5) -> list[tuple[str, RefEntry]]:
        """最近归档的条目（新→旧），用于诊断引用 miss。"""
        with self._lock:
            items = sorted(self._data.items(), key=lambda kv: kv[1][0], reverse=True)
            return [(k, v) for k, (_ts, v) in items[:limit]]

    # -------------------------------------------------- 内部
    def _load(self) -> None:
        if not self.file_path.is_file():
            return
        now = time.time()
        parsed: list[tuple[float, str, RefEntry]] = []
        try:
            for line in self.file_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                self._disk_lines += 1
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                key, value, ts = obj.get("k"), obj.get("v"), obj.get("t")
                if not key or not isinstance(value, dict):
                    continue
                if self.ttl and isinstance(ts, (int, float)) and now - ts > self.ttl:
                    continue
                parsed.append((float(ts or now), key, RefEntry.from_dict(value)))
        except OSError as exc:
            log.error("读取 ref-index 失败: %s", exc)
            return

        for ts, key, entry in sorted(parsed, key=lambda x: x[0]):
            self._data.pop(key, None)
            self._data[key] = (ts, entry)
        log.info("ref-index 回放完成: %d 条（磁盘 %d 行）", len(self._data), self._disk_lines)
        if self._should_compact_locked():
            self._compact_locked()

    def _evict_locked(self) -> None:
        now = time.time()
        if self.ttl:
            expired = [k for k, (ts, _) in self._data.items() if now - ts > self.ttl]
            for k in expired:
                self._data.pop(k, None)
        overflow = len(self._data) - self.max_entries
        if overflow > 0:
            oldest = sorted(self._data.items(), key=lambda kv: kv[1][0])[:overflow]
            for k, _ in oldest:
                self._data.pop(k, None)
            log.debug("ref-index 淘汰 %d 条最旧记录", len(oldest))

    def _append_locked(self, key: str, entry: RefEntry, ts: float) -> None:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps({"k": key, "v": entry.to_dict(), "t": ts}, ensure_ascii=False)
            with self.file_path.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")
            self._disk_lines += 1
        except OSError as exc:
            log.error("ref-index 追加写失败: %s", exc)

    def _should_compact_locked(self) -> bool:
        return self._disk_lines > max(len(self._data) * self.compact_ratio, 1000)

    def _compact_locked(self) -> None:
        tmp = self.file_path.with_suffix(self.file_path.suffix + ".tmp")
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            before = self._disk_lines
            with tmp.open("w", encoding="utf-8") as fp:
                for key, (ts, entry) in self._data.items():
                    fp.write(json.dumps({"k": key, "v": entry.to_dict(), "t": ts}, ensure_ascii=False) + "\n")
            tmp.replace(self.file_path)
            self._disk_lines = len(self._data)
            log.info("ref-index compact: %d 行 -> %d 行", before, self._disk_lines)
        except OSError as exc:
            log.error("ref-index compact 失败: %s", exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # -------------------------------------------------- 诊断
    def __len__(self) -> int:
        return len(self._data)

    def stats(self) -> dict:
        return {
            "entries": len(self._data),
            "disk_lines": self._disk_lines,
            "max_entries": self.max_entries,
            "ttl_seconds": self.ttl,
            "file": str(self.file_path),
        }

    def close(self) -> None:
        with self._lock:
            self._compact_locked()
