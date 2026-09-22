"""acctkit 规范模型：Principal（provider 验出的外部身份）与 Identity（落库后的内部 uid）。

分层纪律：provider 只管「从请求头验证出是谁」（Principal），不碰存储；
uid 的分配/账号归并交给 IdentityStore。加新 provider（微信）或换存储都不动
调用方的隔离逻辑（资源 owner 恒存 uid）。本模块零 Web 框架依赖——
HTTP 状态码到响应体的映射是宿主的事，这里只带 AuthError.status 建议值。
"""
from __future__ import annotations

from dataclasses import dataclass, field


class AuthError(Exception):
    """鉴权失败。status 是给宿主的建议码（默认 401=缺/坏凭据；500=服务端没配好）。"""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.message = message
        self.status = status


class NoCredentials(AuthError):
    """该 provider 在请求里找不到属于它的凭据（如没有 Authorization 头）——
    不是「凭据无效」而是「这条 provider 不适用」，引擎据此尝试下一个 provider。"""

    def __init__(self, provider: str):
        super().__init__(f"{provider}: 请求未携带其凭据", status=401)
        self.provider = provider


@dataclass
class Principal:
    """provider 验出的外部身份（尚未映射到内部 uid）。"""
    provider: str
    external_id: str
    email: str | None = None
    name: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class Identity:
    """解析到稳定内部 uid 的身份——调用方资源隔离都认它。"""
    uid: str
    provider: str
    external_id: str
    email: str | None = None
    name: str | None = None
    # 首登标记：供上层（如配额/引导）可选使用；测试断言用
    is_new: bool = False
