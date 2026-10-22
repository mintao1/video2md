"""Read only: fetch latest failed worker status and dataset file listing."""
import json
import re
import subprocess
from pathlib import Path

PROGRAM='''
import json
from gpu_router.providers.kaggle import KaggleProvider
p=KaggleProvider()
user=p._creds_user()
slug=user+'/gpu-router-job'
arts=p.task_fetch(slug,['status.json'])
status=json.loads(arts['status.json'])
listing=p._kg(['datasets','files',p._dataset_slug()],timeout=60)
print(json.dumps({'worker_status':status,'dataset_files':listing.replace(user,'<account>')},ensure_ascii=False).replace(user,'<account>'))
'''
r=subprocess.run(['ssh','-o','BatchMode=yes','lcode-4c',
 'kubectl -n gpu-router exec -i deployment/gpu-router -c router -- python -'],
 input=PROGRAM,capture_output=True,text=True,timeout=100,check=True)
d=json.loads(r.stdout.strip().splitlines()[-1])
raw=d.pop('dataset_files')
d.update(dataset_filenames_first_page=re.findall(r'(?m)^([\w.-]+\.jpg)\s+\d+\s',raw),
         more_files_exist='Next Page Token' in raw,listing_is_complete=False,
         opaque_cursor_and_warnings_omitted=True)
Path(__file__).with_name('ocr-input.json').write_text(json.dumps(d,ensure_ascii=False,indent=2))
print(json.dumps(d,ensure_ascii=False))
