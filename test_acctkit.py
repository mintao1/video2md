"""acctkit 模块测试：在唯一对外接口上测（headers Mapping → Identity），不碰任何宿主。

覆盖：off 语义（Fake/Dev 按序兜底）、provider 尝试次序与错误语义、GoogleProvider
（打桩验签：缺头/缺 client_id/验签失败/成功四路）、JsonFileStore 自动开户+email 回填
+跨实例持久化+put_identity 账号联动预留位、build_from_env 前缀复用。全部离线零依赖。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from acctkit import (AuthEngine, AuthError, DevProvider, FakeProvider, GoogleProvider,
                     JsonFileStore, MemoryStore, NoCredentials, Principal,
                     build_from_env)


def _auth(engine: AuthEngine, headers: dict):
    return asyncio.run(engine.authenticate(headers))


def _off_engine(store=None) -> AuthEngine:
    return AuthEngine([FakeProvider(), DevProvider()], store or MemoryStore())


# ---------------------------------------------------------------- 引擎：off 语义与次序

def test_off_mode_dev_stable_and_fake_injects():
    eng = _off_engine()
    a = _auth(eng, {})
    b = _auth(eng, {})
    assert a.uid == b.uid and a.provider == "dev"
    assert a.is_new and not b.is_new  # 首登开标记，二次命中不再 new
    alice = _auth(eng, {"x-test-auth": "alice"})
    bob = _auth(eng, {"x-test-auth": "bob"})
    assert len({alice.uid, bob.uid, a.uid}) == 3  # 两个注入身份 + dev 各自独立 uid
    # X-Test-Auth 同值两次 → 同 uid（稳定性）
    assert _auth(eng, {"x-test-auth": "alice"}).uid == alice.uid


def test_provider_order_no_credentials_falls_through():
    hits = []

    class Spy(GoogleProvider):
        async def authenticate(self, headers):
            hits.append(self.name)
            return await super().authenticate(headers)

    eng = AuthEngine([Spy("cid"), DevProvider()], MemoryStore())
    ident = _auth(eng, {})  # 无 Authorization → Google NoCredentials → dev 兜底
    assert ident.provider == "dev" and hits == ["google"]


def test_provider_error_short_circuits():
    class Boom(FakeProvider):
        async def authenticate(self, headers):
            raise AuthError("凭据无效（模拟）", status=401)

    second_called: list = []

    class SpyDev(DevProvider):
        async def authenticate(self, headers):
            second_called.append(1)
            return await super().authenticate(headers)

    eng = AuthEngine([Boom(), SpyDev()], MemoryStore())
    with pytest.raises(AuthError) as ei:
        _auth(eng, {"x-test-auth": "anyone"})
    assert ei.value.status == 401 and not second_called  # AuthError 直抛，不再试后家


def test_all_fallthrough_401_empty_500():
    with pytest.raises(AuthError) as ei:
        _auth(AuthEngine([FakeProvider()], MemoryStore()), {})  # 只挂 Fake 且无头
    assert ei.value.status == 401  # 「未登录」而非服务端错
    with pytest.raises(AuthError) as ei:
        _auth(AuthEngine([], MemoryStore()), {})
    assert ei.value.status == 500


# ---------------------------------------------------------------- GoogleProvider

class _StubGoogle(GoogleProvider):
    """打桩验签：不联网拿 JWKS，只走引擎装配语义。"""
    payload = {"sub": "g-1", "email": "a@b.c", "name": "A B", "picture": "p.png"}

    def _verify(self, token: str) -> dict:
        assert token == "good-token"
        return self.payload


def test_google_happy_path():
    eng = AuthEngine([_StubGoogle("cid")], MemoryStore())
    ident = _auth(eng, {"authorization": "Bearer good-token"})
    assert ident.provider == "google" and ident.external_id == "g-1"
    assert ident.email == "a@b.c" and ident.name == "A B"


def test_google_failure_modes():
    eng = AuthEngine([_StubGoogle("cid")], MemoryStore())
    # 没带 Bearer → provider 层抛 NoCredentials（引擎层才转通用 401，见上组测试）
    with pytest.raises(NoCredentials):
        asyncio.run(eng.providers[0].authenticate({}))
    with pytest.raises(AuthError):  # Bearer 有但 token 不对 → stub assert 炸 → 401（细节不外泄）
        _auth(eng, {"authorization": "Bearer bad-token"})
    eng2 = AuthEngine([GoogleProvider("")], MemoryStore())  # client_id 没配 → 500
    with pytest.raises(AuthError) as ei:
        _auth(eng2, {"authorization": "Bearer x"})
    assert ei.value.status == 500


# ---------------------------------------------------------------- JsonFileStore

def test_json_store_provision_backfill_persist(tmp_path: Path):
    p = tmp_path / "users.json"
    s1, s2 = JsonFileStore(p), JsonFileStore(p)  # 同文件两实例 = 模拟进程重启
    a = s1.resolve(Principal("google", "g-1", email="a@b.c", name="A"))
    assert a.is_new and p.exists()
    # 首登没给 email → 后续登录回填，且落盘可见
    b = s1.resolve(Principal("test", "t-2", name="B"))
    s1.resolve(Principal("test", "t-2", email="b@x.y"))
    c = s2.resolve(Principal("test", "t-2"))
    assert c.uid == b.uid and c.email == "b@x.y"
    # 跨实例稳定：同 (provider,external_id) → 同 uid
    assert s2.resolve(Principal("google", "g-1")).uid == a.uid


def test_put_identity_account_linking_reserve(tmp_path: Path):
    s = JsonFileStore(tmp_path / "users.json")
    s.put_identity("google", "g-9", "uid_person")
    s.put_identity("test", "wx-9", "uid_person")  # 同一人第二身份（微信联动预留位）
    g = s.resolve(Principal("google", "g-9"))
    w = s.resolve(Principal("test", "wx-9"))
    assert g.uid == w.uid == "uid_person"
    s.put_identity("google", "g-9", "uid_person")  # 幂等追加不重复
    assert len(s._load()["uid_person"]["identities"]) == 2


# ---------------------------------------------------------------- build_from_env

def test_build_from_env_off_and_prefix(tmp_path: Path):
    env = {"V2MD_AUTH": "off", "V2MD_USERS_FILE": str(tmp_path / "u.json")}
    eng = build_from_env(env, "V2MD_")
    assert [p.name for p in eng.providers] == ["fake", "dev"]  # off 的按序兜底装配
    assert _auth(eng, {}).provider == "dev"
    # 换前缀 = 换项目零改（store 也随前缀键走）
    env2 = {"MYAPP_AUTH": "off", "MYAPP_USERS_FILE": str(tmp_path / "m.json")}
    eng2 = build_from_env(env2, "MYAPP_")
    assert _auth(eng2, {"x-test-auth": "z"}).external_id == "z"


def test_build_from_env_google_list_and_unknown():
    env = {"V2MD_AUTH": "google", "V2MD_AUTH_PROVIDERS": "google",
           "GOOGLE_CLIENT_ID": "cid-xyz", "V2MD_USERS_FILE": "unused"}
    eng = build_from_env(env, "V2MD_", store=MemoryStore())
    assert isinstance(eng.providers[0], GoogleProvider)
    assert eng.providers[0].client_id == "cid-xyz"  # env 的 client_id 注入构造
    with pytest.raises(AuthError) as ei:
        build_from_env({"V2MD_AUTH": "google", "V2MD_AUTH_PROVIDERS": "wechat"},
                       "V2MD_", store=MemoryStore())
    assert ei.value.status == 500  # 未注册的 provider 名（微信 = register_provider 挂点）


def run_all() -> None:  # `python3 test_acctkit.py` 兜底（无 pytest 时）
    import sys
    sys.exit(pytest.main([__file__, "-q"]))


if __name__ == "__main__":
    run_all()
