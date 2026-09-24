"""QQ 机器人高层封装：事件分发 + 消息回复。

用法::

    from qqbot import QQBot, BotConfig, Event

    bot = QQBot(BotConfig.from_env())

    @bot.on("C2C_MESSAGE_CREATE")
    def on_c2c(event: Event):
        print("收到单聊消息:", event.content)
        bot.reply(event, f"你说的是：{event.content}")

    @bot.on("GROUP_AT_MESSAGE_CREATE")
    def on_group(event: Event):
        bot.reply(event, "记账机器人已就绪")

    bot.run()
"""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .agent import AgentResult, AgentRunner, Deduplicator, SessionManager, build_prompt

from .api import QQBotAPI
from .config import BotConfig, setup_logging
from . import media
from .quote import QuoteResolver, ResolvedQuote
from .refindex import (
    JsonlRefIndexStore,
    MemoryRefIndexStore,
    RefAttachment,
    RefEntry,
    RefIndices,
    build_attachment_summaries,
    classify_content_type,
    hint_to_kind,
    parse_ref_indices,
)
from .ws import GatewayClient

log = logging.getLogger("qqbot.bot")

Handler = Callable[["Event"], Any]

#: 需要归档进引用索引的事件（都是「一条消息」语义）
_INDEXABLE_EVENTS = frozenset(
    {
        "C2C_MESSAGE_CREATE",
        "GROUP_AT_MESSAGE_CREATE",
        "GROUP_MESSAGE_CREATE",
        "AT_MESSAGE_CREATE",
        "MESSAGE_CREATE",
        "DIRECT_MESSAGE_CREATE",
    }
)


def _short(value: str, keep: int = 20) -> str:
    text = str(value or "")
    return text if len(text) <= keep else f"{text[:keep]}…"


#: 输出清洗：万一 agent 把内部信息写进回复，这里再拦一道
_LEAK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{8,}=*"), "Bearer ***"),
    (re.compile(r"(?i)\bEBKTOOL_(?:TOKEN|SERVER_BASEURL)\b\s*[=:]\s*\S+"), "EBKTOOL_*=***"),
    (re.compile(r"(?i)\bQQ_BOT_(?:CLIENT_SECRET|APP_ID)\b\s*[=:]\s*\S+"), "QQ_BOT_*=***"),
    (re.compile(r"(?<![\w/])/(?:opt|home|root|Users|var|etc|srv)/[\w./\-]{2,}"), "<path>"),
]


def sanitize_reply(text: str, max_chars: int = 500) -> str:
    """把 agent 输出整理成适合发到 QQ 的纯文本。"""
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        return ""
    for pattern, replacement in _LEAK_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 1].rstrip() + "…"
    return cleaned.strip()


@dataclass
class Event:
    """一个网关事件。"""

    name: str
    data: dict = field(default_factory=dict)
    id: str = ""
    seq: Optional[int] = None
    raw: dict = field(default_factory=dict)
    #: 引用解析结果（由 QQBot 在分发前填好）；不是引用消息时为 None
    quote: Optional[ResolvedQuote] = None
    #: 本消息附件落盘结果：{附件下标: 本地路径}
    local_attachments: dict = field(default_factory=dict)

    # ---------------------------------------------------------------- 通用字段
    @property
    def content(self) -> str:
        return str(self.data.get("content") or "")

    @property
    def timestamp(self) -> str:
        return str(self.data.get("timestamp") or "")

    @property
    def author(self) -> dict:
        return self.data.get("author") or {}

    @property
    def msg_id(self) -> str:
        """用于被动回复的消息 ID（事件的 d.id）。"""
        return str(self.data.get("id") or self.id or "")

    # ---------------------------------------------------------------- 消息类型
    @property
    def message_type(self) -> int:
        """消息内容类型：0=纯文本, 3=结构化卡片, 101=并行消息, 102=聊天记录, 103=引用消息。"""
        value = self.data.get("message_type")
        return value if isinstance(value, int) else -1

    @property
    def is_quoted(self) -> bool:
        """是否为引用消息（message_type=103）。"""
        return self.message_type == 103

    @property
    def is_text_only(self) -> bool:
        """纯文本且无附件。"""
        return not self.attachments and not self.quoted_attachments

    # ---------------------------------------------------------------- 附件
    @property
    def attachments(self) -> list[dict]:
        """当前消息的附件列表（图片/语音/视频/文件）。"""
        raw = self.data.get("attachments")
        return [a for a in raw if isinstance(a, dict)] if isinstance(raw, list) else []

    @property
    def images(self) -> list[dict]:
        """当前消息里的图片附件。"""
        return [a for a in self.attachments if media.is_image(a)]

    @property
    def voice(self) -> Optional[dict]:
        """语音附件（含 ``voice_wav_url`` 与 ``asr_refer_text``），无则 None。"""
        for attachment in self.attachments:
            if media.is_voice(attachment):
                return attachment
        return None

    @property
    def asr_text(self) -> str:
        """语音消息的 ASR 识别文本，无则空字符串。"""
        attachment = self.voice
        if not attachment:
            return ""
        return str(attachment.get("asr_refer_text") or "")

    @property
    def files(self) -> list[dict]:
        """既不是图片也不是语音的其余附件。"""
        return [a for a in self.attachments if not media.is_image(a) and not media.is_voice(a)]

    @property
    def msg_elements(self) -> list[dict]:
        """引用消息（message_type=103）里被引用的内容元素。"""
        raw = self.data.get("msg_elements")
        return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []

    @property
    def quoted_attachments(self) -> list[dict]:
        """被引用消息里的附件（递归展开 msg_elements）。

        官方示例只演示了引用文本，引用图片是否回传附件需实测；
        拿不到时返回空列表，不会报错。
        """
        return media.collect_attachments(self.msg_elements)

    @property
    def quoted_images(self) -> list[dict]:
        """被引用消息里的图片。"""
        return [a for a in self.quoted_attachments if media.is_image(a)]

    @property
    def quoted_text(self) -> str:
        """被引用消息里的文本（递归拼接 msg_elements 的 content）。"""
        parts: list[str] = []

        def walk(elements: list) -> None:
            for element in elements or []:
                if not isinstance(element, dict):
                    continue
                text = str(element.get("content") or "").strip()
                if text:
                    parts.append(text)
                walk(element.get("msg_elements") or [])

        walk(self.msg_elements)
        return "\n".join(parts)

    @property
    def parallel_message(self) -> dict:
        """并行消息节点（QUOTE 场景里描述被引用内容）。"""
        raw = self.data.get("parallel_message")
        return raw if isinstance(raw, dict) else {}

    @property
    def parallel_nodes(self) -> list[dict]:
        """``parallel_message.msg_nodes``，引用图片时 content 是 "[图片]" 占位符。"""
        nodes = self.parallel_message.get("msg_nodes")
        return [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else []

    @property
    def quoted_summary(self) -> str:
        """被引用内容的摘要文本。

        引用图片时这里是占位符 ``[图片]``，**不包含图片 URL**。
        """
        parts = [str(n.get("content") or "") for n in self.parallel_nodes]
        if not parts:
            parts = [str(e.get("content") or "") for e in self.msg_elements]
        return "\n".join(p for p in parts if p.strip())

    @property
    def quoted_kinds(self) -> set[str]:
        """被引用内容的媒体类型集合（根据占位符 / message_type 推断）。

        :return: ``{"image"}`` / ``{"video"}`` / ``set()`` 等
        """
        kinds: set[str] = set()
        for node in self.parallel_nodes:
            kind = hint_to_kind(node.get("content"))
            if kind:
                kinds.add(kind)
            elif node.get("message_type") == 7:
                kinds.add("media")
        for attachment in self.quoted_attachments:
            if media.is_image(attachment):
                kinds.add("image")
            elif media.is_voice(attachment):
                kinds.add("voice")
        return kinds

    @property
    def quoted_is_image(self) -> bool:
        """被引用的内容是否包含图片。

        注意：实测（2026-09）单聊引用图片时，``msg_elements`` 里
        **不含 attachments**，只有 ``parallel_message`` 的 ``[图片]`` 占位符，
        所以只能判断“引用了图”，拿不到图片 URL。
        图片本身要靠图片事件时已落盘的记录（按时间窗口配对）。
        """
        return "image" in self.quoted_kinds

    @property
    def all_images(self) -> list[dict]:
        """本消息 + 被引用消息里的全部图片。"""
        return self.images + self.quoted_images

    # ---------------------------------------------------------------- 场景字段
    @property
    def scene_ext(self) -> dict[str, str]:
        """``message_scene.ext`` 解析后的 dict（msg_idx / ref_msg_idx / auth_token）。"""
        return media.parse_scene_ext(self.scene_ext_raw)

    @property
    def scene_ext_raw(self) -> list:
        """``message_scene.ext`` 原始列表，形如 ``["msg_idx=REFIDX_xx=="]``。"""
        scene = self.data.get("message_scene")
        if not isinstance(scene, dict):
            return []
        ext = scene.get("ext")
        return ext if isinstance(ext, list) else []

    @property
    def ref_indices(self) -> RefIndices:
        """引用索引（官方规则：103 引用消息时 ``msg_elements[0].msg_idx`` 优先）。"""
        return parse_ref_indices(self.scene_ext_raw, self.message_type, self.msg_elements)

    @property
    def msg_idx(self) -> str:
        """本条消息的索引，作为 ref-index 的写入键。"""
        return self.scene_ext.get("msg_idx", "")

    @property
    def ref_msg_idx(self) -> str:
        """被引用消息的索引（引用场景才有）。

        注意：``message_type == 103`` 时以 ``msg_elements[0].msg_idx`` 为准，
        而不是 ``message_scene.ext`` 里的 ``ref_msg_idx``。
        """
        return self.ref_indices.ref_msg_idx or ""

    @property
    def auth_token(self) -> str:
        """事件附带的鉴权令牌（群聊事件里才有），下载附件时可能需要。"""
        return self.scene_ext.get("auth_token", "")

    # ---------------------------------------------------------------- 场景字段
    @property
    def user_openid(self) -> str:
        """单聊用户 OpenID。"""
        return str(self.author.get("user_openid") or self.author.get("id") or "")

    @property
    def member_openid(self) -> str:
        """群聊中发送者的 OpenID。"""
        return str(self.author.get("member_openid") or self.author.get("id") or "")

    @property
    def group_openid(self) -> str:
        """群 OpenID（GROUP_AT_MESSAGE_CREATE 事件）。"""
        return str(self.data.get("group_openid") or "")

    @property
    def channel_id(self) -> str:
        return str(self.data.get("channel_id") or "")

    @property
    def guild_id(self) -> str:
        return str(self.data.get("guild_id") or "")

    def __str__(self) -> str:  # pragma: no cover - 仅日志用
        extra = ""
        if self.attachments:
            kinds = [str(a.get("content_type") or "?") for a in self.attachments]
            extra = f" attachments={kinds}"
        if self.is_quoted:
            extra += f" quoted_images={len(self.quoted_images)}"
        if self.quote is not None:
            extra += f" quote[{self.quote.source}]"
        return (
            f"<Event {self.name} id={self.id} type={self.message_type} "
            f"content={self.content[:40]!r}{extra}>"
        )


class QQBot:
    """把鉴权、网关订阅、消息发送组装到一起的机器人实例。"""

    def __init__(self, config: Optional[BotConfig] = None, **overrides):
        if config is None:
            config = BotConfig.from_env(**overrides)
        self.config = config
        setup_logging(config.log_level)

        self.api = QQBotAPI(config)
        self._handlers: dict[str, list[Handler]] = {}
        self._gateway: Optional[GatewayClient] = None
        self._bot_user: Optional[dict] = None

        # 引用索引：收到消息就建档，收到引用消息时回查
        self.refindex: Optional[object] = None
        self.quotes = QuoteResolver(None)
        if config.ref_index_enabled:
            self.refindex = JsonlRefIndexStore(
                config.ref_index_path,
                max_entries=config.ref_index_max_entries,
                ttl=config.ref_index_ttl_days * 24 * 60 * 60,
            )
            self.quotes = QuoteResolver(self.refindex)
            log.info("引用索引已启用: %s", self.refindex.stats())

        # agent 层：会话 / 去重 / 线程池
        self.sessions = SessionManager(config.session_prefix, config.session_rotation)
        self.dedup = Deduplicator(config.dedup_ttl)
        self.agent = AgentRunner(config)
        self._executor = ThreadPoolExecutor(
            max_workers=config.agent_max_concurrency, thread_name_prefix="qqbot-agent"
        )
        self._user_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._closed = False
        log.info(
            "agent 配置: enabled=%s rotation=%s concurrency=%d timeout=%.0fs cwd=%s",
            config.agent_enabled, config.session_rotation,
            config.agent_max_concurrency, config.agent_timeout, self.agent.cwd,
        )

    # ================================================================ 事件注册
    def on(self, event_name: str) -> Callable[[Handler], Handler]:
        """装饰器：注册事件处理器。event_name 传 "*" 表示接收所有事件。"""

        def decorator(func: Handler) -> Handler:
            self._handlers.setdefault(event_name, []).append(func)
            return func

        return decorator

    def add_handler(self, event_name: str, func: Handler) -> None:
        self._handlers.setdefault(event_name, []).append(func)

    # ================================================================ 生命周期
    def run(self) -> None:
        """阻塞运行：连接网关并持续接收事件，断线自动重连。"""
        self._gateway = GatewayClient(
            self.config,
            self.api,
            on_event=self._dispatch,
            on_ready=self._on_ready,
            on_resumed=self._on_resumed,
        )
        self._gateway.run_forever()

    def stop(self) -> None:
        self._closed = True
        if self._gateway:
            self._gateway.close()
        self._executor.shutdown(wait=False, cancel_futures=True)
        if self.refindex is not None:
            try:
                self.refindex.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                log.exception("关闭引用索引失败")

    @property
    def gateway(self) -> Optional[GatewayClient]:
        return self._gateway

    @property
    def bot_user(self) -> Optional[dict]:
        return self._bot_user

    def _on_ready(self, payload: dict) -> None:
        data = payload.get("d") or {}
        self._bot_user = data.get("user")
        user = self._bot_user or {}
        log.info(
            "机器人已上线: %s (id=%s) session_id=%s",
            user.get("username", "?"),
            user.get("id", "?"),
            data.get("session_id"),
        )

    def _on_resumed(self, payload: dict) -> None:
        log.info("会话已恢复，继续接收事件")

    def _dispatch(self, payload: dict) -> None:
        name = payload.get("t") or ""
        event = Event(
            name=name,
            data=payload.get("d") if isinstance(payload.get("d"), dict) else {},
            id=str(payload.get("id") or ""),
            seq=payload.get("s"),
            raw=payload,
        )

        # 附件落盘（引用回查必须靠它：URL 带 rkey，有时效）
        try:
            event.local_attachments = self._auto_download(event)
        except Exception:  # noqa: BLE001 - 落盘失败不能影响业务
            log.exception("附件落盘失败: %s", event.name)

        # 引用索引：先归档当前消息，再解析引用（对标官方 quoteRef 中间件）
        self._index_event(event)
        try:
            event.quote = self.quotes.resolve(event)
        except Exception:  # noqa: BLE001 - 解析失败不能影响业务
            log.exception("解析引用失败: %s", event.name)

        handlers = list(self._handlers.get(name, ())) + list(self._handlers.get("*", ()))
        if not handlers:
            log.debug("未注册处理器的事件: %s", name)
            return
        for handler in handlers:
            try:
                handler(event)
            except Exception:  # noqa: BLE001 - 单个业务异常不影响连接
                log.exception("处理事件 %s 时出错", name)

    # ================================================================ 引用索引
    def _index_event(self, event: Event) -> None:
        """把一条入站消息归档到引用索引，供后续引用消息回查。"""
        if self.refindex is None:
            return
        # 只归档真正的消息类事件
        if event.name not in _INDEXABLE_EVENTS:
            return

        key = event.msg_idx or event.msg_id
        if not key:
            return

        try:
            local_paths = event.local_attachments or {}
            entry = RefEntry(
                message_id=event.msg_id,
                msg_idx=event.msg_idx,
                sender_id=event.user_openid or event.member_openid,
                sender_name=str(event.author.get("username") or ""),
                content=event.content[: self.config.ref_index_content_limit]
                if self.config.ref_index_content_limit
                else event.content,
                timestamp=event.timestamp,
                is_bot=bool(event.author.get("bot")),
                scope="group" if event.group_openid else "c2c",
                attachments=build_attachment_summaries(event.attachments, local_paths),
            )
            self.refindex.set(key, entry)  # type: ignore[attr-defined]
            log.debug(
                "引用索引写入 key=%s content=%r attachments=%d",
                _short(key), entry.content[:40], len(entry.attachments),
            )
        except Exception:  # noqa: BLE001
            log.exception("写入引用索引失败")

    def _auto_download(self, event: Event) -> dict[int, str]:
        """按配置把附件落盘，返回 {下标: 本地路径}。

        必须收到就下载：附件 URL 带 ``rkey`` 签名参数，有时效，
        等用户引用时再取就晚了。
        """
        dest_dir = self.config.auto_download_dir
        if not dest_dir or not event.attachments:
            return {}

        wanted = set(self.config.auto_download_kinds)
        saved: dict[int, str] = {}
        for index, attachment in enumerate(event.attachments):
            kind = classify_content_type(attachment.get("content_type"))
            if wanted and kind not in wanted:
                continue
            try:
                path = self.download_attachment(
                    attachment, dest_dir, index=index, max_bytes=self.config.auto_download_max_bytes
                )
                saved[index] = str(path)
                log.info("附件已落盘 [%d] %s -> %s", index, kind, path)
            except Exception as exc:  # noqa: BLE001
                log.warning("附件落盘失败 [%d] %s: %s", index, kind, exc)
        return saved

    # ================================================================ 附件
    def download_attachment(self, attachment: dict, dest_dir, **kwargs):
        """下载单个附件到 dest_dir（收到即落盘，别只存 URL）。"""
        return media.download_attachment(attachment, dest_dir, session=self.api.session, **kwargs)

    def download_event_attachments(
        self,
        event: Event,
        dest_dir,
        *,
        only: str = "all",
        max_bytes: Optional[int] = None,
    ) -> list:
        """把事件里的附件全部落盘，返回路径列表。

        :param only: ``all`` / ``images`` / ``quoted`` / ``voice``
        """
        if only == "images":
            targets = event.images
        elif only == "quoted":
            targets = event.quoted_attachments
        elif only == "voice":
            targets = [event.voice] if event.voice else []
        else:
            targets = event.attachments

        saved = []
        for index, attachment in enumerate(targets):
            saved.append(
                self.download_attachment(attachment, dest_dir, index=index, max_bytes=max_bytes)
            )
        return saved

    # ================================================================ 交给 agent
    def dispatch_to_agent(self, event: Event) -> bool:
        """把一条消息交给 pi agent 处理（**非阻塞**）。

        返回 True 表示已提交到后台线程。以下情况直接返回 False：

        * agent 未启用
        * 机器人自己发的消息
        * openid 不在白名单
        * **没有文本**（纯图片消息：只归档、不回复、不启 agent）
        * 重复推送（msg_id 命中去重）
        * 该用户上一条还在处理中

        为什么必须放后台：网关的事件回调跑在**接收循环**里，
        阻塞超过 2 个心跳周期（约 90s）会被判定为僵尸连接而重连。
        """
        cfg = self.config
        if not cfg.agent_enabled or self._closed:
            return False
        if event.author.get("bot"):
            return False

        openid = event.user_openid or event.member_openid
        if cfg.allowed_openids and openid not in cfg.allowed_openids:
            log.info("openid 不在白名单，忽略: %s", _short(openid))
            return False

        # 纯图片消息（或任何无文本消息）：只归档，静默
        if not event.content.strip():
            log.info(
                "无文本消息（图片 %d 张），仅归档不处理: %s",
                len(event.images), _short(event.msg_id),
            )
            return False

        if self.dedup.is_duplicate(event.msg_id or event.id):
            log.info("重复推送，忽略: %s", _short(event.msg_id))
            return False

        session_id = self.sessions.session_id(openid)
        try:
            prompt, images = build_prompt(event, cfg)
        except Exception:  # noqa: BLE001
            log.exception("组装 prompt 失败")
            return False

        lock = self._lock_for(openid)
        if not lock.acquire(blocking=False):
            log.warning("该用户上一条还在处理，忽略本条: %s", _short(openid))
            self.safe_reply(event, "上一条还在处理，等几秒再发～")
            return False

        try:
            self._executor.submit(self._agent_job, event, lock, session_id, prompt, images)
        except RuntimeError:
            lock.release()
            log.error("线程池已关闭，无法处理本条消息")
            return False

        log.info("已提交 agent: session=%s queued=%d", session_id, self._queued_jobs())
        return True

    def _queued_jobs(self) -> int:
        """线程池里还在排队的任务数（仅用于日志）。"""
        try:
            queue = getattr(self._executor, "_work_queue", None)
            return max(int(queue.qsize()) - 1, 0) if queue is not None else 0
        except Exception:  # noqa: BLE001
            return 0

    def _agent_job(
        self,
        event: Event,
        lock: threading.Lock,
        session_id: str,
        prompt: str,
        images: list[str],
    ) -> None:
        try:
            result = self.agent.run(prompt, session_id=session_id, images=images)
            self.on_agent_result(event, result)
        except Exception:  # noqa: BLE001 - 后台线程不能裸死
            log.exception("agent 任务异常")
        finally:
            lock.release()

    def on_agent_result(self, event: Event, result: AgentResult) -> None:
        """把 agent 结果回给用户。可覆写成自定义行为。"""
        if result.usable:
            self.safe_reply(event, result.text)
        elif result.timed_out:
            self.safe_reply(event, "处理超时了，稍后再试一次～")
        else:
            self.safe_reply(event, "我这边处理出错了，稍后再试一次～")

    def safe_reply(self, event: Event, text: str) -> Optional[dict]:
        """清洗 + 截断后再回复，任何异常都只记日志。"""
        cleaned = sanitize_reply(text, self.config.reply_max_chars)
        if not cleaned:
            log.warning("清洗后回复为空，跳过")
            return None
        try:
            return self.reply(event, cleaned)
        except Exception:  # noqa: BLE001 - 回复失败不能影响连接
            log.exception("回复失败")
            return None

    def _lock_for(self, openid: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._user_locks.get(openid)
            if lock is None:
                lock = threading.Lock()
                self._user_locks[openid] = lock
            return lock

    @property
    def inflight_agents(self) -> int:
        """还在处理的 agent 数（近似值，仅用于日志/诊断）。"""
        return sum(1 for lock in self._user_locks.values() if lock.locked())

    # ================================================================ 发送消息
    def send_c2c(self, user_openid: str, content: str, **kwargs) -> dict:
        """主动/被动发送单聊消息。"""
        return self.api.send_c2c_message(user_openid, content=content, **kwargs)

    def send_group(self, group_openid: str, content: str, **kwargs) -> dict:
        """主动/被动发送群聊消息。"""
        return self.api.send_group_message(group_openid, content=content, **kwargs)

    def send_channel(self, channel_id: str, content: str, **kwargs) -> dict:
        return self.api.send_channel_message(channel_id, content=content, **kwargs)

    def reply(
        self,
        event: Event,
        content: Optional[str] = None,
        *,
        msg_seq: Optional[int] = None,
        **kwargs,
    ) -> dict:
        """按事件来源自动选择单聊/群聊/频道接口做被动回复。

        被动回复必须带 msg_id，且同一 msg_id 下 msg_seq 不能重复，
        否则会命中 40054005（消息被去重）。
        """
        channel = kwargs.pop("channel", None)

        if channel is None:
            channel = {
                "C2C_MESSAGE_CREATE": "c2c",
                "GROUP_AT_MESSAGE_CREATE": "group",
                "GROUP_MESSAGE_CREATE": "group",
                "AT_MESSAGE_CREATE": "channel",
                "MESSAGE_CREATE": "channel",
                "DIRECT_MESSAGE_CREATE": "channel",
            }.get(event.name)

        if channel == "c2c":
            return self.api.send_c2c_message(
                event.user_openid, content=content, msg_id=event.msg_id, msg_seq=msg_seq, **kwargs
            )
        if channel == "group":
            return self.api.send_group_message(
                event.group_openid, content=content, msg_id=event.msg_id, msg_seq=msg_seq, **kwargs
            )
        if channel == "channel":
            return self.api.send_channel_message(
                event.channel_id, content=content, msg_id=event.msg_id, **kwargs
            )
        raise ValueError(f"事件 {event.name} 不支持自动回复，请显式调用 send_c2c / send_group")
