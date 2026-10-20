"""鉴权 provider（可插拔）：headers in → Principal out，不碰存储/uid。

契约：``await authenticate(headers) -> Principal`` 或抛
- NoCredentials：请求里没找到本 provider 的凭据（引擎换下一个 provider 试）
- AuthError：找到了凭据但无效（引擎直接向上抛）

``headers`` 是 Mapping[str, str]（键按 HTTP 惯例小写；主流框架的 Headers 类型
都是大小写不敏感 Mapping，直接传即可），刻意不依赖任何 Web 框架。凭据形态不同的登录方式
实现一个 authenticate 注册进 ``register_provider`` 即可挂上。本模块=内核基座
（基座 provider：google/wechat/dev/fake）；仓特有扩展（WxApp/Token/GoogleDevice 等）
归消费方仓 ``ext_*`` 模块自注册（X18 拆分），不进本基座，引擎与存储零改动
（微信开放平台扫码 = ``WeChatProvider``，已落地）。
"""
from __future__ import annotations

import abc
import asyncio
import json
import urllib.request
from collections.abc import Callable, Mapping
from urllib.parse import urlencode

from .models import AuthError, NoCredentials, Principal


class _GoogleHttpxResponse:
    """Small google-auth transport response adapter backed by HTTPX."""

    def __init__(self, response):
        self.status = response.status_code
        self.data = response.content
        self.headers = response.headers


class _GoogleHttpxRequest:
    """Use the same proven HTTPX CONNECT path as the OAuth token exchange."""

    def __init__(self, proxy_url: str):
        import httpx

        self._client = httpx.Client(proxy=proxy_url or None)

    def __call__(self, url, method="GET", body=None, headers=None, timeout=120, **kwargs):
        import httpx
        from google.auth import exceptions as gexceptions

        try:
            response = self._client.request(
                method,
                url,
                content=body,
                headers=headers,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise gexceptions.TransportError(str(exc)) from exc
        return _GoogleHttpxResponse(response)


def credential_token(headers: Mapping[str, str], provider: str, scheme: str = "Bearer") -> str:
    """从 ``Authorization: <scheme> <token>`` 取凭据；缺头/scheme 不符 → NoCredentials。

    scheme 比较大小写不敏感（HTTP 惯例）；各 provider 用自己的 scheme 头互不抢
    （Google=Bearer、微信=Wechat），错家的头会被 NoCredentials 弹给引擎换下家。
    """
    raw = headers.get("authorization") or ""
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != scheme.lower() or not parts[1].strip():
        raise NoCredentials(provider)
    return parts[1].strip()


def bearer_token(headers: Mapping[str, str], provider: str) -> str:
    """从 Authorization: Bearer <token> 取 token；缺头/格式不符 → NoCredentials。"""
    return credential_token(headers, provider, "Bearer")


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

    def __init__(self, client_id: str = "", *, proxy_url: str = ""):
        self.client_id = (client_id or "").strip()
        self.proxy_url = (proxy_url or "").strip()

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
        from google.oauth2 import id_token as gid

        request = _GoogleHttpxRequest(self.proxy_url)
        return gid.verify_oauth2_token(token, request, audience=self.client_id)


class WeChatProvider(AuthProvider):
    """微信开放平台扫码登录（网站/移动应用）：``Authorization: Wechat <code>`` 带
    OAuth 授权码，服务端用 appid+secret 换 openid/unionid。

    前端引导扫码 → 微信回调带 code（5 分钟有效、**单次使用**）→ 客户端把 code 打到这里
    换身份。code 无效/已被消费 = 凭据问题（401，客户端须重新引导扫码，不自动重试）；
    appid/secret 配错（40013/41004）= 服务端配置问题（500）。scope 拿得到 unionid 时
    external_id 用 unionid（跨应用账号归并主键，配 put_identity 联动），否则 openid。

    零新依赖：urllib 直连且显式禁代理——api.weixin.qq.com 是国内站，进程环境里为
    境外源准备的 HTTP_PROXY/V2MD_PROXY 隧道出口不该也不能参与微信换票。
    access_token 只用于顺带拉昵称头像（best-effort，失败不挡登录），不外发不落盘。
    """
    name = "wechat"
    _get_json_direct = staticmethod(lambda url: _get_json_direct(url))
    _CONFIG_ERRCODES = {40013, 41004, 40002}  # invalid appid / appsecret missing / invalid secret

    def __init__(self, app_id: str = "", app_secret: str = ""):
        self.app_id = (app_id or "").strip()
        self.app_secret = (app_secret or "").strip()

    async def authenticate(self, headers: Mapping[str, str]) -> Principal:
        code = credential_token(headers, self.name, "Wechat")
        if not (self.app_id and self.app_secret):
            raise AuthError("服务端未配置微信 appid/secret", status=500)
        try:  # 同步阻塞 I/O → 丢线程（同 Google._verify 纪律）
            tok = await asyncio.to_thread(self._exchange, code)
        except AuthError:
            raise
        except Exception as exc:  # 网络/解析异常按凭据失败收口（细节不外泄）
            raise AuthError(f"微信授权码换取失败：{type(exc).__name__}", status=401) from exc
        openid = str(tok.get("openid") or "")
        unionid = str(tok.get("unionid") or "")
        if not openid:
            raise AuthError("微信未返回 openid", status=401)
        info: dict = {}
        try:  # 资料接口 best-effort：挂了只是没昵称头像，登录照过
            info = await asyncio.to_thread(self._userinfo,
                                           str(tok.get("access_token") or ""), openid) or {}
        except Exception:
            pass
        raw = {"openid": openid}
        if unionid:
            raw["unionid"] = unionid
        for k in ("nickname", "headimgurl"):
            if info.get(k):
                raw[k] = info[k]
        return Principal(provider=self.name, external_id=unionid or openid,
                         name=str(info.get("nickname") or "") or None, raw=raw)

    def _get_json(self, url: str) -> dict:
        return self._get_json_direct(url)

    def _exchange(self, code: str) -> dict:
        tok = self._get_json_direct("https://api.weixin.qq.com/sns/oauth2/access_token?" + urlencode({
            "appid": self.app_id, "secret": self.app_secret,
            "code": code, "grant_type": "authorization_code"}))
        errcode = tok.get("errcode")
        if errcode:
            status = 500 if int(errcode) in self._CONFIG_ERRCODES else 401
            raise AuthError(f"微信授权码无效（errcode={errcode}）", status=status)
        return tok

    def _userinfo(self, access_token: str, openid: str) -> dict:
        return self._get_json_direct("https://api.weixin.qq.com/sns/userinfo?" + urlencode({
            "access_token": access_token, "openid": openid, "lang": "zh_CN"}))


def _get_json_direct(url: str) -> dict:
    """GET → JSON：urllib 直连且显式禁代理（api.weixin.qq.com 国内站，不走境外
    隧道出口），10s 超时。微信系 provider 共用。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": "acctkit"})
    with opener.open(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


# provider 名 → 工厂（env 进来，方便 google 这类需要配置的构造；自定义登录方式 register 进来）
# 仓特有 provider（token/wxsession 等）由消费方 ext_* 模块 register_provider 自注册
FactoriesT = dict[str, Callable[[Mapping[str, str]], AuthProvider]]
_FACTORIES: FactoriesT = {
    GoogleProvider.name: lambda env: GoogleProvider(
        env.get("GOOGLE_CLIENT_ID", ""),
        proxy_url=env.get("V2MD_GOOGLE_OAUTH_PROXY_URL", ""),
    ),
    WeChatProvider.name: lambda env: WeChatProvider(env.get("WECHAT_APP_ID", ""),
                                                     env.get("WECHAT_APP_SECRET", "")),
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
# === 本仓扩展自注册（X18 拆分：上方为 acctkit-core v0.1.0 逐字节快照，哨兵执法）===

class GoogleDeviceProvider(GoogleProvider):
    """Google OAuth 设备流（RFC 8628）：``Authorization: GoogleDevice <device_code>``
    带设备码，服务端到 Google token 端点换 id_token，再走父类同一套 JWKS 验签取 sub。

    给「没有浏览器重定向能力」的客户端（Obsidian 插件、CLI）用：客户端先经
    ``POST /auth/device/code`` 拿 user_code+verification_url（或直连 Google），用户
    在任意浏览器输码授权；客户端轮询本凭据直到 authorization_pending 转好。
    external_id=sub 与 GoogleProvider **同口径**——设备流登录和前端 id_token 登录
    归并成同一个 uid，产物归属不割裂。需要 client 是 OAuth 应用里「TV 与 Limited Input
    类型」（Web 型 client 无 device code 授权端点）。

    Google 端点可注入（_token_url）供测试打桩；网络异常按凭据失败收口 401（细节不外泄），
    未配置 client_id = 500（服务端问题），authorization_pending/slow_down = 401（未登录）。
    """
    name = "google_device"

    def __init__(self, client_id: str = "", client_secret: str = "",
                 token_url: str = "https://oauth2.googleapis.com/token"):
        super().__init__(client_id)
        self.client_secret = (client_secret or "").strip()
        self._token_url = token_url

    async def authenticate(self, headers: Mapping[str, str]) -> Principal:
        device_code = credential_token(headers, self.name, "GoogleDevice")
        if not self.client_id:
            raise AuthError("服务端未配置 Google client_id", status=500)
        try:  # 同步阻塞 I/O → 丢线程（同 WeChat._exchange 纪律）
            tok = await asyncio.to_thread(self._exchange, device_code)
        except AuthError:
            raise
        except Exception as exc:
            raise AuthError(f"Google 设备码换取失败：{type(exc).__name__}", status=401) from exc
        idt = str(tok.get("id_token") or "")
        if not idt:
            raise AuthError("Google 设备流未返回 id_token", status=401)
        return await self._principal_from_idt(idt)

    def _exchange(self, device_code: str) -> dict:
        # googleapis 在墙内部署须经代理出墙（env HTTPS_PROXY；4C mihomo 实测直连
        # Errno101）。ProxyHandler({}) 显式禁代理是 2026-09-27 生产照出的坑——改
        # getproxies() 尊重环境：无代理环境行为不变（空表=直连同旧），有代理则走。
        # WeChat 的 _get_json 保持直连（api.weixin 国内可达，不走此径）。
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(urllib.request.getproxies()))
        req = urllib.request.Request(
            self._token_url,
            data=urlencode({"client_id": self.client_id, "client_secret": self.client_secret,
                            "device_code": device_code,
                            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}
                           ).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": "acctkit"})
        try:
            with opener.open(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                body = {}
            err = str(body.get("error") or "")
            # pending/slow_down = 用户还没授权（属「未登录」401，客户端继续轮询）；
            # expired/invalid_grant = 凭据废了（401，须重新发起设备流）
            raise AuthError(f"Google 设备流未就绪（{err or e.code}）", status=401) from e

    async def _principal_from_idt(self, idt: str) -> Principal:
        try:
            payload = await asyncio.to_thread(self._verify, idt)
        except AuthError:
            raise
        except Exception as exc:
            raise AuthError(f"Google ID-Token 校验失败：{type(exc).__name__}", status=401) from exc
        sub = str(payload.get("sub") or "")
        if not sub:
            raise AuthError("Google ID-Token 缺 sub", status=401)
        return Principal(provider="google", external_id=sub,  # 归并到 google 身份口径
                         email=payload.get("email"), name=payload.get("name"),
                         raw={k: payload.get(k) for k in ("email", "email_verified", "name", "picture")
                              if payload.get(k) is not None})


_FACTORIES[GoogleDeviceProvider.name] = lambda env: GoogleDeviceProvider(
    env.get("GOOGLE_CLIENT_ID", ""), env.get("GOOGLE_CLIENT_SECRET", ""))
