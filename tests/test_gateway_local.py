#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""网关状态机本地自测：用假的 websocket 服务端驱动，不依赖真实 QQ 后台。

覆盖：

    Hello -> Identify -> READY -> Dispatch -> 心跳/ACK
    4009 关闭  -> 重连时走 Resume
    4013 关闭  -> FatalWebSocketError，不再重连

运行： python tests/test_gateway_local.py
"""

from __future__ import annotations

import json
import queue
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websocket  # noqa: E402
from websocket import ABNF  # noqa: E402

import qqbot.ws as wsmod  # noqa: E402
from qqbot.config import BotConfig  # noqa: E402
from qqbot.errors import FatalWebSocketError  # noqa: E402
from qqbot.intents import Intents  # noqa: E402


class FakeWS:
    """模拟网关侧的一个 websocket 连接。"""

    def __init__(self, url: str):
        self.url = url
        self.sent: list[dict] = []
        self.closed = False
        self._inbox: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._timeout: float | None = 1.0
        self._lock = threading.Lock()

    # -- websocket-client 兼容接口 --
    def settimeout(self, timeout) -> None:
        self._timeout = timeout

    def send(self, payload: str) -> None:
        with self._lock:
            self.sent.append(json.loads(payload))

    def recv_data(self):
        try:
            kind, data = self._inbox.get(timeout=self._timeout)
        except queue.Empty:
            raise websocket.WebSocketTimeoutException("fake timeout")
        if kind == "close":
            return ABNF.OPCODE_CLOSE, data
        return ABNF.OPCODE_TEXT, data

    def close(self) -> None:
        self.closed = True

    # -- 测试驱动 --
    def push(self, payload: dict) -> None:
        self._inbox.put(("text", json.dumps(payload)))

    def push_close(self, code: int, reason: str = "") -> None:
        self._inbox.put(("close", struct.pack("!H", code) + reason.encode()))

    def sent_of(self, op: int) -> list[dict]:
        with self._lock:
            return [p for p in self.sent if p.get("op") == op]

    def wait_sent(self, op: int, timeout: float = 5.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            items = self.sent_of(op)
            if items:
                return items[0]
            time.sleep(0.02)
        raise AssertionError(f"等待 op={op} 发送超时，已发送: {self.sent}")


class FakeServer:
    def __init__(self):
        self.connections: list[FakeWS] = []
        self._cv = threading.Condition()

    def create(self, url, **_kwargs) -> FakeWS:
        conn = FakeWS(url)
        with self._cv:
            self.connections.append(conn)
            self._cv.notify_all()
        return conn

    def wait_connection(self, index: int, timeout: float = 5.0) -> FakeWS:
        deadline = time.time() + timeout
        with self._cv:
            while len(self.connections) <= index and time.time() < deadline:
                self._cv.wait(deadline - time.time())
        if len(self.connections) <= index:
            raise AssertionError(f"等待第 {index + 1} 个连接超时")
        return self.connections[index]


class StubAPI:
    """只提供 GatewayClient 需要的两个能力。"""

    def __init__(self, token: str = "FAKE_TOKEN"):
        self.token = token
        self.gateway_calls = 0

    def get_gateway(self) -> str:
        self.gateway_calls += 1
        return "wss://fake.gateway/websocket"

    def get_token(self, **_kwargs) -> str:
        return self.token


CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  -> {detail}" if detail and not ok else ""))


def test_handshake_dispatch_resume() -> None:
    print("\n[用例1] Hello -> Identify -> READY -> Dispatch -> 心跳 -> 4009 后 Resume")

    config = BotConfig(app_id="123", client_secret="secret", intents=int(Intents.DEFAULT), log_level="WARNING")
    server = FakeServer()
    original = websocket.create_connection
    websocket.create_connection = server.create  # type: ignore[assignment]

    events: list[dict] = []
    ready: list[dict] = []
    resumed: list[dict] = []

    client = wsmod.GatewayClient(
        config,
        StubAPI(),
        on_event=events.append,
        on_ready=ready.append,
        on_resumed=resumed.append,
    )

    runner = threading.Thread(target=client.run_forever, daemon=True)
    runner.start()

    try:
        # ---- 第 1 条连接 ----
        c0 = server.wait_connection(0)
        c0.push({"op": 10, "d": {"heartbeat_interval": 1000}})

        identify = c0.wait_sent(wsmod.OP_IDENTIFY)
        d = identify["d"]
        check("Hello 后发送 Identify", True)
        check("token 格式为 'QQBot {access_token}'", d["token"] == "QQBot FAKE_TOKEN", repr(d["token"]))
        check("intents 正确", d["intents"] == int(Intents.DEFAULT), repr(d["intents"]))
        check("shard 为 [0, 1]", d["shard"] == [0, 1], repr(d["shard"]))
        check("携带 properties", isinstance(d.get("properties"), dict))

        # READY
        c0.push(
            {
                "op": 0,
                "s": 1,
                "t": "READY",
                "d": {
                    "version": 1,
                    "session_id": "session-abc",
                    "user": {"id": "999", "username": "测试机器人", "bot": True},
                    "shard": [0, 1],
                },
            }
        )
        time.sleep(0.2)
        check("触发 on_ready", len(ready) == 1)
        check("保存 session_id", client.session_id == "session-abc", repr(client.session_id))
        check("保存 last_seq", client.last_seq == 1, repr(client.last_seq))

        # 业务事件
        c0.push({"op": 0, "s": 2, "t": "C2C_MESSAGE_CREATE", "d": {"id": "MSG1", "content": "记 30 午饭"}})
        c0.wait_sent(wsmod.OP_HEARTBEAT)  # 顺便等一次心跳
        deadline = time.time() + 3
        while not any(p.get("t") == "C2C_MESSAGE_CREATE" for p in events) and time.time() < deadline:
            time.sleep(0.02)
        dispatched = [p for p in events if p.get("t") == "C2C_MESSAGE_CREATE"]
        check("分发 Dispatch 事件", len(dispatched) == 1, repr([p.get('t') for p in events]))
        check("事件内容完整", bool(dispatched) and dispatched[0]["d"]["content"] == "记 30 午饭")
        check("last_seq 更新到 2", client.last_seq == 2, repr(client.last_seq))

        # 心跳：d 应带最新 seq
        hb = c0.sent_of(wsmod.OP_HEARTBEAT)
        check("按周期发送心跳(op 1)", bool(hb), repr(c0.sent))
        if hb:
            check("心跳 d = 最新 seq", hb[-1]["d"] in (1, 2), repr(hb[-1]))

        # 心跳 ACK
        c0.push({"op": 11})
        time.sleep(0.2)
        check("处理心跳 ACK(op 11)", client._pending_acks == 0)

        # ---- 4009 关闭，应 Resume ----
        c0.push_close(4009, "session expired")
        c1 = server.wait_connection(1, timeout=8)
        c1.push({"op": 10, "d": {"heartbeat_interval": 1000}})

        resume = c1.wait_sent(wsmod.OP_RESUME, timeout=8)
        rd = resume["d"]
        check("4009 后重连走 Resume", True)
        check("Resume 带 session_id", rd.get("session_id") == "session-abc", repr(rd))
        check("Resume 带 seq", rd.get("seq") == 2, repr(rd))
        check("Resume 不带 intents", "intents" not in rd, repr(rd))
        check("Resume token 格式正确", rd.get("token") == "QQBot FAKE_TOKEN", repr(rd.get("token")))

        c1.push({"op": 0, "s": 3, "t": "RESUMED", "d": ""})
        time.sleep(0.3)
        check("触发 on_resumed", len(resumed) == 1)
        check("未重复 Identify", len(c1.sent_of(wsmod.OP_IDENTIFY)) == 0)

    finally:
        client.close()
        runner.join(timeout=3)
        websocket.create_connection = original  # type: ignore[assignment]


def test_fatal_close() -> None:
    print("\n[用例2] 4013（intent 无权限）应抛出 FatalWebSocketError 且不重连")

    config = BotConfig(app_id="123", client_secret="secret", log_level="WARNING")
    server = FakeServer()
    original = websocket.create_connection
    websocket.create_connection = server.create  # type: ignore[assignment]

    client = wsmod.GatewayClient(config, StubAPI(), on_event=lambda _p: None)
    error: list[BaseException] = []

    def _run():
        try:
            client.run_forever()
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)

    runner = threading.Thread(target=_run, daemon=True)
    runner.start()
    try:
        c0 = server.wait_connection(0)
        c0.push({"op": 10, "d": {"heartbeat_interval": 1000}})
        c0.wait_sent(wsmod.OP_IDENTIFY)
        c0.push_close(4013, "invalid intent")
        runner.join(timeout=5)
        check("抛出 FatalWebSocketError", any(isinstance(e, FatalWebSocketError) for e in error), repr(error))
        check("未建立新连接", len(server.connections) == 1, repr(len(server.connections)))
    finally:
        client.close()
        websocket.create_connection = original  # type: ignore[assignment]


def test_invalid_session_clears_state() -> None:
    print("\n[用例3] op 9 Invalid Session(d=false) 应清空会话并重新 Identify")

    config = BotConfig(app_id="123", client_secret="secret", log_level="WARNING")
    server = FakeServer()
    original = websocket.create_connection
    websocket.create_connection = server.create  # type: ignore[assignment]

    client = wsmod.GatewayClient(config, StubAPI(), on_event=lambda _p: None)
    runner = threading.Thread(target=client.run_forever, daemon=True)
    runner.start()
    try:
        c0 = server.wait_connection(0)
        c0.push({"op": 10, "d": {"heartbeat_interval": 1000}})
        c0.wait_sent(wsmod.OP_IDENTIFY)
        c0.push({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "s1", "user": {}}})
        time.sleep(0.2)
        check("READY 后 session 存在", client.session_id == "s1")

        c0.push({"op": 9, "d": False})
        c1 = server.wait_connection(1, timeout=8)
        c1.push({"op": 10, "d": {"heartbeat_interval": 1000}})
        c1.wait_sent(wsmod.OP_IDENTIFY, timeout=8)
        check("op9 后重新 Identify", True)
        check("会话已清空", client.session_id is None and client.last_seq is None, repr(client.session_id))
    finally:
        client.close()
        runner.join(timeout=3)
        websocket.create_connection = original  # type: ignore[assignment]


def main() -> int:
    print("=" * 62)
    print("  QQ 机器人网关状态机本地自测")
    print("=" * 62)
    test_handshake_dispatch_resume()
    test_fatal_close()
    test_invalid_session_clears_state()

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
