"""Bounded cache-only fitness audit; no network, accounts or qualification writes."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from .contract_snapshot import compare_definition
from .fitness import COSTS, one_contract
from .qualification import SourceSet, realized_calendar, execution_evidence, first_clearing_final
from .runtime import Guard, digest, write_json, utcnow
from .vendor import timestamp_ns, iso_ns

CONTRACTS = ('MGCJ5', '1OZJ5', 'MHGK5', 'MCLJ5', 'MZCK5', 'M6EM5', 'MESM5')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def bound(path):
    p = Path(path).resolve()
    return dict(path=str(p), sha256=digest(p), bytes=p.stat().st_size)


def verify_price_records(prices, catalog, identity):
    refs = {(r['source_sha256'], r['source_line']): r for r in prices['records']}
    if len(refs) != len(prices['records']) or not refs:
        raise ValueError('DUPLICATE_OR_EMPTY_PRICE_REFERENCES')
    found = []
    for schema in sorted({r['schema'] for r in refs.values()}):
        for row in catalog.rows(schema, identity=identity):
            ref = refs.get((row['source_sha256'], row['source_line']))
            if ref is None: continue
            if row['raw_record_sha256'] != ref['raw_record_sha256'] or row['ts_event'] != ref['bucket_start']:
                raise ValueError('PRICE_RECORD_HASH_OR_TIME_CHANGED')
            found.append(row)
    if len(found) != len(refs): raise ValueError('MISSING_BOUND_PRICE_RECORD')
    found.sort(key=lambda r: r['ts_event_ns'])
    if len({r['ts_event_ns'] for r in found}) != len(found):
        raise ValueError('DUPLICATE_PRICE_BUCKET')
    from decimal import Decimal
    actual = dict(open=found[0]['open'], high=str(max(Decimal(r['high']) for r in found)),
                  low=str(min(Decimal(r['low']) for r in found)), close=found[-1]['close'],
                  volume=sum(r['volume'] for r in found))
    expected = prices['session_ohlcv'] or prices['observed_partial_ohlcv']
    if any(Decimal(str(actual[k])) != Decimal(str(expected[k])) for k in actual):
        raise ValueError('PRICE_AGGREGATE_DOES_NOT_MATCH_BOUND_RAW_RECORDS')
    return dict(status='PASS_BOUND_RECORDS_TO_RAW_TO_RECEIPT', ohlcv=actual,
                bucket_count=len(found), first_bucket=found[0]['ts_event'],
                last_bucket=found[-1]['ts_event'], records=prices['records'])


def diagnostic_execution(catalog,contract,identity,cohort,prices,tick):
    if not cohort or not cohort['coverage_qualified']:
        return dict(status='UNKNOWN_NO_QUALIFIED_COMPLETE_RESET_COHORT',tradable_open=False,
                    tradable_stop=False,not_evidence_of_exchange_halt=True)
    coverage=catalog.interval_coverage(contract,'statistics',timestamp_ns(cohort['reset_event_at']),
                                        timestamp_ns(cohort['segments'][-1][1]))
    if coverage['status']!='COMPLETE_REQUEST_AVAILABLE_DATASET' or prices['price_coverage']!='COMPLETE_OBSERVED_BUCKETS':
        return dict(status='UNKNOWN_PRICE_OR_STATISTICS_COVERAGE',tradable_open=False,
                    tradable_stop=False,request_coverage=coverage,not_evidence_of_exchange_halt=True)
    return execution_evidence(catalog,identity,cohort,prices,tick_size=tick)


def execute(round_dir, output):
    round_dir, output = Path(round_dir).resolve(), Path(output).resolve()
    state = read(round_dir/'ROUND_STATE.json'); root = Path(state['data_root']).resolve()
    if not output.is_relative_to(round_dir/'resumes') or output == round_dir:
        raise ValueError('NEW_RESUME_OUTPUT_REQUIRED')
    resume = read(output/'RESUME_STATE.json')
    if resume['hard_deadline_utc'] != state['hard_deadline_utc']:
        raise ValueError('NO_AUTHORIZATION_EXTENSION')
    guard = Guard(state['hard_deadline_utc'], output); start = guard.check({'stage':'fitness_start'})
    source_dir = root/'continuations/20260916T014732Z/resumes/20260916T022339Z/integration_final_03/sessions'
    registry_path = Path(__file__).resolve().parents[2]/'docs/futures_f0/contract_registry.csv'
    registry = {r['root']:r for r in csv.DictReader(registry_path.open(encoding='utf-8-sig'))}
    samples = {c:read(source_dir/(c+'_2025-03-04.json')) for c in CONTRACTS}
    corn_path = round_dir/'final_evidence_checks_01/MZCK5_BOUNDARY_AUDIT.json'
    corn = read(corn_path)
    entries = {e['source_sha256']:e for d in samples.values() for e in d['sources']}
    entries.update({e['source_sha256']:e for e in corn['new_sources']})
    # Only receipts overlapping the already observed March sample. No large
    # archive load, no HTTP, and no reinterpretation of price qualification.
    for path in (root/'vendor_cache').glob('*.receipt.json'):
        receipt = read(path); p = receipt['request']
        if (p['schema'] not in ('statistics','status','definition') or
            not set(CONTRACTS) & set(p['symbols'].split(',')) or
            timestamp_ns(p['start']) >= timestamp_ns('2025-03-06T00:00:00Z') or
            timestamp_ns(p['end']) <= timestamp_ns('2025-03-03T00:00:00Z')): continue
        entries[receipt['sha256']] = dict(path=str(path.with_name(path.name.replace('.receipt.json','.csv'))),
            receipt_path=str(path), source_sha256=receipt['sha256'], receipt_sha256=digest(path),
            price_encoding='fixed_1e9')
    conditions = []
    for path in (root/'continuations').glob('*/metadata/*.json'):
        doc = read(path)
        if isinstance(doc,dict) and doc.get('method') == 'metadata.get_dataset_condition':
            p = doc['parameters']
            if p['start_date'] <= '2025-03-05' and p['end_date'] >= '2025-03-03':
                conditions.append(bound(path))
    catalog = SourceSet(list(entries.values()), conditions, root=root, guard=guard)
    records = []; overview = []; evidence = []; source_errors = []
    # Preserve failed completeness evidence. Individual source failures deny
    # qualification, but cannot erase independently verifiable price facts.
    for source, receipt in catalog.sources:
        count=0; quarantined=0
        for r in source.records(guard=guard):
            count+=1;quarantined+=r['parse_status']!='PARSED_VENDOR_RECORD'
        expected=receipt.get('authenticated_estimate',{}).get('get_record_count')
        if count!=expected or quarantined:
            source_errors.append(dict(source_sha256=receipt['sha256'],request=receipt['request'],
                request_sha256=receipt['request_sha256'],actual_count=count,quoted_count=expected,
                quarantined_records=quarantined,status='SOURCE_NOT_QUALIFIED_COUNT_OR_PARSE_MISMATCH'))
    for contract, sample in samples.items():
        guard.check({'stage':'fitness_contract', 'contract':contract})
        identity = (sample['publisher_id'],sample['instrument_id']); row = registry[sample['root']]
        prices = corn['aggregation'] if contract == 'MZCK5' else sample['prices']
        price_proof = verify_price_records(prices, catalog, identity)
        reference = sample['definition']['selected_record']
        definition = next((r for r in catalog.rows('definition',identity=identity)
            if r['source_sha256']==reference['source_sha256'] and r['source_line']==reference['source_line']),None)
        if definition is None or definition['raw_record_sha256'] != reference['raw_record_sha256']:
            raise ValueError('DEFINITION_REFERENCE_CHANGED')
        units = compare_definition(definition,row)
        if units['unit_comparison'] != 'MATCH': raise ValueError('UNIT_MISMATCH_NO_FITNESS_OUTPUT')
        try:
            calendars = realized_calendar(catalog, contract_id=contract,identity=identity)
            calendar_error=None
        except ValueError as error:
            calendars=[];calendar_error=str(error)
        cohort = next((r for r in calendars if r['session']=='2025-03-04'),None)
        execution=diagnostic_execution(catalog,contract,identity,cohort,prices,row['tick_in_quote_units'])
        if calendar_error:execution['source_failure_reason']=calendar_error
        ranges=sorted((timestamp_ns(r['request']['start']),timestamp_ns(r['request']['end']))
            for s,r in catalog.sources if r['request']['schema']=='statistics' and contract in r['request']['symbols'].split(','))
        actual_end=ranges[0][0] if ranges else timestamp_ns('2025-03-04T00:00:00Z')
        for begin,end in ranges:
            if begin>actual_end:break
            actual_end=max(actual_end,end)
        audit_end=min(actual_end,timestamp_ns('2025-03-06T00:00:00Z'))
        try:
            final, settlement = first_clearing_final(catalog,identity,'2025-03-04',audit_end-1)
        except ValueError as error:
            final=None;settlement=dict(status='UNKNOWN_SOURCE_QUALIFICATION_FAILED',reason=str(error))
        settlement['actual_contiguous_request_end_utc']=iso_ns(actual_end)
        settlement['bounded_audit_end_utc_exclusive']=iso_ns(audit_end)
        if settlement['status']=='FIRST_FINAL_WITHOUT_LATER_ECONOMIC_REVISION':
            settlement['status']='OBSERVED_FIRST_FINAL_NO_LATER_REVISION_WITHIN_ACQUIRED_SLICE_ONLY'
        settlement['outside_acquired_slice']='UNKNOWN'
        settlement['no_future_version_backfill'] = True
        limits=[dict(source_sha256=r['source_sha256'],source_line=r['source_line'],
                raw_record_sha256=r['raw_record_sha256'],capture_at=r['ts_recv'],event_at=r['ts_event'],
                stat_type=r['stat_type'],price=r['price'],action=r['update_action'])
            for r in catalog.rows('statistics',identity=identity,before=audit_end-1) if r['stat_type'] in (17,18)]
        evidence.append(dict(contract=contract,sample=bound(source_dir/(contract+'_2025-03-04.json')),
            replacement_price_evidence=bound(corn_path) if contract=='MZCK5' else None,
            price=price_proof,units=units,dated_calendar=cohort,execution=execution,
            settlement=settlement,observed_limit_messages=limits,
            null_limit_is_not_unlimited_permission=any(r['price'] is None for r in limits),
            old_boundary=sample['boundaries'],
            overall_qualification='INSUFFICIENT_EVIDENCE',
            blockers=['ATR20_AND_PRIOR55_CONTINUOUS_HISTORY_NOT_PRESENT',
                      'TWENTY_PRIOR_EXECUTION_SESSIONS_NOT_PRESENT',
                      'DATED_SAFE_EXIT_AND_THIS_CONTRACT_QUALIFIED_INPUT_PATH_NOT_VERIFIED'],
            historical_customer_received_at=None,supplier_published_at=None,
            wrote_qualified_inputs=False))
        for alpha in ('.1','.2'):
            for cost in COSTS:
                for direction in (1,-1):
                    result = one_contract(raw_price=price_proof['ohlcv']['open'],
                        multiplier=row['usd_multiplier_per_quote_unit'],tick_size=row['tick_in_quote_units'],
                        margin_fraction=alpha,direction=direction,cost=cost,
                        root_launch=row['root_first_trade_date'],decision_session='2025-03-04')
                    result.update(contract=contract,market=row['market'],session='2025-03-04',
                        observed_price_label='FIRST_OBSERVED_SESSION_BUCKET_OPEN_NOT_VERIFIED_ORDER_PRICE')
                    records.append(result)
        base = records[-12]  # 10%, baseline, long; hypothetical adverse entry.
        overview.append(dict(market=row['market'],contract=contract,session='2025-03-04',
            raw_price=price_proof['ohlcv']['open'],multiplier=row['usd_multiplier_per_quote_unit'],
            tick_value=row['tick_value_usd'],
            raw_margin_10=base['raw_margin_usd'],raw_margin_20=records[-6]['raw_margin_usd'],
            entry_margin_10_base_long=base['entry_margin_usd'],entry_margin_20_base_long=records[-6]['entry_margin_usd'],
            margin_fit_10=base['layer_margin_integer_cap']>=1,margin_fit_20=records[-6]['layer_margin_integer_cap']>=1,
            zero_gap_max_atr_base_illustration=base['zero_gap_atr_ceiling_illustration'],
            atr20='UNKNOWN',prior_execution_liquidity=base['liquidity']['status'],
            liquidity_reason=base['liquidity']['reason'],observed_sample_volume=price_proof['ohlcv']['volume'],
            root_launch=row['root_first_trade_date'],specific_contract_first_trade='UNKNOWN',
            observed_activation=definition['activation'],observed_expiration=definition['expiration'],
            delivery_type=row['delivery_type'],safe_exit_session='UNKNOWN',
            dated_calendar_status='OBSERVED_COHORT_WITH_COVERAGE' if cohort and cohort['coverage_qualified'] else 'UNKNOWN_OR_PARTIAL',
            open_proxy=execution['tradable_open'],stop_proxy=execution['tradable_stop'],
            first_final_clearing_capture=final['ts_recv'] if final else None,
            financial_status_10=base['financial_status'],financial_status_20=records[-6]['financial_status'],
            actual_signals_evaluated=False,actual_orders_created=0,actual_rejection_events=0,
            qualification='INSUFFICIENT_EVIDENCE',scope='THIS_CONTRACT_AND_DATE_ONLY_NOT_2022_2025_CONCLUSION'))
        write_json(output/'contract_evidence'/(contract+'.json'),evidence[-1],immutable=True)
    catalog.assert_unchanged()
    sources = dict(source_entries=list(entries.values()),condition_entries=conditions,source_errors=source_errors,
                   complete_response_checks=list(catalog.proofs.values()),registry=bound(registry_path))
    write_json(output/'SOURCE_AUDIT.json',sources,immutable=True)
    write_json(output/'ONE_CONTRACT_SCENARIOS.json',records,immutable=True)
    write_json(output/'SIX_MARKET_FITNESS.json',overview,immutable=True)
    with (output/'SIX_MARKET_FITNESS.csv').open('x',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(overview[0]));writer.writeheader();writer.writerows(overview)
    finish=guard.check({'stage':'fitness_completed'})
    write_json(output/'FITNESS_RUN.json',dict(start=start,end=finish,maximum_observed_rss=guard.peak_rss,
        minimum_available_ram=guard.min_available,contracts=len(overview),scenarios=len(records),
        network_requests=0,new_account_runs=0,existing_accounts_rerun=0,
        status='COMPLETED_CACHE_ONLY_FITNESS_NOT_STRATEGY_EVALUATION'),immutable=True)
    return overview


def existing_mes_prefix(round_dir,output):
    """Read prior input/evaluation evidence only; never replay an engine."""
    round_dir,output=Path(round_dir).resolve(),Path(output).resolve()
    state=read(round_dir/'ROUND_STATE.json')
    if not output.is_relative_to(round_dir/'resumes'):
        raise ValueError('NEW_RESUME_OUTPUT_REQUIRED')
    guard=Guard(state['hard_deadline_utc'],output);guard.check({'stage':'existing_mes_fitness'})
    validation_path=round_dir/'account_prefix_01/INPUT_VALIDATION.json'
    manifest_path=round_dir/'qualified_continuous_02/manifest.json'
    signals_path=round_dir/'delivery_evidence/READY_SIGNAL_EVIDENCE.json'
    validation,manifest,signals=map(read,(validation_path,manifest_path,signals_path))
    if digest(manifest_path)!=validation['manifest_sha256'] or validation['sessions']!=64:
        raise ValueError('PREVIOUS_ACCOUNT_INPUT_MANIFEST_CHANGED')
    features={r['session']:r for r in signals}
    values=[];samples={};sources=[]
    for batch in validation['batches']:
        for contract,sha in zip(batch['contracts'],batch['derivation_hashes'],strict=True):
            if not contract.startswith('MES'):continue
            pin=manifest['session_derivations'][sha];path=manifest_path.parent/pin['path']
            if digest(path)!=pin['sha256']:raise ValueError('PREVIOUS_DERIVED_INPUT_FILE_CHANGED')
            d=read(path)
            if d['derivation_sha256']!=sha or d['contract_id']!=contract or d['session']!=batch['session']:
                raise ValueError('PREVIOUS_DERIVATION_ASSOCIATION_CHANGED')
            samples[(contract,batch['session'])]=d;sources.append(bound(path))
    for (contract,session),d in samples.items():
        guard.check({'stage':'mes_dated_fitness', 'session':session})
        previous=next((v for (c,day),v in samples.items() if c==contract and v['next_session']==session),None)
        feature=features.get(previous['session']) if previous else None
        kwargs=dict(atr=feature['atr20'],previous_execution_close=previous['prices']['session_ohlcv']['close']) if feature else {}
        for alpha in ('.1','.2'):
            for cost in COSTS:
                for direction in (1,-1):
                    r=one_contract(raw_price=d['prices']['session_ohlcv']['open'],multiplier=5,tick_size=.25,
                        margin_fraction=alpha,direction=direction,cost=cost,**kwargs)
                    r.update(contract=contract,session=session,derivation_sha256=d['derivation_sha256'],
                             feature_source=bound(signals_path) if feature else None,
                             feature_session=feature['session'] if feature else None,
                             prior_signal_present=(feature['signal_close']>feature['prior55_high'] or
                                 feature['signal_close']<feature['prior55_low']) if feature else None)
                    values.append(r)
    opens=[float(d['prices']['session_ohlcv']['open']) for d in samples.values()]
    summary=dict(kind='EXISTING_MES_PRICE_AND_FEATURE_FITNESS_NO_ACCOUNT_REPLAY',
        source_validation=bound(validation_path),source_manifest=bound(manifest_path),feature_evidence=bound(signals_path),
        first_session=validation['input_start'],last_session=validation['input_end'],
        distinct_dates=len(validation['batches']),contract_dates=len(samples),
        raw_open_min=min(opens),raw_open_max=max(opens),
        raw_10_percent_margin_min=min(opens)*.5,raw_10_percent_margin_max=max(opens)*.5,
        raw_20_percent_margin_min=min(opens),raw_20_percent_margin_max=max(opens),
        evaluated_cost_margin_direction_rows=len(values),
        layer_margin_fail_rows=sum(r['layer_margin_integer_cap']==0 for r in values),
        observed_prior_atr_and_close_rows=sum(r['risk']['status']!='UNKNOWN' for r in values),
        known_atr_size_risk_fail_rows=sum(r['risk']['status']=='RULE_NOT_ALLOWED' for r in values),
        ready_signal_evaluations=len(signals),
        actual_entry_signals=sum(r['signal_close']>r['prior55_high'] or r['signal_close']<r['prior55_low'] for r in signals),
        actual_margin_rejection_events=0,actual_account_runs=0,existing_accounts_changed=False,
        prior_account_count=12,uncovered_next_date=validation['stop_boundary'],
        scenario_rows=values,used_derivation_files=sources)
    write_json(output/'EXISTING_MES_FITNESS.json',summary,immutable=True)
    return {k:v for k,v in summary.items() if k not in ('scenario_rows','used_derivation_files')}


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--round',required=True);p.add_argument('--output',required=True)
    p.add_argument('--existing-mes-prefix-only',action='store_true')
    args=p.parse_args();function=existing_mes_prefix if args.existing_mes_prefix_only else execute
    print(json.dumps(function(args.round,args.output),ensure_ascii=False,indent=2))
