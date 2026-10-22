"""Read-only, content-free production evidence for this authorized retest."""
import json
import subprocess
import sys
from pathlib import Path

program = '''
import json,hashlib
from pathlib import Path
from video2md import api
from video2md import llm
from video2md.config import ENV
from video2md.pg import shared_pool
from acctkit.models import Identity
runs=api._load_runs()
matches=[r for r in runs.values() if 'BV1vV411k7GB' in r.get('url','')]
r=sorted(matches,key=lambda r:r.get('created',''))[-1] if matches else None
owner=(r or runs['4df77d9c'])['owner']
cost=api.get_cost(Identity(uid=owner,provider='google',external_id=owner))
cost.pop('uid',None)
out={'run':None,'cost':cost,'source_sha256':{f:hashlib.sha256(Path('/app/video2md/'+f).read_bytes()).hexdigest() for f in ['bilibili.py','resolve.py','download.py']},'web_index_sha256':hashlib.sha256(Path('/app/web-dist/index.html').read_bytes()).hexdigest()}
out['default_model']=llm.LLM_MODEL
out['fake_llm_enabled']=str(ENV.get('V2MD_FAKE_LLM') or '').lower() in ('1','on','true')
with shared_pool().connection() as connection:
 row=connection.execute('SELECT count, ts FROM v2md_submit_log WHERE uid=%s',(owner,)).fetchone()
out['submit_audit']={'present':bool(row),'window_count':row[0] if row else None,'timestamp':str(row[1]) if row else None}
if r:
 safe={k:r.get(k) for k in ['run_id','status','created','stages','total','usage','cost_usd','cost_pricing','tmp_cleaned','finished']}
 safe['entries']=[{k:e.get(k) for k in ['id','url','playlist_index','playlist_title','title','bilibili']} for e in r.get('entries',[])]
 safe['artifacts']=[{'stage_metrics':a.get('stage_metrics',{}),'selection_mode':a.get('selection_mode'),'selected_count':len(a.get('selected') or []),'note_present':bool(a.get('note') and Path(a['note']).is_file()),'title':a.get('title'),'markdown_chars':len(a.get('md') or ''),'clips_done':a.get('clips_done'),'transcript_chars':len(a.get('transcript') or '')} for a in r.get('artifacts',{}).values()]
 safe['diagnostic']=api._public_run_diagnostic(r)
 safe['model_calls']=[{k:s.get(k) for k in ['name','status','model','usage','durationMs'] if k in s} for s in r.get('span_log',[]) if s.get('model')]
 out['run']=safe
print(json.dumps(out,ensure_ascii=False))
'''
r = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'lcode-4c',
                    'kubectl -n video2md exec -i deployment/video2md-api -c api -- python -'],
                   input=program, text=True, capture_output=True, check=True)
data = json.loads(r.stdout.strip().splitlines()[-1])
Path(sys.argv[1]).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
run = data['run'] or {}
print(json.dumps({k: run.get(k) for k in ['run_id','status','stages','usage','cost_usd','diagnostic']}, ensure_ascii=False))
