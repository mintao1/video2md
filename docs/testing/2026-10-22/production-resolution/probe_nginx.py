"""Bounded read of nginx log; only whitelisted paths/status/timestamps leave host."""
import json
import subprocess
from pathlib import Path

PROGRAM=r'''
import json,re,os
from datetime import datetime
p='/var/log/nginx/access.log'
with open(p,'rb') as f:
 size=f.seek(0,2);f.seek(max(0,size-2*1024*1024));raw=f.read(2*1024*1024).decode('utf8','replace')
rows=[]
for line in raw.splitlines():
 m=re.search(r'\[([^\]]+)\].*"(GET|POST) ([^ ?]+)(?:\?[^ ]*)? HTTP/[^\"]+" (\d+) (\d+)',line)
 if m and (m[3] in ('/me','/auth/google/login','/auth/google/callback','/auth/google/verify','/auth/google/token','/read/dd586072','/runs/dd586072/note') or m[3].startswith('/runs/dd586072/media/')):
  try: at=datetime.strptime(m[1],'%d/%b/%Y:%H:%M:%S %z')
  except ValueError: continue
  if at.isoformat()>='2026-10-22T03:40:00':rows.append({'ts':at.isoformat(),'method':m[2],'path':m[3],'status':int(m[4]),'body_bytes_sent':int(m[5])})
print(json.dumps({'http':rows,'bounded_log_bytes':len(raw.encode())}))
'''
r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','ControlMaster=auto','-o','ControlPersist=180','-o','ControlPath=/Users/linmintao/.config/video2md-settlement/ssh-4c','lcode-4c','sudo -n python3 -'],input=PROGRAM,text=True,capture_output=True,timeout=45)
if r.returncode:
 print(json.dumps({'transport_exit_code':r.returncode,'stderr_last_line':r.stderr.splitlines()[-1:]}));raise SystemExit(1)
d=json.loads(r.stdout.strip().splitlines()[-1]);Path(__file__).with_name('http-current.json').write_text(json.dumps(d,indent=2)+'\n')
print(json.dumps({'rows':len(d['http']),'me':[r for r in d['http'] if r['path']=='/me'],
 'media_statuses':sorted(set(r['status'] for r in d['http'] if '/media/' in r['path']))}))
