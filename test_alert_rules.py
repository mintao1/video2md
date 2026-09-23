#!/usr/bin/env python3
"""test_alert_rules.py — 告警配置离线校验（不依赖 docker/promtool）。

覆盖：prometheus-rules.yml 结构（groups/rules/labels/annotations）、
8 条告警名与表达式指标名齐全、alertmanager.yml 路由/receiver/抑制结构、
feishu_webhook 卡片拼装（firing/resolved/字段兜底）与 AM payload 兼容。

运行：python3 test_alert_rules.py（纯 YAML + import，零网络）。
"""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

DEPLOY = Path(__file__).parent / "deploy"

# ---- prometheus-rules.yml：结构与告警清单 ----------------------------------------
rules_doc = yaml.safe_load((DEPLOY / "prometheus-rules.yml").read_text())
groups = rules_doc.get("groups") or []
assert groups, "至少一个 rule group"
rules = [r for g in groups for r in g.get("rules", [])]
names = {r["alert"] for r in rules if "alert" in r}
expected = {
    "Video2mdSilent", "WatchdogTripped", "RunFailed", "RunPartial",
    "CircuitBreakerTripped", "QueueStarving", "LLMFailures", "CollectorDown",
}
assert expected <= names, f"缺告警：{expected - names}"
for r in rules:
    assert r.get("expr"), f"{r.get('alert')} 缺 expr"
    assert r.get("labels", {}).get("severity") in ("critical", "warning"), \
        f"{r.get('alert')} severity 非法"
    assert r.get("annotations", {}).get("summary"), f"{r.get('alert')} 缺 summary"

# 表达式消费的指标名与 observability.py 实际导出的一致（拼错 = 永远不触发的哑规则）
expr_all = "\n".join(str(r["expr"]) for r in rules)
for metric in ("v2md_queue_queued", "v2md_watchdog_trips_total", "v2md_runs_total",
               "v2md_circuit_breaker_trips_total", "v2md_llm_requests_total"):
    assert metric in expr_all, f"规则未引用指标 {metric}"
# up 指标来自 scrape 元数据（collector exporter 目标）
assert "up{" in expr_all

# ---- prometheus.yml：rule_files + alertmanager 接线 -------------------------------
prom = yaml.safe_load((DEPLOY / "prometheus.yml").read_text())
assert prom.get("rule_files"), "prometheus.yml 未挂 rule_files"
am = prom.get("alerting", {}).get("alertmanagers", [])
assert any("127.0.0.1:9093" in t
           for m in am for sc in m.get("static_configs", []) for t in sc.get("targets", [])), \
    "alerting 未指向 127.0.0.1:9093"

# ---- alertmanager.yml：路由/receiver/抑制 -----------------------------------------
amcfg = yaml.safe_load((DEPLOY / "alertmanager.yml").read_text())
route = amcfg["route"]
assert route["receiver"] == "feishu"
assert "alertname" in route["group_by"] and "severity" in route["group_by"]
receivers = {r["name"]: r for r in amcfg["receivers"]}
assert "feishu" in receivers
wh = receivers["feishu"]["webhook_configs"][0]
assert wh["url"] == "http://127.0.0.1:9760/alert", "转译器地址漂移"
assert wh.get("send_resolved") is True
# 密钥纪律：本文件不含飞书 hook 域名（密钥只在 secrets/obs.env）
assert "open.feishu.cn" not in (DEPLOY / "alertmanager.yml").read_text()
inhibit = amcfg.get("inhibit_rules", [])
assert any("critical" in m for m in inhibit[0].get("source_matchers", [])), "缺 critical→warning 抑制"

# ---- feishu_webhook.py：卡片拼装与 AM payload 兼容 ---------------------------------
sys.path.insert(0, str(DEPLOY))
import feishu_webhook  # noqa: E402

am_payload = {
    "version": "4",
    "status": "firing",
    "alerts": [{
        "status": "firing",
        "labels": {"alertname": "WatchdogTripped", "severity": "critical",
                   "job": "video2md"},
        "annotations": {"summary": "看门狗判定 run 卡死"},
        "startsAt": "2026-09-23T04:00:00.000Z",
        "generatorURL": "http://127.0.0.1:9090/graph",
    }, {
        "status": "firing",
        "labels": {"alertname": "RunPartial", "severity": "warning"},
        "annotations": {},  # 字段缺失兜底
        "startsAt": "",
    }],
}
card = feishu_webhook._card(am_payload)
assert card["msg_type"] == "interactive"
assert card["card"]["header"]["template"] == "red"
content = card["card"]["elements"][0]["text"]["content"]
assert "WatchdogTripped" in content and "RunPartial" in content
assert "看门狗判定 run 卡死" in content
assert "[图表](http://127.0.0.1:9090/graph)" in content

resolved = feishu_webhook._card({"status": "resolved", "alerts": am_payload["alerts"]})
assert resolved["card"]["header"]["template"] == "green"

# 空 alerts / 全缺字段不炸
empty = feishu_webhook._card({})
assert "(无告警明细)" in empty["card"]["elements"][0]["text"]["content"]

print("✅ test_alert_rules.py 全部通过")
