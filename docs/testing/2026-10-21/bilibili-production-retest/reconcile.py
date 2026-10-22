"""Recompute settled application costs from this run's redacted evidence."""
import json
from pathlib import Path

root = Path(__file__).resolve().parent
before = json.loads((root / 'production-before.json').read_text())
after = json.loads((root / 'production-final.json').read_text())
run = after['run']
usage = run.get('usage') or {}
prices = run.get('cost_pricing') or {}
cached = usage.get('llm_cached_in_tok', 0)
expected = round(
    (usage.get('llm_in_tok', 0) - cached) * prices.get('llm_in_mtok', 0) / 1e6
    + cached * prices.get('llm_cached_in_mtok', prices.get('llm_in_mtok', 0)) / 1e6
    + usage.get('llm_out_tok', 0) * prices.get('llm_out_mtok', 0) / 1e6
    + usage.get('gpu_s', 0) * prices.get('gpu_min', 0) / 60,
    6,
)
deltas = {
    key: round(after['cost']['usage'][key] - value, 6)
    for key, value in before['cost']['usage'].items()
}
gpu_stages = round(sum(
    metric.get('gpu_seconds', 0)
    for artifact in run['artifacts']
    for metric in artifact['stage_metrics'].values()
), 3)
failed_wall = round(sum(
    (span.get('durationMs') or 0) / 1000
    for span in run.get('failed_spans', [])
), 3)
result = {
    'run_id': run['run_id'],
    'status': run['status'],
    'usage': usage,
    'expected_cost_usd': expected,
    'actual_cost_usd': run.get('cost_usd'),
    'cost_formula_matches': run.get('cost_usd') is not None
    and abs(expected - run['cost_usd']) < 0.00000051,
    'monthly_deltas': deltas,
    'monthly_matches_run': deltas['runs_n'] == 1
    and all(abs(deltas[key] - usage.get(key, 0)) < 0.000001
            for key in ['llm_in_tok', 'llm_out_tok', 'gpu_s'])
    and abs(deltas['spend_usd'] - (run.get('cost_usd') or 0)) < 0.00000051,
    'all_two_notes_present': len(run['artifacts']) == 2
    and all(a['note_present'] for a in run['artifacts']),
    'partial_failures': run.get('partial_failures'),
    'successful_stage_gpu_seconds': gpu_stages,
    'failed_remote_stage_wall_seconds': failed_wall,
    'all_remote_stage_wall_seconds_estimate': round(gpu_stages + failed_wall, 3),
    'all_remote_stage_wall_matches_usage_within_1s':
    abs(gpu_stages + failed_wall - usage.get('gpu_s', 0)) <= 1,
    'gpu_scope': 'Remote stage wall-clock time; free GPU policy. Not hardware busy sampling.',
    'billing_scope': 'Application cost attribution, not supplier cash invoice reconciliation.',
}
(root / 'billing-reconciliation.json').write_text(
    json.dumps(result, ensure_ascii=False, indent=2) + '\n'
)
print(json.dumps(result, ensure_ascii=False))
