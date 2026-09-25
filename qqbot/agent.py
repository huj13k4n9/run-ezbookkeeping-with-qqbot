"""pi agent 调用层：会话 ID、去重、prompt 组装、子进程执行。

设计要点（对应 PLAN_QQ_AGENT.md 第 3/6/7 节）：

* **会话**：`--session-id qq-<openid哈希>-<时间桶>`，按用户隔离、按 day/week 轮换。
* **去重**：进程内 TTL 字典，挡 QQ 的重复推送。
* **prompt**：由 bot 组装，注入运行时信息（时间、确认阈值），
  静态规则写在 `agent/AGENTS.md` 里。
* **不阻塞**：本模块只负责「跑一次」；并发与后台线程由 `qqbot.bot` 负责。

注意：pi 会从 **cwd** 向上发现 `AGENTS.md`，所以 `cwd` 必须是 `agent/`。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Optional

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

if TYPE_CHECKING:  # pragma: no cover
    from .bot import Event

log = logging.getLogger("qqbot.agent")


# ============================================================ pi 的 models.json
# pi 的 JSON 配置文件支持 // 行注释和尾随逗号（**不支持** /* */ 块注释）——
# 见 pi 的 dist/utils/json.js#stripJsonComments。
#
# 这里用**完全相同**的规则实现一遍：同一份文件 pi 读得懂而我们读不懂
# （或反过来）是最难查的一类问题。
_JSON_STR_OR_LINECOMMENT = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*')
_JSON_STR_OR_TRAILING_COMMA = re.compile(r'"(?:\\.|[^"\\])*"|,(\s*[}\]])')


def strip_json_comments(text: str) -> str:
    """按 pi 的规则去掉 ``//`` 行注释和尾随逗号，字符串字面量原样保留。"""

    def drop_line_comment(match: re.Match[str]) -> str:
        found = match.group(0)
        return found if found[0] == '"' else ""

    def drop_trailing_comma(match: re.Match[str]) -> str:
        if match.group(1) is not None:
            return match.group(1)
        found = match.group(0)
        return found if found[0] == '"' else ""

    text = _JSON_STR_OR_LINECOMMENT.sub(drop_line_comment, text)
    return _JSON_STR_OR_TRAILING_COMMA.sub(drop_trailing_comma, text)


def resolve_models_json_path(config) -> Path:  # noqa: ANN001
    """定位 pi 的 models.json。

    优先用显式配置（``QQ_BOT_AGENT_MODELS_JSON``），否则按 pi 的规则：
    ``$PI_CODING_AGENT_DIR/models.json``（默认 ``~/.pi/agent/models.json``）。
    """
    explicit = getattr(config, "agent_models_json", None)
    if explicit:
        return Path(explicit).expanduser()
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR") or "~/.pi/agent"
    return Path(agent_dir).expanduser() / "models.json"


def load_models_json(config) -> tuple[Optional[str], Optional[str]]:  # noqa: ANN001
    """读 models.json，返回 ``(defaultModel, 问题描述)``。

    为什么自己读这个键：pi 的默认模型在 ``settings.json`` 里，而
    ``models.json`` 只管端点 —— 于是用户得在**两个**文件（外加 `.env`）
    里分别填 apiKey / baseUrl / 模型名。这里把 pi 会忽略的 ``defaultModel``
    读出来当 ``--model`` 用，就能让端点、密钥、模型名全放在一个文件里。

    pi 对未知顶层键不作限制（已实测），所以多加这个键不影响 pi 自己。
    文件不存在 = 正常情况，不算问题。
    """
    path = resolve_models_json_path(config)
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"读不了 {path}: {exc}"

    try:
        data = json.loads(strip_json_comments(raw))
    except (ValueError, TypeError) as exc:
        return None, (
            f"{path} 解析失败: {exc}"
            "（只支持 // 行注释和尾随逗号，不支持 /* */ 块注释）"
        )

    if not isinstance(data, dict):
        return None, f"{path} 的顶层应该是个对象"

    value = data.get("defaultModel")
    if value is None:
        return None, None
    if not isinstance(value, str) or not value.strip():
        return None, f"{path} 里的 defaultModel 应该是非空字符串"
    return value.strip(), None


# ============================================================ 结果
@dataclass
class AgentResult:
    """一次 agent 调用的结果。"""

    ok: bool = False
    text: str = ""
    returncode: Optional[int] = None
    duration: float = 0.0
    timed_out: bool = False
    session_id: str = ""
    stderr_tail: str = ""
    error: str = ""
    prompt: str = ""
    images: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """是否可以直接把 text 回给用户。"""
        return self.ok and bool(self.text.strip())


# ============================================================ 时区
def resolve_timezone(name: str):
    """尽量解析时区名，失败则退化为 UTC。"""
    if ZoneInfo is not None and name:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - 未知时区不该让机器人挂掉
            log.warning("未知时区 %r，退化为 UTC", name)
    return dt_timezone.utc


# ============================================================ 会话
class SessionManager:
    """按用户 + 时间桶生成 pi 的 session-id。

    pi 对 session-id 的限制：只允许字母、数字、`.`、`_`、`-`。
    """

    def __init__(self, prefix: str = "qq", rotation: str = "day"):
        self.prefix = prefix or "qq"
        self.rotation = (rotation or "day").lower()
        if self.rotation not in ("day", "week", "none"):
            raise ValueError(f"非法的 rotation: {rotation}")

    @staticmethod
    def digest(openid: str) -> str:
        """openid 脱敏后进 ID（避免把原始 openid 写进 session 文件名）。"""
        return hashlib.sha1((openid or "").encode("utf-8")).hexdigest()[:12]

    def bucket(self, now: datetime) -> str:
        if self.rotation == "day":
            return now.strftime("%Y%m%d")
        if self.rotation == "week":
            iso = now.isocalendar()
            return f"{iso[0]}W{iso[1]:02d}"
        return ""

    def session_id(self, openid: str, now: Optional[datetime] = None) -> str:
        now = now or datetime.now(dt_timezone.utc)
        parts = [self.prefix, self.digest(openid)]
        bucket = self.bucket(now)
        if bucket:
            parts.append(bucket)
        return "-".join(parts)


# ============================================================ 去重
class Deduplicator:
    """进程内 TTL 去重（QQ 会重复推送同一条消息）。

    重启后丢失可接受 —— 重复推送通常发生在几秒内。
    """

    def __init__(self, ttl: float = 600.0, *, max_entries: int = 4096):
        self.ttl = float(ttl)
        self.max_entries = max_entries
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_duplicate(self, key: str, now: Optional[float] = None) -> bool:
        """第一次见到返回 False；重复返回 True。空 key 不算重复。"""
        if not key:
            return False
        now = now if now is not None else time.time()
        with self._lock:
            self._purge_locked(now)
            first = self._seen.get(key)
            if first is not None and now - first <= self.ttl:
                return True
            self._seen[key] = now
            return False

    def forget(self, key: str) -> None:
        with self._lock:
            self._seen.pop(key, None)

    def _purge_locked(self, now: float) -> None:
        if len(self._seen) < 64 and len(self._seen) < self.max_entries:
            # 小表不必每次扫描
            if len(self._seen) < max(64, self.max_entries // 4):
                return
        expired = [k for k, ts in self._seen.items() if now - ts > self.ttl]
        for key in expired:
            self._seen.pop(key, None)
        if len(self._seen) >= self.max_entries:
            oldest = sorted(self._seen.items(), key=lambda kv: kv[1])
            for key, _ in oldest[: len(self._seen) - self.max_entries // 2]:
                self._seen.pop(key, None)

    def __len__(self) -> int:
        return len(self._seen)


# ============================================================ prompt
def _fmt_time(event: "Event", fallback_tz) -> str:
    """优先用消息自带的 timestamp（就是用户发消息的时间），否则用服务器当前时间。"""
    raw = ""
    data = getattr(event, "data", None)
    if isinstance(data, dict):
        raw = str(data.get("timestamp") or "")
    if raw:
        return raw
    return datetime.now(fallback_tz).isoformat(timespec="seconds")


def parse_event_time(raw: str) -> Optional[tuple[int, int]]:
    """RFC3339 -> (unix 秒, utcOffset 分钟)。解析不了返回 None。

    `transactions-add` 需要的是 `--time <unix>` + `--utcOffset <分钟>`，
    让模型自己换算很容易错，所以由 bot 直接算好注入 prompt。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    offset = parsed.utcoffset()
    if offset is None:
        return None
    return int(parsed.timestamp()), int(offset.total_seconds() // 60)


def build_prompt(event: "Event", config, *, now: Optional[datetime] = None) -> tuple[str, list[str]]:
    """组装 prompt，返回 (prompt 文本, 图片绝对路径列表)。

    有文本才应该被调用 —— 纯图片消息由 bot 拦在外面（见 PLAN 第 5.1 节）。
    """
    tz = resolve_timezone(config.timezone)
    lines: list[str] = []

    raw_ts = ""
    data = getattr(event, "data", None)
    if isinstance(data, dict):
        raw_ts = str(data.get("timestamp") or "")
    lines.append(f"[当前时间] {_fmt_time(event, tz)}")

    # transactions-add 要的是 unix 秒 + utcOffset 分钟，直接算好给模型，不让它换算
    stamp = parse_event_time(raw_ts)
    if stamp is None:
        moment = datetime.now(tz)
        offset = moment.utcoffset()
        stamp = (int(moment.timestamp()), int((offset or timedelta(0)).total_seconds() // 60))
    lines.append(f"[时间] unix={stamp[0]} utcOffset={stamp[1]}")

    tools_path = resolve_passthrough_path(config.ebktools_path)
    lines.append(f"[工具] {tools_path}")

    if config.confirm_amount_threshold and config.confirm_amount_threshold > 0:
        lines.append(f"[策略] 确认阈值 {config.confirm_amount_threshold}")

    openid = ""
    author = getattr(event, "author", None) or {}
    if isinstance(author, dict):
        openid = str(author.get("user_openid") or author.get("id") or "")
    lines.append(f"[用户] {openid}")

    content = str(getattr(event, "content", "") or "").strip()
    lines.append(f"[消息] {content}")

    quote = getattr(event, "quote", None)
    images: list[str] = []

    if quote is not None:
        if quote.resolved:
            lines.append(f"[引用] {quote.text or '(空)'}")
        else:
            hint = quote.hint or "未知类型"
            lines.append(f"[引用] （本条引用未能解析，平台提示：{hint}）")
            lines.append("[说明] 用户是在指着某条你拿不到的消息说话，"
                         "如果缺关键信息就直接问他，不要猜。")
        for path in quote.local_paths:
            images.append(str(path))

    # 当前消息自身的附件（正常情况 C2C 不会有，防御性处理）
    local_own = getattr(event, "local_attachments", None) or {}
    if isinstance(local_own, dict):
        for _, path in sorted(local_own.items()):
            images.append(str(path))

    # 去重、只保留真实存在的文件，并转成绝对路径
    # （落盘路径可能相对于 bot 的 cwd，必须转成绝对路径，
    #   因为 pi 的 @path 是从 agent 的 cwd 解析的）
    unique: list[str] = []
    for raw in images:
        resolved = resolve_passthrough_path(raw)
        if resolved.is_file() and str(resolved) not in unique:
            unique.append(str(resolved))
        else:
            log.warning("prompt 里的附件不存在或重复，已跳过: %s", raw)

    if unique:
        lines.append("[附件] " + " ".join(unique))

    return "\n".join(lines), unique


def resolve_passthrough_path(value: str | os.PathLike) -> Path:
    """把相对路径按 **bot 的 cwd** 解析成绝对路径。

    不能用 agent 的 cwd —— 两者不在同一个目录（见 PLAN 第 3 节）。
    """
    path = Path(value)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


# ============================================================ 运行
def build_agent_env(config, base_env: Optional[dict] = None) -> dict:
    """只透传白名单环境变量，避免把 QQ secret 泄漏给 agent 进程。

    白名单条目支持两种写法：

    * 精确名称，如 ``PATH``
    * **前缀通配**，以 ``*`` 结尾，如 ``LANGFUSE_*``
      （第三方集成（Langfuse 等）会加新变量，用前缀比逐个列举更结实）

    注意：`EBKTOOL_SERVER_BASEURL` / `EBKTOOL_TOKEN` 必须透传，
    否则 agent 没法调 ebktools.sh。它们只在进程环境里，
    AGENTS.md 已禁止 agent 回显。
    """
    source = base_env if base_env is not None else os.environ
    env: dict[str, str] = {}

    prefixes: list[str] = []
    for pattern in config.agent_passthrough_env:
        if pattern.endswith("*"):
            prefixes.append(pattern[:-1])
        elif pattern in source:
            env[pattern] = source[pattern]

    if prefixes:
        for name, value in source.items():
            if name not in env and any(name.startswith(p) for p in prefixes):
                env[name] = value

    # 无论是否在白名单里，都保证 ebktools 能用
    for name in ("EBKTOOL_SERVER_BASEURL", "EBKTOOL_TOKEN"):
        if name in source and name not in env:
            env[name] = source[name]
    env.setdefault("TZ", config.timezone)
    return env


class AgentRunner:
    """跑一次 pi。线程安全（无共享可变状态）。"""

    def __init__(self, config):
        self.config = config
        self.cwd = Path(config.agent_cwd)
        if not self.cwd.is_absolute():
            self.cwd = (Path.cwd() / self.cwd).resolve()

        # 模型只有一个来源：models.json 的 defaultModel（见 README）。
        # 环境变量不再参与 —— 单一来源比“哪边优先”好推理。
        self.models_json = resolve_models_json_path(config)
        self.model, self.models_json_problem = load_models_json(config)

    @property
    def model_source(self) -> str:
        """模型是从哪来的（用于启动自检打印）。"""
        return str(self.models_json) if self.model else ""

    def validate(self) -> list[str]:
        """启动自检：返回一串问题描述，空列表表示一切就绪。

        QQ bot 代码与 agent 目录通常**不在同一个地方**，很容易配错。
        """
        problems: list[str] = []
        if not self.cwd.is_dir():
            problems.append(f"agent cwd 不存在: {self.cwd}")
        elif not (self.cwd / "AGENTS.md").is_file():
            problems.append(f"agent cwd 里没有 AGENTS.md: {self.cwd}（约束不会生效）")

        tools = resolve_passthrough_path(self.config.ebktools_path)
        if not tools.is_file():
            problems.append(f"ebktools.sh 不存在: {tools}")

        # models.json 写错的话 pi 会静默忽略（表现为「配置没生效」），
        # 容器入口会报，但本地直接跑 run_bot.py 时只靠这里。
        if self.models_json_problem:
            problems.append(self.models_json_problem)
        return problems

    # ---------------------------------------------------------------- argv
    def build_argv(self, session_id: str, prompt: str, images: Iterable[str]) -> list[str]:
        cfg = self.config
        argv: list[str] = [cfg.agent_command, "--print"]
        if session_id:
            argv += ["--session-id", session_id]
        if cfg.agent_tools:
            argv += ["--tools", cfg.agent_tools]
        if self.model:
            argv += ["--model", self.model]
        argv += list(cfg.agent_extra_args)

        # pi 的用法：pi [options] [--] [@files...] [messages...]
        argv.append("--")
        argv += [f"@{path}" for path in images]
        argv.append(prompt)
        return argv

    # ---------------------------------------------------------------- run
    def run(
        self,
        prompt: str,
        *,
        session_id: str = "",
        images: Optional[Iterable[str]] = None,
    ) -> AgentResult:
        images = list(images or ())
        argv = self.build_argv(session_id, prompt, images)
        result = AgentResult(session_id=session_id, prompt=prompt, images=images)

        if not self.cwd.is_dir():
            result.error = f"agent 工作目录不存在: {self.cwd}"
            log.error("%s", result.error)
            return result

        log.info(
            "启动 agent: session=%s images=%d prompt=%r",
            session_id or "(none)", len(images), _short(prompt, 120),
        )
        log.debug("argv=%s", " ".join(shlex.quote(a) for a in argv))

        started = time.time()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(self.cwd),
                env=build_agent_env(self.config),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.agent_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            result.timed_out = True
            result.duration = time.time() - started
            result.error = f"agent 超时（>{self.config.agent_timeout:.0f}s）"
            # subprocess.run 超时会杀掉进程，这里只是记录已产出的 stderr
            result.stderr_tail = _tail(exc.stderr)
            log.warning("%s session=%s", result.error, session_id)
            return result
        except FileNotFoundError:
            result.duration = time.time() - started
            result.error = f"找不到 pi 可执行文件: {self.config.agent_command}"
            log.error("%s", result.error)
            return result
        except OSError as exc:
            result.duration = time.time() - started
            result.error = f"启动 agent 失败: {exc}"
            log.exception("%s", result.error)
            return result

        result.duration = time.time() - started
        result.returncode = proc.returncode
        result.text = (proc.stdout or "").strip()
        result.stderr_tail = _tail(proc.stderr)
        result.ok = proc.returncode == 0 and bool(result.text)

        if result.ok:
            log.info("agent 完成: %.1fs, 输出 %d 字", result.duration, len(result.text))
        else:
            log.warning(
                "agent 异常: returncode=%s 输出 %d 字 用时 %.1fs stderr=%r",
                proc.returncode, len(result.text), result.duration, _short(result.stderr_tail, 300),
            )
        return result


def _tail(value: Any, limit: int = 800) -> str:
    text = value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value or "")
    return text[-limit:]


def _short(value: str, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else f"{text[:limit]}…"
