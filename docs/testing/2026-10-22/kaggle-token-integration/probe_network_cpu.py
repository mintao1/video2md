"""Independent scheduling control, CPU environment inspection only, no LLM."""
import json
import subprocess
from pathlib import Path

PROGRAM = r'''
import json,time,uuid
from gpu_router.providers.kaggle import KaggleProvider
p=KaggleProvider(); user=p._creds_user()
p.kernel=user+'/gpu-router-diag-network'
nonce=uuid.uuid4().hex
script="""
import json,shutil
from pathlib import Path
out={'control_id':'NONCE','ffmpeg':shutil.which('ffmpeg'),
     'files':[{ 'name':p.name,'bytes':p.stat().st_size} for p in Path('/kaggle/input').rglob('*') if p.is_file()]}
Path('/kaggle/working/network-control.json').write_text(json.dumps(out))
""".replace('NONCE',nonce)
t0=time.monotonic()
task=p.task_submit(p._dataset_slug(),{'script':script,'gpu':False,'internet':True})
print(json.dumps({'event':'submitted','kind':'cpu_network_control','llm_calls':0}),flush=True)
previous=''
for i in range(60):
 state,_=p.task_status(task)
 if state!=previous:
  print(json.dumps({'event':'state','status':state,'elapsed_s':round(time.monotonic()-t0,3)}),flush=True);previous=state
 if state in ('done','error'):
  arts=p.task_fetch(task,['network-control.json'])
  if 'network-control.json' in arts:
   d=json.loads(arts['network-control.json'])
   if d.get('control_id')==nonce:
    print('RESULT '+json.dumps(dict(d,status=state,elapsed_s=round(time.monotonic()-t0,3),llm_calls=0,fresh_control_id_matches=True)),flush=True)
    break
 time.sleep(10)
else:print('RESULT '+json.dumps({'status':'observation_timeout','llm_calls':0}),flush=True)
'''
p=subprocess.Popen(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','lcode-4c',
 'kubectl -n gpu-router exec -i deployment/gpu-router -c router -- python -'],
 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
p.stdin.write(PROGRAM);p.stdin.close()
for line in p.stdout:
    print(line.rstrip(),flush=True)
    if line.startswith('RESULT '):
        Path(__file__).with_name('network-cpu-control.json').write_text(json.dumps(json.loads(line[7:]),indent=2)+'\n')
p.wait()
if p.returncode:
    print(json.dumps({'probe_process_failed':True,'exit_code':p.returncode}))
raise SystemExit(p.returncode)
