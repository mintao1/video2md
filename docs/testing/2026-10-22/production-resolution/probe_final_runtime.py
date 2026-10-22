"""Read-only current images/readiness/source and canonical OAuth relay state."""
import json
import subprocess
from pathlib import Path
from datetime import datetime,timezone

OUT=Path(__file__).parent

def ssh(host,cmd,program=None):
 r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',host,cmd],input=program,text=True,capture_output=True,timeout=60)
 if r.returncode:raise RuntimeError('Runtime evidence read failed; source output suppressed')
 return r.stdout

server='''
import json,subprocess
rows=[]
for ns,name in [('video2md','video2md-api'),('gpu-router','gpu-router')]:
 d=json.loads(subprocess.check_output(['kubectl','-n',ns,'get','deployment',name,'-o','json']))
 selector=','.join(k+'='+v for k,v in d['spec']['selector']['matchLabels'].items())
 pods=json.loads(subprocess.check_output(['kubectl','-n',ns,'get','pods','-l',selector,'-o','json']))['items']
 rows.append({'namespace':ns,'deployment':name,'images':{c['name']:c['image'] for c in d['spec']['template']['spec']['containers']},
  'ready_replicas':d.get('status',{}).get('readyReplicas',0),
  'pods':[{'name':p['metadata']['name'],'containers':[{k:c.get(k) for k in ('name','ready','restartCount','image','imageID')} for c in p.get('status',{}).get('containerStatuses',[])]} for p in pods]})
print(json.dumps(rows))
'''
d={'observed_at':datetime.now(timezone.utc).isoformat(),'deployments':json.loads(ssh('lcode-4c','python3 -',server))}
program='''
import hashlib,json
from pathlib import Path
from gpu_router.providers import kaggle
from gpu_router.providers.kaggle import KaggleProvider
p=KaggleProvider()
p.kernel=p._creds_user()+'/gpu-router-job'
print(json.dumps({'provider_source_sha256':hashlib.sha256(Path(kaggle.__file__).read_bytes()).hexdigest(),
 'business_kernel_status':p.task_status(p.kernel)[0]}))
'''
d['gpu_runtime']=json.loads(ssh('lcode-4c','kubectl -n gpu-router exec -i deployment/gpu-router -c router -- python -',program).strip().splitlines()[-1])
relay='''
import json,subprocess
r={}
for unit in ('video2md-oauth-relay.service','oauth-relay.service'):
 r[unit]={op:subprocess.run(['systemctl',op,unit],capture_output=True,text=True).stdout.strip() for op in ('is-active','is-enabled')}
r['listener']=subprocess.check_output(['ss','-H','-ltn','( sport = :18080 )'],text=True).strip()
print(json.dumps(r))
'''
d['relay']=json.loads(ssh('lcode-cn','python3 -',relay))
(OUT/'final-runtime.json').write_text(json.dumps(d,indent=2)+'\n')
(OUT/'gpu-runtime-g.json').write_text(json.dumps(d['gpu_runtime'],indent=2)+'\n')
print(json.dumps({'deployments':[(r['deployment'],r['images'],r['ready_replicas']) for r in d['deployments']],
 'gpu_runtime':d['gpu_runtime'],'relay':d['relay']}))
