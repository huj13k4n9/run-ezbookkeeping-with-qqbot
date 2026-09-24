"""引用消息解析：把「用户在回复哪条消息」还原出来。

对标官方 Node SDK 的 `quoteRef` 中间件，但做了两处改进：

1. **两级解析而不是用完即弃** —— ``msg_elements[0]`` 与 ref-index store 的结果
   会**合并**而不是二选一：store 里有落盘好的 ``local_path``（附件 URL 有时效），
   ``msg_elements`` 里有带签名的原始 URL 和 ASR 文本。
2. **报告命中来源** —— ``ResolvedQuote.source`` 取值 ``both`` / ``store`` /
   ``msg_elements`` / ``none``，方便定位「为什么引用的图拿不到」。

只处理**引用消息**：没有 ``ref_msg_idx`` 的消息直接返回 ``None``，
绝不用「最近一张图」之类的时间窗口去猜，避免和普通文本消息混淆。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from .refindex import (
    RefAttachment,
    RefEntry,
    RefIndexStore,
    build_attachment_summaries,
    collect_flat_attachments,
    hint_to_kind,
    parse_ref_indices,
)

if TYPE_CHECKING:  # pragma: no cover
    from .bot import Event

log = logging.getLogger("qqbot.quote")

#: 引用来源
SOURCE_BOTH = "both"
SOURCE_STORE = "store"
SOURCE_ELEMENTS = "msg_elements"
SOURCE_NONE = "none"


@dataclass
class ResolvedQuote:
    """一条已解析（或已确认解析失败）的引用。"""

    #: 用于回查的引用键（msg_elements[0].msg_idx 或 ext 里的 ref_msg_idx）
    ref_key: str = ""
    #: 命中来源，见 SOURCE_* 常量
    source: str = SOURCE_NONE
    #: store 命中的原始记录（可能为 None）
    entry: Optional[RefEntry] = None
    #: 被引用消息的文本（含附件占位符）
    text: str = ""
    #: 被引用消息的附件摘要（带 local_path）
    attachments: list[RefAttachment] = field(default_factory=list)
    #: 从 msg_elements[0] 取到的原文
    raw_content: str = ""
    #: 平台给出的被引用内容摘要，如 "[图片]"（解析失败时仍有值）
    hint: str = ""
    #: 【仅诊断，不参与解析】疑似同一消息但 REFIDX 已变的候选（近期归档且类型匹配 hint）
    candidates: list[tuple[str, RefEntry]] = field(default_factory=list)

    # ---------------------------------------------------------------- 便捷判断
    @property
    def resolved(self) -> bool:
        """是否真的解析出了内容。"""
        return self.source != SOURCE_NONE

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)

    @property
    def media(self) -> list[RefAttachment]:
        """非 unknown 类型的附件（图片/语音/视频/文件）。"""
        return [a for a in self.attachments if a.type != "unknown"]

    @property
    def images(self) -> list[RefAttachment]:
        return [a for a in self.attachments if a.type == "image"]

    @property
    def voice(self) -> Optional[RefAttachment]:
        for attachment in self.attachments:
            if attachment.type == "voice":
                return attachment
        return None

    @property
    def local_paths(self) -> list[str]:
        """已落盘的附件本地路径（引用图片时就是它）。"""
        return [a.local_path for a in self.attachments if a.local_path]

    def describe(self) -> str:
        """单行诊断信息。"""
        bits = [f"source={self.source}", f"ref={self.ref_key[:24]}…" if self.ref_key else "ref=<none>"]
        if self.text:
            bits.append(f"text={self.text!r}")
        if self.attachments:
            kinds = [f"{a.type}:{a.filename or '-'}" for a in self.attachments]
            bits.append("attachments=" + ",".join(kinds))
        if not self.resolved and self.hint:
            bits.append(f"hint={self.hint!r}")
        return " ".join(bits)


class QuoteResolver:
    """根据 ref-index store + 事件自带的 msg_elements 解析引用消息。"""

    def __init__(self, store: Optional[RefIndexStore] = None):
        self.store = store

    # ---------------------------------------------------------------- 主入口
    def resolve(self, event: "Event") -> Optional[ResolvedQuote]:
        """解析引用；不是引用消息时返回 ``None``。"""
        indices = event.ref_indices
        ref_key = indices.ref_msg_idx or ""
        if not ref_key:
            # 没有引用键 -> 不是引用消息，不猜
            return None

        entry = self.store.get(ref_key) if self.store else None
        elements_attachments = collect_flat_attachments(event.msg_elements)
        elements_content = ""
        if event.msg_elements:
            elements_content = str(event.msg_elements[0].get("content") or "").strip()
        has_elements = bool(elements_content or elements_attachments)

        if entry and has_elements:
            source = SOURCE_BOTH
        elif entry:
            source = SOURCE_STORE
        elif has_elements:
            source = SOURCE_ELEMENTS
        else:
            source = SOURCE_NONE

        if source == SOURCE_NONE:
            candidates = self._find_candidates(event)
            self._log_miss(ref_key, event, candidates)
            return ResolvedQuote(
                ref_key=ref_key,
                source=SOURCE_NONE,
                text="",
                hint=event.quoted_summary,
                candidates=candidates,
            )

        attachments = self._merge_attachments(entry, elements_attachments)
        text = self._build_text(entry, elements_content, attachments)

        quote = ResolvedQuote(
            ref_key=ref_key,
            source=source,
            entry=entry,
            text=text,
            attachments=attachments,
            raw_content=elements_content,
            hint=event.quoted_summary,
        )
        age = f"（归档于 {time.time() - entry.archived_at:.0f}s 前）" if entry and entry.archived_at else ""
        log.info("引用解析成功: %s%s", quote.describe(), age)
        return quote

    # ---------------------------------------------------------------- 内部
    def _find_candidates(self, event: "Event") -> list[tuple[str, RefEntry]]:
        """【仅诊断】找出「疑似就是被引用那条」的近期归档。

        实测发现：机器人**被动回复过**的消息，其 REFIDX 会变，
        导致引用时给的新 REFIDX 在本地缓存里查不到。
        这里按平台 hint（如 ``[图片]``）匹配最近归档里同类型的条目，
        只用于日志提示，**不参与解析**，避免把纯文本消息混淆进来。
        """
        kind = hint_to_kind(event.quoted_summary)
        if not kind:
            return []
        recent = getattr(self.store, "recent", None)
        if not callable(recent):
            return []
        try:
            items = recent(10)
        except Exception:  # noqa: BLE001 - 诊断失败不能影响主流程
            return []
        return [(key, entry) for key, entry in items
                if any(a.type == kind for a in entry.attachments)]

    def _log_miss(
        self,
        ref_key: str,
        event: "Event",
        candidates: Optional[list[tuple[str, RefEntry]]] = None,
    ) -> None:
        """引用 miss 时把线索打出来。"""
        log.info("引用解析失败: ref=%s hint=%r", _short(ref_key, 28), event.quoted_summary)
        recent = getattr(self.store, "recent", None)
        if not callable(recent):
            return
        try:
            items = recent(5)
        except Exception:  # noqa: BLE001
            return
        for key, entry in items:
            age = time.time() - (entry.archived_at or 0) if entry.archived_at else -1
            log.info(
                "  最近归档: key=%s  age=%s  content=%r  attachments=%d",
                _short(key, 28),
                f"{age:.0f}s" if age >= 0 else "?",
                entry.content[:24],
                len(entry.attachments),
            )
        for key, entry in candidates or []:
            age = time.time() - (entry.archived_at or 0) if entry.archived_at else -1
            log.warning(
                "  疑似 REFIDX 已变: key=%s  age=%s  （类型匹配 %s；"
                "常见原因是该消息被机器人被动回复过，QQ 会重新生成 REFIDX）",
                _short(key, 28),
                f"{age:.0f}s" if age >= 0 else "?",
                event.quoted_summary,
            )
    @staticmethod
    def _merge_attachments(
        entry: Optional[RefEntry], elements_attachments: list[dict]
    ) -> list[RefAttachment]:
        """以 store 记录为主（有 local_path），msg_elements 作为补充。"""
        if entry and entry.attachments:
            return list(entry.attachments)
        if elements_attachments:
            return build_attachment_summaries(elements_attachments)
        return []

    @staticmethod
    def _build_text(
        entry: Optional[RefEntry],
        elements_content: str,
        attachments: list[RefAttachment],
    ) -> str:
        if entry and entry.text.strip():
            return entry.text.strip()
        if elements_content:
            return elements_content
        if attachments:
            return "\n".join(a.render() for a in attachments)
        return ""


def _short(value: str, keep: int = 16) -> str:
    return value if len(value) <= keep else f"{value[:keep]}…"
