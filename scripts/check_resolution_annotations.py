#!/usr/bin/env python3
"""Replay 43 source-bound review observations without closing production gaps."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import math
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "docs/evals/production-resolution-annotations.jsonl"
MISSING = object()


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def cases(path=DATASET):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def local_path(root, relative):
    """Resolve checkout references without absolute paths or symlink escapes."""
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        raise ValueError("non-local evidence reference")
    return path


def evidence_path(root, relative):
    path = local_path(root, relative)
    if not any(path.is_relative_to((root / folder).resolve()) for folder in ("docs/testing", "docs/evals")):
        raise ValueError("non-public evidence reference")
    return path


def at(data, pointer):
    try:
        if not pointer.startswith("/"):
            return MISSING
        for token in pointer[1:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            data = data[int(token)] if isinstance(data, list) else data[token]
        return data
    except (KeyError, ValueError, TypeError, IndexError):
        return MISSING


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def equal(a, b):
    if a is MISSING or b is MISSING:
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
    if number(a) and number(b):
        return a == b
    return type(a) is type(b) and a == b and type(a) not in (float, int)


def matches(data, rule):
    value = at(data, rule["path"])
    expected = at(data, rule["other"]) if "other" in rule else rule.get("value", MISSING)
    if value is MISSING or expected is MISSING:
        return False
    op = rule["op"]
    if op in ("eq", "same"):
        return equal(value, expected)
    if op == "gt":
        return number(value) and number(expected) and value > expected
    if op == "length":
        return isinstance(value, (dict, list, str)) and type(expected) is int and len(value) == expected
    if op == "contains":
        return isinstance(value, str) and isinstance(expected, str) and expected in value
    return False


def load_case(case, root=ROOT, *, verify=True):
    data = {}
    for name, ref in case["input"]["sources"].items():
        path = evidence_path(root, ref["path"])
        raw = path.read_bytes()
        if verify and hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise ValueError("evidence digest mismatch")
        data[name] = json.loads(raw) if path.suffix == ".json" else raw.decode()
    return data


def reconcile(data):
    """Recalculate from request rows, ignoring handwritten 'matches' booleans."""
    r = data["balance"]
    rows = r["requests"]
    if not rows or len({x["request_id"] for x in rows}) != len(rows):
        return False
    if len(rows) != r["provider_request_count"]:
        return False
    keys = {"llm_in_tok": "input_tokens", "llm_out_tok": "output_tokens", "llm_cached_in_tok": "cached_tokens"}
    for app, provider in keys.items():
        values = [x[provider] for x in rows]
        if not all(type(v) is int and v >= 0 for v in values):
            return False
        if sum(values) != r["application_usage"][app] or sum(values) != r["provider_usage"][app]:
            return False
    from decimal import Decimal

    usd = sum(Decimal(x["attribution_usd"]) for x in rows)
    return (
        usd == Decimal(r["provider_attribution_usd"])
        and abs(usd - Decimal(r["application_attribution_usd"])) <= Decimal("0.0000005")
    )


def delivered(data):
    book = data["book"]
    media = book["media"]
    return (
        book["run_id"] == "dd586072"
        and book["status"] == "done"
        and len(book["notes"]) == book["note_count"] == 2
        and all(type(n["markdown_bytes"]) is int and n["markdown_bytes"] > 0 for n in book["notes"])
        and len(media) == len({m["name"] for m in media}) == 22
        and all(m["exists"] is True and number(m["bytes"]) and m["bytes"] > 0 for m in media)
        and len([m for m in media if m["name"].endswith(".jpg")]) == 11
        and len([m for m in media if m["name"].endswith(".mp4")]) == 11
        and all(m["ffprobe_exit_code"] == 0 and number(m["duration_s"]) and m["duration_s"] > 0
                for m in media if m["name"].endswith(".mp4"))
    )


def login(data):
    record = data["login"]
    if not module("check_production_failure_cost").stable_oauth(record):
        return False
    for row in record["observations"]:
        times = [datetime.fromisoformat(row[k]) for k in
                 ("login_started_at", "callback_at", "me_at", "refresh_at")]
        if any(t.tzinfo is None for t in times) or times != sorted(times):
            return False
        if (times[2] - times[0]).total_seconds() != row["elapsed_s"]:
            return False
        name = row.get("screenshot")
        if not isinstance(name, str) or not name.endswith(".png") or Path(name).name != name:
            return False
    return True


def gpu_packet(data):
    p = data["probe"]
    return (
        module("check_kaggle_followup").check("kfi-002", p)
        and p["passed"] is True
        and p["isolated_dataset_and_kernel"] is True
        and type(p["upload_file_count"]) is int and p["upload_file_count"] == 2
        and p["result_count"] == 1 and p["error_count"] == 0 and p["nonempty_chars"] > 0
        and p["rapidocr_version"] == "3.10.0" and p["onnxruntime_version"] == "1.31.0"
        and p["provider_source_sha256"] == data["runtime"]["gpu_runtime"]["provider_source_sha256"]
    )


CHECKS = {"reconcile": reconcile, "delivered": delivered, "login": login, "gpu_packet": gpu_packet}


def check_case(case, data):
    try:
        return (
            all(matches(data, rule) for rule in case["expectedOutput"]["assertions"])
            and all(CHECKS[name](data) for name in case["expectedOutput"].get("checks", []))
        )
    except (KeyError, ValueError, TypeError, ArithmeticError, AttributeError):
        return False


def validate(rows, root=ROOT):
    """Coverage contracts are separate from whether old observations replay."""
    if not isinstance(rows, list) or not all(isinstance(c, dict) for c in rows):
        return ["matrix: invalid annotation rows"]
    if [c.get("id") for c in rows] != [f"pra-{i:03d}" for i in range(1, 44)]:
        return ["matrix: missing, duplicate or unordered annotation id"]
    errors = []
    for i, case in enumerate(rows, 1):
        try:
            meta = case["metadata"]
            assert meta["annotation_index"] == i and type(meta["annotation_index"]) is int
            assert meta["source_message_id"].startswith("msg_")
            assert meta["source_quote"] and meta["interpretation"]
            assert meta["offline_can_close_production"] is False
            assert meta["regressions"] and meta["production_evidence_required"]
            assert len(case["expectedOutput"]["assertions"]) >= 3
            assert len(case["counterexamples"]) >= 3
            assert case["input"]["sources"]
            for ref in case["input"]["sources"].values():
                assert len(ref["sha256"]) == 64 and int(ref["sha256"], 16) >= 0
                evidence_path(root, ref["path"])
            for entry in meta["regressions"]:
                filename, symbol = entry.split("::")
                tree = ast.parse(local_path(root, filename).read_text())
                assert any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == symbol
                           for n in tree.body), "missing executable test selector"
        except (KeyError, AssertionError, ValueError, TypeError, AttributeError, OSError, SyntaxError):
            errors.append(f"{case['id']}: invalid coverage contract")
    return errors


def production_gaps(root=ROOT):
    """Run the original strict criteria; normal generation never implies closure."""
    path = root / "docs/evals/production-failure-cost-resolution-2026-10-22.json"
    matrix = json.loads(path.read_text())
    errors = module("check_production_failure_cost").evaluate(matrix, expected_release=matrix["release"])
    path = root / "docs/testing/2026-10-22/production-resolution/reader-readiness-evidence.json"
    if not module("check_reader_readiness").evaluate(json.loads(path.read_text())):
        errors.append("media-readiness: bounded real measurement not demonstrated")
    return errors


def evaluate(rows, root=ROOT):
    errors = validate(rows, root)
    if errors:
        return errors
    for case in rows:
        try:
            data = load_case(case, root)
            if not check_case(case, data):
                errors.append(f"{case['id']}: observation or boundary regression")
        except (OSError, ValueError, TypeError, KeyError):
            errors.append(f"{case['id']}: missing, changed or invalid public evidence")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--require-closed", action="store_true")
    args = parser.parse_args()
    try:
        errors = evaluate(cases(args.dataset))
        if args.require_closed:
            errors += ["OPEN " + e for e in production_gaps()]
    except (OSError, ValueError, TypeError, KeyError):
        errors = ["invalid dataset/evidence; source values suppressed"]
    for error in errors:
        print("FAIL " + error)
    if not errors:
        print("PASS 43 offline annotation replays; no new production acceptance")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
