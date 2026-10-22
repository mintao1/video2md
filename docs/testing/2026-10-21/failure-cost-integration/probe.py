"""Content-free, read-only production probes. No authorization codes/tokens used."""
import json
import subprocess
import sys
from pathlib import Path

api_program = '''
import asyncio,hashlib,json,time
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from video2md import api
from video2md.config import ENV
runs=api._load_runs()
result={'active_runs':[r['run_id'] for r in runs.values() if r.get('status') not in ('done','partial','error','cancelled')],
 'source_sha256':{f:hashlib.sha256(Path('/app/video2md/'+f).read_bytes()).hexdigest() for f in ('api.py','llm.py','graph.py')},
 'oauth_timeout_s':api._google_oauth_timeout_s(),
 'model':ENV.get('LLM_MODEL'),
 'provider_host':urlsplit(ENV.get('LLM_BASE_URL') or '').hostname,
 'google_proxy_host':urlsplit(api._google_oauth_proxy() or '').hostname,
 'google_proxy_port':urlsplit(api._google_oauth_proxy() or '').port,
 'remote_modes':{key:ENV.get(key) for key in ('V2MD_REMOTE','V2MD_OCR_REMOTE','V2MD_CLIPS_REMOTE')}}
async def probes():
 rows=[]
 for n in range(3):
  t=time.monotonic()
  try:
   async with httpx.AsyncClient(proxy=api._google_oauth_proxy(),timeout=6) as c:
    r=await c.post(api._GOOGLE_OAUTH_TOKEN_URL,data={'grant_type':'authorization_code','code':'invalid-probe-no-credentials'})
   rows.append({'attempt':n+1,'status':r.status_code,'elapsed_s':round(time.monotonic()-t,3)})
  except Exception as e:
   rows.append({'attempt':n+1,'error_type':type(e).__name__,'elapsed_s':round(time.monotonic()-t,3)})
 return rows
result['oauth_no_credential_probes']=asyncio.run(probes())
print(json.dumps(result))
'''
gpu_program = '''
import hashlib,json,os,subprocess
from pathlib import Path
p=Path('/app/gpu_plugins/v2md/providers/colab_machinery.py')
result={'machinery_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'source_file':str(p)}
print(json.dumps(result))
'''
which=sys.argv[1]
program=api_program if which=='api' else gpu_program
command='kubectl -n video2md exec -i deployment/video2md-api -c api -- python -' if which=='api' else 'kubectl -n gpu-router exec -i deployment/gpu-router -- python -'
r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','lcode-4c',command],input=program,text=True,capture_output=True,check=True)
result=json.loads(r.stdout.strip().splitlines()[-1])
Path(sys.argv[2]).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False))
