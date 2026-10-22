"""Read-only candidate book/media/attempt proof; no body text or credentials."""
import json
import subprocess
from pathlib import Path

PROGRAM='''
import json,re,hashlib,subprocess
from datetime import datetime,timezone
from pathlib import Path
from video2md import api
r=api._load_runs()['8c5d87f3']
ident=api.Identity(uid=r['owner'],provider='google',external_id='read-only-evidence')
notes=api.get_run_notes(r['run_id'],ident)['notes']
media=[]
for name in dict.fromkeys(n for note in notes for n in re.findall(r'!\\[\\[attachments/([^\\]|]+)(?:\\|[^\\]]*)?\\]\\]',note['md'])):
 response=api.get_run_media(r['run_id'],name,ident)
 p=Path(response.path)
 row={'name':name,'exists':p.is_file(),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
 if name.endswith('.mp4'):
  q=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_name','-of','json',str(p)],capture_output=True,text=True,timeout=15)
  row['ffprobe_exit_code']=q.returncode
  if q.returncode==0:
   info=json.loads(q.stdout);row['duration_s']=float(info['format']['duration']);row['codecs']=[s['codec_name'] for s in info.get('streams',[])]
 media.append(row)
spans=[{k:s[k] for k in ('event','status','durationMs','attempts','attempt_usage','usage_complete','usage') if k in s}
 for s in r.get('span_log',[]) if 'llm' in str(s.get('event','')).lower()]
out={'observed_at':datetime.now(timezone.utc).isoformat(),'run_id':r['run_id'],'status':r['status'],
 'note_count':len(notes),'notes':[{'chars':len(n['md']),'markdown_bytes':len(n['md'].encode()),'sha256':hashlib.sha256(n['md'].encode()).hexdigest(),
 'wiki_images':len(re.findall(r'!\\[\\[attachments/[^\\]]+\\.(?:jpg|png)(?:\\|[^\\]]*)?\\]\\]',n['md'])),
 'wiki_videos':len(re.findall(r'!\\[\\[attachments/[^\\]]+\\.mp4(?:\\|[^\\]]*)?\\]\\]',n['md'])),
 'markdown_images':len(re.findall(r'!\\[[^\\]]*\\]\\([^)]*\\)',n['md'])),
 'attachment_mentions':n['md'].count('attachments/')} for n in notes],
 'media':media,'llm_spans':spans,'usage_settlements':r.get('usage_settlements'),
 'partial_failures':r.get('partial_failures'), 'usage':r.get('usage'), 'cost_usd':r.get('cost_usd'),
 'source_sha256':{f:hashlib.sha256(Path('/app/video2md/'+f).read_bytes()).hexdigest() for f in ('api.py','graph.py','llm.py','runs.py','transcribe.py','note.py')}}
print(json.dumps(out))
'''
r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','lcode-4c',
 'kubectl -n video2md exec -i deployment/video2md-api -c api -- python -'],input=PROGRAM,text=True,capture_output=True,timeout=120)
if r.returncode:
 raise RuntimeError('Read-only book probe failed; output suppressed')
d=json.loads(r.stdout.strip().splitlines()[-1])
Path(__file__).with_name('book-delivery.json').write_text(json.dumps(d,indent=2)+'\n')
print(json.dumps({'run_id':d['run_id'],'status':d['status'],'notes':d['note_count'],
 'media_count':len(d['media']),'media_bytes':sum(m['bytes'] for m in d['media']),
 'video_count':sum(m['name'].endswith('.mp4') for m in d['media'])}))
