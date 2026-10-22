"""Read-only auth HTTP proof. Suppress queries, headers, users and tokens."""
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

OUT=Path(__file__).parent

def ssh(command, program=None):
    r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','ControlMaster=auto','-o','ControlPersist=180','-o','ControlPath=/Users/linmintao/.config/video2md-settlement/ssh-4c','lcode-4c',command],
                     input=program,text=True,capture_output=True,timeout=60)
    if r.returncode:
        raise RuntimeError('Read-only evidence transport failed; output suppressed')
    return r.stdout

program='''
import json,hashlib
from video2md.config import LOG_DIR
from pathlib import Path
rows=[]
for line in (LOG_DIR/'api-requests.jsonl').read_text().splitlines():
 try: d=json.loads(line)
 except ValueError: continue
 if d.get('ts','')>='2026-10-22T03:40:00' and d.get('path') in ('/auth/google/login','/auth/google/callback','/auth/google/token','/auth/google/verify'):
  rows.append({k:d.get(k) for k in ('ts','method','path','code','error_type') if k in d})
print(json.dumps({'audit':rows,'api_sha256':hashlib.sha256(Path('/app/video2md/api.py').read_bytes()).hexdigest()}))
'''
d=json.loads(ssh('kubectl -n video2md exec -i deployment/video2md-api -c api -- python -',program).strip().splitlines()[-1])
(OUT/'auth-audit-current.json').write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
raw=ssh('sudo -n tail -n 2500 /var/log/nginx/access.log')
http=[]
for line in raw.splitlines():
    m=re.search(r'\[([^\]]+)\].*"(GET|POST) ([^ ?]+)(?:\?[^ ]*)? HTTP/[^\"]+" (\d+)',line)
    if m and (m[3] in ('/me','/auth/google/login','/auth/google/callback','/auth/google/verify','/auth/google/token','/read/dd586072','/runs/dd586072/note') or m[3].startswith('/runs/dd586072/media/')):
        at=datetime.strptime(m[1],'%d/%b/%Y:%H:%M:%S %z')
        if at.isoformat()>='2026-10-22T03:40:00':
            http.append({'ts':at.isoformat(),'method':m[2],'path':m[3],'status':int(m[4])})
d['http']=http
d['observed_at']=datetime.now(timezone.utc).isoformat()
(OUT/'auth-http-current.json').write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'audit_rows':len(d['audit']),'http_rows':len(http),'http':http},ensure_ascii=False))
