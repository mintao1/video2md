"""Repair duplicate last-applied metadata, then apply an image-only runtime diff."""
import collections
import copy
import json
import subprocess
from pathlib import Path

HERE = Path(__file__).parent
PRIVATE = Path('/Users/linmintao/.config/video2md-settlement')


def remote(command, data=None):
    r = subprocess.run(['ssh','-o','BatchMode=yes','lcode-4c',command], input=data,
                       capture_output=True,text=True,timeout=90)
    if r.returncode:
        raise RuntimeError('Deployment command rejected; no override used')
    return r.stdout


before=json.loads(remote('kubectl -n video2md get deployment video2md-api -o json'))
assert before['spec']['template']['spec']['containers'][0]['image']=='video2md:0.2.0-failure-cost-20261021b'
old=json.loads(before['metadata']['annotations']['kubectl.kubernetes.io/last-applied-configuration'])
counts=collections.Counter(e['name'] for e in old['spec']['template']['spec']['containers'][0]['env'])
assert counts['V2MD_ENV']==2
baseline=copy.deepcopy(before)
baseline.pop('status',None)
for key in ('managedFields','resourceVersion','uid','creationTimestamp','generation'):
    baseline['metadata'].pop(key,None)
baseline['metadata'].get('annotations',{}).pop('kubectl.kubernetes.io/last-applied-configuration',None)
# This updates only apply bookkeeping. It is backed up and does not edit live env.
remote('kubectl apply set-last-applied -f -',json.dumps(baseline))
current=json.loads(remote('kubectl -n video2md get deployment video2md-api -o json'))
assert current['spec']==before['spec']
desired=copy.deepcopy(baseline)
desired['metadata']['resourceVersion']=current['metadata']['resourceVersion']
desired['spec']['template']['spec']['containers'][0]['image']='video2md:0.2.0-resume-billing-20261022c'
actual=json.loads(remote('kubectl apply --dry-run=server -f - -o json',json.dumps(desired)))
assert actual['spec']==desired['spec']
p=PRIVATE/'api-deployment-c-verified.json';p.write_text(json.dumps(desired));p.chmod(0o600)
(HERE/'api-dry-run.json').write_text(json.dumps({'old_last_applied_duplicate_env_names':{'V2MD_ENV':2},
 'bookkeeping_repaired_to_live_snapshot':True,'live_spec_unchanged_by_metadata_repair':True,
 'only_image_changes':True,'all_environment_preserved':True,'force_conflicts':False},indent=2))
result=remote('kubectl apply -f - && kubectl -n video2md rollout status deployment/video2md-api --timeout=60s',json.dumps(desired))
(HERE/'api-rollout.txt').write_text(result)
print(result)
