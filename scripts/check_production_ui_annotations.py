#!/usr/bin/env python3
"""Eight production UI annotations: offline contracts and source-bound history."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import math
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "docs/evals/production-ui-annotations.jsonl"
spec = importlib.util.spec_from_file_location("annotation_evidence", ROOT / "scripts/check_resolution_annotations.py")
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def integer(value):
    return type(value) is int and value >= 0


def configured(record, url, model):
    return (
        set(record) == {"base_url", "model", "configured", "has_api_key"}
        and record["base_url"] == url and record["model"] == model
        and record["configured"] is True and record["has_api_key"] is True
    )


def settings_save(packet, policy):
    url, model = packet["base_url"], packet["model"]
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https" and bool(parsed.hostname)
        and not any((parsed.username, parsed.password, parsed.query, parsed.fragment))
        and isinstance(model, str) and bool(model.strip())
        and packet["actions"] == [
            {"action": "fill", "field": field} for field in ("base_url", "api_key", "model")
        ] + [{"action": "save"}]
        and packet["key_supplied"] is True
        and type(packet["save_status"]) is int and packet["save_status"] == 200
        and configured(packet["response"], url, model)
        and "配置已保存，后续启动的转换任务将使用此配置。" in packet["visible_text"]
    )


def settings_privacy(packet, policy):
    return (
        packet["actions"] == ["leave-profile", "reopen-profile", "read-settings"]
        and configured(packet["response"], packet["base_url"], packet["model"])
        and packet["key_input_type"] == "password" and packet["key_input_value"] == ""
        and isinstance(packet["storage_writes"], list)
        and all(isinstance(write["key"], str) and bool(write["key"])
                and write["contains_llm_key"] is False for write in packet["storage_writes"])
        and "已保存个人配置" in packet["visible_text"]
    )


def runtime(packet, policy):
    rows = packet["runtime"]
    expected = policy["expected_images"]
    return (
        len(rows) == len(expected)
        and {row["deployment"] for row in rows} == set(expected)
        and all(row["images"] == [expected[row["deployment"]]]
                and integer(row["generation"]) and row["generation"] > 0
                and integer(row["ready"]) and row["ready"] >= 1
                and integer(row["updated"]) and row["updated"] >= 1 for row in rows)
    )


def ocr(packet, policy):
    run = packet["run"]
    count = policy["entry_count"]
    entries = run["entries"]
    if len(entries) != count or len({entry["url"] for entry in entries}) != count:
        return False
    spans = run["spans"]
    names = [f"entry {i}/{count} ocr" for i in range(1, count + 1)] + ["stage:ocr"]
    selected = [span for span in spans if span["event"].endswith(" ocr") or span["event"] == "stage:ocr"]
    if len(selected) != len(names) or {span["event"] for span in selected} != set(names):
        return False
    if not all(span["status"] == "completed" and integer(span["durationMs"])
               and span["durationMs"] > 0 for span in selected):
        return False
    elapsed = {span["event"]: span["durationMs"] for span in selected}
    overhead = elapsed["stage:ocr"] - sum(elapsed[name] for name in names[:-1])
    return 0 <= overhead <= policy["max_stage_overhead_ms"]


def manifest(packet):
    images, videos = packet["expected_images"], packet["expected_videos"]
    names = images + videos
    return (
        bool(images) and bool(videos) and len(names) == len(set(names))
        and all(isinstance(name, str) and Path(name).name == name for name in names)
        and all(name.endswith((".jpg", ".png", ".webp")) for name in images)
        and all(name.endswith(".mp4") for name in videos)
    )


def assets(packet, policy):
    if not manifest(packet):
        return False
    rows = packet["assets"]
    names = packet["expected_images"] + packet["expected_videos"]
    return (
        len(rows) == len(names) and {row["name"] for row in rows} == set(names)
        and all(row["exists"] is True and integer(row["bytes"]) and row["bytes"] > 0 for row in rows)
        and all(type(row["ffprobe_exit_code"]) is int and row["ffprobe_exit_code"] == 0
                and number(row["duration_s"]) and row["duration_s"] > 0
                for row in rows if row["name"] in packet["expected_videos"])
    )


def ready_sample(packet, sample):
    if not manifest(packet):
        return False
    dom = sample["dom"]
    rows = dom["videos"]
    images, videos = packet["expected_images"], packet["expected_videos"]
    return (
        dom["visibility"] == "visible"
        and integer(dom["imageCount"]) and dom["imageCount"] == len(images)
        and integer(dom["decodedImages"]) and dom["decodedImages"] == len(images)
        and len(dom["imageLabels"]) == len(images) and set(dom["imageLabels"]) == set(images)
        and integer(dom["loadingCount"]) and dom["loadingCount"] == 0
        and len(rows) == len(videos) and {row["label"] for row in rows} == set(videos)
        and all(type(row["readyState"]) is int and 2 <= row["readyState"] <= 4
                and (row["error"] is None or type(row["error"]) is int and row["error"] == 0)
                for row in rows)
    )


def ready(packet, policy):
    return ready_sample(packet, packet["samples"][-1])


def timely(packet, policy):
    samples = packet["samples"]
    if len(samples) < 2:
        return False
    times = [sample["elapsed_ms"] for sample in samples]
    if not all(integer(time) for time in times) or not 0 <= times[0] <= 5000:
        return False
    if not all(0 < later - earlier <= policy["max_sample_gap_ms"]
               for earlier, later in pairwise(times)):
        return False
    if not all(sample["dom"]["visibility"] == "visible" for sample in samples):
        return False
    return any(ready_sample(packet, sample) for sample in samples
               if sample["elapsed_ms"] <= policy["readiness_budget_ms"])


def playback(packet, policy):
    played = packet["playback"]
    dom = packet["samples"][-1]["dom"]
    return (
        played["action"] == "play" and played["label"] in packet["expected_videos"]
        and any(row["label"] == played["label"] and type(row["readyState"]) is int
                and 2 <= row["readyState"] <= 4
                and (row["error"] is None or type(row["error"]) is int and row["error"] == 0)
                for row in dom["videos"])
        and played["paused"] is False
        and all(number(played[key]) for key in ("before_s", "after_s", "duration_s"))
        and played["before_s"] < played["after_s"] <= played["duration_s"]
        and (played["error"] is None or type(played["error"]) is int and played["error"] == 0)
    )


def ledger(snapshot):
    rows = snapshot["runs"]
    ids = [row["run_id"] for row in rows]
    if not ids or len(set(ids)) != len(ids) or not all(isinstance(rid, str) and rid for rid in ids):
        raise ValueError("invalid ledger")
    monthly = snapshot["cost"]["usage"]
    for key in ("spend_usd", "gpu_s", "llm_in_tok", "llm_out_tok", "runs_n"):
        if not number(monthly[key]):
            raise ValueError("unknown monthly usage")
    for key in ("llm_in_tok", "llm_out_tok", "runs_n"):
        if not integer(monthly[key]):
            raise ValueError("invalid token count")
    for row in rows:
        if row["usage"] is not None and not all(number(value) for value in row["usage"].values()):
            raise ValueError("invalid run usage")
        if row["usage"] is not None and not all(integer(value) for key, value in row["usage"].items()
                                              if key.startswith("llm_")):
            raise ValueError("invalid run tokens")
        if row["cost_usd"] is not None and not number(row["cost_usd"]):
            raise ValueError("invalid attribution")
    return {row["run_id"]: (row["usage"], row["cost_usd"]) for row in rows}, monthly


def dedup(packet, policy):
    before, after = ledger(packet["before"]), ledger(packet["after"])
    target = packet["run_id"]
    return (
        before == after and target in before[0] and packet["after"]["active_runs"] == []
        and before[0][target][0] is not None and number(before[0][target][1])
        and all(before[0][target][0][key] > 0 for key in ("llm_in_tok", "llm_out_tok"))
    )


CHECKS = {fn.__name__: fn for fn in (
    settings_save, settings_privacy, runtime, ocr, assets, ready, timely, playback, dedup,
)}


def check(name, packet, policy):
    try:
        return CHECKS[name](packet, policy) is True
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, ArithmeticError):
        return False


def cases(path=DATASET):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_sources(case, root=ROOT):
    result = {}
    for key, ref in case["input"]["sources"].items():
        path = evidence.evidence_path(root, ref["path"])
        if not any(path.relative_to(root.resolve()).is_relative_to(Path(folder))
                   for folder in ("docs/testing", "docs/evals")):
            raise ValueError("non-public resolved evidence")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise ValueError("changed evidence")
        result[key] = json.loads(raw) if path.suffix == ".json" else raw if path.suffix == ".png" else raw.decode()
    return result


def historical_packet(case, sources):
    index = case["metadata"]["annotation_index"]
    if index in (1, 2):
        # Screenshots + AX state do not reconstruct a missing fill/save/reopen action log.
        audit = sources.get("action_audit")
        if (not audit or audit.get("kind") != "production_browser_action_audit"
                or audit.get("packet", {}).get("kind") != "production"):
            return None
        return audit["packet"]
    if index == 3:
        return {"runtime": sources["runtime"]}
    if index == 4:
        return {"run": sources["progress"]["new_task_runtime"]}
    book = sources["book"]
    chapter = case["input"]["chapter"]
    rows = [row for row in book["media"] if row["name"].startswith(f"p{chapter:03d}-")]
    notes = book["notes"][chapter - 1]
    images = [row["name"] for row in rows if row["name"].endswith((".jpg", ".png", ".webp"))]
    videos = [row["name"] for row in rows if row["name"].endswith(".mp4")]
    if (len(images), len(videos)) != (notes["wiki_images"], notes["wiki_videos"]):
        raise ValueError("manifest does not match note references")
    packet = {"run_id": book["run_id"], "expected_images": images, "expected_videos": videos,
              "assets": rows, "samples": sources["samples"]}
    if index == 8:
        packet.update(before=sources["before"], after=sources["after"])
        # No play action/time advancement was recorded; do not fabricate playback.
    return packet


def validate(rows, root=ROOT):
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return ["invalid catalog"]
    if [row.get("id") for row in rows] != [f"pua-{i:03d}" for i in range(1, 9)]:
        return ["missing, duplicate or unordered annotation"]
    errors = []
    for index, case in enumerate(rows, 1):
        try:
            meta, expected = case["metadata"], case["expectedOutput"]
            assert type(meta["annotation_index"]) is int and meta["annotation_index"] == index
            assert meta["source_message_id"].startswith("msg_") and meta["source_quote"]
            assert meta["offline_can_close_production"] is False and meta["production_evidence_required"]
            assert expected["criteria"] and expected["checks"]
            assert len(set(expected["checks"])) == len(expected["checks"])
            assert set(expected["checks"]) <= CHECKS.keys()
            assert set(meta["historical_outcomes"]) == set(expected["checks"])
            assert all(value is None or type(value) is bool for value in meta["historical_outcomes"].values())
            assert case["input"]["sample"]["kind"] == "synthetic"
            assert case["input"]["sources"]
            assert len(case["counterexamples"]) >= 3 and meta["regressions"]
            for negative in case["counterexamples"]:
                assert negative["check"] in expected["checks"] and negative["name"]
                assert evidence.at(case["input"]["sample"], negative["path"]) is not evidence.MISSING
            load_sources(case, root)
            for selector in meta["regressions"]:
                filename, symbol = selector.split("::", 1)
                path = evidence.local_path(root, filename)
                assert (filename.startswith("backend/tests/") and path.suffix == ".py"
                        or filename.startswith("frontend/web/tests/") and path.suffix == ".mjs")
                source = path.read_text()
                if path.suffix == ".py":
                    assert any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol
                               for node in ast.parse(source).body)
                else:
                    assert any(f"{fn}({quote}{symbol}{quote}" in source
                               for fn in ("test", "it") for quote in ("'", '"'))
        except (KeyError, TypeError, ValueError, AssertionError, OSError, SyntaxError, AttributeError):
            errors.append(f"pua-{index:03d}: invalid catalog or public evidence")
    return errors


def replay(rows, root=ROOT):
    errors, results = validate(rows, root), []
    if errors:
        return errors, results
    for case in rows:
        policy = case["expectedOutput"]
        packet = historical_packet(case, load_sources(case, root))
        actual = {name: None if packet is None else check(name, packet, policy) for name in policy["checks"]}
        if actual != case["metadata"]["historical_outcomes"]:
            errors.append(f"{case['id']}: historical verdict changed")
        results.append({"id": case["id"], "annotation_index": case["metadata"]["annotation_index"],
                        "checks": actual, "accepted": all(value is True for value in actual.values()),
                        "status": "incomplete" if any(value is None for value in actual.values())
                        else "pass" if all(actual.values()) else "fail"})
    return errors, results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-closed", action="store_true")
    args = parser.parse_args()
    try:
        errors, results = replay(cases(args.dataset))
        if args.output:
            args.output.write_text(json.dumps({"kind": "offline_historical_replay", "results": results,
                                              "new_production_acceptance": False}, ensure_ascii=False, indent=2) + "\n")
        if args.require_closed:
            errors += [f"OPEN {row['id']}: {row['status']}" for row in results if not row["accepted"]]
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
        errors = ["invalid catalog/evidence; source values suppressed"]
    for error in errors:
        print(error)
    if not errors:
        print("PASS 8 offline annotation replays; no new production acceptance")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
