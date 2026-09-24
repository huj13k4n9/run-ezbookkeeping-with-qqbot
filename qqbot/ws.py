"""QQ 机器人 WebSocket 网关客户端。

完整实现网关生命周期：

    连接 -> Hello(op 10) -> Identify(op 2) / Resume(op 6)
         -> READY / RESUMED -> Dispatch(op 0) + 心跳(op 1)/ACK(op 11)
         -> 断线自动重连（优先 Resume，失败则重新 Identify）

文档：
* WebSocket 方式: https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html
* opcode:        https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/opcode.html
"""

from __future__ import annotations

import json
import logging
import platform
import struct
import threading
from typing import Any, Callable, Optional

import websocket
from websocket import ABNF

from .api import QQBotAPI
from .config import BotConfig
from .errors import FatalWebSocketError

log = logging.getLogger("qqbot.gateway")

# ---------------------------------------------------------------- OpCode
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

#: 不可重试的关闭码（来自官方文档「WebSocket 错误码」表）
FATAL_CLOSE_CODES: dict[int, str] = {
    4001: "无效的 opcode",
    4002: "无效的 payload",
    4010: "无效的 shard",
    4011: "连接需要处理的 guild 过多，请合理分片",
    4012: "无效的 version",
    4013: "无效的 intent",
    4014: "intent 无权限",
    4914: "机器人已下架，仅允许连接沙箱环境",
    4915: "机器人已封禁，请申请解封后再连接",
}

#: 这些关闭码不允许 Resume，必须重新 Identify
IDENTIFY_ONLY_CLOSE_CODES = {4006, 4007}


class _Reconnect(Exception):
    """内部信号：需要重连本连接。

    :param resume: 是否保留 session 走 Resume（False 表示清空后重新 Identify）
    """

    def __init__(self, resume: bool):
        self.resume = resume
        super().__init__(f"reconnect(resume={resume})")


class GatewayClient:
    """维护到 QQ 网关的 WebSocket 长连接，并把事件回调出去。"""

    def __init__(
        self,
        config: BotConfig,
        api: QQBotAPI,
        *,
        on_event: Callable[[dict], None],
        on_ready: Optional[Callable[[dict], None]] = None,
        on_resumed: Optional[Callable[[dict], None]] = None,
    ):
        self.config = config
        self.api = api
        self.on_event = on_event
        self.on_ready = on_ready
        self.on_resumed = on_resumed

        # 会话状态（用于 Resume）
        self.session_id: Optional[str] = None
        self.last_seq: Optional[int] = None
        self.bot_user: Optional[dict] = None

        self.heartbeat_interval: float = 45.0  # 秒，Hello 下发后覆盖

        self._closed = threading.Event()
        self._hb_stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        self._pending_acks = 0
        self._zombie = threading.Event()
        self._reconnect_delay = 1.0
        self._current_ws: Optional[websocket.WebSocket] = None

    # ================================================================ 公开接口
    @property
    def ready(self) -> bool:
        return self.session_id is not None

    def close(self) -> None:
        """停止连接与重连（线程安全，可从其它线程调用）。"""
        self._closed.set()
        self._stop_heartbeat()

    def run_forever(self) -> None:
        """阻塞运行，断线自动重连。"""
        self._closed.clear()
        while not self._closed.is_set():
            try:
                self._session_loop()
                self._reconnect_delay = 1.0
            except FatalWebSocketError as exc:
                log.error("致命错误，停止重连: %s", exc)
                self._closed.set()
                raise
            except Exception as exc:  # noqa: BLE001 - 网络层任何异常都要能重连
                log.warning("连接中断: %s: %s", type(exc).__name__, exc)

            if self._closed.is_set():
                break
            delay = self._reconnect_delay
            log.info("%.1fs 后重连网关…", delay)
            self._closed.wait(delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, self.config.max_reconnect_delay)
        log.info("网关客户端已停止")

    # ================================================================ 单次连接生命周期
    def _session_loop(self) -> None:
        url = self.api.get_gateway()
        log.info("连接网关: %s", url)

        ws = websocket.create_connection(
            url,
            timeout=self.config.connect_timeout,
            enable_multithread=True,
            header=[f"User-Agent: {self.config.user_agent}"],
        )
        try:
            self._handshake(ws)
            self._event_loop(ws)
        except _Reconnect as sig:
            if not sig.resume:
                log.info("清空会话状态，下次连接将重新 Identify")
                self.session_id = None
                self.last_seq = None
            self._reconnect_delay = 1.0
        finally:
            self._stop_heartbeat()
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def _handshake(self, ws: websocket.WebSocket) -> None:
        """等待 Hello，然后按会话状态选择 Identify 或 Resume。"""
        hello = self._wait_hello(ws)
        interval_ms = (hello.get("d") or {}).get("heartbeat_interval", 45000)
        try:
            self.heartbeat_interval = max(float(interval_ms) / 1000.0, 1.0)
        except (TypeError, ValueError):
            self.heartbeat_interval = 45.0
        log.info("收到 Hello，心跳周期 %.1fs", self.heartbeat_interval)

        if self.session_id is not None and self.last_seq is not None:
            self._send_resume(ws)
        else:
            self._send_identify(ws)

        # 心跳在 Hello 之后即可开始，不必等 READY
        self._start_heartbeat(ws)

    def _wait_hello(self, ws: websocket.WebSocket) -> dict:
        ws.settimeout(self.config.connect_timeout)
        while True:
            opcode, data = self._recv(ws)
            if opcode == ABNF.OPCODE_CLOSE:
                self._raise_for_close(data)
            if opcode != ABNF.OPCODE_TEXT:
                continue
            payload = self._decode(data)
            if payload.get("op") == OP_HELLO:
                return payload
            log.debug("等待 Hello 期间收到: %s", payload)

    # ================================================================ 事件循环
    def _event_loop(self, ws: websocket.WebSocket) -> None:
        while not self._closed.is_set():
            try:
                opcode, data = self._recv(ws, timeout=max(self.heartbeat_interval / 2, 1.0))
            except websocket.WebSocketTimeoutException:
                if self._zombie.is_set():
                    raise ConnectionError("心跳连续未收到 ACK，判定连接已失效")
                continue

            if opcode == ABNF.OPCODE_CLOSE:
                self._raise_for_close(data)

            if opcode not in (ABNF.OPCODE_TEXT, ABNF.OPCODE_BINARY):
                continue

            self._handle_payload(self._decode(data))

    def _handle_payload(self, payload: dict) -> None:
        op = payload.get("op")
        seq = payload.get("s")
        if isinstance(seq, int):
            # 记录最新序列号，Resume 时回传，网关会补发之后的事件
            self.last_seq = seq

        if op == OP_DISPATCH:
            event_name = payload.get("t")
            data = payload.get("d")
            if event_name == "READY":
                data = data or {}
                self.session_id = data.get("session_id") or self.session_id
                self.bot_user = data.get("user")
                self._reconnect_delay = 1.0
                self._safe_call(
                    self.on_ready,
                    payload,
                    desc="on_ready",
                )
            elif event_name == "RESUMED":
                self._reconnect_delay = 1.0
                self._safe_call(self.on_resumed, payload, desc="on_resumed")
            self._safe_call(self.on_event, payload, desc="on_event")

        elif op == OP_HEARTBEAT_ACK:
            self._pending_acks = 0
            self._zombie.clear()
            log.debug("心跳 ACK")

        elif op == OP_HEARTBEAT:
            # 服务端要求立刻心跳
            log.debug("服务端请求心跳，立即发送")
            self._send_heartbeat(self._current_ws)

        elif op == OP_RECONNECT:
            log.info("服务端通知重连 (op 7)")
            raise _Reconnect(resume=True)

        elif op == OP_INVALID_SESSION:
            can_resume = bool(payload.get("d"))
            log.warning("Invalid Session (op 9)，可 Resume=%s", can_resume)
            raise _Reconnect(resume=can_resume)

    # ================================================================ 发送
    def _send_identify(self, ws: websocket.WebSocket) -> None:
        payload = {
            "op": OP_IDENTIFY,
            "d": {
                # 新版网关要求 "QQBot {AccessToken}"
                "token": f"QQBot {self.api.get_token()}",
                "intents": int(self.config.intents),
                "shard": [self.config.shard[0], self.config.shard[1]],
                "properties": {
                    "$os": platform.system().lower(),
                    "$browser": self.config.user_agent,
                    "$device": self.config.user_agent,
                },
            },
        }
        log.info(
            "发送 Identify intents=%d(%s) shard=%s",
            self.config.intents,
            _intents_desc(self.config.intents),
            self.config.shard,
        )
        ws.send(json.dumps(payload))

    def _send_resume(self, ws: websocket.WebSocket) -> None:
        payload = {
            "op": OP_RESUME,
            "d": {
                "token": f"QQBot {self.api.get_token()}",
                "session_id": self.session_id,
                "seq": self.last_seq,
            },
        }
        log.info("发送 Resume session_id=%s seq=%s", self.session_id, self.last_seq)
        ws.send(json.dumps(payload))

    def _send_heartbeat(self, ws: Optional[websocket.WebSocket]) -> None:
        if ws is None:
            return
        try:
            ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self.last_seq}))
            log.debug("发送心跳 d=%s", self.last_seq)
        except Exception as exc:  # noqa: BLE001
            log.debug("心跳发送失败: %s", exc)
            self._zombie.set()

    def _start_heartbeat(self, ws: websocket.WebSocket) -> None:
        self._stop_heartbeat()
        self._current_ws = ws
        self._zombie.clear()
        self._pending_acks = 0
        self._hb_stop = threading.Event()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, args=(ws,), name="qqbot-heartbeat", daemon=True
        )
        self._hb_thread.start()

    def _stop_heartbeat(self) -> None:
        self._hb_stop.set()
        thread = self._hb_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._hb_thread = None
        self._current_ws = None

    def _heartbeat_loop(self, ws: websocket.WebSocket) -> None:
        interval = self.heartbeat_interval
        while not self._hb_stop.wait(interval):
            if self._closed.is_set():
                return
            if self._pending_acks >= 2:
                log.warning("连续 %d 次心跳未收到 ACK，标记连接为僵尸状态", self._pending_acks)
                self._zombie.set()
                return
            self._pending_acks += 1
            self._send_heartbeat(ws)

    # ================================================================ 工具
    def _recv(self, ws: websocket.WebSocket, timeout: Optional[float] = None):
        if timeout is not None:
            ws.settimeout(timeout)
        return ws.recv_data()

    @staticmethod
    def _decode(data: Any) -> dict:
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8")
        try:
            payload = json.loads(data)
        except (TypeError, ValueError):
            log.warning("无法解析网关消息: %r", data)
            return {}
        return payload if isinstance(payload, dict) else {}

    def _raise_for_close(self, data: Any) -> None:
        """解析关闭帧中的关闭码并决定后续动作。"""
        code: Optional[int] = None
        reason = ""
        if isinstance(data, (bytes, bytearray)) and len(data) >= 2:
            code = struct.unpack("!H", data[:2])[0]
            reason = bytes(data[2:]).decode("utf-8", "replace")

        if code in FATAL_CLOSE_CODES:
            raise FatalWebSocketError(code, FATAL_CLOSE_CODES[code] or reason)

        if code in IDENTIFY_ONLY_CLOSE_CODES:
            log.warning("网关关闭 code=%s (%s)，需重新 Identify", code, reason)
            raise _Reconnect(resume=False)

        log.warning("网关关闭 code=%s reason=%s，尝试 Resume 重连", code, reason)
        raise _Reconnect(resume=True)

    @staticmethod
    def _safe_call(callback: Optional[Callable[[dict], None]], payload: dict, *, desc: str) -> None:
        if callback is None:
            return
        try:
            callback(payload)
        except Exception:  # noqa: BLE001 - 业务回调异常不能拖垮连接
            log.exception("事件回调 %s 抛出异常", desc)


def _intents_desc(intents: int) -> str:
    from .intents import describe

    return describe(intents)
