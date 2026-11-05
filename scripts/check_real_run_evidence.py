#!/usr/bin/env python3
"""Validate a redacted, live production run-evidence bundle.

This gate intentionally accepts only evidence captured from real runs.  It is
separate from the offline JSONL evals: fixtures and deterministic replay can
protect contracts, but they cannot close model/provider/source acceptance.
The evidence format stores hashes and counters, never URLs, prompts, notes,
transcripts, cookies, tokens, or API keys.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
from pathlib import Path
from typing import Any

SOURCE_KINDS = {"bilibili", "youtube", "douyin", "xiaohongshu", "local"}
STAGES = ("resolve", "download", "transcribe", "frames", "ocr", "select", "clips", "note")
QUALITY = ("faithfulness", "coverage", "structure", "non_hallucination", "media_reference_valid")
PROVIDERS = ("resolve_provider", "asr_provider", "ocr_provider", "clips_provider", "llm_model")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SECRET = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._-]{12,}|api[_-]?key\s*[:=]|access[_-]?token\s*[:=]|"
    r"client[_-]?secret\s*[:=]|cookie\s*[:=]|[?&](?:token|sig|signature|key)=|"
    r"(?:https?|ftp)://[^\s]+)"
)
FORBIDDEN_KEYS = {"url", "source_url", "prompt", "completion", "transcript", "note", "cookie", "token", "api_key"}


def _finite(value: Any, *, minimum: float = 0.0, maximum: float | None = None) -> bool:
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        return False
    return maximum is None or value <= maximum


def _contains_secret(value: Any) -> bool:
    return bool(SECRET.search(json.dumps(value, ensure_ascii=False, sort_keys=True)))


def _forbidden_key(value: Any, path: str = "$") -> list[str]:
    failures: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                failures.append(f"{path}.{key}: raw content/credential field is forbidden")
            failures.extend(_forbidden_key(nested, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            failures.extend(_forbidden_key(nested, f"{path}[{index}]"))
    return failures


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _validate_run(run: dict[str, Any], index: int) -> list[str]:
    prefix = f"run-{index}"
    failures: list[str] = []
    if not isinstance(run.get("run_id"), str) or not 4 <= len(run["run_id"]) <= 64:
        failures.append(f"{prefix}-run-id: redacted run_id is required")
    source_kind = run.get("source_kind")
    if source_kind not in SOURCE_KINDS:
        failures.append(f"{prefix}-source: source_kind must be one of {sorted(SOURCE_KINDS)}")
    if not isinstance(run.get("source_ref_sha256"), str) or not SHA256.fullmatch(run["source_ref_sha256"]):
        failures.append(f"{prefix}-source-hash: source_ref_sha256 must be a SHA-256 hash")

    route = run.get("route")
    if not isinstance(route, dict):
        failures.append(f"{prefix}-route: route object is required")
    else:
        for key in PROVIDERS:
            value = route.get(key)
            if not isinstance(value, str) or not value.strip() or value.lower() in {"mock", "fixture", "unknown"}:
                failures.append(f"{prefix}-route-{key}: observed provider/model is required")
        if type(route.get("fallback_used")) is not bool:
            failures.append(f"{prefix}-route-fallback: fallback_used must be boolean")

    stages = run.get("stages")
    if not isinstance(stages, list) or [stage.get("name") for stage in stages if isinstance(stage, dict)] != list(STAGES):
        failures.append(f"{prefix}-stages: stages must contain the canonical ordered pipeline")
    else:
        for stage in stages:
            if stage.get("status") != "completed" or not _finite(stage.get("duration_ms"), minimum=1):
                failures.append(f"{prefix}-stage-{stage.get('name')}: completed status and positive duration required")

    quality = run.get("quality")
    if not isinstance(quality, dict):
        failures.append(f"{prefix}-quality: quality metrics are required")
    else:
        for key in QUALITY:
            if not _finite(quality.get(key), minimum=0, maximum=1):
                failures.append(f"{prefix}-quality-{key}: metric must be in [0,1]")
        if _finite(quality.get("faithfulness")) and quality["faithfulness"] < 0.8:
            failures.append(f"{prefix}-quality-faithfulness: below 0.80")
        if _finite(quality.get("coverage")) and quality["coverage"] < 0.75:
            failures.append(f"{prefix}-quality-coverage: below 0.75")
        if quality.get("non_hallucination") != 1 or quality.get("media_reference_valid") != 1:
            failures.append(f"{prefix}-quality-safety: non_hallucination and media_reference_valid must equal 1")

    artifact = run.get("artifact")
    if not isinstance(artifact, dict):
        failures.append(f"{prefix}-artifact: artifact counters are required")
    else:
        for key, minimum in (("transcript_chars", 1), ("note_chars", 200), ("timestamp_count", 1)):
            if type(artifact.get(key)) is not int or artifact[key] < minimum:
                failures.append(f"{prefix}-artifact-{key}: must be >= {minimum}")
        for key in ("image_refs", "video_refs"):
            if type(artifact.get(key)) is not int or artifact[key] < 0:
                failures.append(f"{prefix}-artifact-{key}: non-negative integer required")

    usage = run.get("usage")
    if not isinstance(usage, dict):
        failures.append(f"{prefix}-usage: usage counters are required")
    else:
        for key in ("llm_in_tokens", "llm_out_tokens", "gpu_seconds"):
            if not _finite(usage.get(key), minimum=0):
                failures.append(f"{prefix}-usage-{key}: finite non-negative number required")
        if not _finite(usage.get("cost_usd"), minimum=0):
            failures.append(f"{prefix}-usage-cost: finite non-negative number required")
    return failures


def validate_evidence(evidence: Any, *, min_runs: int = 5, required_sources: set[str] | None = None) -> list[str]:
    failures: list[str] = []
    required_sources = required_sources or set(SOURCE_KINDS)
    if not isinstance(evidence, dict):
        return ["root: evidence must be an object"]
    if evidence.get("kind") != "production-real-run":
        failures.append("root-kind: kind must be production-real-run")
    if evidence.get("redacted") is not True:
        failures.append("root-redacted: redacted=true is required")
    if evidence.get("live_observed") is not True:
        failures.append("root-live: live_observed=true is required")
    if evidence.get("offline_can_close_production") is not False:
        failures.append("root-offline-boundary: offline_can_close_production must be false")
    capture = evidence.get("capture")
    if not isinstance(capture, dict) or not _valid_timestamp(capture.get("captured_at")):
        failures.append("capture: captured_at ISO timestamp is required")
    release = evidence.get("release")
    if not isinstance(release, dict) or not all(isinstance(release.get(key), str) and release[key].strip() for key in ("version", "commit", "image")):
        failures.append("release: version, commit and image are required")
    runs = evidence.get("runs")
    if not isinstance(runs, list) or len(runs) < min_runs:
        failures.append(f"runs: at least {min_runs} real runs are required")
        runs = runs if isinstance(runs, list) else []
    ids = [run.get("run_id") for run in runs if isinstance(run, dict)]
    if len(ids) != len(set(ids)):
        failures.append("runs: duplicate run_id values")
    for index, run in enumerate(runs, 1):
        if isinstance(run, dict):
            failures.extend(_validate_run(run, index))
        else:
            failures.append(f"run-{index}: run must be an object")
    observed_sources = {run.get("source_kind") for run in runs if isinstance(run, dict)}
    missing = sorted(required_sources - observed_sources)
    if missing:
        failures.append(f"sources: missing real source kinds: {', '.join(missing)}")
    if _contains_secret(evidence):
        failures.append("secrets: possible credential, URL, or signed URL found")
    failures.extend(_forbidden_key(evidence))
    return sorted(set(failures))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--min-runs", type=int, default=5)
    parser.add_argument("--required-sources", default=",".join(sorted(SOURCE_KINDS)))
    args = parser.parse_args()
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL\n- input: {exc}")
        return 1
    required = {item.strip() for item in args.required_sources.split(",") if item.strip()}
    failures = validate_evidence(evidence, min_runs=max(1, args.min_runs), required_sources=required)
    if failures:
        print("FAIL")
        print("\n".join(f"- {failure}" for failure in failures))
        return 1
    print("PASS: redacted live production run evidence is complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
