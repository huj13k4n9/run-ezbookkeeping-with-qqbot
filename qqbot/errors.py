"""QQ 机器人 SDK 异常定义。"""

from __future__ import annotations

from typing import Any, Optional


class QQBotError(Exception):
    """所有 SDK 异常的基类。"""


class QQBotConfigError(QQBotError):
    """配置缺失或非法。"""


class QQBotAuthError(QQBotError):
    """获取 access_token 失败。

    注意：/app/getAppAccessToken 的业务错误通过响应体的 code 返回，
    即使失败 HTTP 状态码仍是 200，所以这里以 code 为准。
    """

    #: code -> 排查建议（来自官方文档）
    HINTS = {
        100001: "请求过于频繁，请降低调用频率后重试",
        100007: "AppID 无效，或机器人状态不正常（被封禁 / 已删除）",
        100016: "AppID 或 ClientSecret 不正确，请与开放平台管理端核对",
        10004: "AppID 对应的机器人不存在",
    }

    def __init__(self, code: Any, message: str, raw: Optional[dict] = None):
        self.code = code
        self.message = message
        self.raw = raw or {}
        hint = self.HINTS.get(code, "")
        text = f"获取 access_token 失败: code={code} message={message!r}"
        if hint:
            text += f" | 排查建议: {hint}"
        super().__init__(text)


class QQBotAPIError(QQBotError):
    """调用 OpenAPI 失败。

    判定依据是 HTTP 状态码和响应体里的 err_code，
    而不是 message（message 内容平台会随时调整）。
    """

    def __init__(
        self,
        *,
        status_code: Optional[int],
        err_code: Optional[int],
        message: str = "",
        trace_id: Optional[str] = None,
        payload: Optional[dict] = None,
        method: str = "",
        url: str = "",
    ):
        self.status_code = status_code
        self.err_code = err_code
        self.message = message
        self.trace_id = trace_id
        self.payload = payload or {}
        self.method = method
        self.url = url

        parts = [f"{method} {url} 失败"]
        if status_code is not None:
            parts.append(f"HTTP {status_code}")
        if err_code:
            parts.append(f"err_code={err_code}")
        if message:
            parts.append(f"message={message!r}")
        if trace_id:
            parts.append(f"trace_id={trace_id}")
        super().__init__(" | ".join(parts))

    @property
    def is_auth_error(self) -> bool:
        """token 失效类错误，可强制刷新 token 后重试。"""
        return self.status_code == 401 or self.err_code in (11241, 11242, 11243, 11244)


class QQBotWebSocketError(QQBotError):
    """网关返回的关闭码对应的错误。"""

    def __init__(self, code: Optional[int], reason: str = ""):
        self.code = code
        self.reason = reason
        super().__init__(f"网关关闭连接 code={code} reason={reason}")


class FatalWebSocketError(QQBotWebSocketError):
    """不可重试的错误（4013/4014 权限问题、4914/4915 封禁等），直接停止重连。"""
