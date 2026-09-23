"""Alertmanager → 飞书群机器人 webhook 转译器（运维三件套 2026-09-23）。

为什么存在：Alertmanager/Grafana 都没有原生飞书 receiver，飞书群机器人只要一张
text/interactive 卡片 JSON——这里收 AM 的 webhook payload，拼卡片转投飞书。

链路：Prometheus 规则 → Alertmanager(9093) → 本服务(9760) → 飞书群机器人。

部署：复用 video2md 镜像跑（依赖 fastapi/httpx 都在），obs compose 里
command: python /app/deploy/feishu_webhook.py。密钥 FEISHU_WEBHOOK_URL 从
secrets/obs.env 注入（compose env_file），本文件不含密钥。

语义纪律：
- 飞书返回 errcode!=0（如关键词不匹配/机器人被删）→ 回 502，让 Alertmanager 按退避
  重试（AM webhook 语义：非 2xx 才重试）；成功回 200。
- firing/resolved 都转发（send_resolved=true），卡片绿/红区分。
- 任何异常不炸进程：转译器挂了只丢告警，AM 会重试。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="feishu-webhook", docs_url=None, redoc_url=None, openapi_url=None)

_SEVERITY_EMOJI = {"critical": "🔴", "warning": "🟡"}
_STATUS_EMOJI = {"firing": "🚨 触发", "resolved": "✅ 已恢复"}


def _card(payload: dict) -> dict:
    """AM webhook payload → 飞书 interactive 卡片。

    AM payload 形如 {version, status, alerts: [{status, labels, annotations,
    startsAt, endsAt, generatorURL}], commonLabels, ...}；字段缺失一律兜空串。
    """
    alerts = payload.get("alerts") or []
    status = str(payload.get("status") or (alerts[0].get("status") if alerts else "") or "firing")
    lines: list[str] = []
    for a in alerts[:10]:  # 飞书卡片别太长；group_by 后同组告警本就不多
        labels = a.get("labels") or {}
        ann = a.get("annotations") or {}
        sev = str(labels.get("severity") or "warning")
        emoji = _SEVERITY_EMOJI.get(sev, "⚪")
        name = str(labels.get("alertname") or "unknown")
        summary = str(ann.get("summary") or ann.get("description") or "")
        starts = str(a.get("startsAt") or "")[:19].replace("T", " ")
        gen = str(a.get("generatorURL") or "")
        line = f"{emoji} **{name}** [{sev}] {summary}"
        if starts:
            line += f"\n开始：{starts}"
        if gen:
            line += f"\n[图表]({gen})"
        lines.append(line)
    if not lines:
        lines.append("(无告警明细)")
    head = _STATUS_EMOJI.get(status, status)
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "red" if status == "firing" else "green",
                "title": {"tag": "plain_text",
                          "content": f"video2md 告警 · {head}"},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md",
                                        "content": "\n---\n".join(lines)}},
                {"tag": "note", "elements": [
                    {"tag": "plain_text",
                     "content": f"Alertmanager → feishu-webhook · {datetime.now().isoformat(timespec='seconds')}"}]},
            ],
        },
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "configured": bool(os.environ.get("FEISHU_WEBHOOK_URL"))}


@app.post("/alert")
async def alert(request: Request) -> JSONResponse:
    url = (os.environ.get("FEISHU_WEBHOOK_URL") or "").strip()
    if not url:
        return JSONResponse(status_code=503,
                            content={"error": "FEISHU_WEBHOOK_URL 未配置（secrets/obs.env）"})
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "非 JSON payload"})
    try:
        resp = httpx.post(url, json=_card(payload), timeout=10)
        body = resp.json() if resp.status_code == 200 else {}
        # 飞书 HTTP 200 但 errcode!=0（关键词不匹配/机器人失效）也视为失败 → 502 让 AM 重试
        if resp.status_code != 200 or body.get("code") not in (None, 0) \
                or body.get("errcode") not in (None, 0):
            print(f"⚠ 飞书投递失败：HTTP {resp.status_code} body={str(body)[:200]}",
                  file=sys.stderr)
            return JSONResponse(status_code=502, content={"error": "feishu rejected"})
    except Exception as exc:
        print(f"⚠ 飞书投递异常：{str(exc)[:200]}", file=sys.stderr)
        return JSONResponse(status_code=502, content={"error": "feishu unreachable"})
    return JSONResponse(status_code=200, content={"ok": True})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("FEISHU_WEBHOOK_PORT", "9760")),
                log_level="warning")
