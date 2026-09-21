#!/usr/bin/env python3
"""把 dify/视频转文档-workflow.yml 覆盖导入本地 Dify 并发布（沿用 boss-scraper-service/import_dify.py 的姿势）。

语义：
- 应用已存在（按名「视频转文档」找，优先核对 DIFY_APP_ID）→ 用新 DSL 覆盖草稿（hash 乐观锁）→ 发布
- 覆盖草稿而非新建应用：API Key / WebApp 链接 / 既有会话全部保留
- 不带试跑：全阶段链路会把真实 URL 打进本地作业服务，不该在导入时烧

用法: dify/import_workflow.py
凭证: ~/.config/dify-zcode.env（DIFY_BASE_URL / DIFY_ADMIN_EMAIL / DIFY_ADMIN_PW / DIFY_APP_ID）
Dify 1.17 控制台: Base64 密码登录 → session cookie + X-CSRF-Token 双提交。
"""
import base64
import json
import os
import sys

import requests
import yaml

sys.path.insert(0, "/Users/linmintao/Desktop/开发项目/dify_kit")
from dify_kit.config import load_config  # noqa: E402

DSL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "视频转文档-workflow.yml")
APP_NAME = "视频转文档"

cfg = load_config()  # 内部已 load_env，DIFY_ADMIN_* 进入 os.environ
BASE = cfg["base_url"].rstrip("/")
BASE = BASE[:-3] if BASE.endswith("/v1") else BASE


def login_session() -> requests.Session:
    s = requests.Session()
    pw = base64.b64encode(os.environ["DIFY_ADMIN_PW"].encode()).decode()
    r = s.post(f"{BASE}/console/api/login", json={
        "email": os.environ["DIFY_ADMIN_EMAIL"], "password": pw,
        "language": "zh-Hans", "remember_me": True}, timeout=30)
    if r.json().get("result") != "success":
        raise SystemExit(f"登录失败: {r.status_code} {r.text[:300]}")
    return s


def csrf(s: requests.Session) -> dict:
    return {"X-CSRF-Token": s.cookies.get("csrf_token", "")}


def find_app(s: requests.Session) -> str:
    r = s.get(f"{BASE}/console/api/apps?page=1&limit=100", headers=csrf(s), timeout=30)
    apps = [a for a in r.json().get("data", []) if a.get("name") == APP_NAME]
    want = os.environ.get("DIFY_APP_ID", "")
    if want:
        hit = next((a for a in apps if a.get("id") == want), None)
        if hit:
            return hit["id"]
    if apps:
        return sorted(apps, key=lambda a: a.get("created_at", 0))[-1]["id"]
    raise SystemExit(f"未找到应用「{APP_NAME}」——先在工作室手动导入一次建应用")


def main() -> None:
    wf = yaml.safe_load(open(DSL, encoding="utf-8"))["workflow"]
    local_ids = {n["id"] for n in wf["graph"]["nodes"]}

    s = login_session()
    app_id = find_app(s)
    d = s.get(f"{BASE}/console/api/apps/{app_id}/workflows/draft",
              headers=csrf(s), timeout=30).json()
    payload = {"graph": wf["graph"], "features": wf.get("features", {})}
    if d.get("hash"):
        payload["hash"] = d["hash"]
    r = s.post(f"{BASE}/console/api/apps/{app_id}/workflows/draft",
               headers=csrf(s), json=payload, timeout=60)
    if r.status_code != 200:
        raise SystemExit(f"草稿更新失败: {r.status_code} {r.text[:400]}")
    print(f"[ok] app_id={app_id} 草稿覆盖: {r.status_code}")

    r = s.post(f"{BASE}/console/api/apps/{app_id}/workflows/publish",
               headers=csrf(s), json={"marked_name": sys.argv[1] if len(sys.argv) > 1 else "v1-zcode",
                                      "marked_comment": sys.argv[2] if len(sys.argv) > 2 else "auto import"},
               timeout=30)
    if r.status_code != 200:
        raise SystemExit(f"发布失败: {r.status_code} {r.text[:400]}")
    print(f"[ok] 发布: {r.status_code}")

    # 服务端取证：把草稿读回来，比对节点 id 集合 —— 确认落进去的就是本地这份图
    d2 = s.get(f"{BASE}/console/api/apps/{app_id}/workflows/draft",
               headers=csrf(s), timeout=30).json()
    remote_ids = {n["id"] for n in (d2.get("graph") or {}).get("nodes", [])}
    if remote_ids != local_ids:
        missing = sorted(local_ids - remote_ids)
        extra = sorted(remote_ids - local_ids)
        raise SystemExit(f"回读校验失败：缺 {missing[:5]} 多 {extra[:5]}")
    n_code = sum(1 for n in (d2.get("graph") or {}).get("nodes", [])
                 if n.get("data", {}).get("type") == "code")
    print(f"[ok] 回读取证: {len(remote_ids)} 节点与本地一致（code 节点 {n_code}）")


if __name__ == "__main__":
    main()
