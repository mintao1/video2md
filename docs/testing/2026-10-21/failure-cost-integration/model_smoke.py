"""One real default-model connectivity probe, separate from task ledger/UI tests."""
import json
import subprocess
from pathlib import Path
program='''
import json,time
from video2md import llm
from video2md.config import ENV
from urllib.parse import urlsplit
span={}
out={'kind':'production_model_connectivity_probe','task_run_created':False,'task_ledger_included':False,
 'provider_host':urlsplit(llm.LLM_BASE).hostname,'model':llm.LLM_MODEL}
t=time.monotonic()
try:
 answer=llm.chat('Reply with the JSON object requested. No explanation.',
                 'Return exactly {"ok":true}',task='metadata',temperature=0,max_tokens=512,span_rec=span,attempts=2)
 out.update(status='success',response_nonempty=bool(answer.strip()))
except Exception as exc:
 out.update(status='failed',error_type=type(exc).__name__)
for key in ('model','attempts','attempt_usage','usage','usage_complete'):
 if key in span:out[key]=span[key]
out['elapsed_s']=round(time.monotonic()-t,3)
print('RESULT '+json.dumps(out))
'''
r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','lcode-4c','kubectl -n video2md exec -i deployment/video2md-api -c api -- python -'],input=program,text=True,capture_output=True)
line=next((line[7:] for line in r.stdout.splitlines() if line.startswith('RESULT ')),None)
if line:
 data=json.loads(line)
 Path(__file__).with_name('production-model-smoke.json').write_text(json.dumps(data,indent=2)+'\n')
 print(json.dumps(data))
else:print('probe failed before returning safe metrics')
raise SystemExit(r.returncode)
