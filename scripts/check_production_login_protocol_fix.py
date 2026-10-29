#!/usr/bin/env python3
"""Offline gate for the post-fix 4c OAuth protocol evidence."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "docs/evals/production-login-protocol-fix-eval-set-2026-10-26.jsonl"
EVIDENCE = ROOT / "docs/evals/production-login-protocol-fix-evidence-2026-10-26.json"
SECRET = re.compile(r"(?i)(?:bearer\s+\S+|api[_-]?key\s*[:=]|access[_-]?token\s*[:=]|cookie\s*[:=]|[?&](?:token|sig|key)=)")

def rows(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]

def evaluate(data: dict, catalog: list[dict]) -> list[str]:
    failures: list[str] = []
    if data.get("kind") != "production": failures.append("evidence-kind")
    env = data.get("environment", {})
    if env.get("host_alias") != "lcode-4c" or env.get("base_url") != "https://ferosse.tech": failures.append("environment")
    dep = data.get("deployment", {})
    if not all(dep.get(k) is True for k in ("rollout_ready", "pod_ready", "other_template_fields_preserved", "server_dry_run_image_only")): failures.append("deployment")
    if dep.get("api_image") != dep.get("migrate_image") or dep.get("rollback_image") == dep.get("api_image"): failures.append("image-tags")
    health = data.get("health", {})
    if not all(health.get(k) is True for k in ("healthz_ok", "preflight_ok")) or not all(health.get("preflight_checks", {}).values()): failures.append("health")
    auth = data.get("authentication", {})
    if auth.get("protocol_checkbox_checked") is not False or auth.get("button_enabled") is not False: failures.append("unchecked-ui")
    if auth.get("forced_click_left_origin") is not True or auth.get("unchecked_click_left_origin") is not True: failures.append("unchecked-navigation")
    if auth.get("oauth_login_initiated_before_protocol_acceptance") is not False: failures.append("oauth-before-acceptance")
    if not all(auth.get(k) is True for k in ("oauth_login_initiated_after_protocol_acceptance", "oauth_login_succeeded", "session_refresh_kept_login", "post_login_library_visible", "post_refresh_library_visible", "logout_to_login_button_disabled")): failures.append("accepted-login-session")
    if not all(auth.get(k) is True for k in ("backend_no_terms_rejected",)) or auth.get("backend_no_terms_location_contains_google") is not False or auth.get("backend_no_terms_created_state") is not False: failures.append("backend-guard")
    if not data.get("history", {}).get("pre_fix_bypass_evidence") or not data.get("history", {}).get("post_fix_release"): failures.append("history-chain")
    if SECRET.search(json.dumps(data, ensure_ascii=False)) or SECRET.search(json.dumps(catalog, ensure_ascii=False)): failures.append("secret-like-literal")
    ids = [r.get("id") for r in catalog]
    if ids != [f"plpf-{i:03d}" for i in range(1, 6)]: failures.append("catalog-ids")
    for row in catalog:
        if row.get("metadata", {}).get("offline_can_close_production") is not False or len(row.get("counterexamples", [])) < 3: failures.append(f"catalog-contract:{row.get('id')}")
    return failures

def main() -> int:
    p = argparse.ArgumentParser(); p.add_argument("--dataset", type=Path, default=DATASET); p.add_argument("--evidence", type=Path, default=EVIDENCE); a = p.parse_args()
    failures = evaluate(json.loads(a.evidence.read_text(encoding="utf-8")), rows(a.dataset))
    if failures:
        print("FAIL"); print("\n".join(f"- {f}" for f in failures)); return 1
    print("PASS: post-fix 4c production OAuth protocol evidence and regression catalog"); return 0
if __name__ == "__main__": raise SystemExit(main())
