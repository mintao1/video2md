#!/usr/bin/env python3
"""Validate the local tenant Colab catalog and its executable regression links."""
from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "docs/evals/tenant-colab-login-eval-set.jsonl"
IDS = [f"tenant-colab-{index:03d}" for index in range(1, 13)]
AREAS = {"onboarding", "authorization", "tenant-isolation", "account-match", "execution",
         "platform-entry", "website-auth"}
RETIRED = ("frontend-login-interaction-set.jsonl", "production-login-ui-eval-set.jsonl")


def load(path: Path = DATASET) -> list:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _texts(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) and v.strip() for v in value)


def _regression(reference: object) -> bool:
    if not isinstance(reference, str):
        return False
    filename, _, name = reference.partition("::")
    path = (ROOT / filename).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        return False
    if filename.startswith("backend/tests/") and filename.endswith(".py") and name.startswith("test_"):
        tree = ast.parse(path.read_text())
        return any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
                   for node in tree.body)
    return (filename.startswith("frontend/web/tests/") and filename.endswith(".integration.test.mjs")
            and not name)


def validate(rows: object) -> list[str]:
    """Check definitions, not measured production behavior or OAuth validity."""
    if not isinstance(rows, list) or [r.get("id") if isinstance(r, dict) else None for r in rows] != IDS:
        return ["catalog: missing, duplicate, unordered or unknown case ids"]
    failures = []
    areas = set()
    for row in rows:
        case_id = row["id"]
        scenario, expected, metadata = (row.get(key) for key in ("input", "expectedOutput", "metadata"))
        if (not isinstance(scenario, dict) or not isinstance(scenario.get("setup"), dict)
                or not scenario["setup"] or not _texts(scenario.get("actions"))):
            failures.append(f"{case_id}: scenario and actions required")
        if not isinstance(expected, dict) or not _texts(expected.get("criteria")):
            failures.append(f"{case_id}: explicit acceptance criteria required")
        if (not isinstance(metadata, dict) or not isinstance(metadata.get("area"), str)
                or metadata["area"] not in AREAS or type(metadata.get("annotation_index")) is not int
                or metadata["annotation_index"] != 1 or metadata.get("mode") != "offline-regression"
                or metadata.get("offline_can_close_production") is not False
                or not isinstance(metadata.get("privacy"), str) or not metadata["privacy"].strip()):
            failures.append(f"{case_id}: requirement mapping, privacy and offline boundary required")
            metadata = {}
        else:
            areas.add(metadata["area"])
        references = metadata.get("regressions")
        if not _texts(references) or not all(_regression(ref) for ref in references):
            failures.append(f"{case_id}: executable regression links required")
        negatives = row.get("counterexamples")
        if not isinstance(negatives, list) or len(negatives) < 2:
            failures.append(f"{case_id}: at least two counterexamples required")
            continue
        names = set()
        for negative in negatives:
            if not isinstance(negative, dict):
                failures.append(f"{case_id}: malformed counterexample")
                continue
            name, output = negative.get("name"), negative.get("expectedOutput")
            refs = negative.get("regressions")
            if (not isinstance(name, str) or not name.strip() or name in names
                    or not isinstance(negative.get("input"), dict) or not negative["input"]
                    or not isinstance(output, dict) or output.get("accept") is not False
                    or not isinstance(output.get("reason"), str) or not output["reason"].strip()
                    or not _texts(refs) or not all(_regression(ref) for ref in refs)):
                failures.append(f"{case_id}: counterexample needs unique name, rejection and executable link")
            if isinstance(name, str):
                names.add(name)
    if areas != AREAS:
        failures.append("catalog: all tenant login requirement areas must be covered")
    for filename in RETIRED:
        if (ROOT / "docs/evals" / filename).exists():
            failures.append("catalog: a superseded login dataset is still active")
    return failures


def run_regressions() -> int:
    """Run offline application tests with disposable stores and no real secrets."""
    with tempfile.TemporaryDirectory(prefix="video2md-tenant-eval-") as directory:
        root = Path(directory)
        env = os.environ.copy()
        for key in env:
            if key.startswith(("V2MD_", "LLM_", "LANGFUSE_", "OTEL_", "OPENAI_", "GOOGLE_")):
                env[key] = ""
        env.update(V2MD_SECRETS_FILE=str(root / "no-secrets"), V2MD_ENV="development", V2MD_AUTH="off",
                   V2MD_CHECKPOINTER="sqlite", V2MD_FAKE_LLM="1", OBSIDIAN_VAULT=str(root / "vault"))
        for name in ("RUNS", "STATE", "USERS", "PREFS", "LLM_SETTINGS"):
            env[f"V2MD_{name}_FILE"] = str(root / f"{name.lower()}.json")
        for name in ("LOG", "UPLOADS", "OUTBOX", "COLAB_USERS"):
            env[f"V2MD_{name}_DIR"] = str(root / name.lower())
        for name in ("RUNS", "STATE", "USERS", "PREFS", "TASKS"):
            env[f"V2MD_{name}_STORE"] = "json"
        python = ROOT / "backend/.venv/bin/python"
        commands = [
            ([str(python) if python.is_file() else sys.executable, "-m", "pytest",
              "backend/tests/test_colab_auth.py", "backend/tests/test_colab_cli.py",
              "backend/tests/test_tenant_colab_eval_set.py", "-q"], ROOT),
            (["node", "--test", "tests/colab-tenant.integration.test.mjs",
              "tests/login-production.integration.test.mjs"], ROOT / "frontend/web"),
        ]
        for command, cwd in commands:
            try:
                result = subprocess.run(command, cwd=cwd, env=env, check=False)
            except OSError:
                print("FAIL: test runtime unavailable; install backend and frontend dependencies")
                return 1
            if result.returncode:
                return 1
    print("OK: offline application regressions. No deployment or new production acceptance.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--run", action="store_true", help="also run linked application tests in disposable stores")
    args = parser.parse_args()
    try:
        rows = load(args.dataset)
        failures = validate(rows)
    except (OSError, ValueError, SyntaxError):
        rows, failures = [], ["catalog: unreadable or malformed definitions"]
    if failures:
        print("FAIL: local catalog")
        print("\n".join(failures))
        return 1
    print(f"OK: local catalog, {len(rows)} cases, {sum(len(r['counterexamples']) for r in rows)} counterexamples", flush=True)
    print("This checks definitions and regression links. No production acceptance.", flush=True)
    return run_regressions() if args.run else 0


if __name__ == "__main__":
    raise SystemExit(main())
