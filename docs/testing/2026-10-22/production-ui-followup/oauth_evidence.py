"""Collect only endpoint/status/timing fields, never OAuth query strings/tokens."""
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

OUT=Path(__file__).parent
PROGRAM='''
import json
from video2md.config import LOG_DIR
rows=[]
for line in (LOG_DIR/'api-requests.jsonl').read_text().splitlines():
 try: d=json.loads(line)
 except ValueError: continue
 if d.get('ts','')>='2026-10-22T00:20:00' and d.get('path') in ('/auth/google/login','/auth/google/callback','/auth/google/token','/auth/google/verify','/runs'):
  rows.append({k:d.get(k) for k in ('ts','method','path','code','run_id','dedup','error_type') if k in d})
print(json.dumps(rows))
'''
def ssh(host,command,program=None):
    return subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',host,command],
                          input=program,text=True,capture_output=True,check=True,timeout=45).stdout

audit=json.loads(ssh('lcode-4c','kubectl -n video2md exec -i deployment/video2md-api -c api -- python -',PROGRAM).strip().splitlines()[-1])
raw=ssh('lcode-4c','kubectl -n video2md logs deployment/video2md-api -c api --since=25m --timestamps=true')
http=[]
for line in raw.splitlines():
    match=re.search(r'^(\S+).*"(GET|POST) ([^ ?]+)(?:\?[^ ]*)? HTTP/[^\"]+" (\d+)',line)
    if match and match[3] in ('/me','/auth/google/login','/auth/google/callback'):
        http.append({'ts':match[1],'method':match[2],'path':match[3],'status':int(match[4])})
relay={}
for unit in ('video2md-oauth-relay.service','oauth-relay.service'):
    state={}
    for operation in ('is-active','is-enabled'):
        r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','lcode-cn',f'systemctl {operation} {unit}'],text=True,capture_output=True,timeout=30)
        state[operation]=r.stdout.strip()
    relay[unit]=state
relay['listener']=ssh('lcode-cn',"ss -ltn '( sport = :18080 )'").strip()
data={'audit':audit,'http':http,'relay':relay,'note':'UI artifacts separately prove actual account selection and library hydration; no credentials included.'}
(OUT/'oauth-evidence.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(data,ensure_ascii=False,indent=2))
