"""Read-only supplemental evidence; all submissions and login use computer use."""
import json
import subprocess
import sys
from pathlib import Path

PROGRAM = '''
import hashlib,json
from datetime import datetime,timezone
from pathlib import Path
from video2md import api
rows=api._load_runs()
uid=rows['3c05faf8']['owner']
ident=api.Identity(uid=uid,provider='google',external_id='read-only-evidence')
cost=api.get_cost(ident)
out={'observed_at':datetime.now(timezone.utc).isoformat(),
 'kind':'read_only_server_state_not_ui',
 'cost':{k:v for k,v in cost.items() if k!='uid'},
 'runs':api.get_runs(ident=ident)['items'],
 'active_runs':[r['run_id'] for r in rows.values() if r.get('owner')==uid and r.get('status') in ('queued','running')],
 'source_sha256':{f:hashlib.sha256(Path('/app/video2md/'+f).read_bytes()).hexdigest() for f in ('api.py','graph.py','llm.py','bilibili.py','download.py')}}
if 'f1fff0b8' in rows:
 r=rows['f1fff0b8']
 spans=r.get('span_log',[])
 errors=' '.join(str(s.get('error','')) for s in spans)
 out['new_task_runtime']={'run_id':r['run_id'],'status':r['status'],
  'partial_failures':r.get('partial_failures'),
  'spans':[{k:s[k] for k in ('event','status','durationMs') if k in s} for s in spans],
  'llm_span_count':sum('llm' in str(s.get('event','')).lower() for s in spans),
  'error_categories':[name for name,present in (
   ('colab_quota_cooldown','colab' in errors and 'quota' in errors),
   ('kaggle_credentials_missing','Kaggle 凭证缺失' in errors)) if present]}
print(json.dumps(out,ensure_ascii=False,default=str))
'''
r = subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','lcode-4c',
 'kubectl -n video2md exec -i deployment/video2md-api -c api -- python -'],
 input=PROGRAM,text=True,capture_output=True,check=True)
result=json.loads(r.stdout.strip().splitlines()[-1])
Path(__file__).with_name(sys.argv[1]).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'observed_at':result['observed_at'],'task_count':len(result['runs']),
 'active_runs':result['active_runs'],'usage':result['cost']['usage']},ensure_ascii=False))
