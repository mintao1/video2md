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

# BOOK_AUTH 模式 → 默认 provider 链（AUTH_PROVIDERS 显式配置优先）
_MODE_PROVIDERS = {
    "token": "token",                    # 单/多静态 token（接入方）
    "wechat": "wxsession,token",         # 双轨：小程序 session + 接入方静态 token
    "google": "google",
}


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

    {prefix}AUTH=off|''|none → providers=[fake, dev]（不校验兜底）；其余模式
    {prefix}AUTH_PROVIDERS 显式列表优先，未给则按模式查 _MODE_PROVIDERS（token→token、
    wechat→wxsession,token 双轨、google→google）；未收录的模式启动即 500
    （fail-fast，替代旧默认 google 的静默错配）。store 不传则建
    {prefix}USERS_FILE 的 JsonFileStore。
    """
    mode = (env.get(prefix + "AUTH", "off") or "off").strip().lower()
    if mode in ("off", "", "none"):
        names = ["fake", "dev"]
    else:
        raw = (env.get(prefix + "AUTH_PROVIDERS", "") or "").strip() \
            or _MODE_PROVIDERS.get(mode, "")
        if not raw:
            raise AuthError(f"未知鉴权模式 {mode}（off|token|wechat|google，或显式 "
                            f"{prefix}AUTH_PROVIDERS 逗号列表）", status=500)
        names = [p.strip() for p in raw.split(",") if p.strip()]
    providers = [make_provider(n, env) for n in names]
    if store is None:
        store = JsonFileStore(Path(env.get(prefix + "USERS_FILE") or "users.json"))
    return AuthEngine(providers, store)
