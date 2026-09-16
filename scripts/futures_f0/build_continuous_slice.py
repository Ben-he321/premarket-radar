"""Offline source-qualified fixed engineering slice; never a full-study claim."""
import argparse
from dataclasses import asdict
from datetime import date
import csv
import json
from pathlib import Path
import shutil
import sys
from filelock import FileLock

REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO))
from src.futures_f0.runtime import Guard,canonical_hash,digest,utcnow,write_json
from src.futures_f0.vendor import EvidenceFile,RawSource,model_time,timestamp_ns
from src.futures_f0.qualification import load_context,qualified_contract,realized_calendar
from src.futures_f0.session_inputs import DatedSession,integrate_session
from src.futures_f0.model import ContractSpec,SessionBar
from src.futures_f0.rolls import decide_roll


def bound(path,source=None):
    result=dict(path=str(path.resolve()),sha256=digest(path))
    if source:result['source']=source
    return result


def prepare_context(rd,root):
    path=rd/'SOURCE_CONTEXT_V2.json'
    if path.exists():return EvidenceFile(path,digest(path),'HASH_BOUND_CONTINUOUS_RAW_CONTEXT')
    context=json.loads((rd/'SOURCE_CONTEXT.json').read_text(encoding='utf-8-sig'))
    plan=json.loads((rd/'acquisition_runs/slice_special_boundary.json').read_text(encoding='utf-8-sig'))
    if plan['status']!='REQUEST_PLAN_COMPLETED_RAW_NOT_AUTOMATICALLY_QUALIFIED':raise ValueError('BOUNDARY_PLAN_INCOMPLETE')
    for item in plan['requests']:
        receipt=root/'vendor_cache'/(item['receipt']['request_sha256']+'.receipt.json')
        raw=receipt.with_name(receipt.name.replace('.receipt.json','.csv'))
        context['sources'].append(dict(path=str(raw),receipt_path=str(receipt),source_sha256=digest(raw),
            receipt_sha256=digest(receipt),price_encoding='fixed_1e9'))
    context['condition_entries']=[]
    for p in sorted((rd/'metadata').glob('*.json')):
        value=json.loads(p.read_text(encoding='utf-8-sig'))
        if value.get('method')=='metadata.get_dataset_condition':context['condition_entries'].append(bound(p))
    rules=json.loads((rd/'rule_sources/RULE_SOURCE_REVIEW.json').read_text(encoding='utf-8-sig'))
    for name in ('nyse_2024','normal_hours'):
        p=rd/'rule_sources'/(name+'.web.json');v=json.loads(p.read_text(encoding='utf-8-sig'))
        rules['sources'][name]=dict(**bound(p),url=v['source_url'],received_at=v['received_at'])
    rules['boundaries_rule_effective_before_slice']='EXPIRY_CORROBORATED_BY_DATED_2024_VENDOR_DEFINITIONS_AND_2023_PUBLISHED_NYSE_CALENDAR'
    rules['created_at']=utcnow().isoformat()
    rulepath=rd/'rule_sources/RULE_SOURCE_REVIEW_V2.json';write_json(rulepath,rules,immutable=True)
    context['rules']=bound(rulepath);context['slice_freeze']=bound(rd/'ENGINEERING_SLICE_FREEZE.json')
    write_json(path,context,immutable=True)
    return EvidenceFile(path,digest(path),'HASH_BOUND_CONTINUOUS_RAW_CONTEXT')


def model_bar(item):
    value=dict(item)
    for k in ('open','high','low','close','settlement'):
        if value.get(k) is not None:value[k]=float(value[k])
    for k in ('session','next_session'):
        if value.get(k):value[k]=date.fromisoformat(value[k])
    for k in ('opens_at','closes_at','available_at','received_at','settlement_available_at','settlement_reference_at',
              'input_cutoff','internal_calculated_at','supplier_published_at','calendar_confirmed_at'):
        if value.get(k):value[k]=model_time(timestamp_ns(value[k]))
    return SessionBar(**value)


def main():
    p=argparse.ArgumentParser();p.add_argument('--round',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--limit-sessions',type=int,default=0)
    p.add_argument('--context',type=Path)
    args=p.parse_args();rd=args.round.resolve();out=args.output.resolve()
    state=json.loads((rd/'ROUND_STATE.json').read_text(encoding='utf-8-sig'));root=Path(state['data_root'])
    if not out.is_relative_to(rd) or out.exists():raise ValueError('NEW_OUTPUT_INSIDE_CURRENT_ROUND_REQUIRED')
    guard=Guard(state['hard_deadline_utc'],out);guard.check({'stage':'build_continuous_start'})
    out.mkdir(parents=True);progress=dict(started_at=utcnow().isoformat(),status='RUNNING',sessions=[],real_accounts=0)
    with FileLock(str(rd/'research.lock'),timeout=0):
        try:
            if args.context:
                if not args.context.resolve().is_relative_to(rd):raise ValueError('CONTEXT_OUTSIDE_CURRENT_ROUND')
                evidence=EvidenceFile(args.context.resolve(),digest(args.context),'HASH_BOUND_CONTINUOUS_RAW_CONTEXT')
            else:evidence=prepare_context(rd,root)
            context,catalog=load_context(evidence,guard=guard)
            contextref=bound(evidence.path,evidence.source)
            source_objects=[s for s,r in catalog.sources]
            prior=Path(state['previous_round'])
            reference_path=root/'continuations/20260915T190902Z/normalization_sample/CME_TRADING_REFERENCE_DATE_SEMANTICS.json'
            # The scope is the known F0 evidence tree; never scan credentials.
            refs=list((root/'continuations').glob('*/normalization_sample/CME_TRADING_REFERENCE_DATE_SEMANTICS.json'))
            if len(refs)!=1:raise ValueError('UNIQUE_EXISTING_REFERENCE_DATE_EVIDENCE_REQUIRED')
            reference_path=refs[0]
            ref=EvidenceFile(reference_path,digest(reference_path),'https://databento.com/docs/venues-and-datasets/glbx-mdp3')
            policy_path=prior/'evidence/causality/CAPTURE_PREFIX_POLICY.json'
            policy=EvidenceFile(policy_path,digest(policy_path),'DOCUMENTED_CAPTURE_PREFIX_SOURCE_REVIEW_NOT_PER_FILE_CERTIFICATION')
            with (REPO/'docs/futures_f0/contract_registry.csv').open(encoding='utf-8-sig') as f:
                registry={r['root']:r for r in csv.DictReader(f)}
            definitions=[];specs={};cohorts={};lookup={};derivations={};calendars={};settlements={};statuses={}
            for contract in ('ESU4','ESZ4','MESU4','MESZ4'):
                rootname='MES' if contract.startswith('MES') else 'ES';row=registry[rootname]
                proof=qualified_contract(catalog,context,contract,row)
                write_json(out/'contract_proofs'/(contract+'.json'),proof,immutable=True)
                # Qualification flags result from the computed raw/rule proof.
                item=dict(contract_id=contract,market='SP500',root=rootname,
                    multiplier=float(row['usd_multiplier_per_quote_unit']),tick_size=float(row['tick_in_quote_units']),
                    listed=None,last_trade=proof['last_trade'],safe_exit_session=proof['safe_exit_session'],
                    exchange=row['exchange'],currency='USD',quote_unit=row['quote_unit'],
                    verified=True,calendar_verified=True,vendor_definition_verified=True,boundary_verified=True,
                    source='RECOMPUTED_RAW_DEFINITION_EXISTENCE_DATED_RULES',eligible_from=proof['eligible_from'],
                    eligibility_basis=proof['eligibility_basis'],qualification_sha256=canonical_hash(proof))
                definitions.append(item)
                specdata=dict(item)
                for k in ('last_trade','safe_exit_session','eligible_from'):specdata[k]=date.fromisoformat(specdata[k])
                specs[contract]=ContractSpec(**specdata)
                cohorts[contract]=realized_calendar(catalog,contract_id=contract,identity=tuple(proof['identity']))
                write_json(out/'calendars'/(contract+'.json'),cohorts[contract],immutable=True)
                dates=[x for x in cohorts[contract] if context['scope']['start']<=x['session']<=context['scope']['end']]
                if args.limit_sessions:dates=dates[:args.limit_sessions]
                expiry=proof['last_trade_at']
                bp=out/'dated_evidence'/(contract+'_boundaries.json')
                write_json(bp,dict(kind='SPECIFIC_CONTRACT_BOUNDARIES_CANDIDATE',mock=False,scope=dict(contract_id=contract),
                    first_notice=dict(status='NOT_APPLICABLE_CASH_SETTLED',source_urls=[row['spec_source_url']]),
                    last_trade=dict(at=expiry,source_urls=[row['spec_source_url']]),preceding_exchange_sessions=[],
                    computed_contract_proof=bound(out/'contract_proofs'/(contract+'.json'))),immutable=True)
                boundary=EvidenceFile(bp,digest(bp),'RAW_DEFINITION_AND_EXCHANGE_RULE_BOUNDARY')
                horizon=max(timestamp_ns(r['request']['end']) for _,r in catalog.sources
                    if r['request']['schema']=='statistics' and contract in r['request']['symbols'].split(','))
                from src.futures_f0.vendor import iso_ns
                for cohort in dates:
                    guard.check({'stage':'build_continuous_session','contract':contract,'session':cohort['session']})
                    cp=out/'dated_evidence'/(contract+'_'+cohort['session']+'.json')
                    write_json(cp,dict(kind='DATED_SESSION_INPUT_CANDIDATE',mock=False,
                       scope=dict(contract_id=contract,session=cohort['session']),timezone='America/Chicago',
                       identity=proof['identity'],segments=cohort['segments'],source_context=contextref,cohort=cohort,
                       calendar_basis='GLBX_STATUS_SESSION_RESET_COHORT',next_session=cohort['next_session'],
                       next_open=cohort['next_open'],source_urls=['https://databento.com/docs/schemas-and-data-formats/status'],
                       source_status='REPLAYED_ACTUAL_SCHEDULED_RESET_COHORT'),immutable=True)
                    ce=EvidenceFile(cp,digest(cp),'ACTUAL_GLBX_STATUS_RESET_COHORT_REPLAY')
                    result=integrate_session(source_objects,window=DatedSession(contract,date.fromisoformat(cohort['session']),ce),
                        decision_at=iso_ns(horizon-1),registry_row=row,boundary_evidence=boundary,reference_evidence=ref,
                        causality_evidence=policy,qualification_context=evidence,guard=guard)
                    rp=out/'derivations'/(contract+'_'+cohort['session']+'.json');write_json(rp,result,immutable=True)
                    key=contract+'|'+cohort['session'];lookup[key]=result
                    derivations[result['derivation_sha256']]=dict(path=str(rp.relative_to(out)),sha256=digest(rp))
                    calendars[key]=dict(timezone='America/Chicago',segments=cohort['segments'],source=ce.source,
                        evidence_sha256=ce.sha256,verified=result['research_qualified'],next_session=cohort['next_session'],
                        dated_evidence=dict(contract_id=contract,**bound(cp,ce.source)))
                    settlements[key]=result['settlement'];statuses[key]=result['execution_status']
                    progress['sessions'].append(dict(contract=contract,session=cohort['session'],qualified=result['research_qualified'],
                       issues=result['qualification_issues'],open_proxy=result['execution_status']['tradable_open'],
                       stop_proxy=result['execution_status']['tradable_stop'],derivation_sha256=result['derivation_sha256']))
                    write_json(out/'BUILD_PROGRESS.json',progress)
                print(json.dumps(dict(contract=contract,sessions=len(dates),qualified=sum(x['qualified'] for x in progress['sessions'] if x['contract']==contract))),flush=True)
            selected=[];mappings={};roll_hashes=[];roll_evidence=[];active='U4';overlaps=[]
            for cohort in cohorts['ESZ4']:
                session=cohort['session']
                if not context['scope']['start']<=session<=context['scope']['end']:continue
                if args.limit_sessions and len(selected)>=args.limit_sessions:break
                def result(c):return lookup.get(c+'|'+session)
                pairs=[result('MESU4'),result('MESZ4')];roll=None
                if active=='U4' and overlaps:
                    last=overlaps[-1]
                    roll=decide_roll(specs['MESU4'],specs['MESZ4'],[(x[0],x[1]) for x in overlaps[-2:]],
                        decision_at=max(last[0].available_at,last[1].available_at,last[2].available_at,last[3].available_at),
                        next_session=date.fromisoformat(session),signal_overlap=(last[2],last[3]))
                    if roll:
                        active='Z4';roll_hashes.append(roll.evidence_hash)
                        roll_evidence.append(dict(instruction=asdict(roll),prior_derivation_hashes=[b.source_hash for pair in overlaps[-2:] for b in pair]))
                signal=result('ES'+active)
                # Before the roll, both overlapping micro contracts retain their
                # true liquidity history. No synthetic roll prices are inserted.
                candidates=[x for x in pairs if x is not None] if active=='U4' or roll else [result('MESZ4')]
                if signal is None or not signal['research_qualified'] or any(x is None or not x['research_qualified'] for x in candidates):
                    progress.setdefault('batch_gaps',[]).append(dict(session=session,reason='REQUIRED_CONTRACT_SESSION_NOT_QUALIFIED'))
                    overlaps=[];continue
                payload=dict(market='SP500',signal=signal['engine_record_candidate'],
                    execution=[x['engine_record_candidate'] for x in candidates],
                    mapping_verified=True,mapping_source='RAW_DEFINITION_IDENTITY_AND_FROZEN_UNIT_MAPPING')
                if roll:payload['roll']=json.loads(json.dumps(asdict(roll),default=str))
                selected.append([payload])
                mappings['SP500|'+session]=dict(market='SP500',signal=signal['contract_id'],
                    execution=[x['contract_id'] for x in candidates],session=session,quote_unit=registry['ES']['quote_unit'])
                overlap=[result(c) for c in ('MESU4','MESZ4','ESU4','ESZ4')]
                if all(x is not None and x['research_qualified'] for x in overlap):
                    overlaps.append(tuple(model_bar(x['engine_record_candidate']) for x in overlap));overlaps=overlaps[-2:]
            (out/'bars.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in selected),encoding='utf-8')
            for name,value in [('definitions',definitions),('calendar',calendars),('settlements',settlements),('status',statuses),('mapping',mappings)]:
                write_json(out/(name+'.json'),value,immutable=True)
            write_json(out/'ROLL_DECISIONS.json',json.loads(json.dumps(roll_evidence,default=str)),immutable=True)
            target=out/'temporal_evidence';target.mkdir()
            pv=json.loads(policy_path.read_text(encoding='utf-8-sig'))
            for name in [policy_path.name,*pv['companion_documents']]:shutil.copyfile(policy_path.parent/name,target/name)
            manifest=dict(kind='EXCHANGE_FUTURES_ACTUAL_CONTRACTS',mock=False,integration_review='VERIFIED_RAW_TO_NORMALIZED',
                research_scope='PREDECLARED_CONTINUOUS_SP500_ENGINEERING_SLICE_NOT_FULL_MATRIX',
                source_object_hashes=sorted({e['source_sha256'] for e in context['sources']}),
                session_derivations=derivations,derivation_source_root=str(root),qualified_source_context=contextref,
                temporal_evidence=dict(path=str((target/policy_path.name).relative_to(out)),sha256=policy.sha256,source=policy.source),
                verified_roll_decision_hashes=roll_hashes,created_at=utcnow().isoformat())
            manifest['roll_decisions']=dict(path='ROLL_DECISIONS.json',sha256=digest(out/'ROLL_DECISIONS.json'),
                                           source='RECOMPUTE_FROM_PRIOR_SOURCE_BOUND_OVERLAPS')
            for name in ('bars','definitions','calendar','settlements','status','mapping'):
                path=out/(name+('.jsonl' if name=='bars' else '.json'))
                manifest[name]=dict(path=path.name,sha256=digest(path),source='COMPUTED_RAW_SOURCE_QUALIFICATION',verified=True)
            write_json(out/'manifest.json',manifest,immutable=True)
            catalog.assert_unchanged()
            progress.update(status='INPUTS_BUILT_ACCOUNT_NOT_YET_EXECUTED',batches=len(selected),rolls=len(roll_hashes))
        except BaseException as error:
            progress.update(status='STOPPED_ERROR',error_type=type(error).__name__,error=str(error));raise
        finally:
            progress.update(stopped_at=utcnow().isoformat(),peak_rss_bytes=guard.peak_rss,minimum_available_bytes=guard.min_available)
            write_json(out/'BUILD_PROGRESS.json',progress)


if __name__=='__main__':main()
