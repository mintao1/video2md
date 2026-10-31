"""Offline schema gates for multilingual and adversarial quality subsets.

These tests validate that the committed cases are executable contract cases. They
do not claim that a real model has passed them; production evidence remains a
separate gate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASETS = {
    "multilingual_edge": ROOT / "docs/evals/multilingual-edge-eval-set.jsonl",
    "adversarial_safety": ROOT / "docs/evals/adversarial-safety-eval-set.jsonl",
    "select_frames": ROOT / "docs/evals/select-frames-eval-set.jsonl",
}
SECRET_RE = re.compile(r"(?:sk-[A-Za-z0-9]{12,}|AIza[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{16,})")


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_quality_subsets_have_v1_target_sizes_and_unique_ids() -> None:
    assert len(load(DATASETS["multilingual_edge"])) == 20
    assert len(load(DATASETS["adversarial_safety"])) == 20
    assert len(load(DATASETS["select_frames"])) == 30
    for rows in map(load, DATASETS.values()):
        ids = [row.get("id") for row in rows]
        assert all(isinstance(case_id, str) and case_id for case_id in ids)
        assert len(ids) == len(set(ids))


def test_each_case_has_executable_contract_and_two_badcases() -> None:
    for name, path in DATASETS.items():
        for row in load(path):
            assert row["id"].startswith(("mle-", "ase-", "sf-"))
            expected = row["expectedOutput"]
            assert expected["invariants"]
            assert isinstance(expected["requiredArtifacts"], list)
            negatives = row["negativeCases"]
            assert len(negatives) >= 2
            assert len({item["name"] for item in negatives}) == len(negatives)
            assert all(item["expected"] == "fail" for item in negatives)


def test_multilingual_subset_covers_distinct_language_and_edge_classes() -> None:
    rows = load(DATASETS["multilingual_edge"])
    languages = {row["language"] for row in rows}
    assert {"zh-CN", "en", "zh-en", "ja", "ko", "ar"} <= languages
    invariants = {item for row in rows for item in row["expectedOutput"]["invariants"]}
    assert {"preserve_meaning", "retain_timestamps", "no_invented_fact", "safe_degradation"} <= invariants


def test_adversarial_subset_covers_injection_isolation_and_cost_boundaries() -> None:
    rows = load(DATASETS["adversarial_safety"])
    attacks = {row["attack"] for row in rows}
    assert {"transcript_prompt_injection", "path_traversal", "cross_tenant_media", "quota_bypass"} <= attacks
    invariants = {item for row in rows for item in row["expectedOutput"]["invariants"]}
    assert {"audit_event", "fail_closed", "no_existence_leak"} <= invariants


def test_select_frames_subset_covers_selection_and_fallback_contracts() -> None:
    rows = load(DATASETS["select_frames"])
    assert len({row["scenario"] for row in rows}) == 30
    signals = {row["input"]["candidate_signal"] for row in rows}
    assert {"ocr_text", "mae_dedup", "llm_failure", "published_whitelist"} <= signals
    assert all(row["input"]["max_frames"] > 0 for row in rows)
    assert all("timestamps_preserved" in row["expectedOutput"]["invariants"] for row in rows)


def test_eval_rows_contain_no_real_secrets_or_signed_urls() -> None:
    for path in DATASETS.values():
        text = path.read_text(encoding="utf-8")
        assert not SECRET_RE.search(text)
        assert "?token=" not in text
        assert "?signature=" not in text

