"""OpenAPI 调用封装（鉴权头自动注入 + token 失效自动重试）。

文档：
* API 调用指南：https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html
* 发送单聊消息：POST /v2/users/{user_openid}/messages
* 发送群聊消息：POST /v2/groups/{group_openid}/messages

所有请求头统一为：
    Authorization: QQBot {access_token}
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests

from .auth import AccessTokenManager
from .config import BotConfig
from .errors import QQBotAPIError

log = logging.getLogger("qqbot.api")

#: 统一的业务错误码 key
_ERR_KEYS = ("err_code", "code")

#: 这些错误码代表 token 不可用，可以刷新后重试一次
_AUTH_ERR_CODES = {11241, 11242, 11243, 11244}


def _pick_error_code(body: Any) -> Optional[int]:
    if not isinstance(body, dict):
        return None
    for key in _ERR_KEYS:
        value = body.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


class QQBotAPI:
    """OpenAPI 客户端。"""

    def __init__(self, config: BotConfig, token_manager: Optional[AccessTokenManager] = None):
        self.config = config
        self.tokens = token_manager or AccessTokenManager(config)
        self._session = self.tokens.session  # 复用连接池（含 keep-alive）

    # ------------------------------------------------------------------ 基础能力
    @property
    def session(self) -> requests.Session:
        """复用的 requests 会话（带 keep-alive），下载附件时也走它。"""
        return self._session

    def get_token(self, *, force: bool = False) -> str:
        return self.tokens.get_token(force=force)

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        _auth_retry: bool = True,
    ) -> dict:
        url = f"{self.config.api_base}{path}"
        headers = {
            "Authorization": f"QQBot {self.get_token()}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }

        log.debug("%s %s", method, url)
        try:
            resp = self._session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                params=params,
                timeout=self.config.request_timeout,
            )
        except requests.RequestException as exc:
            raise QQBotAPIError(
                status_code=None,
                err_code=None,
                message=f"网络请求失败: {exc}",
                method=method,
                url=url,
            ) from exc

        trace_id = resp.headers.get("X-Tps-trace-ID") or resp.headers.get("X-Tps-Trace-Id")
        try:
            body: Any = resp.json()
        except ValueError:
            body = None

        err_code = _pick_error_code(body)

        # token 失效 -> 强制刷新后重试一次
        if _auth_retry and (resp.status_code == 401 or err_code in _AUTH_ERR_CODES):
            log.warning("鉴权失败(HTTP %s, err_code=%s)，刷新 token 后重试一次", resp.status_code, err_code)
            self.tokens.invalidate()
            self.get_token(force=True)
            return self.request(method, path, json_body=json_body, params=params, _auth_retry=False)

        # 成功：200 有包体 / 201、202 异步（可能带 err_code）/ 204 无包体
        failed = resp.status_code >= 400 or (err_code not in (None, 0))
        if failed:
            message = ""
            if isinstance(body, dict):
                message = str(body.get("message") or body.get("msg") or "")
            if not message:
                message = (resp.text or "")[:200]
            raise QQBotAPIError(
                status_code=resp.status_code,
                err_code=err_code,
                message=message,
                trace_id=trace_id,
                payload=body if isinstance(body, dict) else {},
                method=method,
                url=url,
            )

        return body if isinstance(body, dict) else {}

    # ------------------------------------------------------------------ 网关
    def get_gateway(self) -> str:
        """GET /gateway 获取通用 WSS 接入点。"""
        data = self.request("GET", "/gateway")
        url = data.get("url")
        if not url:
            raise QQBotAPIError(
                status_code=200,
                err_code=None,
                message=f"网关地址响应异常: {data!r}",
                method="GET",
                url=f"{self.config.api_base}/gateway",
            )
        return url

    def get_gateway_bot(self) -> dict:
        """GET /gateway/bot 获取带分片建议的网关信息。"""
        return self.request("GET", "/gateway/bot")

    # ------------------------------------------------------------------ 机器人自身
    def me(self) -> dict:
        """GET /users/@me 验证鉴权链路是否通。"""
        return self.request("GET", "/users/@me")

    # ------------------------------------------------------------------ 消息发送
    @staticmethod
    def _build_body(
        *,
        content: Optional[str],
        markdown: Optional[dict],
        keyboard: Optional[dict],
        media: Optional[dict],
        msg_type: Optional[int],
        msg_id: Optional[str],
        event_id: Optional[str],
        msg_seq: Optional[int],
        message_reference: Optional[dict] = None,
        extra: Optional[dict] = None,
    ) -> dict:
        if msg_type is None:
            if media is not None:
                msg_type = 7
            elif markdown is not None:
                msg_type = 2
            else:
                msg_type = 0

        body: dict = {"msg_type": msg_type}
        if content is not None:
            body["content"] = content
        if markdown is not None:
            body["markdown"] = markdown
        if keyboard is not None:
            body["keyboard"] = keyboard
        if media is not None:
            body["media"] = media
        if message_reference is not None:
            body["message_reference"] = message_reference
        if msg_id:
            body["msg_id"] = msg_id
        if event_id:
            body["event_id"] = event_id
        if msg_seq is not None:
            body["msg_seq"] = msg_seq
        if extra:
            body.update(extra)

        if msg_type in (0, 2) and not (content or markdown):
            raise ValueError("msg_type=0/2 时必须提供 content 或 markdown")
        if msg_type == 2 and content:
            raise ValueError("Markdown 消息中 content 必须为空（content 与 markdown 不能同时传）")
        if msg_type == 7 and not media:
            raise ValueError("msg_type=7 时必须提供 media")
        return body

    def send_c2c_message(
        self,
        user_openid: str,
        *,
        content: Optional[str] = None,
        markdown: Optional[dict] = None,
        keyboard: Optional[dict] = None,
        media: Optional[dict] = None,
        msg_type: Optional[int] = None,
        msg_id: Optional[str] = None,
        event_id: Optional[str] = None,
        msg_seq: Optional[int] = None,
        msg_reference: Optional[dict] = None,
        is_wakeup: bool = False,
    ) -> dict:
        """发送单聊消息。

        * 被动回复：传 msg_id（来自 C2C_MESSAGE_CREATE 事件的 d.id），60 分钟内有效，
          同一条消息最多回复 4 次，每次要用不同的 msg_seq。
        * 主动消息：不传 msg_id，受主动消息频控限制。
        """
        extra = {"is_wakeup": True} if is_wakeup else None
        body = self._build_body(
            content=content,
            markdown=markdown,
            keyboard=keyboard,
            media=media,
            msg_type=msg_type,
            msg_id=msg_id,
            event_id=event_id,
            msg_seq=msg_seq,
            message_reference=msg_reference,
            extra=extra,
        )
        return self.request("POST", f"/v2/users/{user_openid}/messages", json_body=body)

    def send_group_message(
        self,
        group_openid: str,
        *,
        content: Optional[str] = None,
        markdown: Optional[dict] = None,
        keyboard: Optional[dict] = None,
        media: Optional[dict] = None,
        msg_type: Optional[int] = None,
        msg_id: Optional[str] = None,
        event_id: Optional[str] = None,
        msg_seq: Optional[int] = None,
        msg_reference: Optional[dict] = None,
    ) -> dict:
        """发送群聊消息。

        被动回复：传 msg_id（来自 GROUP_AT_MESSAGE_CREATE 事件的 d.id），5 分钟内有效，
        同一条消息最多回复 5 次。群消息不支持流式参数。
        """
        body = self._build_body(
            content=content,
            markdown=markdown,
            keyboard=keyboard,
            media=media,
            msg_type=msg_type,
            msg_id=msg_id,
            event_id=event_id,
            msg_seq=msg_seq,
            message_reference=msg_reference,
        )
        return self.request("POST", f"/v2/groups/{group_openid}/messages", json_body=body)

    def send_text(
        self,
        *,
        user_openid: Optional[str] = None,
        group_openid: Optional[str] = None,
        content: str,
        **kwargs,
    ) -> dict:
        """纯文本发送的便捷入口，二选一指定接收方。"""
        if bool(user_openid) == bool(group_openid):
            raise ValueError("user_openid 与 group_openid 必须且只能传一个")
        if user_openid:
            return self.send_c2c_message(user_openid, content=content, **kwargs)
        return self.send_group_message(group_openid, content=content, **kwargs)

    # ------------------------------------------------------------------ 频道消息（旧版频道/子频道）
    def send_channel_message(
        self,
        channel_id: str,
        *,
        content: Optional[str] = None,
        msg_id: Optional[str] = None,
        image: Optional[str] = None,
        embed: Optional[dict] = None,
        ark: Optional[dict] = None,
        message_reference: Optional[dict] = None,
    ) -> dict:
        """向子频道发送消息：POST /channels/{channel_id}/messages。"""
        body: dict = {}
        if content is not None:
            body["content"] = content
        if msg_id:
            body["msg_id"] = msg_id
        if image:
            body["image"] = image
        if embed:
            body["embed"] = embed
        if ark:
            body["ark"] = ark
        if message_reference:
            body["message_reference"] = message_reference
        if not body:
            raise ValueError("频道消息至少要提供 content / image / embed / ark 之一")
        return self.request("POST", f"/channels/{channel_id}/messages", json_body=body)

    # ------------------------------------------------------------------ 撤回
    def delete_c2c_message(self, user_openid: str, message_id: str) -> dict:
        return self.request("DELETE", f"/v2/users/{user_openid}/messages/{message_id}")

    def delete_group_message(self, group_openid: str, message_id: str) -> dict:
        return self.request("DELETE", f"/v2/groups/{group_openid}/messages/{message_id}")
