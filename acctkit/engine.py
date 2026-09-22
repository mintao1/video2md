"""AuthEngine：acctkit 的唯一对外接口——headers in, Identity out。

藏起来的全部行为：provider 按序尝试（NoCredentials 换下一个 / AuthError 直抛）、
全落空判 401、未配置判 500、验出的 Principal 落到稳定 uid（同步存储 I/O 丢线程，
不堵事件循环）。**没有 mode 标志**：「不校验模式」= 列表排成 [Fake, Dev]
（Fake 无测试头抛 NoCredentials，Dev 永远兜底），和 google/wechat 生产接线共用
同一条按序尝试语义。
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path

from .models import AuthError, Identity, NoCredentials
from .providers import AuthProvider, make_provider
from .store import IdentityStore, JsonFileStore


class AuthEngine:
    def __init__(self, providers: Sequence[AuthProvider], store: IdentityStore):
        self.providers: list[AuthProvider] = list(providers)
        self.store = store

    async def authenticate(self, headers: Mapping[str, str]) -> Identity:
        """请求头 → 已落库 uid 的 Identity。失败抛 AuthError（status 建议码）。"""
        last: AuthError | None = None
        for p in self.providers:
            try:
                principal = await p.authenticate(headers)
            except NoCredentials as exc:
                last = exc  # 该 provider 不适用，继续下一个
                continue
            # resolve 是同步锁 I/O → 线程（JsonFileStore 场景；MemoryStore 也兼容）
            return await asyncio.to_thread(self.store.resolve, principal)
        # 所有配置的 provider 都没拿到凭据 → 401（属「未登录」，不是服务端错）
        if last is not None:
            raise AuthError("未认证：请求未携带有效凭据", status=401) from last
        raise AuthError("未配置任何鉴权 provider", status=500)


def build_from_env(env: Mapping[str, str], prefix: str = "V2MD_",
                   store: IdentityStore | None = None) -> AuthEngine:
    """按环境映射装配引擎（键：{prefix}AUTH / {prefix}AUTH_PROVIDERS /
    {prefix}USERS_FILE / GOOGLE_CLIENT_ID）；宿主项目换前缀即复用同一套配置策略。

    {prefix}AUTH=off|''|none → providers=[fake, dev]（不校验兜底）；
    否则 {prefix}AUTH_PROVIDERS 逗号列表按序实例化（provider 工厂注册表见
    providers.register_provider）。store 不传则建 {prefix}USERS_FILE 的 JsonFileStore。
    """
    mode = (env.get(prefix + "AUTH", "off") or "off").strip().lower()
    if mode in ("off", "", "none"):
        names = ["fake", "dev"]
    else:
        names = [p.strip() for p in (env.get(prefix + "AUTH_PROVIDERS", "google") or "").split(",")
                 if p.strip()]
    providers = [make_provider(n, env) for n in names]
    if store is None:
        store = JsonFileStore(Path(env.get(prefix + "USERS_FILE") or "users.json"))
    return AuthEngine(providers, store)
