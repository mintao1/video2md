"""Fresh physical CUDA proof in a separate dataset/kernel, without business input overlap."""
import json
import subprocess
import time
import uuid
from pathlib import Path

CONTROL_ID=uuid.uuid4().hex
BODY='''
import torch
job=json.load(open(f"{IN}/probe.json"))
r=subprocess.run(["nvidia-smi","--query-gpu=name","--format=csv,noheader"],capture_output=True,text=True)
json.dump({"control_id":job["control_id"],"cuda_available":torch.cuda.is_available(),
 "cuda_devices":torch.cuda.device_count(),"cuda_device_names":[torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())],
 "nvidia_exit_code":r.returncode},open(f"{OUT}/out.json","w"))
_status(state="done")
'''
PROGRAM=f'''
import json,time
from gpu_router.providers.kaggle import KaggleProvider
p=KaggleProvider()
u=p._creds_user()
p.kernel=u+'/gpu-router-diag-verify-20261022'
p.dataset=u+'/gpu-router-diag-verify-20261022'
spec=p._script({BODY!r})
spec['internet']=False
t0=time.monotonic()
task,arts=p.run_task({{'probe.json':json.dumps({{'control_id':{CONTROL_ID!r}}}).encode()}},spec,['out.json','status.json'],max_wait_s=600)
d=json.loads(arts['out.json'])
d.update(kernel_status='done',requested_control_id={CONTROL_ID!r},fresh_control_id_matches=d.get('control_id')=={CONTROL_ID!r},elapsed_s=round(time.monotonic()-t0,3),isolated_dataset_and_kernel=True)
print(json.dumps(d),flush=True)
'''
t0=time.monotonic()
r=subprocess.run(['ssh','-o','BatchMode=yes','lcode-4c',
 'kubectl -n gpu-router exec -i deployment/gpu-router -c router -- python -'],
 input=PROGRAM,text=True,capture_output=True,timeout=1000)
if r.returncode:
    print(json.dumps({'probe_failed':True,'exception_details_suppressed':True,'elapsed_s':round(time.monotonic()-t0,3)}))
    raise SystemExit(1)
d=json.loads(r.stdout.strip().splitlines()[-1])
Path(__file__).with_name('cuda-d.json').write_text(json.dumps(d,indent=2)+'\n')
print(json.dumps(d))
