"""Real public-certificate GETs through deployed verification transport; no token."""
import json
import subprocess
from pathlib import Path

PROGRAM='''
import hashlib,json,time
from pathlib import Path
from acctkit import providers
from video2md import api
request=providers._GoogleHttpxRequest(api._google_oauth_proxy() or '')
rows=[]
for i in range(2):
 t=time.monotonic();r=request('https://www.googleapis.com/oauth2/v1/certs')
 rows.append({'status':r.status,'elapsed_s':round(time.monotonic()-t,4),
  'cache_control':r.headers.get('cache-control'),'age':r.headers.get('age'),
  'cert_count':len(json.loads(r.data))})
print(json.dumps({'source_sha256':hashlib.sha256(Path(providers.__file__).read_bytes()).hexdigest(),
 'measurements':rows,'cached_entries':len(request._cache),
 'same_response_object':request._cache and next(iter(request._cache.values()))[1] is r}))
'''
r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ControlPath=/Users/linmintao/.config/video2md-settlement/ssh-4c','lcode-4c','kubectl -n video2md exec -i deployment/video2md-api -c api -- python -'],input=PROGRAM,text=True,capture_output=True,timeout=90)
if r.returncode: raise RuntimeError('Public-certificate probe failed; output suppressed')
d=json.loads(r.stdout.strip().splitlines()[-1]);Path(__file__).with_name('cert-cache-runtime.json').write_text(json.dumps(d,indent=2)+'\n');print(json.dumps(d))
