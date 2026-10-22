"""Build a fail-closed matrix from the captured browser and server evidence."""
import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE=Path(__file__).parent
ROOT=HERE.parents[3]
TARGET=ROOT/'docs/evals/production-failure-cost-evidence-2026-10-22.json'
def read(name):
    return json.loads((HERE/name).read_text())
def ref(name):
    p=HERE/name
    return {'path':'../testing/2026-10-22/production-ui-followup/'+name,
            'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}

before,after=read('before-submission.json'),read('final-state.json')
release=read('deployment.json')['images']['api']
manifest=json.loads((ROOT/'docs/testing/2026-10-21/failure-cost-integration/release-manifest.json').read_text())
assert all(after['source_sha256'][Path(p).name]==digest for p,digest in manifest['files'].items())
oauth,me=read('oauth-evidence.json'),read('me-http-evidence.json')['lcode-4c']
starts=[r for r in oauth['audit'] if r['path']=='/auth/google/login']
callbacks=[r for r in oauth['audit'] if r['path']=='/auth/google/callback']
assert len(starts)==len(callbacks)==3
tz=timezone(timedelta(hours=8))
observations=[]
for i,(start,callback) in enumerate(zip(starts,callbacks),1):
    begin=datetime.fromisoformat(start['ts']).replace(tzinfo=tz)
    returned=datetime.fromisoformat(callback['ts']).replace(tzinfo=tz)
    end=datetime.fromisoformat(starts[i]['ts']).replace(tzinfo=tz) if i<len(starts) else returned+timedelta(seconds=60)
    successes=[(datetime.strptime(r['ts'],'%d/%b/%Y:%H:%M:%S %z'),r) for r in me if r['status']==200]
    successes=[(at,r) for at,r in successes if returned<=at<end]
    assert start['code']==callback['code']==302 and len(successes)==2
    login_at,refresh_at=(at for at,_ in successes)
    observations.append({'attempt_id':f'ui-login-{i}','outcome':'success',
      'login_started_at':begin.isoformat(),'callback_at':returned.isoformat(),
      'me_at':login_at.isoformat(),'refresh_at':refresh_at.isoformat(),
      'me_status':200,'refresh_status':200,'elapsed_s':(login_at-begin).total_seconds(),
      'ui_library_screenshot':f'login-{i}.png'})

relay=oauth['relay']
assert relay['video2md-oauth-relay.service']=={'is-active':'active','is-enabled':'enabled'}
assert relay['oauth-relay.service']=={'is-active':'inactive','is-enabled':'disabled'}
assert '127.0.0.1:18080' in relay['listener']
run_id='f1fff0b8'
common={'release':release,'run_id':run_id}
results=[
 {'id':'failure-cost-001','status':'not-tested','metrics':{**common,
  'entries':[{'entry':i,'failed_stages':['transcribe'],'llm_calls_after_failure':0,'blocked_before_model':True} for i in (1,2)],
  'limitation':'Real UI run stopped at ASR. It does not exercise OCR/clips failures plus a healthy control.'},
  'evidence_refs':[ref('final-state.json'),ref('bilibili-terminal.png')]},
 {'id':'failure-cost-002','status':'not-tested','metrics':{**common,'attempts':[],
  'recorded_usage':{'input_tokens':0,'output_tokens':0,'cached_tokens':0},
  'limitation':'No model request occurred. Current-release isolated retry replay passed previously; this new UI run cannot close real empty-response retry acceptance.'},
  'evidence_refs':[ref('final-state.json')]},
 {'id':'failure-cost-003','status':'pass','metrics':{**common,'observations':observations,
  'relay':{'active_units':['video2md-oauth-relay.service'],'retired_unit_enabled':False,'listener_host':'127.0.0.1'}},
  'evidence_refs':[ref('oauth-evidence.json'),ref('me-http-evidence.json'),ref('deployment.json'),
                   *[ref(f'login-{i}.png') for i in (1,2,3)]]},
 {'id':'failure-cost-004','status':'not-tested','metrics':{**common,
  'application_cost_usd':0,'amount_source':'application_attribution_only','supplier_rows':[],
  'limitation':'Official provider landing page opened, Go navigation timed out; no independent settled invoice/usage retrieved. Zero application cost is not supplier cash reconciliation.'},
  'evidence_refs':[ref('final-state.json'),ref('billing-detail.png')]}
]
TARGET.write_text(json.dumps({'kind':'production','release':release,'run_id':run_id,
 'observed_date':'2026-10-22','note':'OAuth passed real UI and refresh acceptance. Three other production findings remain open. Historical evidence is preserved.',
 'results':results},ensure_ascii=False,indent=2)+'\n')
old,new=(s['cost']['usage'] for s in (before,after))
delta={k:round(new[k]-old[k],6) for k in old}
assert delta=={'spend_usd':0,'llm_in_tok':0,'llm_out_tok':0,'gpu_s':0.089,'runs_n':1}
assert after['active_runs']==[] and after['new_task_runtime']['llm_span_count']==0
(HERE/'results.json').write_text(json.dumps({'verdict':'PARTIAL_EXTERNAL_BLOCKERS','release':release,
 'real_google_login_successes':3,'login_me_and_refresh_http_200':True,
 'explicit_frontend_bilibili_submission':True,'run_id':run_id,'parts':[1,2],
 'run_status':'error','failure_stage':'transcribe','new_books':0,
 'application_usage_delta':delta,'supplier_cash_verified':False,
 'source_hashes_match_previous_release':True,'production_runtime_changed_this_turn':False,
 'outstanding':['Colab quota cooldown','Kaggle credential fields empty','OCR/clips end-to-end and real retry observation','independent provider invoice/usage'],
 'gate_expectation':'failure-cost-003 passes; 001/002/004 fail closed'},ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'oauth_elapsed_s':[r['elapsed_s'] for r in observations],'delta':delta,'evidence':str(TARGET)},ensure_ascii=False))
