"""Execute reviewed operational code over stdin; no raw supplier headers are sent."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).parent
source = (ROOT / 'scripts/backfill_opencode_usage.py').read_text()
plan = json.loads((HERE / 'backfill-plan.json').read_text())
program = f'''
import json,hashlib
from pathlib import Path
from video2md import api,runs
ns={{'__name__':'backfill_operation'}}
exec({source!r},ns)
plan=json.loads({json.dumps(plan)!r})
before=runs._load_runs()
uid=before[plan['run_id']]['owner']
ident=api.Identity(uid=uid,provider='google',external_id='audited-backfill')
prior_cost=api.get_cost(ident)
first=ns['apply'](plan,runs.RUNS_FILE.parent/'usage-backups')
second=ns['apply'](plan,runs.RUNS_FILE.parent/'usage-backups')
after=runs._load_runs()
other_keys=set(before)-{{plan['run_id']}}
print(json.dumps({{'first':first,'second':second,
 'other_records_unchanged':all(before[k]==after.get(k) for k in other_keys),
 'run_keys_unchanged':set(before)==set(after),
 'before_month':prior_cost['usage'],'after_month':api.get_cost(ident)['usage'],
 'audit_count':len(after[plan['run_id']].get('usage_corrections',[])),
 'non_accounting_fields_unchanged':{{k:v for k,v in before[plan['run_id']].items() if k not in ('usage','cost_usd','usage_corrections')}}=={{k:v for k,v in after[plan['run_id']].items() if k not in ('usage','cost_usd','usage_corrections')}}}},ensure_ascii=False))
'''
r = subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','lcode-4c',
                    'kubectl -n video2md exec -i deployment/video2md-api -c api -- python -'],
                   input=program,text=True,capture_output=True,timeout=90,check=True)
result=json.loads(r.stdout.strip().splitlines()[-1])
(HERE/'backfill-applied.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False))
