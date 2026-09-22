"""acctkit —— 可插拔账号登录模块（零 Web 框架依赖，零宿主依赖）。

主接口只有一个：``AuthEngine.authenticate(headers) -> Identity``。
两个可替换缝：providers（Google 已落地；微信/任意登录方式 register_provider 挂入）
与 store（users.json 文件 / 内存 / 将来 DB 同协议适配器）。宿主接线约 20 行：
build_from_env(env, 前缀) → 在框架依赖里调 engine.authenticate(request.headers)。
"""
from __future__ import annotations

from .engine import AuthEngine, build_from_env
from .models import AuthError, Identity, NoCredentials, Principal
from .providers import (AuthProvider, DevProvider, FakeProvider, GoogleProvider,
                        bearer_token, make_provider, register_provider)
from .store import IdentityStore, JsonFileStore, MemoryStore

__all__ = [
    "AuthEngine", "build_from_env",
    "AuthError", "NoCredentials", "Principal", "Identity",
    "AuthProvider", "GoogleProvider", "DevProvider", "FakeProvider",
    "bearer_token", "make_provider", "register_provider",
    "IdentityStore", "JsonFileStore", "MemoryStore",
]
