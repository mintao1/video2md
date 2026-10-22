"""One review finding → executable positives, counterexamples and production guard."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/check_kaggle_followup.py'
SPEC = importlib.util.spec_from_file_location('kaggle_followup_gate', SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
DATASET = ROOT / 'docs/evals/kaggle-billing-followup-eval-set.jsonl'
CASES = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
EXAMPLES = [(c['id'], 'positive', c['input'], True) for c in CASES]
EXAMPLES += [(c['id'], bad['name'], bad['input'], False) for c in CASES for bad in c['counterexamples']]


def matrix():
    return {'kind': 'synthetic', 'release': CASES[-1]['input']['expected_release'],
            'gpu_release': 'gpu-router:fixture', 'run_id': 'dd586072',
            'results': [{'id': c['id'], 'status': 'pass', 'metrics': copy.deepcopy(c['input']),
                         'evidence_refs': [{'path': 'proof.json', 'sha256': 'a' * 64}]} for c in CASES]}


def evaluate(evidence, synthetic=True):
    return gate.evaluate(evidence, expected_release=CASES[-1]['input']['expected_release'],
                         expected_gpu_release='gpu-router:fixture', expected_run_id='dd586072',
                         allow_synthetic=synthetic)


def test_every_annotation_finding_has_an_executable_dataset_and_counterexamples():
    assert [c['id'] for c in CASES] == list(gate.CHECKS)
    assert len({c['metadata']['finding'] for c in CASES}) == 12
    for c in CASES:
        assert c['metadata']['validator'] == gate.CHECKS[c['id']].__name__
        assert c['metadata']['annotation_index'] == 1
        assert c['metadata']['synthetic_can_close_production'] is False
        assert len(c['counterexamples']) >= 4


@pytest.mark.parametrize('case_id,name,metrics,expected', EXAMPLES,
                         ids=[c + ':' + n for c, n, _, _ in EXAMPLES])
def test_each_positive_and_counterexample(case_id, name, metrics, expected):
    assert gate.check(case_id, metrics) is expected


@pytest.mark.parametrize('case_id', list(gate.CHECKS))
@pytest.mark.parametrize('defect', ['missing', 'duplicate', 'pending', 'empty', 'no-ref', 'bad-digest'])
def test_each_case_fails_closed(case_id, defect):
    evidence = matrix()
    row = next(r for r in evidence['results'] if r['id'] == case_id)
    if defect == 'missing':
        evidence['results'].remove(row)
    elif defect == 'duplicate':
        evidence['results'].append(copy.deepcopy(row))
    elif defect == 'pending':
        row['status'] = 'blocked'
    elif defect == 'empty':
        row['metrics'] = {}
    elif defect == 'no-ref':
        row['evidence_refs'] = []
    else:
        row['evidence_refs'][0]['sha256'] = 'invalid'
    assert evaluate(evidence)


def test_synthetic_passes_exercise_checks_but_never_close_production():
    assert evaluate(matrix()) == []
    assert any('synthetic' in f for f in evaluate(matrix(), synthetic=False))


@pytest.mark.parametrize('field', ['release', 'gpu_release', 'run_id'])
def test_exact_tested_identity_required(field):
    evidence = matrix()
    evidence[field] = 'other'
    assert any(field in f for f in evaluate(evidence))


@pytest.mark.parametrize('case_id', ['kfi-003', 'kfi-004', 'kfi-005', 'kfi-007'])
def test_business_evidence_cannot_be_borrowed_from_an_old_run(case_id):
    evidence = matrix()
    next(r for r in evidence['results'] if r['id'] == case_id)['metrics']['run_id'] = 'old'
    assert any(case_id in f for f in evaluate(evidence))


def test_test_files_are_actually_in_required_ci_commands():
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
    job = workflow['jobs']['test']
    assert job.get('continue-on-error') is not True and 'if' not in job
    for filename in ('tests/test_kaggle_followup_eval_set.py', 'tests/test_opencode_usage_reconciliation.py'):
        steps = [step for step in job['steps'] if filename in shlex.split(step.get('run', ''), comments=True)]
        assert len(steps) == 1
        assert steps[0]['run'].startswith('uv run pytest ')
        assert steps[0].get('continue-on-error') is not True and 'if' not in steps[0]
    production = yaml.safe_load((ROOT / '.github/workflows/production-pipeline-gate.yml').read_text())
    steps = [s for s in production['jobs']['verify-every-stage']['steps']
             if 'scripts/check_kaggle_followup.py' in shlex.split(s.get('run', ''))]
    assert len(steps) == 1 and steps[0]['if'] == '${{ always() }}'
    assert steps[0].get('continue-on-error') is not True
    assert '--expected-gpu-release' in steps[0]['run'] and '--expected-run-id' in steps[0]['run']
    package = json.loads((ROOT / 'frontend/web/package.json').read_text())
    assert 'tests/*.integration.test.mjs' in shlex.split(package['scripts']['test:integration'])
    assert any(s.get('run') == 'npm run test:integration' for s in workflow['jobs']['web-integration']['steps'])
    assert (ROOT / 'frontend/web/tests/kaggle-followup.integration.test.mjs').is_file()
    lint_commands = '\n'.join(s.get('run', '') for s in workflow['jobs']['lint']['steps'])
    assert '../scripts/check_kaggle_followup.py' in lint_commands
    assert 'tests/test_kaggle_followup_eval_set.py' in lint_commands


def test_hashed_evidence_detects_changed_files(tmp_path):
    path = tmp_path / 'proof.json'
    path.write_text('{}')
    evidence = matrix()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for row in evidence['results']:
        row['evidence_refs'][0]['sha256'] = digest
    path.write_text('{"changed":true}')
    assert any('digest mismatch' in f for f in gate.verify_files(evidence, tmp_path))


def test_historical_missing_8000_output_tokens_replays_actual_reconciler():
    spec = importlib.util.spec_from_file_location('opencode_usage', ROOT / 'scripts/reconcile_opencode_usage.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    folder = ROOT / 'docs/testing/2026-10-22/kaggle-token-integration'
    source = json.loads((folder / 'supplier-usage-24h-redacted.json').read_text())
    rows = [{**r, 'sessionID': 'video2md-' + r['run_id']} for r in source['rows']]
    old = json.loads((folder / 'provider-reconciliation-original.json').read_text())
    result = module.reconcile({'truncated': False, 'items': rows},
                              {'run_id': old['run_id'], 'usage': old['application_usage'],
                               'cost_usd': old['application_attribution_usd']})
    assert result['missing_usage'] == {'llm_in_tok': 18694, 'llm_out_tok': 8000, 'llm_cached_in_tok': 9344}
    assert result['usage_matches'] is False and result['supplier_cash_verified'] is False


def test_cli_rejects_synthetic_claim_without_dumping_source_values(tmp_path):
    evidence = matrix()
    evidence['secret_canary'] = 'DO_NOT_PRINT_PRIVATE_VALUES'
    path = tmp_path / 'matrix.json'
    path.write_text(json.dumps(evidence))
    result = subprocess.run([sys.executable, str(SCRIPT), str(path),
                             '--expected-release', evidence['release'],
                             '--expected-gpu-release', evidence['gpu_release'],
                             '--expected-run-id', evidence['run_id']], capture_output=True, text=True)
    assert result.returncode == 1
    assert 'synthetic' in result.stdout
    assert 'DO_NOT_PRINT_PRIVATE_VALUES' not in result.stdout + result.stderr


def current_evidence():
    return json.loads((ROOT / 'docs/evals/kaggle-billing-followup-evidence-2026-10-22.json').read_text())


def test_real_snapshot_preserves_all_three_open_cases_and_valid_file_hashes():
    evidence = current_evidence()
    failures = gate.evaluate(evidence, expected_release=evidence['release'],
                             expected_gpu_release=evidence['gpu_release'], expected_run_id=evidence['run_id'])
    assert {f.split(':')[0] for f in failures} == {'kfi-007', 'kfi-011', 'kfi-012'}
    assert gate.verify_files(evidence, ROOT / 'docs/evals') == []


@pytest.mark.parametrize('case_id', ['kfi-007', 'kfi-011', 'kfi-012'])
def test_claiming_pass_cannot_close_unresolved_real_observations(case_id):
    evidence = current_evidence()
    row = next(r for r in evidence['results'] if r['id'] == case_id)
    row['status'] = 'pass'
    assert gate.check(case_id, row['metrics']) is False


@pytest.mark.parametrize('case_id', list(gate.CHECKS))
def test_metrics_must_match_the_hashed_observation_not_just_any_valid_file(case_id):
    evidence = current_evidence()
    row = next(r for r in evidence['results'] if r['id'] == case_id)
    row['metrics']['unobserved_claim'] = True
    assert case_id + ': metrics not bound to observation source' in gate.verify_files(evidence, ROOT / 'docs/evals')


@pytest.mark.parametrize('role', ['gpu_manifest', 'gpu_runtime', 'cuda_probe'])
def test_gpu_probe_is_bound_to_deployed_image_source_and_nonce(role, tmp_path):
    evidence = current_evidence()
    row = next(r for r in evidence['results'] if r['id'] == 'kfi-002')
    ref = next(r for r in row['evidence_refs'] if r['role'] == role)
    source = json.loads((ROOT / 'docs/evals' / ref['path']).read_text())
    key = {'gpu_manifest': 'image', 'gpu_runtime': 'provider_source_sha256', 'cuda_probe': 'control_id'}[role]
    source[key] = 'wrong-source'
    path = tmp_path / 'changed-source.json'
    path.write_text(json.dumps(source))
    ref.update(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assert 'kfi-002: GPU probe not bound to deployed source and release' in gate.verify_files(evidence, ROOT / 'docs/evals')


@pytest.mark.parametrize('case_id,path,value', [
    ('kfi-002', 'nvidia_exit_code', False), ('kfi-003', 'task_count_before', False),
    ('kfi-004', 'total', 2.0), ('kfi-005', 'completed_new_books', False),
    ('kfi-009', 'provider_attribution_usd', '-1'), ('kfi-009', 'provider_attribution_usd', 'nan'),
])
def test_invalid_numeric_evidence_rejected(case_id, path, value):
    metrics = copy.deepcopy(next(c['input'] for c in CASES if c['id'] == case_id))
    metrics[path] = value
    assert gate.check(case_id, metrics) is False


@pytest.mark.parametrize('metrics', [None, [], True, 'claimed-pass'])
def test_malformed_metrics_fail_closed(metrics):
    evidence = matrix()
    evidence['results'][0]['metrics'] = metrics
    assert evaluate(evidence)
