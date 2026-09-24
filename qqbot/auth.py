"""access_token 鉴权。

文档：https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html

要点：
* POST /app/getAppAccessToken  body = {"appId": ..., "clientSecret": ...}
* 有效期 7200s；有效期内重复请求返回同一个 token
* 到期前 60s 内请求才会下发新 token，旧 token 在这 60s 内仍有效
* 业务错误通过响应体 code 返回，HTTP 状态码仍是 200
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests

from .config import BotConfig
from .errors import QQBotAuthError

log = logging.getLogger("qqbot.auth")


def mask(token: Optional[str], keep: int = 6) -> str:
    """日志里脱敏展示 token。"""
    if not token:
        return "<none>"
    if len(token) <= keep * 2:
        return token[:keep] + "***"
    return f"{token[:keep]}...{token[-keep:]}"


class AccessTokenManager:
    """线程安全的 access_token 缓存与自动刷新。"""

    def __init__(self, config: BotConfig, session: Optional[requests.Session] = None):
        self.config = config
        self.session = session or requests.Session()
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    # ------------------------------------------------------------------ 公开接口
    def get_token(self, *, force: bool = False) -> str:
        """返回可用的 access_token，必要时自动刷新。"""
        with self._lock:
            now = time.time()
            margin = max(self.config.token_refresh_margin, 0)
            if not force and self._token and now < self._expires_at - margin:
                return self._token
            self._fetch_locked()
            assert self._token is not None
            return self._token

    def invalidate(self) -> None:
        """标记当前 token 失效，下次 get_token 会强制刷新。"""
        with self._lock:
            self._expires_at = 0.0

    @property
    def expires_in(self) -> float:
        """距离过期还有多少秒（未获取过则为 0）。"""
        if not self._token:
            return 0.0
        return max(self._expires_at - time.time(), 0.0)

    # ------------------------------------------------------------------ 内部实现
    def _fetch_locked(self) -> None:
        body = {
            "appId": self.config.app_id,
            "clientSecret": self.config.client_secret,
        }
        log.debug("请求 access_token: POST %s", self.config.token_url)
        try:
            resp = self.session.post(
                self.config.token_url,
                json=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=self.config.request_timeout,
            )
        except requests.RequestException as exc:
            raise QQBotAuthError("network_error", str(exc)) from exc

        try:
            data = resp.json()
        except ValueError as exc:
            raise QQBotAuthError(
                "invalid_response",
                f"HTTP {resp.status_code} 返回非 JSON: {resp.text[:200]!r}",
            ) from exc

        token = data.get("access_token")
        if not token:
            # 失败时 HTTP 200 + {"code": 100016, "message": "..."}
            raise QQBotAuthError(data.get("code"), str(data.get("message", "")), data)

        try:
            expires_in = float(data.get("expires_in", 7200))
        except (TypeError, ValueError):
            expires_in = 7200.0

        self._token = token
        self._expires_at = time.time() + expires_in
        log.info(
            "获取 access_token 成功 %s (expires_in=%ss)",
            mask(token),
            int(expires_in),
        )
