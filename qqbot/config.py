"""配置加载：环境变量优先，其次读取 .env 文件。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

from .errors import QQBotConfigError
from .intents import Intents

#: 正式环境
PROD_API_BASE = "https://api.bot.qq.com"
#: 沙箱环境（旧版域名，仅当 QQ_BOT_SANDBOX=1 时使用）
SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"

_TRUTHY = {"1", "true", "yes", "on", "y"}


def load_env_file(path: str | os.PathLike = ".env", *, override: bool = False) -> None:
    """极简 .env 解析，避免引入 python-dotenv 依赖。

    默认不覆盖已存在的环境变量（环境变量优先级更高）。
    """
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if override or key not in os.environ:
            os.environ[key] = value


def _env(*names: str) -> Optional[str]:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v.strip()
    return None


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in _TRUTHY


def _env_list(*names: str) -> Tuple[str, ...]:
    """逗号分隔的环境变量 -> 元组（会自动 strip 并去空）。"""
    raw = _env(*names)
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _env_opt_str(*names: str) -> Optional[str]:
    """空字符串视为未设置。"""
    raw = _env(*names)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _env_int(*names: str, default: int) -> int:
    v = _env(*names)
    if v is None:
        return default
    try:
        return int(v, 0)  # 支持 0x / 0b 写法
    except ValueError:
        return default


@dataclass
class BotConfig:
    """QQ 机器人运行配置。"""

    app_id: str
    client_secret: str

    #: OpenAPI / 网关的基地址
    api_base: str = PROD_API_BASE
    #: 是否使用沙箱环境
    sandbox: bool = False

    #: 订阅的事件位图（默认不含公域频道事件）
    intents: int = int(Intents.DEFAULT)
    #: [shard_id, shard_total]，不分片用 (0, 1)
    shard: Tuple[int, int] = (0, 1)

    #: access_token 提前刷新时间（秒），文档建议过期前 60s 内刷新
    token_refresh_margin: int = 60
    #: 单次 HTTP 请求超时（秒）
    request_timeout: float = 15.0
    #: websocket 建连超时（秒）
    connect_timeout: float = 20.0
    #: 重连退避上限（秒）
    max_reconnect_delay: float = 60.0

    log_level: str = "INFO"
    user_agent: str = "run-bookkeeping-qqbot/1.0"

    #: 自动回复时使用的 msg_seq 起始值
    default_msg_seq: int = 1

    # ---------------- 引用索引（ref-index）----------------
    #: 是否启用引用索引（收到消息就建档，引用时回查）
    ref_index_enabled: bool = True
    #: 索引持久化文件（JSONL）
    ref_index_path: str = "data/ref-index.jsonl"
    #: 索引条目 TTL（天），官方默认 7 天
    ref_index_ttl_days: int = 7
    #: 索引最大条数，官方默认 50000
    ref_index_max_entries: int = 50_000
    #: 单条消息文本入库前的截断长度（防止超大文本吃内存）
    ref_index_content_limit: int = 2000

    # ---------------- 附件自动落盘 ----------------
    #: 附件落盘目录；None 表示不自动下载（此时引用回查拿不到本地文件）
    auto_download_dir: Optional[str] = None
    #: 自动落盘的附件类型：image / voice / video / file
    auto_download_kinds: Tuple[str, ...] = ("image", "voice")
    #: 单个附件大小上限（字节）
    auto_download_max_bytes: int = 20 * 1024 * 1024

    # ---------------- 会话（pi session）----------------
    #: 会话轮换周期：day / week / none
    session_rotation: str = "day"
    #: 会话 ID 前缀
    session_prefix: str = "qq"
    #: 时区（用于计算时间桶与 prompt 里的时间字符串）
    timezone: str = "Asia/Shanghai"

    # ---------------- 去重 ----------------
    #: msg_id 去重窗口（秒）；QQ 会重复推送同一条消息
    dedup_ttl: int = 600

    # ---------------- 回复策略 ----------------
    #: 金额 >= 该值时先向用户确认再入账；0 = 关闭确认
    confirm_amount_threshold: int = 1000
    #: 回复硬截断字数（QQ 超长会报 40054007）
    reply_max_chars: int = 500
    #: openid 白名单；空 = 不限制
    allowed_openids: Tuple[str, ...] = ()

    # ---------------- pi agent ----------------
    #: 是否启用 agent（关闭后只归档，不处理）
    agent_enabled: bool = True
    #: pi 可执行文件（可填绝对路径）
    agent_command: str = "pi"
    #: pi 的工作目录（决定 AGENTS.md 从哪里被发现）
    #: 支持绝对路径；相对路径按 **bot 的 cwd** 解析。
    #: QQ bot 代码与 agent 目录通常不在一起，部署时建议直接写绝对路径。
    agent_cwd: str = "agent"
    #: ebktools.sh 路径（会写进 prompt 的 [工具] 行）；相对路径同样按 bot 的 cwd 解析
    ebktools_path: str = ".agents/skills/ezbookkeeping/scripts/ebktools.sh"
    #: 工具白名单
    agent_tools: str = "bash,read"
    #: 单次 agent 超时（秒）
    agent_timeout: float = 120.0
    #: 透传 --model（多模态模型填这里）；空 = 用 pi 默认模型
    agent_model: Optional[str] = None
    #: 全局并发上限（同时跑几个 agent）
    agent_max_concurrency: int = 2
    #: 追加任意 CLI 参数（空格分隔，如 "--thinking high"）
    agent_extra_args: Tuple[str, ...] = ()
    #: agent 进程要透传的环境变量名（其余不透传，避免泄露 QQ secret）
    #: 注意：EBKTOOL_* 必须在这里，否则 agent 无法调 ebktools.sh
    agent_passthrough_env: Tuple[str, ...] = (
        "PATH", "HOME", "LANG", "LC_ALL", "TZ",
        "PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR",
        "EBKTOOL_SERVER_BASEURL", "EBKTOOL_TOKEN",
    )

    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.app_id:
            raise QQBotConfigError("缺少 app_id（QQ_BOT_APP_ID）")
        if not self.client_secret:
            raise QQBotConfigError("缺少 client_secret（QQ_BOT_CLIENT_SECRET）")

        self.intents = int(self.intents)

        if self.sandbox and self.api_base == PROD_API_BASE:
            self.api_base = SANDBOX_API_BASE
        self.api_base = self.api_base.rstrip("/")
        shard_id, shard_total = self.shard
        if shard_total < 1 or not (0 <= shard_id < shard_total):
            raise QQBotConfigError(f"非法的 shard 配置: {self.shard}，应为 [0, total) 且 total>=1")

        self.session_rotation = (self.session_rotation or "day").strip().lower()
        if self.session_rotation not in ("day", "week", "none"):
            raise QQBotConfigError(
                f"非法的 session_rotation: {self.session_rotation}，应为 day / week / none"
            )
        if self.agent_max_concurrency < 1:
            raise QQBotConfigError("agent_max_concurrency 至少为 1")
        if self.reply_max_chars < 1:
            raise QQBotConfigError("reply_max_chars 至少为 1")

    @property
    def token_url(self) -> str:
        return f"{self.api_base}/app/getAppAccessToken"

    @property
    def gateway_url(self) -> str:
        return f"{self.api_base}/gateway"

    @classmethod
    def from_env(cls, env_file: str | os.PathLike | None = ".env", **overrides) -> "BotConfig":
        """从环境变量（可配合 .env）构造配置。"""
        if env_file:
            load_env_file(env_file)

        sandbox = _env_bool("QQ_BOT_SANDBOX", False)
        shard_id = _env_int("QQ_BOT_SHARD_ID", default=0)
        shard_total = _env_int("QQ_BOT_SHARD_TOTAL", default=1)

        params = dict(
            app_id=_env("QQ_BOT_APP_ID", "QQBOT_APPID", "APP_ID", "APPID") or "",
            client_secret=_env("QQ_BOT_CLIENT_SECRET", "QQBOT_SECRET", "CLIENT_SECRET", "APPSECRET") or "",
            api_base=_env("QQ_BOT_API_BASE") or (SANDBOX_API_BASE if sandbox else PROD_API_BASE),
            sandbox=sandbox,
            intents=_env_int("QQ_BOT_INTENTS", default=int(Intents.DEFAULT)),
            shard=(shard_id, shard_total),
            token_refresh_margin=_env_int("QQ_BOT_TOKEN_REFRESH_MARGIN", default=60),
            request_timeout=float(_env_int("QQ_BOT_REQUEST_TIMEOUT", default=15)),
            connect_timeout=float(_env_int("QQ_BOT_CONNECT_TIMEOUT", default=20)),
            log_level=(_env("QQ_BOT_LOG_LEVEL") or "INFO").upper(),
            ref_index_enabled=_env_bool("QQ_BOT_REF_INDEX_ENABLED", True),
            ref_index_path=_env("QQ_BOT_REF_INDEX_PATH") or "data/ref-index.jsonl",
            ref_index_ttl_days=_env_int("QQ_BOT_REF_INDEX_TTL_DAYS", default=7),
            ref_index_max_entries=_env_int("QQ_BOT_REF_INDEX_MAX_ENTRIES", default=50_000),
            auto_download_dir=_env("QQ_BOT_AUTO_DOWNLOAD_DIR"),
            auto_download_kinds=tuple(
                k.strip() for k in (_env("QQ_BOT_AUTO_DOWNLOAD_KINDS") or "image,voice").split(",") if k.strip()
            ),
            auto_download_max_bytes=_env_int("QQ_BOT_AUTO_DOWNLOAD_MAX_MB", default=20) * 1024 * 1024,
            session_rotation=(_env("QQ_BOT_SESSION_ROTATION") or "day"),
            session_prefix=_env("QQ_BOT_SESSION_PREFIX") or "qq",
            timezone=_env("QQ_BOT_TIMEZONE") or "Asia/Shanghai",
            dedup_ttl=_env_int("QQ_BOT_DEDUP_TTL", default=600),
            confirm_amount_threshold=_env_int("QQ_BOT_CONFIRM_AMOUNT_THRESHOLD", default=1000),
            reply_max_chars=_env_int("QQ_BOT_REPLY_MAX_CHARS", default=500),
            allowed_openids=_env_list("QQ_BOT_ALLOWED_OPENIDS"),
            agent_enabled=_env_bool("QQ_BOT_AGENT_ENABLED", True),
            agent_command=_env("QQ_BOT_AGENT_COMMAND") or "pi",
            agent_cwd=_env("QQ_BOT_AGENT_CWD") or "agent",
            ebktools_path=_env("QQ_BOT_EBKTOOLS_PATH")
            or ".agents/skills/ezbookkeeping/scripts/ebktools.sh",
            agent_tools=_env("QQ_BOT_AGENT_TOOLS") or "bash,read",
            agent_timeout=float(_env_int("QQ_BOT_AGENT_TIMEOUT", default=120)),
            agent_model=_env_opt_str("QQ_BOT_AGENT_MODEL"),
            agent_max_concurrency=_env_int("QQ_BOT_AGENT_MAX_CONCURRENCY", default=2),
            agent_extra_args=tuple(
                part for part in (_env("QQ_BOT_AGENT_EXTRA_ARGS") or "").split() if part
            ),
            agent_passthrough_env=(
                _env_list("QQ_BOT_AGENT_PASSTHROUGH_ENV")
                or (
                    "PATH", "HOME", "LANG", "LC_ALL", "TZ",
                    "PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR",
                    "EBKTOOL_SERVER_BASEURL", "EBKTOOL_TOKEN",
                )
            ),
        )
        params.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**params)


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
