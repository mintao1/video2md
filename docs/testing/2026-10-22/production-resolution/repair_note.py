"""Audited one-run repair using existing media; no inference or task submission."""
import json
import subprocess
from pathlib import Path

PROGRAM=r'''
import copy,hashlib,json,os,re,tempfile
from datetime import datetime,timezone
from pathlib import Path
from psycopg.types.json import Json
from video2md import runs,note
from video2md.clips import insert_clip_embeds

RID='dd586072'
EXPECTED='de62e0a8815194fdc5a23863d460ccd032045384e09629b2823470a07a2a904b'
sha=lambda text:hashlib.sha256(text.encode()).hexdigest()
ledger_before=runs._load_runs()
owner=ledger_before['3c05faf8']['owner']
be=runs._runs_backend()
assert be is not None

def repair():
 with be.pool.connection() as conn,conn.transaction():
  row=conn.execute('SELECT record FROM v2md_runs WHERE run_id=%s FOR UPDATE',(RID,)).fetchone()
  current=row[0]
  assert current['owner']==owner and current['status']=='done' and not current.get('deleted')
  updated=copy.deepcopy(current)
  key=current['entries'][1]['url']
  art=updated['artifacts'][key]
  before=art['md']
  prior=next((a for a in current.get('note_repairs',[]) if a.get('before_sha256')==EXPECTED),None)
  if prior:
   assert sha(before)==prior['after_sha256']
   after=before;changed=False
  else:
   assert sha(before)==EXPECTED
   assert not re.search(re.escape('![[attachments/')+r'[^]]+[.]mp4'+re.escape(']]'),before)
   valid={name for name,_ in art['published']}
   after=insert_clip_embeds(note.sanitize_embeds(before,valid),art['clips'])
   image_names=re.findall(re.escape('![[attachments/')+r'([^]]+[.]jpg)'+re.escape(']]'),after)
   clip_names=re.findall(re.escape('![[attachments/')+r'([^]]+[.]mp4)'+re.escape(']]'),after)
   assert len(image_names)==5 and len(clip_names)==5 and set(image_names)<=valid
   assert all(Path(name).name==name for name in image_names+clip_names)
   from video2md import artsink
   assert all((artsink.attach_dir()/name).is_file() and (artsink.attach_dir()/name).stat().st_size for name in image_names+clip_names)
   path=Path(art['note']);assert path.read_text()==before
   backup_dir=Path('/app/data/note-repair-backups');backup_dir.mkdir(mode=0o750,exist_ok=True);backup_dir.chmod(0o750)
   backup=backup_dir/(RID+'-'+EXPECTED+'.json')
   if backup.exists():assert json.loads(backup.read_text())==current
   else:
    fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:json.dump(current,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
   art['md']=after
   audit={'kind':'canonical_frame_reference_repair','entry':2,'at':datetime.now(timezone.utc).isoformat(),
    'before_sha256':EXPECTED,'after_sha256':sha(after),'image_count':5,'clip_count':5,
    'model_calls':0,'backup_file_mode':'0600','backup_directory_mode':'0750'}
   updated.setdefault('note_repairs',[]).append(audit)
   assert updated['usage']==current['usage'] and updated['cost_usd']==current['cost_usd']
   conn.execute('UPDATE v2md_runs SET record=%s::jsonb,updated=now() WHERE run_id=%s',(Json(updated),RID))
   changed=True
 # The DB is authoritative for the reader. A retry also converges the vault file
 # if a process stops between the DB commit and this atomic filesystem replace.
 path=Path(art['note'])
 if path.read_text()!=after:
  assert sha(path.read_text())==EXPECTED
  fd,tmp=tempfile.mkstemp(prefix='.note-repair-',dir=path.parent)
  with os.fdopen(fd,'w') as f:f.write(after);f.flush();os.fsync(f.fileno())
  os.chmod(tmp,path.stat().st_mode&0o777);os.replace(tmp,path)
 assert sha(path.read_text())==sha(after)
 return {'changed':changed,'entry':2,'before_sha256':EXPECTED,'after_sha256':sha(after),'model_calls':0,
  'file_synced':True,'images':len(re.findall(re.escape('![[attachments/')+r'[^]]+[.]jpg'+re.escape(']]'),after)),
  'clips':len(re.findall(re.escape('![[attachments/')+r'[^]]+[.]mp4'+re.escape(']]'),after))}
first=repair();second=repair();ledger_after=runs._load_runs()
others=[rid for rid in ledger_before if rid!=RID and ledger_before[rid]!=ledger_after[rid]]
before=copy.deepcopy(ledger_before[RID]);after=copy.deepcopy(ledger_after[RID])
before['artifacts'][before['entries'][1]['url']].pop('md');after['artifacts'][after['entries'][1]['url']].pop('md')
after.pop('note_repairs')
assert before==after and not others
print(json.dumps({'first':first,'second':second,'unrelated_runs_changed':others,'other_fields_unchanged':True,
 'usage_unchanged':ledger_before[RID]['usage']==ledger_after[RID]['usage'],
 'cost_unchanged':ledger_before[RID]['cost_usd']==ledger_after[RID]['cost_usd']}))
'''
r=subprocess.run(['ssh','-o','BatchMode=yes','lcode-4c','kubectl -n video2md exec -i deployment/video2md-api -c api -- python -'],input=PROGRAM,text=True,capture_output=True,timeout=90)
if r.returncode:
 import re
 err={'exit_code':r.returncode,'stdin_line_numbers':re.findall(r'File \"<stdin>\", line (\d+)',r.stderr),'exception_type':r.stderr.strip().splitlines()[-1].split(':',1)[0] if r.stderr.strip() else 'transport'}
 Path(__file__).with_name('note-repair-rejection.json').write_text(json.dumps(err,indent=2)+'\n')
 print(json.dumps(err));raise SystemExit(1)
d=json.loads(r.stdout.strip().splitlines()[-1]);Path(__file__).with_name('note-repair-applied.json').write_text(json.dumps(d,indent=2)+'\n');print(json.dumps(d))
