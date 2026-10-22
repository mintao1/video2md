"""Prepare a fresh manifest locally; require an image-only server dry-run diff."""
import copy
import json
import re
import subprocess
import sys
from pathlib import Path

namespace, deployment, container, old_image, new_image = sys.argv[1:]
HERE = Path(__file__).parent
PRIVATE = Path('/Users/linmintao/.config/video2md-settlement')


def remote(cmd, data=None):
    r=subprocess.run(['ssh','-o','BatchMode=yes','lcode-4c',cmd],input=data,
                     capture_output=True,text=True,timeout=90)
    if r.returncode:
        (HERE/(deployment+'-rollout-error.txt')).write_text(r.stderr)
        raise RuntimeError('Deployment rejected; details saved; no override used')
    return r.stdout


assert namespace in ('video2md','gpu-router')
assert all(re.fullmatch(r'[a-z0-9-]+', v) for v in (deployment,container))
assert all(re.fullmatch(r'[A-Za-z0-9./_-]+:[A-Za-z0-9._-]+',v) for v in (old_image,new_image))
before=json.loads(remote(f'kubectl -n {namespace} get deployment {deployment} -o json'))
assert next(c for c in before['spec']['template']['spec']['containers'] if c['name']==container)['image']==old_image
p=PRIVATE/(deployment+'-before-'+new_image.split(':')[-1]+'.json');p.write_text(json.dumps(before));p.chmod(0o600)
desired=copy.deepcopy(before);desired.pop('status',None);desired['metadata'].pop('managedFields',None)
desired['metadata'].get('annotations',{}).pop('kubectl.kubernetes.io/last-applied-configuration',None)
for c in desired['spec']['template']['spec']['containers']:
    if c['name']==container:c['image']=new_image
actual=json.loads(remote('kubectl apply --dry-run=server -f - -o json',json.dumps(desired)))
assert desired['spec']==actual['spec']
(HERE/(deployment+'-dry-run.json')).write_text(json.dumps({'old_image':old_image,'new_image':new_image,
 'only_image_changed':True,'all_environment_and_volumes_preserved':True,'force_conflicts':False},indent=2))
result=remote(f'kubectl apply -f - && kubectl -n {namespace} rollout status deployment/{deployment} --timeout=60s',json.dumps(desired))
(HERE/(deployment+'-rollout.txt')).write_text(result)
print(result)
