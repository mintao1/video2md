"""Assemble source-bound observations. Missing live fault evidence stays red."""
import copy
import hashlib
import importlib.util
import json
import os
from datetime import datetime,timedelta,timezone
from pathlib import Path

HERE=Path(__file__).parent.resolve()
ROOT=HERE.parents[3]
EVAL=ROOT/'docs/evals'
PRIVATE=Path.home()/'.config/video2md-settlement'
RELEASE='video2md:0.2.0-note-embeds-20261022f'
GPU='gpu-router:0.2.3-ocr-layout-20261022g'
RID='dd586072'
read=lambda p:json.loads(Path(p).read_text())
write=lambda p,d:Path(p).write_text(json.dumps(d,ensure_ascii=False,indent=2))

def ref(path,role=None,base=EVAL):
 path=Path(path).resolve()
 r={'path':os.path.relpath(path,base),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
 if role:r['role']=role
 return r

book=read(HERE/'book-delivery.json')
runtime=read(HERE/'final-runtime.json')
assert runtime['deployments'][0]['images']['api']==RELEASE
assert runtime['gpu_runtime']['provider_source_sha256']==read(HERE/'gpu-layout-manifest.json')['source_sha256']
http=read(HERE/'http-current.json')['http']
auth=read(HERE/'auth-http-current.json')['audit']
zone=timezone(timedelta(hours=8))
starts=[datetime.fromisoformat(r['ts']).replace(tzinfo=zone) for r in auth if r['path']=='/auth/google/login' and r['ts']>='2026-10-22T04:36:00']
callbacks=[datetime.fromisoformat(r['ts']).replace(tzinfo=zone) for r in auth if r['path']=='/auth/google/callback' and r['ts']>='2026-10-22T04:36:00']
assert len(starts)==len(callbacks)==3
observations=[]
for i,(start,callback) in enumerate(zip(starts,callbacks),1):
 end=starts[i] if i<len(starts) else datetime.max.replace(tzinfo=zone)
 successes=[datetime.fromisoformat(r['ts']) for r in http if r['path']=='/me' and r['status']==200 and callback<=datetime.fromisoformat(r['ts'])<end]
 assert len(successes)>=2
 elapsed=(successes[0]-start).total_seconds()
 assert 0<elapsed<=60
 observations.append({'attempt_id':f'final-ui-login-{i}','outcome':'success','login_started_at':start.isoformat(),
  'callback_at':callback.isoformat(),'me_at':successes[0].isoformat(),'refresh_at':successes[1].isoformat(),
  'elapsed_s':elapsed,'me_status':200,'refresh_status':200,'screenshot':f'login-f-{i}.png'})
relay=runtime['relay']
canonical={'active_units':['video2md-oauth-relay.service'] if relay['video2md-oauth-relay.service']['is-active']=='active' and relay['oauth-relay.service']['is-active']=='inactive' else [],
 'retired_unit_enabled':relay['oauth-relay.service']['is-enabled']!='disabled','listener_host':'127.0.0.1' if '127.0.0.1:18080' in relay['listener'] else 'unknown'}
write(HERE/'oauth-final-observations.json',{'release':RELEASE,'observations':observations,'relay':canonical,
 'historical_slower_attempts_retained_in':'auth-http-current.json'})

reconciliation=read(HERE/'final-reconciliation.json')
export_path=PRIVATE/'provider-requests-dd-final-2026-10-22.json'
export=read(export_path)
rows=[r for r in export['items'] if r.get('sessionID')=='video2md-'+RID]
assert export['truncated'] is False and len(rows)==5
account='90aae05929df'
assert {hashlib.sha256(r['workspaceID'].encode()).hexdigest()[:12] for r in rows}=={account}
provider={r['request_id']:r for r in reconciliation['requests']}
application=[]
used=set()
for span in book['llm_spans']:
 for attempt in span['attempt_usage']:
  u=attempt['usage']; fingerprint=(u['prompt_tokens'],u['completion_tokens'],u.get('prompt_tokens_details',{}).get('cached_tokens',0))
  matches=[r for r in provider.values() if tuple(r[k] for k in ('input_tokens','output_tokens','cached_tokens'))==fingerprint and r['request_id'] not in used]
  assert len(matches)==1
  r=matches[0];used.add(r['request_id'])
  application.append({'request_id':r['request_id'],'input_tokens':fingerprint[0],'output_tokens':fingerprint[1],
   'cached_tokens':fingerprint[2],'outcome':attempt['outcome'],'usage_source':'provider_response',
   'request_id_correlation':'unique_usage_fingerprint','task':span['event']})
# Selection lacks a stored span in this run. Derive its counts from the
# independently recorded total minus the four observed note-stage requests.
fields={'input_tokens':'llm_in_tok','output_tokens':'llm_out_tok','cached_tokens':'llm_cached_in_tok'}
residual={k:reconciliation['application_usage'][v]-sum(r[k] for r in application) for k,v in fields.items()}
remaining=[r for k,r in provider.items() if k not in used]
assert len(remaining)==1 and all(residual[k]==remaining[0][k] for k in fields)
application.append(dict(residual,request_id=remaining[0]['request_id'],outcome='success',usage_source='run_total_residual',
 request_id_correlation='sole_unmatched_export_request_and_aggregate_residual',task='select_frames',independent_attempt_span_available=False))
supplier=[]
for raw in rows:
 rid=raw['requestID'];r=provider[rid]
 at=datetime.fromtimestamp(raw['startedAt']/1000,timezone.utc).isoformat()
 supplier.append({k:r[k] for k in fields}|{'request_id':rid,'run_id':RID,'account_ref':account,'product':raw['product'],
  'occurred_at':at,'attribution_usd':float(r['attribution_usd'])})
 for app in application:
  if app['request_id']==rid:app.update(run_id=RID,account_ref=account,occurred_at=at,occurred_at_source='matched_supplier_export')
receipt=read(HERE/'supplier-paid-receipt.json');coverage=read(HERE/'supplier-coverage-current.json')
billing={'run_id':RID,'release':RELEASE,'provider':'OpenCode Go','provider_export_id':reconciliation['provider_export_sha256'],
 'provider_export_sha256':reconciliation['provider_export_sha256'],'account_ref':account,'currency':'USD',
 'scope':'run-isolated','settlement_status':'settled','application_cost_usd':float(reconciliation['application_attribution_usd']),
 'application_requests':application,'supplier_rows':supplier,'billing_mode':'subscription_receipt',
 'amount_source':'provider_export_attribution','product':'go','export_truncated':False,'extra_usage_enabled':False,
 'subscription_invoice':{'invoice_id':receipt['invoice_id'],'account_ref':account,'product':'go',
  'status':'paid','currency':'CNY','amount_paid':67.72,'receipt_date':receipt['receipt_date'],
  'paid_until':coverage['paid_until'],'document_sha256':receipt['pdf_sha256']},
 'limitations':['select_frames has no independent stored attempt span; its usage is the aggregate residual',
  'request IDs correlate four spans by unique token fingerprint; timestamps come from the matched supplier export',
  'USD attribution is not a second cash charge; the PDF amount-due text alone is not proof of payment']}
write(HERE/'billing-accepted-metrics.json',billing)

case1={'run_id':RID,'release':RELEASE,'entries':[
 {'entry':'dd586072-ocr-failure','failed_stages':['ocr'],'llm_calls_after_failure':0,'blocked_before_model':True,
  'observation_release':'video2md:0.2.0-ocr-guard-20261022d'},
 {'entry':'dd586072-healthy','failed_stages':[],'llm_calls':5}],
 'missing_evidence':'Same-candidate actual clips failure and downstream-call observation; offline regression is not live evidence.'}
case2={'run_id':RID,'release':RELEASE,'attempts':application,'recorded_usage':{k:reconciliation['application_usage'][v] for k,v in fields.items()},
 'missing_evidence':'This successful run had no observed empty response; selection lacks a stored independent attempt span.'}
case3={'run_id':RID,'release':RELEASE,'observations':observations,'relay':canonical}
matrix={'kind':'production','release':RELEASE,'run_id':RID,
 'generation_release':'video2md:0.2.0-ocr-guard-20261022d','post_generation_repairs':['public_certificate_cache','canonical_frame_reference_repair'],
 'results':[
 {'id':'failure-cost-001','status':'not-tested','metrics':case1,'evidence_refs':[ref(HERE/'second-ocr-errors.json'),ref(HERE/'book-delivery.json')]},
 {'id':'failure-cost-002','status':'not-tested','metrics':case2,'evidence_refs':[ref(HERE/'book-delivery.json'),ref(HERE/'final-reconciliation.json')]},
 {'id':'failure-cost-003','status':'pass','metrics':case3,'evidence_refs':[ref(HERE/'oauth-final-observations.json'),ref(HERE/'http-current.json'),ref(HERE/'final-runtime.json'),*[ref(HERE/f'login-f-{i}.png') for i in (1,2,3)]]},
 {'id':'failure-cost-004','status':'pass','metrics':billing,'evidence_refs':[
  ref(HERE/'final-reconciliation.json'),ref(HERE/'book-delivery.json'),ref(HERE/'billing-final-ui.png'),ref(HERE/'billing-final-ui.txt'),ref(HERE/'supplier-paid-receipt.json','supplier_paid_status'),
  ref(HERE/'supplier-coverage-current.json','subscription_coverage'),ref(PRIVATE/'invoice-2026-10.pdf','supplier_invoice'),ref(export_path,'supplier_export')]}]}
matrix_path=EVAL/'production-failure-cost-resolution-2026-10-22.json';write(matrix_path,matrix)

old=read(EVAL/'kaggle-billing-followup-evidence-2026-10-22.json')
results=copy.deepcopy(old['results'])
by_id={r['id']:r for r in results}
by_id['kfi-002']['metrics']=read(HERE/'ocr-engine-probe.json')
by_id['kfi-002']['evidence_refs']=[ref(HERE/'gpu-layout-manifest.json','gpu_manifest'),ref(HERE/'gpu-runtime-g.json','gpu_runtime'),ref(HERE/'ocr-engine-probe.json','cuda_probe')]
reader_rows=[r for r in http if r['path']=='/read/dd586072' and r['status']==200 and r['ts']>='2026-10-22T04:35:00']
assert read(HERE/'reader-shell-http.json')['status']==200 and book['note_count']==2 and all(n['markdown_bytes']>0 for n in book['notes'])
by_id['kfi-007'].update(status='pass',metrics={'run_id':RID,'status':'done','books':[{'book_id':RID,'run_id':RID,
 'markdown_bytes':sum(n['markdown_bytes'] for n in book['notes']),'reader_http_status':200,'readable':True,'reused_old_book':False}]},
 evidence_refs=[ref(HERE/'book-delivery.json'),ref(HERE/'http-current.json'),ref(HERE/'note-repair-applied.json'),ref(HERE/'reader-playback.json'),ref(HERE/'reader-shell-http.json'),ref(HERE/'reader-second-repaired.json')])
by_id['kfi-008'].update(status='pass',metrics={'run_id':RID,'truncated':False,'rows':[{'request_id':r['request_id'],'run_id':RID,'usage_observed':True} for r in reconciliation['requests']],
 'headers_in_public_evidence':False,'private_original_preserved':True},evidence_refs=[ref(HERE/'final-reconciliation.json')])
by_id['kfi-009'].update(status='pass',metrics=reconciliation,evidence_refs=[ref(HERE/'final-reconciliation.json')])
before=by_id['kfi-010']['metrics']['application_usage']
audit=read(HERE/'audit-readback.json')['runs'][0]['usage_corrections'][0]
by_id['kfi-011'].update(status='pass',metrics={'run_id':'3c05faf8','after':read(HERE/'backfill-reconciliation.json'),
 'audit':{'run_id':'3c05faf8','id':audit['correction_id'],'provider_export_sha256':audit['provider_export_sha256'],
  'before_usage':before,'after_usage':{k:audit['after_usage'][k] for k in before}},'before_usage':before,'applied':True,
 'second_apply_changed':False,'unrelated_runs_changed':[],'cash_charge_created':False},
 evidence_refs=[ref(HERE/'backfill-applied.json'),ref(HERE/'backfill-reconciliation.json'),ref(HERE/'audit-readback.json')])
by_id['kfi-012'].update(status='fail',metrics={'run_id':RID,'expected_release':RELEASE,'matrix':matrix},
 evidence_refs=[ref(matrix_path,'failure_cost_matrix')])
source={'observations':{r['id']:r['metrics'] for r in results},
 'lifecycle_note':'kfi-003/004/005/006/010 preserve original submission, queued/cleanup and pre-correction historical observations; do not imply those events recur on the final image.'}
source_path=HERE/'resolution-case-observations.json';write(source_path,source)
for row in results:
 row['evidence_refs']=[r for r in row['evidence_refs'] if r.get('role')!='case_source']+[ref(source_path,'case_source')]
followup={'kind':'production','release':RELEASE,'gpu_release':GPU,'run_id':RID,'results':results}
write(EVAL/'kaggle-billing-resolution-2026-10-22.json',followup)
print(json.dumps({'oauth_elapsed_s':[r['elapsed_s'] for r in observations],'paid_cny':67.72,
 'requests':len(rows),'usage_matches':reconciliation['usage_matches'],'attribution_usd':reconciliation['application_attribution_usd'],
 'strict_live_fault_cases_left_open':['failure-cost-001','failure-cost-002']}))
