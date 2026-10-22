"""Verify immutable deployed sources and replay failures in an isolated process."""
import hashlib
import json
import subprocess
from pathlib import Path
out=Path(__file__).parent
manifest=json.loads((out/'release-manifest.json').read_text())
program='''
import copy,hashlib,json
from pathlib import Path
import httpx
from video2md import api,graph
paths=PATHS
result={'kind':'production_process_isolated_replay','source_sha256':{f:hashlib.sha256(Path('/app/'+f.removeprefix('backend/')).read_bytes()).hexdigest() for f in paths}}
r=api._load_runs()['3c05faf8']
result['historical_task_diagnostic']=api._public_run_diagnostic(r)
checks=[]
for stage in ('ocr','clips'):
 rec=copy.deepcopy(r)
 rec['run_id']='isolated-production-guard'
 rec['entries']=rec['entries'][:1]
 rec['total']=1
 rec['partial_failures']={stage:['1']}
 calls=[]
 graph._reload=lambda *args:rec
 graph._persist=lambda *args:None
 def forbidden(*a,**kw):
  calls.append(a)
  raise AssertionError('must not invoke model')
 graph.llm_task=forbidden
 graph.select_frames=forbidden
 state=graph.node_note_entry({'run_id':rec['run_id']})
 assert not calls and state['entry_idx']==2 and stage in state['errors'][-1]
 if stage=='ocr':
  selected=graph.node_select_frames({'run_id':rec['run_id']})
  assert not calls and selected['select_payload']=={}
 checks.append({'failed_stage':stage,'model_calls':len(calls),'passed':True})
result['dependency_replay']=checks
with httpx.Client(trust_env=False,timeout=10) as c:
 result['health_status']=c.get('http://127.0.0.1:8790/healthz').status_code
 result['anonymous_me_status']=c.get('http://127.0.0.1:8790/me').status_code
 result['anonymous_cost_status']=c.get('http://127.0.0.1:8790/cost').status_code
print(json.dumps(result,ensure_ascii=False))
'''.replace('PATHS',repr(list(manifest['files'])))
args=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','lcode-4c','kubectl -n video2md exec -i deployment/video2md-api -c api -- python -']
r=subprocess.run(args,input=program,text=True,capture_output=True,check=True)
d=json.loads(r.stdout.strip().splitlines()[-1])
assert d['source_sha256']==manifest['files']
assert d['health_status']==200 and d['anonymous_me_status']==401 and d['anonymous_cost_status']==401
assert d['historical_task_diagnostic']['stage']=='clips'
(out/'production-runtime-verification.json').write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(d,ensure_ascii=False))
# Original production repro, same source code, only process-local mock network/sinks.
s=Path('docs/testing/2026-10-21/bilibili-production-retest/repro_retry_usage.py').read_text()
s=s.replace("root = Path(__file__).resolve().parents[4]", "root = Path('/app')").replace("root / 'backend/video2md'", "root / 'video2md'").replace("root / 'backend/video2md/llm.py'", "root / 'video2md/llm.py'")
r=subprocess.run(args,input=s,text=True,capture_output=True)
(out/'production-retry-after.txt').write_text(r.stdout+r.stderr)
assert r.returncode==0
print('PASS: original production retry repro now records 300 input / 100 output tokens')
