"""CPU-only read of the diagnostic dataset's actual mount; no input mutation."""
import json
import subprocess
import uuid
from pathlib import Path

CONTROL=uuid.uuid4().hex
SCRIPT='''
import json
from pathlib import Path
parts=__DATASET__.split('/')
root=Path('/kaggle/input')
paths=[root/parts[1],root/'datasets'/parts[0]/parts[1],root/parts[0]/parts[1]]
base=next(p for p in paths if p.is_dir())
json.dump({'control_id':__CONTROL__,'files':[str(p.relative_to(base)) for p in base.rglob('*') if p.is_file()]},open('/kaggle/working/layout.json','w'))
'''
PROGRAM=f'''
import json,time
from gpu_router.providers.kaggle import KaggleProvider
p=KaggleProvider();u=p._creds_user()
p.kernel=u+'/gpu-router-diag-network'
p.dataset=u+'/gpu-router-ocr-verify-20261022'
assert p.task_status(p.kernel)[0] in ('done','error')
script={SCRIPT!r}.replace('__DATASET__',json.dumps(p.dataset)).replace('__CONTROL__',json.dumps({CONTROL!r}))
tid=p.task_submit(p.dataset,{{'script':script,'gpu':False,'internet':False}})
t0=time.monotonic()
while time.monotonic()-t0<300:
 time.sleep(10)
 state,detail=p.task_status(tid)
 if state=='done':
  arts=p.task_fetch(tid,['layout.json'])
  d=json.loads(arts['layout.json']);assert d['control_id']=={CONTROL!r}
  d.update(kernel_status='done',gpu_requested=False,elapsed_s=round(time.monotonic()-t0,3))
  print(json.dumps(d),flush=True);break
 if state=='error':raise RuntimeError('CPU layout kernel failed')
else:raise RuntimeError('CPU layout observation timeout')
'''
r=subprocess.run(['ssh','-o','BatchMode=yes','lcode-4c','kubectl -n gpu-router exec -i deployment/gpu-router -c router -- python -'],input=PROGRAM,capture_output=True,text=True,timeout=600,check=True)
d=json.loads(r.stdout.strip().splitlines()[-1]);Path(__file__).with_name('input-layout.json').write_text(json.dumps(d,indent=2));print(json.dumps(d))
