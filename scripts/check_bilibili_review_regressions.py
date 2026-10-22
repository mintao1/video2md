#!/usr/bin/env python3
"""Offline replay of 44 annotated Bilibili findings; never calls production."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "docs/evals/bilibili-production-review-eval-set.jsonl"
DEFAULT_EVIDENCE = ROOT / "docs/testing/2026-10-21/bilibili-remediation"
JSON_FILES = {
    "final": "production-final.json",
    "before": "pre-download-fix.json",
    "dedup": "after-dedup.json",
    "billing": "billing-reconciliation.json",
    "billing_gate": "billing-evidence.json",
    "report": "evidence.json",
    "release": "release-verification.json",
    "manifest": "release-manifest-final.json",
    "deployment": "deployment-status.json",
    "reader": "reader-final.json",
    "playback": "reader-range-playback.json",
    "recovered": "reader-recovered-clip-playback.json",
    "incident": "local-test-isolation.json",
    "home": "home-egress-services.json",
    "transport": "media-transport-diagnosis.json",
    **{f"progress{i}": f"progress-{i:02d}.json" for i in (1, 3, 6, 11, 14)},
}
TEXT_FILES = {
    "pending_ui": "submission-after-fix.txt",
    "completed_ui": "production-completed.txt",
    "billing_ui": "billing-final.txt",
    "library_ui": "library-final.txt",
    "loading_ui": "reader-incremental-loading.txt",
    "reader_ui": "reader-final.txt",
    "idempotence": "home-egress-idempotence.txt",
    "nav_red": "import-navigation-red.txt",
    "nav_green": "import-navigation-green.txt",
}
PROBE_FILES = {
    "source": "source-probe.txt",
    "pagelist": "pagelist-fallback-probe.txt",
    "outage": "source-outage-fallback.txt",
}
MISSING = object()
OPEN_ITEMS = {
    2: "8集样本的原始成功探针未留存；离线覆盖8/100集不替代该历史在线证据",
    22: "重新登录已记录，但第二次登录过期原因和会话稳定性仍未定位",
    44: "登录过期未定位；本地误写前的全部值未确认恢复",
}


def cases() -> list[dict]:
    return [
        json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()
    ]


def load_evidence(directory: Path = DEFAULT_EVIDENCE) -> dict:
    data = {
        key: json.loads((directory / name).read_text())
        for key, name in JSON_FILES.items()
    }
    data.update(
        {key: (directory / name).read_text() for key, name in TEXT_FILES.items()}
    )
    data.update(
        {
            key: json.loads((directory / name).read_text())
            for key, name in PROBE_FILES.items()
        }
    )
    data["original"] = json.loads(
        (directory.parent / "bilibili-collection-production/evidence.json").read_text()
    )
    return data


def number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def derived(data: dict) -> dict:
    """Recompute cross-file facts instead of trusting handwritten PASS flags."""
    r, old = data["final"]["run"], data["original"]["billing_before"]
    usage, prices = r["usage"], r["cost_pricing"]
    quantities = [usage[k] for k in ("llm_in_tok", "llm_out_tok", "gpu_s")]
    rates = [prices[k] for k in ("llm_in_mtok", "llm_out_mtok", "gpu_min")]
    cached = usage.get("llm_cached_in_tok", 0)
    valid_numbers = all(number(v) and v >= 0 for v in quantities + rates)
    valid_numbers &= type(cached) is int and 0 <= cached <= usage["llm_in_tok"]
    valid_numbers &= prices["llm_in_mtok"] > 0 and prices["llm_out_mtok"] > 0
    valid_numbers &= prices["gpu_min"] > 0 or (
        prices["gpu_min"] == 0
        and prices.get("gpu_policy") == "free"
        and data["billing_gate"]["billing"].get("gpu_policy") == "free"
        and bool(data["billing_gate"]["billing"].get("gpu_free_reason"))
    )
    formula = round(
        (usage["llm_in_tok"] - cached) / 1e6 * prices["llm_in_mtok"]
        + cached / 1e6 * prices["llm_cached_in_mtok"]
        + usage["llm_out_tok"] / 1e6 * prices["llm_out_mtok"]
        + usage["gpu_s"] / 60 * prices["gpu_min"],
        6,
    )
    after = data["final"]["cost"]["usage"]
    deltas = {k: round(after[k] - old[k], 6) for k in after}
    expected = {**usage, "runs_n": 1, "spend_usd": r["cost_usd"]}
    expected.pop("llm_cached_in_tok", None)
    gpu_values = [
        a["stage_metrics"][s]["gpu_seconds"]
        for a in r["artifacts"]
        for s in ("transcribe", "ocr", "clips")
    ]
    gpu_sum = sum(gpu_values)
    manifest = data["manifest"]
    files = manifest["files"]
    hashes = set(data["final"]["source_sha256"]) == {
        "bilibili.py",
        "resolve.py",
        "download.py",
    }
    hashes &= all(
        files["backend/video2md/" + name] == value
        for name, value in data["final"]["source_sha256"].items()
    )
    hashes &= files["frontend/web/dist/index.html"] == data["final"]["web_index_sha256"]
    hashes &= (
        hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        == manifest["manifest_sha256"]
    )
    hashes &= data["release"]["manifest_sha256"] == manifest["manifest_sha256"]
    ids = [
        data[key]["run"]["run_id"]
        for key in ("final", "before", "dedup", "progress6", "progress11", "progress14")
    ]
    ids += [data["billing"]["run_id"], data["billing_gate"]["submission"]["run_id"]]
    delta_keys = {"llm_in_tok", "llm_out_tok", "gpu_s", "spend_usd", "runs_n"}
    return {
        "lineage": bool(ids[0]) and len(set(ids)) == 1,
        "formula": valid_numbers
        and number(r["cost_usd"])
        and r["cost_usd"] > 0
        and abs(formula - r["cost_usd"]) < 0.00000051,
        "monthly": set(deltas) == set(expected)
        and all(
            number(expected[k]) and abs(deltas[k] - expected[k]) < 0.00000051
            for k in deltas
        ),
        "gpu_sum": number(usage["gpu_s"])
        and usage["gpu_s"] > 0
        and all(number(v) and v > 0 for v in gpu_values)
        and abs(gpu_sum - usage["gpu_s"]) < 0.001,
        "dedup": data["dedup"]["cost"]["usage"] == after
        and data["dedup"]["run"]["usage"] == usage
        and data["dedup"]["run"]["cost_usd"] == r["cost_usd"]
        and set(data["billing"]["duplicate_submit_deltas"]) == delta_keys
        and all(
            number(v) and v == 0
            for v in data["billing"]["duplicate_submit_deltas"].values()
        ),
        "release_hashes": hashes and data["release"]["image"] == manifest["image"],
        "image_matches": all(
            c["image"] in {manifest["image"], "docker.io/library/" + manifest["image"]}
            for p in data["deployment"]
            for c in p["containers"]
        ),
        "home_services": set(data["home"]["services_loaded"])
        == set(data["report"]["network_services"]["launch_agents"])
        and len(data["home"]["services_loaded"]) == 2
        and all(v is True for v in data["home"]["services_loaded"].values()),
        "real_inference": data["final"]["fake_llm_enabled"] is False
        and len(
            [
                call
                for call in r["model_calls"]
                if call.get("status") == "completed"
                and call.get("model") == data["final"]["default_model"]
                and all(
                    type(call.get("usage", {}).get(k)) is int and call["usage"][k] > 0
                    for k in ("prompt_tokens", "completion_tokens")
                )
            ]
        )
        >= 4,
    }


def at(data: object, path: str) -> object:
    parts = path.split(".") if path else []

    def walk(value, remaining):
        if not remaining:
            return value
        head, *tail = remaining
        if head == "*":
            return (
                [walk(v, tail) for v in value] if isinstance(value, list) else MISSING
            )
        if isinstance(value, dict):
            return walk(value[head], tail) if head in value else MISSING
        if isinstance(value, list) and head.isdigit() and int(head) < len(value):
            return walk(value[int(head)], tail)
        return MISSING

    return walk(data, parts)


def equal(a: object, b: object) -> bool:
    if a is MISSING or b is MISSING:
        return False
    if type(a) is bool or type(b) is bool:
        return type(a) is type(b) and a == b
    if type(a) in (float, int) or type(b) in (float, int):
        return number(a) and number(b) and a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def rule_matches(data: dict, rule: dict) -> bool:
    value, op = at(data, rule["path"]), rule["op"]
    target = at(data, rule["other"]) if "other" in rule else rule.get("value")
    if value is MISSING:
        return False
    if op == "eq":
        return equal(value, target)
    if op == "len":
        return (
            isinstance(value, (list, dict, str))
            and type(target) is int
            and len(value) == target
        )
    if op == "gt":
        return number(value) and number(target) and value > target
    if op == "contains":
        return isinstance(value, str) and isinstance(target, str) and target in value
    if op == "all_eq":
        return (
            isinstance(value, list)
            and bool(value)
            and all(equal(v, target) for v in value)
        )
    if op == "all_gt":
        return (
            isinstance(value, list)
            and bool(value)
            and number(target)
            and all(number(v) and v > target for v in value)
        )
    return False


def check_case(case: dict, evidence: dict) -> bool:
    try:
        data = {**evidence, "derived": derived(evidence)}
        return all(
            rule_matches(data, rule) for rule in case["expectedOutput"]["assertions"]
        )
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, OverflowError):
        return False


def evaluate(evidence: dict, *, require_closed: bool = False) -> list[str]:
    rows = cases()
    try:
        valid = (
            [r["id"] for r in rows] == [f"bbr-{i:03d}" for i in range(1, 45)]
            and [r["metadata"]["annotation_index"] for r in rows] == list(range(1, 45))
            and all(r["expectedOutput"]["assertions"] for r in rows)
        )
    except (KeyError, TypeError):
        valid = False
    if not valid:
        return ["matrix: missing, duplicate or unknown annotation id"]
    failures = [
        f"{row['id']}: evidence violates annotated regression contract"
        for row in rows
        if not check_case(row, evidence)
    ]
    if require_closed:
        failures += [f"bbr-{i:03d}: OPEN: {reason}" for i, reason in OPEN_ITEMS.items()]
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument(
        "--require-closed",
        action="store_true",
        help="Reject unclosed production observations",
    )
    args = parser.parse_args()
    try:
        failures = evaluate(
            load_evidence(args.evidence_dir), require_closed=args.require_closed
        )
    except (OSError, ValueError) as exc:
        failures = [f"matrix: unreadable evidence ({type(exc).__name__})"]
    if failures:
        print("FAIL\n" + "\n".join(failures))
        return 1
    print(
        "PASS: 44 offline annotation replays; no new production acceptance; OPEN bbr-002/022/044"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
