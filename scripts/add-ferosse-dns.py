#!/usr/bin/env python3
"""Create or update the ferosse.tech A records in Tencent DNSPod.

Credentials are requested interactively and are never written to disk.
"""
from __future__ import annotations

import datetime as dt
import getpass
import hashlib
import hmac
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any

HOST = "dnspod.tencentcloudapi.com"
SERVICE = "dnspod"
VERSION = "2021-03-23"
REGION = os.environ.get("TENCENTCLOUD_REGION", "ap-guangzhou")
DOMAIN = "ferosse.tech"
TARGET_IP = "110.42.234.238"


def sign(secret_id: str, secret_key: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    timestamp = int(dt.datetime.now(dt.timezone.utc).timestamp())
    date = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).strftime("%Y-%m-%d")
    content_type = "application/json; charset=utf-8"
    canonical_headers = f"content-type:{content_type}\nhost:{HOST}\n"
    signed_headers = "content-type;host"
    hashed_payload = hashlib.sha256(body.encode("utf-8")).hexdigest()
    canonical_request = f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{hashed_payload}"
    credential_scope = f"{date}/{SERVICE}/tc3_request"
    string_to_sign = "TC3-HMAC-SHA256\n{}\n{}\n{}".format(
        timestamp,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    )

    def hmac_sha256(key: bytes, value: str) -> bytes:
        return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()

    secret_date = hmac_sha256(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = hmac_sha256(secret_date, SERVICE)
    secret_signing = hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (
        f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    request = urllib.request.Request(
        f"https://{HOST}/",
        data=body.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": content_type,
            "Host": HOST,
            "X-TC-Action": action,
            "X-TC-Version": VERSION,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Region": REGION,
            "Authorization": authorization,
        },
    )
    try:
        with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"腾讯云 API HTTP {exc.code}: {detail}") from exc
    response = result.get("Response", {})
    if "Error" in response:
        error = response["Error"]
        raise RuntimeError(f"腾讯云 API {error.get('Code')}: {error.get('Message')}")
    return response


def records(secret_id: str, secret_key: str, subdomain: str) -> list[dict[str, Any]]:
    try:
        response = sign(
            secret_id,
            secret_key,
            "DescribeRecordList",
            {"Domain": DOMAIN, "Subdomain": subdomain, "Limit": 100},
        )
    except RuntimeError as exc:
        # DNSPod uses this error when the queried subdomain has no records yet.
        # Treat it as an empty list so the caller can create the first record.
        if "ResourceNotFound.NoDataOfRecord" in str(exc):
            return []
        raise
    return response.get("RecordList", [])


def upsert(secret_id: str, secret_key: str, subdomain: str) -> None:
    current = records(secret_id, secret_key, subdomain)
    cname = [r for r in current if r.get("Type") == "CNAME"]
    if cname:
        raise RuntimeError(f"{subdomain}.{DOMAIN} 已有 CNAME，未自动覆盖")
    a_records = [r for r in current if r.get("Type") == "A"]
    matching = [r for r in a_records if r.get("Value") == TARGET_IP]
    if matching:
        print(f"已存在：{subdomain}.{DOMAIN} -> {TARGET_IP}")
        return
    if len(a_records) > 1:
        values = ", ".join(str(r.get("Value")) for r in a_records)
        raise RuntimeError(f"{subdomain}.{DOMAIN} 有多个 A 记录（{values}），未自动覆盖")
    if a_records:
        action = "ModifyRecord"
        payload = {
            "Domain": DOMAIN,
            "RecordId": a_records[0]["RecordId"],
            "SubDomain": subdomain,
            "RecordType": "A",
            "RecordLine": a_records[0].get("Line", "默认"),
            "Value": TARGET_IP,
            "TTL": 600,
        }
        sign(secret_id, secret_key, action, payload)
        print(f"已更新：{subdomain}.{DOMAIN} -> {TARGET_IP}")
    else:
        action = "CreateRecord"
        payload = {
            "Domain": DOMAIN,
            "SubDomain": subdomain,
            "RecordType": "A",
            "RecordLine": "默认",
            "Value": TARGET_IP,
            "TTL": 600,
        }
        sign(secret_id, secret_key, action, payload)
        print(f"已创建：{subdomain}.{DOMAIN} -> {TARGET_IP}")


def main() -> int:
    print("请输入新生成的腾讯云 API 密钥（旧 SecretKey 已暴露，请勿继续使用）。")
    secret_id = input("SecretId: ").strip()
    secret_key = getpass.getpass("SecretKey（输入时不回显）: ").strip()
    if not secret_id or not secret_key:
        print("SecretId/SecretKey 不能为空", file=sys.stderr)
        return 2
    try:
        sign(secret_id, secret_key, "DescribeDomain", {"Domain": DOMAIN})
        upsert(secret_id, secret_key, "@")
        upsert(secret_id, secret_key, "www")
    except Exception as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    print("DNSPod 记录已处理。等待缓存刷新后可运行：dig +short A ferosse.tech")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
