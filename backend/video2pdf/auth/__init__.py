"""acctkit 接线层：V2PDF_* env → 单例 AuthEngine；current_identity 是本宿主的 FastAPI 适配器。

登录工程本体在顶层 `acctkit/`（零框架/零宿主依赖，可整体拷走复用）。
这里只三件事：① 按本项目配置建引擎（store 用 config.USERS_FILE——容器指 data 卷）；
② 把 FastAPI Request 降为 headers Mapping 喂引擎；③ re-export 保 api.py 既有 import 不动。
"""
from __future__ import annotations

from fastapi import (
    Request,  # 接线层允许碰框架（acctkit 本体不碰）；勿用 TYPE_CHECKING 字符串注解——FastAPI 解析不到会把参数当 query 导致 422
)

from acctkit import AuthEngine, AuthError, Identity, NoCredentials, Principal, build_from_env  # noqa: F401
from acctkit.store import IdentityStore, JsonFileStore  # noqa: F401

from ..config import ENV, USERS_FILE


def _build_store() -> IdentityStore:
    """users 存储分派（P3）：缺省 JsonFileStore（现状零变化）；
    V2PDF_USERS_STORE=postgres 且 V2PDF_PG_DSN 在 → PostgresIdentityStore（协议同缝）。"""
    if (ENV.get("V2PDF_USERS_STORE") or "").strip().lower() == "postgres":
        from ..pg import shared_pool
        from .pg_store import PostgresIdentityStore
        return PostgresIdentityStore(shared_pool())
    return JsonFileStore(USERS_FILE)


from . import ext_google  # noqa: F401, E402  自注册 google 多 audience 覆盖（须在 build_from_env 之前）

# 单例：模块 import 时装配一次（off/google 语义见 acctkit.build_from_env）
engine: AuthEngine = build_from_env(ENV, "V2PDF_", store=_build_store())


async def current_identity(request: Request) -> Identity:
    """FastAPI 依赖：请求 → 已落库 uid 的 Identity（未认证抛 AuthError→api 层转 401）。"""
    return await engine.authenticate(request.headers)


async def optional_identity(request: Request) -> Identity | None:
    """可选鉴权依赖：无/坏凭据返回 None 不 401——匿名可达端点（如 /preflight）的
    「带有效凭证则升级视图」判权用。注意 off 档 providers=[fake,dev] 恒过，
    是否算真实凭证由调用方按 V2PDF_AUTH 档位另判（见 api._preflight_privileged）。"""
    try:
        return await engine.authenticate(request.headers)
    except AuthError:
        return None


# 测试/运维辅助：直落本引擎的身份表（预置映射 / Principal→Identity 解析）
def put_identity(provider: str, external_id: str, uid: str) -> None:
    engine.store.put_identity(provider, external_id, uid)


def resolve_principal(p: Principal) -> Identity:
    return engine.store.resolve(p)
