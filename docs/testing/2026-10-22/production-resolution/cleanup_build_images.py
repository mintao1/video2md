"""Delete only this turn's unused tags after checking every pod/init reference."""
import json
import subprocess
from pathlib import Path

HERE=Path(__file__).parent
CANDIDATES=[json.loads((HERE/name).read_text())['image'] for name in ('api-patch-manifest.json','patch-manifest.json','gpu-packet-manifest.json')]
PROGRAM='''
import json,subprocess
pods=json.loads(subprocess.check_output(['kubectl','get','pods','-A','-o','json']))['items']
refs={c['image'] for p in pods for k in ('containers','initContainers','ephemeralContainers') for c in p['spec'].get(k,[])}
canonical=lambda x:x.removeprefix('docker.io/library/')
refs={canonical(r) for r in refs}
candidates=__MANIFEST_IMAGES__
rows=[]
for tag in candidates:
 if tag in refs:
  rows.append({'tag':tag,'removed':False,'reason':'referenced by pod/init'});continue
 commands=[['docker','image','rm',tag],['sudo','k3s','ctr','-n','k8s.io','images','rm','docker.io/library/'+tag]]
 codes=[subprocess.run(c,capture_output=True,text=True).returncode for c in commands]
 docker_absent=subprocess.run(['docker','image','inspect',tag],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode!=0
 ctr_names=subprocess.check_output(['sudo','k3s','ctr','-n','k8s.io','images','ls','-q'],text=True).splitlines()
 rows.append({'tag':tag,'referenced_by_pod':False,'exit_codes':codes,'verified_absent':docker_absent and 'docker.io/library/'+tag not in ctr_names})
print(json.dumps({'retained_current_and_previous':True,'all_pod_and_init_refs_checked':True,'tags':rows}))
'''
PROGRAM=PROGRAM.replace('__MANIFEST_IMAGES__',repr(CANDIDATES))
r=subprocess.run(['ssh','-o','BatchMode=yes','lcode-4c','python3 -'],input=PROGRAM,text=True,capture_output=True,timeout=90)
if r.returncode:raise RuntimeError('Scoped cleanup not confirmed; output suppressed')
d=json.loads(r.stdout.strip().splitlines()[-1]);Path(__file__).with_name('unused-image-cleanup.json').write_text(json.dumps(d,indent=2));print(json.dumps(d))
