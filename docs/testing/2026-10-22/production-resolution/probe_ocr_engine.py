"""One real OCR image and physical CUDA proof, isolated from business dataset/kernel."""
import json
import subprocess
from pathlib import Path

EXPECTED=json.loads(Path(__file__).with_name('gpu-layout-manifest.json').read_text())['source_sha256']
FOOTER='''
import torch
nv=subprocess.run(["nvidia-smi","--query-gpu=name","--format=csv,noheader"],capture_output=True,text=True)
try:
    measured=json.load(open(f"{OUT}/out.json"))
    measured.update(cuda_available=torch.cuda.is_available(),cuda_devices=torch.cuda.device_count(),
       cuda_device_names=[torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())],nvidia_exit_code=nv.returncode)
    json.dump(measured,open(f"{OUT}/out.json","w"))
except FileNotFoundError:
    pass
'''
PROGRAM=f'''
import json,time,tempfile,hashlib
from pathlib import Path
import httpx
from gpu_router.providers.kaggle import KaggleProvider
from gpu_router.providers import kaggle
p=KaggleProvider()
u=p._creds_user()
p.kernel=u+'/gpu-router-diag-network'
p.dataset=u+'/gpu-router-ocr-verify-20261022'
assert p.task_status(p.kernel)[0] in ('done','error')
p.max_wait=600
source=hashlib.sha256(Path(kaggle.__file__).read_bytes()).hexdigest()
assert source=={EXPECTED!r}
base_script=p._script
base_run=p.run_task
capture={{}}
def instrument(body):
    spec=base_script(body)
    spec['script']+={FOOTER!r}
    return spec
def observed_run(files,spec,want,**kw):
    capture['requested_control_id']=json.loads(files['ocr_job.json'])['control_id']
    capture['upload_file_count']=len(files)
    tid,arts=base_run(files,spec,want,**kw)
    capture['arts']=arts
    return tid,arts
p._script=instrument
p.run_task=observed_run
t0=time.monotonic()
result={{'passed':False,'provider_source_sha256':source,'isolated_dataset_and_kernel':True}}
try:
    with tempfile.TemporaryDirectory(prefix='ocr-verify-') as directory:
        tmp=Path(directory)
        response=httpx.get('https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/master/resources/test_files/ch_en_num.jpg',follow_redirects=True,timeout=45)
        response.raise_for_status()
        assert response.content[:2] == bytes([255,216])
        image=tmp/'ocr-check.jpg'
        image.write_bytes(response.content)
        rows=p.ocr([image],tmp)
        measured=json.loads(capture['arts']['out.json'])
        st=json.loads(capture['arts']['status.json'])
        for key in ('control_id','cuda_available','cuda_devices','cuda_device_names','nvidia_exit_code'):
            result[key]=measured[key]
        result.update(kernel_status=st['state'],requested_control_id=capture['requested_control_id'],
            fresh_control_id_matches=result['control_id']==capture['requested_control_id'],
            rapidocr_version=st.get('rapidocr_version'),onnxruntime_version=st.get('onnxruntime_version'),
            result_count=len(rows),nonempty_chars=sum(len(r.get('text','')) for r in rows),
            error_count=sum(bool(r.get('err')) for r in rows),upload_file_count=capture['upload_file_count'])
        result['passed']=len(rows)==1 and result['nonempty_chars']>0 and result['error_count']==0
except Exception as exc:
    result['error_type']=type(exc).__name__
    result['error_summary']=str(exc).replace(u,'<account>')[:500]
    if capture.get('arts',{{}}).get('status.json'):
        result['worker_status']=json.loads(capture['arts']['status.json'])
if capture.get('requested_control_id'):
    result.setdefault('requested_control_id',capture['requested_control_id'])
result['elapsed_s']=round(time.monotonic()-t0,3)
print(json.dumps(result,ensure_ascii=False).replace(u,'<account>'),flush=True)
'''
r=subprocess.run(['ssh','-o','BatchMode=yes','lcode-4c',
 'kubectl -n gpu-router exec -i deployment/gpu-router -c router -- python -'],
 input=PROGRAM,text=True,capture_output=True,timeout=1800)
if not r.stdout.strip():
    print('Probe transport failed; sensitive output suppressed')
    raise SystemExit(2)
d=json.loads(r.stdout.strip().splitlines()[-1])
Path(__file__).with_name('ocr-engine-probe.json').write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(d,ensure_ascii=False))
raise SystemExit(0 if d.get('passed') else 1)
