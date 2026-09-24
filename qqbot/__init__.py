"""QQ 机器人接入 SDK（鉴权 + WebSocket 消息订阅 + 消息发送）。

快速开始::

    from qqbot import QQBot, Event

    bot = QQBot()   # 自动读取 .env / 环境变量中的 QQ_BOT_APP_ID / QQ_BOT_CLIENT_SECRET

    @bot.on("C2C_MESSAGE_CREATE")
    def _(event: Event):
        bot.reply(event, f"收到：{event.content}")

    bot.run()
"""

from .agent import (
    AgentResult,
    AgentRunner,
    Deduplicator,
    SessionManager,
    build_agent_env,
    build_prompt,
)
from .api import QQBotAPI
from .auth import AccessTokenManager, mask
from .bot import Event, QQBot
from .config import SANDBOX_API_BASE, PROD_API_BASE, BotConfig, load_env_file, setup_logging
from .errors import (
    FatalWebSocketError,
    QQBotAPIError,
    QQBotAuthError,
    QQBotConfigError,
    QQBotError,
    QQBotWebSocketError,
)
from .intents import C2C_EVENTS, EVENT_INTENTS, Intents, describe
from .quote import (
    SOURCE_BOTH,
    SOURCE_ELEMENTS,
    SOURCE_NONE,
    SOURCE_STORE,
    QuoteResolver,
    ResolvedQuote,
)
from .refindex import (
    MSG_TYPE_QUOTE,
    QUOTED_HINT_KINDS,
    JsonlRefIndexStore,
    MemoryRefIndexStore,
    RefAttachment,
    RefEntry,
    RefIndexStore,
    RefIndices,
    build_attachment_summaries,
    classify_content_type,
    collect_flat_attachments,
    hint_to_kind,
    parse_ref_indices,
)
from .media import (
    collect_attachments,
    download_attachment,
    guess_filename,
    is_image,
    is_voice,
    parse_scene_ext,
)
from .ws import GatewayClient

__version__ = "1.0.0"

__all__ = [
    "QQBot",
    "Event",
    "AgentResult",
    "AgentRunner",
    "SessionManager",
    "Deduplicator",
    "build_prompt",
    "build_agent_env",
    "QQBotAPI",
    "BotConfig",
    "GatewayClient",
    "AccessTokenManager",
    "Intents",
    "EVENT_INTENTS",
    "C2C_EVENTS",
    "describe",
    "RefIndexStore",
    "JsonlRefIndexStore",
    "MemoryRefIndexStore",
    "RefEntry",
    "RefAttachment",
    "RefIndices",
    "parse_ref_indices",
    "build_attachment_summaries",
    "collect_flat_attachments",
    "classify_content_type",
    "hint_to_kind",
    "QUOTED_HINT_KINDS",
    "MSG_TYPE_QUOTE",
    "QuoteResolver",
    "ResolvedQuote",
    "SOURCE_BOTH",
    "SOURCE_STORE",
    "SOURCE_ELEMENTS",
    "SOURCE_NONE",
    "download_attachment",
    "collect_attachments",
    "guess_filename",
    "parse_scene_ext",
    "is_image",
    "is_voice",
    "mask",
    "load_env_file",
    "setup_logging",
    "PROD_API_BASE",
    "SANDBOX_API_BASE",
    "QQBotError",
    "QQBotConfigError",
    "QQBotAuthError",
    "QQBotAPIError",
    "QQBotWebSocketError",
    "FatalWebSocketError",
    "__version__",
]
