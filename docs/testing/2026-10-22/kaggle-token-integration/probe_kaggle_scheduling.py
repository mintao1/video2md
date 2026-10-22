"""One real private Kaggle diagnostic; no LLM and no credentials uploaded."""
import json
import subprocess
import sys
from pathlib import Path

PROGRAM = r'''
import json,time,subprocess,uuid
from pathlib import Path
from gpu_router.providers.kaggle import KaggleProvider
p=KaggleProvider()
if EXPLICIT_GPU:
 original=p._kg
 def requested(args,timeout=300):
  if args[:2]==['kernels','push'] and '--accelerator' not in args:
   args=args+['--accelerator','NvidiaTeslaT4']
  text=original(args,timeout=timeout)
  if 'kernel push error' in text.lower():
   raise RuntimeError('Kaggle rejected kernel push: '+text[-300:])
  return text
 p._kg=requested
user=p._creds_user()
p.kernel=user+'/gpu-router-diag-accelerator'
control_id=uuid.uuid4().hex
script="""
import json,subprocess,shutil
from pathlib import Path
out={'control_id':'CONTROL_ID','input_files':[str(p) for p in Path('/kaggle/input').rglob('*') if p.is_file()],
     'nvidia_binary':shutil.which('nvidia-smi'),'nvidia_devices':[str(p) for p in Path('/dev').glob('nvidia*')]}
try:
 import torch
 out.update(cuda_available=torch.cuda.is_available(),cuda_devices=torch.cuda.device_count(),
            cuda_device_names=[torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())])
except Exception as e:out['cuda_probe_error']=type(e).__name__
try:
 r=subprocess.run(['nvidia-smi','--query-gpu=name','--format=csv,noheader'],capture_output=True,text=True,timeout=10)
 out.update(gpu_names=r.stdout.strip().splitlines(),nvidia_exit_code=r.returncode)
except Exception as e:out['gpu_probe_error']=type(e).__name__
Path('/kaggle/working/layout.json').write_text(json.dumps(out))
"""
script=script.replace('CONTROL_ID',control_id)
t0=time.monotonic()
task=p.task_submit(p._dataset_slug(),{'script':script,'gpu':True,'internet':False})
print(json.dumps({'event':'submitted','kind':'private_layout_control'}),flush=True)
for n in range(90):
 state,_=p.task_status(task)
 if state in ('done','error'):
  arts=p.task_fetch(task,['layout.json'])
  if 'layout.json' in arts:
   d=json.loads(arts['layout.json'])
   if d.get('control_id')==control_id:
    result={'kernel_status':state,'elapsed_s':round(time.monotonic()-t0,3),'llm_calls':0,'fresh_control_id_matches':True}
    d['input_files']=[s.replace(user,'<account>') for s in d['input_files']];result.update(d)
    print('RESULT '+json.dumps(result),flush=True)
    break
 time.sleep(10)
else:print('RESULT '+json.dumps({'kernel_status':'observation_timeout','llm_calls':0}),flush=True)
'''
explicit='--explicit-gpu' in sys.argv
PROGRAM=PROGRAM.replace('EXPLICIT_GPU',repr(explicit))
r=subprocess.Popen(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','lcode-4c',
 'kubectl -n gpu-router exec -i deployment/gpu-router -- python -'],
 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
r.stdin.write(PROGRAM)
r.stdin.close()
for line in r.stdout:
    print(line.rstrip(),flush=True)
    if line.startswith('RESULT '):
        name='kaggle-scheduling-control.json' if '--after-verification' in sys.argv else ('kaggle-layout-explicit-gpu.json' if explicit else 'kaggle-layout.json')
        Path(__file__).with_name(name).write_text(json.dumps(json.loads(line[7:]),indent=2)+'\n')
r.wait()
if r.returncode:
    # Never forward arbitrary CLI diagnostics that might include credentials.
    error=r.stderr.read()
    result={'probe_process_failed':True,'exit_code':r.returncode,
            'push_rejected':'Kaggle rejected kernel push' in error,
            'phone_verification':'phone' in error.lower(),
            'quota':'quota' in error.lower(),'permission':'403' in error or 'permission' in error.lower()}
    Path(__file__).with_name('kaggle-scheduling-control-error.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
raise SystemExit(r.returncode)
