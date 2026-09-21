#!/usr/bin/env python3
"""给 Dify 应用「视频转文档」配置 Langfuse Cloud tracing。

链路：console 登录（~/.config/dify-zcode.env，密码 Base64）→
  POST /apps/{id}/trace-config 存 keys → POST /apps/{id}/trace 启用 → GET 验证。

用法: LANGFUSE_PK=pk-lf-xxx LANGFUSE_SK=sk-lf-xxx LANGFUSE_HOST=https://cloud.langfuse.com \
      python3 scripts/configure_langfuse_trace.py
"""
import base64
import json
import os
import sys

import requests

BASE = os.environ.get("DIFY_BASE_URL", "http://localhost:8090/v1")
BASE = BASE[:-3] if BASE.endswith("/v1") else BASE
APP_ID = os.environ.get("DIFY_APP_ID", "65ef09e5-4a5b-4cb2-9c63-3af9ae0f3c79")

# 从 ~/.config/dify-zcode.env 补全管理员凭据（python 侧不落盘）
cred_path = os.path.expanduser("~/.config/dify-zcode.env")
with open(cred_path) as f:
    for line in f:
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"'))

PK = os.environ["LANGFUSE_PK"]
SK = os.environ["LANGFUSE_SK"]
HOST = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")

s = requests.Session()
pw = base64.b64encode(os.environ["DIFY_ADMIN_PW"].encode()).decode()
r = s.post(f"{BASE}/console/api/login", json={
    "email": os.environ["DIFY_ADMIN_EMAIL"], "password": pw,
    "language": "zh-Hans", "remember_me": True}, timeout=30)
assert r.json().get("result") == "success", f"登录失败: {r.status_code} {r.text[:200]}"
H = {"X-CSRF-Token": s.cookies.get("csrf_token", "")}

# 1. 存 tracing 配置（POST；已存在则改用 PATCH）
cfg = {"public_key": PK, "secret_key": SK, "host": HOST}
body = {"tracing_provider": "langfuse", "tracing_config": cfg}
r = s.post(f"{BASE}/console/api/apps/{APP_ID}/trace-config", json=body, headers=H, timeout=30)
if r.status_code not in (200, 201):
    r = s.patch(f"{BASE}/console/api/apps/{APP_ID}/trace-config", json=body, headers=H, timeout=30)
print("trace-config:", r.status_code, r.text[:150])

# 2. 启用 tracing
r = s.post(f"{BASE}/console/api/apps/{APP_ID}/trace",
           json={"enabled": True, "tracing_provider": "langfuse"}, headers=H, timeout=30)
print("enable:", r.status_code, r.text[:150])

# 3. 验证
r = s.get(f"{BASE}/console/api/apps/{APP_ID}/trace", headers=H, timeout=30)
d = r.json()
d.pop("tracing_config", None)  # 不回显 keys
print("verify:", json.dumps(d, ensure_ascii=False)[:300])
