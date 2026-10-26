"""google provider 多 audience 扩展（宿主仓 ext，acctkit core 零改动——哨兵只锁 core 文件）。

网页端授权码流（GOOGLE_WEB_CLIENT_ID，「Web」型 client）与设备流（GOOGLE_CLIENT_ID，
「TV 与 Limited Input」型）并存时，两边签发的 id_token aud 不同；引擎侧 provider 必须
两边都认，否则网页登录拿到的 token 会在 /me 被 401。external_id=sub 同口径——两种登录
方式归并同一 uid，产物归属不割裂。

不能用「加第二个 provider 名」解决：AuthEngine.authenticate 遇 AuthError 立即抛、不
fall-through（core 引擎语义），aud 不符属 AuthError——只能一个 provider 内多 audience
依次验。GOOGLE_WEB_CLIENT_ID 缺省时 audiences=[GOOGLE_CLIENT_ID]，与基座 GoogleProvider
逐调用等价（单 client 档零行为差）。
"""
from __future__ import annotations

from typing import Mapping

from acctkit.providers import GoogleProvider, _GoogleHttpxRequest, register_provider


class MultiAudGoogleProvider(GoogleProvider):
    """按 audiences 列表依次验 aud（google-auth 的 verify_oauth2_token 每次只认一个
    audience 参数）；全部不符时抛最后一个异常——对上层与基座同为「校验失败 401」。"""

    def __init__(self, client_id: str = "", *, audiences: list[str] | None = None,
                 proxy_url: str = ""):
        super().__init__(client_id, proxy_url=proxy_url)
        self._audiences = [a for a in (audiences or [client_id]) if a]

    def _verify(self, token: str) -> dict:
        from google.oauth2 import id_token as gid

        last: Exception | None = None
        request = _GoogleHttpxRequest(self.proxy_url)
        for cid in self._audiences:
            try:
                return gid.verify_oauth2_token(token, request, audience=cid)
            except Exception as exc:
                last = exc
        raise last if last is not None else ValueError("未配置任何 Google audience")


def _factory(env: Mapping[str, str]):
    web = (env.get("GOOGLE_WEB_CLIENT_ID") or "").strip()
    base = (env.get("GOOGLE_CLIENT_ID") or "").strip()
    cids = list(dict.fromkeys([web, base]))  # 去重保序：web 在前（新流优先），回落 base
    # client_id 回落 web：基座 authenticate 对空 client_id 直接 500——只配 web 档不能栽在这
    return MultiAudGoogleProvider(
        base or web,
        audiences=cids or [""],
        proxy_url=(env.get("V2PDF_GOOGLE_OAUTH_PROXY_URL") or "").strip(),
    )


# 同名覆盖基座工厂（core 提供的扩展点，register_provider 语义=同名覆盖）。
# 须在 build_from_env 之前 import 本模块（见 auth/__init__.py）。
register_provider("google", _factory)
