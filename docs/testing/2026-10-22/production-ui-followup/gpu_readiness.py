"""Inspect existing GPU availability and credential presence without using secrets."""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROGRAM='''
import json,os
from datetime import datetime,timezone
from pathlib import Path
out={'observed_at':datetime.now(timezone.utc).isoformat(),'home':str(Path.home()),'uid':os.getuid(),
 'kaggle_env_present':{k:bool(os.getenv(k)) for k in ('KAGGLE_USERNAME','KAGGLE_KEY','KAGGLE_API_TOKEN')}}
p=Path.home()/'.kaggle/kaggle.json'
out['credential_file']={'path':str(p),'exists':p.is_file()}
if p.is_file():
 try:
  d=json.loads(p.read_text())
  out['credential_file'].update(valid_json=True,username_present=bool(d.get('username')),key_present=bool(d.get('key')))
 except Exception as exc: out['credential_file']['read_error_type']=type(exc).__name__
print(json.dumps(out))
'''
r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','lcode-4c',
 'kubectl -n gpu-router exec -i deployment/gpu-router -- python -'],
 input=PROGRAM,text=True,capture_output=True,check=True,timeout=45)
result=json.loads(r.stdout.strip().splitlines()[-1])
name=sys.argv[1] if len(sys.argv)>1 else 'gpu-presence-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json'
Path(__file__).with_name(name).write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
