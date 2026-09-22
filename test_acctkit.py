"""acctkit 模块测试：在唯一对外接口上测（headers Mapping → Identity），不碰任何宿主。

覆盖：off 语义（Fake/Dev 按序兜底）、provider 尝试次序与错误语义、GoogleProvider
（打桩验签：缺头/缺 client_id/验签失败/成功四路）、WeChatProvider（打桩换票：
unionid 优先/openid 兜底/scheme 各认各家/errcode 500 与 401 分路/资料接口降级）、
JsonFileStore 自动开户+email 回填+跨实例持久化+put_identity 账号联动预留位、
build_from_env 前缀复用。全部离线零依赖。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from acctkit import (AuthEngine, AuthError, DevProvider, FakeProvider, GoogleProvider,
                     JsonFileStore, MemoryStore, NoCredentials, Principal,
                     WeChatProvider, build_from_env)


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


# ---------------------------------------------------------------- WeChatProvider

class _StubWeChat(WeChatProvider):
    """打桩 HTTP 层（_get_json）：真实 _exchange/_userinfo 的错误映射照跑，
    只是不联网。resp/user 由测试摆好；user 给异常 = 模拟资料接口挂掉。"""
    resp = {"access_token": "at-1", "expires_in": 7200, "scope": "snsapi_login",
            "openid": "wx-open-1", "unionid": "uni-1"}
    user = {"nickname": "小薇", "headimgurl": "h.png", "openid": "wx-open-1"}

    def __init__(self, app_id: str = "wx-app-id", app_secret: str = "wx-app-secret"):
        super().__init__(app_id, app_secret)

    def _get_json(self, url: str) -> dict:
        if "oauth2/access_token" in url:
            assert "code=good-code" in url and "appid=wx-app-id" in url \
                and "secret=wx-app-secret" in url and "grant_type=authorization_code" in url
            return dict(self.resp)
        if "sns/userinfo" in url:
            if isinstance(self.user, Exception):
                raise self.user
            assert "access_token=at-1" in url
            return dict(self.user)
        raise AssertionError(f"未预期 URL：{url}")


def test_wechat_happy_unionid_preferred():
    eng = AuthEngine([_StubWeChat()], MemoryStore())
    ident = _auth(eng, {"authorization": "Wechat good-code"})
    assert ident.provider == "wechat" and ident.external_id == "uni-1"  # unionid 归并主键优先
    assert ident.name == "小薇"
    assert ident.is_new and eng.store.resolve(Principal("wechat", "uni-1")).uid == ident.uid


def test_wechat_no_unionid_falls_to_openid():
    w = _StubWeChat()
    w.resp = {k: v for k, v in w.resp.items() if k != "unionid"}
    p = asyncio.run(w.authenticate({"authorization": "Wechat good-code"}))
    assert p.external_id == "wx-open-1" and "unionid" not in p.raw
    assert p.raw["openid"] == "wx-open-1" and p.name == "小薇"


def test_wechat_userinfo_failure_degrades():
    w = _StubWeChat()
    w.user = ConnectionError("资料接口挂了")  # 登录不能死于昵称头像
    p = asyncio.run(w.authenticate({"authorization": "Wechat good-code"}))
    assert p.external_id == "uni-1" and p.name is None and p.raw["openid"] == "wx-open-1"


def test_wechat_error_mapping():
    w = _StubWeChat()
    with pytest.raises(NoCredentials):  # 不是我的 scheme → 弹给引擎换家
        asyncio.run(w.authenticate({"authorization": "Bearer some-jwt"}))
    eng = AuthEngine([_StubWeChat()], MemoryStore())
    eng.providers[0].resp = {"errcode": 40029, "errmsg": "invalid code"}  # code 无效/已用
    with pytest.raises(AuthError) as ei:
        _auth(eng, {"authorization": "Wechat good-code"})
    assert ei.value.status == 401
    eng.providers[0].resp = {"errcode": 40013, "errmsg": "invalid appid"}  # 服务端配置错
    with pytest.raises(AuthError) as ei:
        _auth(eng, {"authorization": "Wechat good-code"})
    assert ei.value.status == 500
    bare = AuthEngine([WeChatProvider("", "")], MemoryStore())  # 没配凭据但带了 code
    with pytest.raises(AuthError) as ei:
        _auth(bare, {"authorization": "Wechat good-code"})
    assert ei.value.status == 500


def test_engine_google_wechat_scheme_split_and_linking(tmp_path: Path):
    # [google, wechat] 各认各家 scheme，互不抢也不误伤
    store = JsonFileStore(tmp_path / "users.json")
    eng = AuthEngine([_StubGoogle("cid"), _StubWeChat()], store)
    g = _auth(eng, {"authorization": "Bearer good-token"})
    w = _auth(eng, {"authorization": "Wechat good-code"})
    assert (g.provider, w.provider) == ("google", "wechat")
    assert g.uid != w.uid  # 未归并前是两个 uid
    # 账号联动实弹：把微信身份挂到 Google 的 uid 上 → 之后扫码登录拿到同一个人
    store.put_identity("wechat", "uni-1", g.uid)
    w2 = _auth(eng, {"authorization": "Wechat good-code"})
    assert w2.uid == g.uid and not w2.is_new
    assert len(store._load()[g.uid]["identities"]) == 2


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


def test_build_from_env_provider_list_and_unknown():
    env = {"V2MD_AUTH": "google", "V2MD_AUTH_PROVIDERS": "google,wechat",
           "GOOGLE_CLIENT_ID": "cid-xyz",
           "WECHAT_APP_ID": "wx-id", "WECHAT_APP_SECRET": "wx-secret",
           "V2MD_USERS_FILE": "unused"}
    eng = build_from_env(env, "V2MD_", store=MemoryStore())
    assert [type(p).__name__ for p in eng.providers] == ["GoogleProvider", "WeChatProvider"]
    assert eng.providers[0].client_id == "cid-xyz"  # env 凭据注入构造（不带入引擎次序）
    assert (eng.providers[1].app_id, eng.providers[1].app_secret) == ("wx-id", "wx-secret")
    with pytest.raises(AuthError) as ei:
        build_from_env({"V2MD_AUTH": "google", "V2MD_AUTH_PROVIDERS": "sms"},
                       "V2MD_", store=MemoryStore())
    assert ei.value.status == 500  # 未注册的 provider 名（自定义登录 = register_provider 挂点）


def run_all() -> None:  # `python3 test_acctkit.py` 兜底（无 pytest 时）
    import sys
    sys.exit(pytest.main([__file__, "-q"]))


if __name__ == "__main__":
    run_all()
