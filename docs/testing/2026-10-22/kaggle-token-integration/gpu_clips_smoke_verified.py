"""Real GPU route diagnostic; controlled public fixture, two 2s clips, no LLM."""
import json
import subprocess
from pathlib import Path
program='''
import hashlib,io,json,tarfile,time
import httpx
from video2md.util import pool_headers
out={'kind':'production_gpu_route_smoke','llm_calls':0,'fixture_url':'https://ferosse.tech/test-assets/model-billing-smoke.mp4'}
t0=time.monotonic()
with httpx.Client(trust_env=False,timeout=30) as c:
 r=c.get('http://127.0.0.1:8790/test-assets/model-billing-smoke.mp4')
 r.raise_for_status()
 out['source_bytes']=len(r.content)
 spec=[{'out':'diag-c01-0000.mp4','s0':0,'s1':2},{'out':'diag-c02-0002.mp4','s0':2,'s1':4}]
 c.headers.update(pool_headers())
 submitted=c.post('http://127.0.0.1:9700/clips/async',data={'spec':json.dumps({'clips':spec})},files={'video':('diagnostic.mp4',r.content,'video/mp4')})
 submitted.raise_for_status()
 accepted=submitted.json()
 jid=accepted.get('job_id') or accepted.get('id')
 assert jid, 'missing job id'
 out['job_id']=jid
 print(json.dumps({'event':'submitted','job_id':jid}),flush=True)
 previous=''
 for n in range(120):
  snap=c.get('http://127.0.0.1:9700/jobs/'+jid).json()
  status=snap.get('status') or (snap.get('job') or {}).get('status')
  if status != previous:
   print(json.dumps({'event':'state','status':status,'elapsed_s':round(time.monotonic()-t0,3)}),flush=True)
   previous=status
  if status in ('done','failed','error','cancelled'):
   out['status']=status
   if status=='done':
    response=c.get('http://127.0.0.1:9700/jobs/'+jid+'/result')
    response.raise_for_status()
    out['archive_bytes']=len(response.content)
    out['archive_sha256']=hashlib.sha256(response.content).hexdigest()
    with tarfile.open(fileobj=io.BytesIO(response.content)) as tf:
     out['clips']=[{'name':m.name,'bytes':m.size,'mp4_header':tf.extractfile(m).read(16)[4:8].decode('ascii','replace')} for m in tf.getmembers() if m.name.endswith('.mp4')]
    out['passed']=len(out['clips'])==2 and all(m['bytes']>0 and m['mp4_header']=='ftyp' for m in out['clips'])
   else:
    out['passed']=False
    out['error_category']='quota' if any(word in str(snap).lower() for word in ('quota','412','toomanyassignments')) else 'provider_failure'
   break
  time.sleep(10)
 else:
  out.update(status='observation_timeout',passed=False)
out['elapsed_s']=round(time.monotonic()-t0,3)
print('RESULT '+json.dumps(out),flush=True)
'''
p=subprocess.Popen(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','lcode-4c','kubectl -n video2md exec -i deployment/video2md-api -c api -- python -'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
p.stdin.write(program)
p.stdin.close()
rows=[]
for line in p.stdout:
 print(line.rstrip(),flush=True)
 rows.append(line)
 if line.startswith('RESULT '):
  Path(__file__).with_name('gpu-clips-verified.json').write_text(json.dumps(json.loads(line[7:]),indent=2)+'\n')
p.wait()
Path(__file__).with_name('gpu-clips-verified.txt').write_text(''.join(rows)+p.stderr.read())
raise SystemExit(p.returncode)
