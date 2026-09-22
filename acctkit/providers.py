"""鉴权 provider（可插拔）：headers in → Principal out，不碰存储/uid。

契约：``await authenticate(headers) -> Principal`` 或抛
- NoCredentials：请求里没找到本 provider 的凭据（引擎换下一个 provider 试）
- AuthError：找到了凭据但无效（引擎直接向上抛）

``headers`` 是 Mapping[str, str]（键按 HTTP 惯例小写；主流框架的 Headers 类型
都是大小写不敏感 Mapping，直接传即可），刻意不依赖任何 Web 框架。凭据形态不同的登录方式
（微信 = 扫码 code→服务端换 openid）将来实现一个 authenticate 注册进
``register_provider`` 即可挂上，引擎与存储零改动。
"""
from __future__ import annotations

import abc
import asyncio
from collections.abc import Callable, Mapping

from .models import AuthError, NoCredentials, Principal


def bearer_token(headers: Mapping[str, str], provider: str) -> str:
    """从 Authorization: Bearer <token> 取 token；缺头/格式不符 → NoCredentials。"""
    raw = headers.get("authorization") or ""
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise NoCredentials(provider)
    return parts[1].strip()


class AuthProvider(abc.ABC):
    name: str = ""

    @abc.abstractmethod
    async def authenticate(self, headers: Mapping[str, str]) -> Principal: ...


class GoogleProvider(AuthProvider):
    """Google OpenID Connect ID-Token 验签：audience=client_id，取 sub 作 external_id。

    后端只做验签（JWKS/iss/aud/exp 由 google-auth 校验），不持 client secret、
    不参与 OAuth 重定向——前端在自己域里登录拿 id_token，打到这里只是被验签的 JWT。
    client_id 走构造注入（google-auth 惰性 import：不用 Google 的项目可完全不装该依赖）。
    """
    name = "google"

    def __init__(self, client_id: str = ""):
        self.client_id = (client_id or "").strip()

    async def authenticate(self, headers: Mapping[str, str]) -> Principal:
        token = bearer_token(headers, self.name)
        if not self.client_id:
            raise AuthError("服务端未配置 Google client_id", status=500)
        try:  # verify_oauth2_token 同步且会拉取 Google 证书（有缓存）→ 丢线程，不堵事件循环
            payload = await asyncio.to_thread(self._verify, token)
        except AuthError:
            raise
        except Exception as exc:  # 验签失败（过期/aud/iss/签名不符）——细节不外泄，只回 401
            raise AuthError(f"Google ID-Token 校验失败：{type(exc).__name__}", status=401) from exc
        sub = str(payload.get("sub") or "")
        if not sub:
            raise AuthError("Google ID-Token 缺 sub", status=401)
        return Principal(provider=self.name, external_id=sub,
                         email=payload.get("email"), name=payload.get("name"),
                         raw={k: payload.get(k) for k in ("email", "email_verified", "name", "picture")
                              if payload.get(k) is not None})

    def _verify(self, token: str) -> dict:
        from google.auth.transport import requests as grequests
        from google.oauth2 import id_token as gid

        return gid.verify_oauth2_token(token, grequests.Request(), audience=self.client_id)


class DevProvider(AuthProvider):
    """不校验模式：固定 dev 身份兜底（本机/隧道开发、e2e、契约测试，不打真上游）。"""
    name = "dev"

    def __init__(self, external_id: str = "dev"):
        self.external_id = external_id

    async def authenticate(self, headers: Mapping[str, str]) -> Principal:
        return Principal(provider=self.name, external_id=self.external_id,
                         name=self.external_id)


class FakeProvider(AuthProvider):
    """测试注入身份：读指定头（默认 X-Test-Auth: <external_id>）直接当已验身份。

    仅在不校验模式下排在 DevProvider 前面——带头则注入不同 uid（断言多用户隔离），
    无头抛 NoCredentials 让 Dev 兜底。生产接线别挂它。
    """
    name = "fake"

    def __init__(self, header: str = "x-test-auth", provider_tag: str = "test"):
        self.header = header
        self.provider_tag = provider_tag

    async def authenticate(self, headers: Mapping[str, str]) -> Principal:
        ext = headers.get(self.header)
        if not ext:
            raise NoCredentials(self.name)
        return Principal(provider=self.provider_tag, external_id=ext, name=ext)


# provider 名 → 工厂（env 进来，方便 google 这类需要配置的构造；微信将来 register 进来）
FactoriesT = dict[str, Callable[[Mapping[str, str]], AuthProvider]]
_FACTORIES: FactoriesT = {
    GoogleProvider.name: lambda env: GoogleProvider(env.get("GOOGLE_CLIENT_ID", "")),
    DevProvider.name: lambda env: DevProvider(),
    FakeProvider.name: lambda env: FakeProvider(),
}


def register_provider(name: str, factory: Callable[[Mapping[str, str]], AuthProvider]) -> None:
    """按名注册 provider 工厂（同名覆盖），供 ``build_from_env`` 的列表解析使用。"""
    _FACTORIES[name] = factory


def make_provider(name: str, env: Mapping[str, str] | None = None) -> AuthProvider:
    f = _FACTORIES.get(name)
    if f is None:
        raise AuthError(f"未知鉴权 provider：{name}", status=500)
    return f(env or {})
