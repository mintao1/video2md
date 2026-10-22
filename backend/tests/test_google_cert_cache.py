"""Cache public certificates only; every token is still cryptographically verified."""
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from acctkit import providers

URL = 'https://www.googleapis.com/oauth2/v1/certs'
CERT = {'fixture': '-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----'}


@pytest.fixture
def transport(monkeypatch):
    cache = getattr(providers._GoogleHttpxRequest, '_cache', None)
    if cache is not None:
        cache.clear()
    calls, clients = [], []
    state = {'now': 100, 'headers': {'cache-control': 'public, max-age=60'}, 'status': 200, 'body': CERT}
    real_client = httpx.Client
    def handle(request):
        calls.append(request)
        return httpx.Response(state['status'], json=state['body'], headers=state['headers'])
    def client(**kwargs):
        instance = real_client(transport=httpx.MockTransport(handle))
        clients.append(instance)
        return instance
    monkeypatch.setattr(httpx, 'Client', client)
    monkeypatch.setattr(providers, '_cert_now', lambda: state['now'], raising=False)
    return state, calls, clients


def test_repeated_requests_and_new_adapter_share_fresh_public_certificate(transport):
    _, calls, clients = transport
    first = providers._GoogleHttpxRequest('http://proxy-a').__call__(URL)
    second = providers._GoogleHttpxRequest('http://proxy-a').__call__(URL)
    assert first.data == second.data
    assert len(calls) == 1
    assert all(c.is_closed for c in clients)


def test_expiry_and_age_never_serve_stale_keys(transport):
    state, calls, _ = transport
    state['headers']['age'] = '50'
    request = providers._GoogleHttpxRequest('')
    request(URL)
    state['now'] = 109
    request(URL)
    assert len(calls) == 1
    state['now'] = 110
    request(URL)
    assert len(calls) == 2


@pytest.mark.parametrize('headers', [{'cache-control':'no-store, max-age=60'},
    {'cache-control':'no-cache, max-age=60'}, {}, {'cache-control':'max-age=bad'},
    {'cache-control':'max-age=0'}, {'cache-control':'max-age=60','age':'bad'},
    {'cache-control':'max-age=60','age':'61'}])
def test_unfresh_or_unusable_cache_headers_are_not_cached(headers, transport):
    state, calls, _ = transport
    state['headers'] = headers
    request = providers._GoogleHttpxRequest('')
    request(URL)
    request(URL)
    assert len(calls) == 2


@pytest.mark.parametrize('bad', ['error', 'invalid-json-shape', 'unrelated-url', 'post', 'private-headers'])
def test_only_successful_public_certificate_gets_are_cached(bad, transport):
    state, calls, _ = transport
    url, kw = URL, {}
    if bad == 'error':
        state['status'] = 503
    elif bad == 'invalid-json-shape':
        state['body'] = {'error':'gateway failure'}
    elif bad == 'unrelated-url':
        url = 'https://example.test/private'
    elif bad == 'post':
        kw['method'] = 'POST'
    else:
        kw['headers'] = {'Authorization':'synthetic-canary'}
    request = providers._GoogleHttpxRequest('')
    request(url, **kw)
    request(url, **kw)
    assert len(calls) == 2


def test_proxy_isolation_and_concurrent_cold_cache_single_fetch(transport):
    _, calls, _ = transport
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: providers._GoogleHttpxRequest('proxy-a')(URL), range(8)))
    assert len(calls) == 1
    providers._GoogleHttpxRequest('proxy-b')(URL)
    assert len(calls) == 2


def test_google_auth_rechecks_audience_expiry_and_signature_on_certificate_hits(transport):
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from google.auth import crypt, jwt

    state, calls, _ = transport
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'offline fixture')])
    now = datetime.now(timezone.utc)
    cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1)).sign(key,hashes.SHA256())
    state['body'] = {'fixture': cert.public_bytes(serialization.Encoding.PEM).decode()}
    pem = key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
    signer = crypt.RSASigner.from_string(pem, key_id='fixture')
    claims = {'iss':'https://accounts.google.com','aud':'client-a','sub':'fixture','iat':int(time.time())-5,'exp':int(time.time())+60}
    provider = providers.GoogleProvider('client-a')
    token = jwt.encode(signer, claims).decode()
    assert provider._verify(token)['sub'] == 'fixture'
    for bad in ({**claims,'aud':'other'}, {**claims,'exp':int(time.time())-1}):
        with pytest.raises(ValueError):
            provider._verify(jwt.encode(signer,bad).decode())
    parts = token.split('.')
    parts[1] = parts[1][:-2] + ('aa' if not parts[1].endswith('aa') else 'bb')
    with pytest.raises(ValueError):
        provider._verify('.'.join(parts))
    from google.auth.exceptions import GoogleAuthError
    with pytest.raises(GoogleAuthError):
        provider._verify(jwt.encode(signer,{**claims,'iss':'https://evil.test'}).decode())
    assert len(calls) == 1


def test_expired_key_is_not_used_when_refresh_fails(transport):
    state, calls, _ = transport
    request = providers._GoogleHttpxRequest('')
    assert request(URL).status == 200
    state.update(now=160,status=503)
    assert request(URL).status == 503
    assert request(URL).status == 503
    assert len(calls) == 3


def test_public_cache_ttl_is_capped_and_network_timeout_bounded(transport):
    state, calls, _ = transport
    state['headers']['cache-control'] = 'public, max-age=999999'
    request = providers._GoogleHttpxRequest('')
    request(URL)
    state['now'] = 3699
    request(URL)
    assert len(calls) == 1
    state['now'] = 3700
    request(URL)
    assert len(calls) == 2
    assert calls[0].extensions['timeout']['read'] == 30
