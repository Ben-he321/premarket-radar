"""B1.1 read-only report assembly and strict allowlist archive; never replays data."""
from __future__ import annotations
import argparse
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
import pandas as pd
from .b11_runtime import ROOT, REPO, PREVIOUS, read, write, sha, utc, preserve_check, status


def md(name, text):
    (ROOT/name).write_text(text.strip()+'\n',encoding='utf-8')


def collect(a='final_v4',b='final_v4'):
    selected=[]
    for tier,version in [('A',a),('B',b)]:
        rows=read(ROOT/'replay'/version/f'tier_{tier}'/'RUN_SUMMARY.json')
        if len(rows)!=20:raise ValueError('FINAL_BATCH_MUST_CONTAIN_ALL_FIXED20')
        for row in rows:
            if not row.get('actually_executed'):raise ValueError('UNEXECUTED_FINAL_SAMPLE')
            if row.get('error_count'):raise ValueError('FINAL_ENGINE_ERRORS_REQUIRE_REVIEW')
            selected.append(row)
    write(ROOT/'FINAL_RUN_SELECTION.json',{'created_at':utc(),'tier_A_version':a,'tier_B_version':b,
        'samples':[{'tier':r['earnings_tier'],'rank':r['sample_rank'],'output':r['output']} for r in selected],
        'selection_basis':'Same immutable first20; version changes repair adapter or enrich actual held-path quotes, never resample outcomes'})
    records=[]
    consumed=[]
    for r in selected:
        eq=r['latest_equity'] or {}
        inventory=pd.read_csv(Path(r['output'])/'quote_inventory.csv')
        for row in inventory[(inventory.ask_consumed>0)|(inventory.bid_consumed>0)].to_dict('records'):
            consumed.append({'earnings_tier':r['earnings_tier'],'sample_rank':r['sample_rank'],**row})
        records.append({k:r.get(k) for k in ['earnings_tier','sample_rank','symbol','sample_date','processed_through','status',
            'events_processed','buy_intents','buy_fills','buy_shares','sell_fills','sell_shares','pending_exit_count','error_count']} |
            {'initial_equity':5500,'cash':eq.get('cash'),'debt':eq.get('debt'),'interest':eq.get('accrued_interest'),
             'net_equity':eq.get('net_equity'),'complete_campaigns':r['campaigns'].get('closed_campaigns'),
             'realized_campaign_pnl':sum(c.get('net_profit',0) or 0 for c in r['campaigns']['campaigns'] if c['status']=='CLOSED'),
             'scope':'INDIVIDUAL_ENGINEERING_SAMPLE_NOT_SHARED_PORTFOLIO'})
    pd.DataFrame(records).to_csv(ROOT/'ACTUAL_ACCOUNT_SUMMARY.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(consumed).to_csv(ROOT/'CONSUMED_QUOTE_EVIDENCE.csv',index=False,encoding='utf-8-sig')
    write(ROOT/'COMPARISON_QUALIFICATION.json',{'created_at':utc(),
        'P50_engineering_samples':'ACTUALLY_EXECUTED',
        'P50_full_pool':'NOT_RUN_DATA_INSUFFICIENT',
        'remaining_frozen_matrix':'NOT_RUN_FULL_POOL_P50_PRECONDITION_UNMET',
        'SPY_QQQ_performance_comparison':'NOT_COMPUTED_NO_COMPARABLE_FULL_POOL_ACCOUNT',
        'reason':'66 candidate fixed continuous 2026-01-02..2026-09-11 joint-capital signal/ranking calendar lacks full causal earnings and L1 coverage. Only immutable first20 single-security engineering campaigns were replayed. Concatenation is prohibited.',
        'not_a_strategy_failure':True,'P200_engineering':'Actual debt, interest, margin and settlement exercised in synthetic integration, not real-data P200 profitability validation',
        'MINUTE_BAR_EXECUTION_PROXY':'NOT_USED_FOR_FILLS; prices originate in vendor SIP L1 plus frozen friction. Minute bars can trigger pending risk exits.',
        'unimplemented_requested_engine_features':[],
        'input_adapter_remaining':'Unencountered new split schemas require explicit ratio review; fractional entitlements need confirmed cash-in-lieu amount. Generic event support is tested.'})
    recoveries=[{'path':str(p),'evidence':read(p)} for r in selected for p in [Path(r['output'])/'RECOVERY_IDEMPOTENCY.json']]
    write(ROOT/'RECOVERY_IDEMPOTENCY_SUMMARY.json',{'created_at':utc(),'real_samples':len(recoveries),
        'passed':sum(x['evidence']['status']=='PASS' for x in recoveries),'records':recoveries,
        'full_local_checkpoints_retained':True,'checkpoint_exclusion_from_zip':'Private checkpoints contain full cached quote inventory; bundle carries digest/replay proof and source instead'})
    return selected


ROOT_NAMES={
 'BEN_B1_1_RESULTS.md','B11_PROTOCOL.json','CREDENTIAL_CAPABILITY_REDACTED.json','FINAL_RUN_SELECTION.json',
 'ACTUAL_ACCOUNT_SUMMARY.csv','COMPARISON_QUALIFICATION.json','coverage_funnel.csv','UNIVERSE_COVERAGE.csv',
 'RULE_INTENT_ALIGNMENT.md','RULE_INTENT_CASE_FIELDS.csv','RULE_INTENT_CASE_EVIDENCE.json','AUCTION_CONDITION_INTERPRETATION.json',
 'REAL_TRACE_REVIEW.json','REAL_TRACE_FINAL_REVIEW.json','RECOVERY_IDEMPOTENCY_SUMMARY.json','RECOVERY.md',
 'PRESERVATION_CHECK.json','TIME_AND_COST_AUDIT.json','RUNTIME_STATUS.json','TASK_STATE.json','DELIVERY_CODE.json',
 'ERROR_AND_REPAIR_LOG.json','OLD_FORWARD_READONLY_BEFORE.json','OLD_FORWARD_READONLY_AFTER.json',
 'QUOTE_PATH_COVERAGE.json','SOURCE_MANIFEST.json','BEN_B1_1_RESULTS_FINAL_REVIEW.md',
 'CONSUMED_QUOTE_EVIDENCE.csv','EXIT_QUOTE_INPUTS.json','EXIT_QUOTE_RECEIPT.json','EXIT_QUOTE_COVERAGE.csv','EXIT_PATH_REQUEST_FREEZE.json',
 'EXIT_ORCL_CAUSAL_TRACE_SNAPSHOT.json',
 'COVERAGE_RECONCILIATION.json',
}
DATA_NAMES={'FIRST20_FROZEN.csv','FIRST20_FROZEN_META.json','FIRST20_INPUTS.json','FIRST20_QUOTE_COVERAGE.csv',
 'HISTORY_INPUTS.json','DATA_INPUT_MANIFEST.json','DATA_INPUTS_READY.json','MARKET_DATA_EVIDENCE.md',
 'RANKING_VOLUME_PRIOR20.csv','FIRST20_DOLLAR_VOLUME_WINDOWS.json','INHERITED_TARGETED_EVIDENCE.json',
 'EXIT_QUOTE_INPUTS.json','EXIT_QUOTE_COVERAGE.json','EXIT_QUOTE_DATA_READY.json'}
DATA_NAMES.update({'ALL66_FIXED_INTERVAL_COVERAGE.csv','HISTORY_DATA_QUALITY.csv','FIRST20_PROTOCOL.json'})
DATA_NAMES.update({'corporate_actions.json','DATA_PROTOCOL.json'})
SAMPLE_NAMES={'summary.json','RUN_SPEC.json','RECOVERY_IDEMPOTENCY.json','orders.csv','fills.csv','campaigns.csv',
 'daily_equity.csv','coverage_funnel.csv','event_trace.csv','account_events.csv','data_gaps.csv','progress.json'}


def bundle():
    target=ROOT/'verification_ben_b1_1_replay_bundle.zip'
    if target.exists():raise ValueError('DELIVERED_ARCHIVE_IMMUTABLE_USE_EXPLICIT_NEW_DELIVERY_VERSION')
    items={}
    def add(path,name=None):
        path=Path(path)
        if path.is_file():items[name or str(path.relative_to(ROOT)).replace('\\','/')]=path
    for name in ROOT_NAMES:add(ROOT/name)
    for name in DATA_NAMES:add(ROOT/'data'/name)
    add(ROOT/'earnings/PRIMARY_EARNINGS.json')
    for p in (ROOT/'engineering').glob('*'):
        if p.suffix in {'.json','.md','.log','.xml'}:add(p)
    # Preserve unsuccessful adapter attempts and sparse-input traces alongside
    # final versions, with an explicit final-version selector in the archive.
    for p in (ROOT/'replay').rglob('*'):
        if p.name in SAMPLE_NAMES or p.name in {'RUN_SUMMARY.json','INPUT_HASHES.json'} or p.name.startswith('adapter_error_'):
            add(p)
    for p in ROOT.glob('replay_*.log'):add(p)
    for p in (REPO/'src/ben_b1').glob('*.py'):add(p,'replay_source/src/ben_b1/'+p.name)
    for p in (REPO/'tests').glob('test_ben_b1*.py'):add(p,'replay_source/tests/'+p.name)
    for p in (REPO/'docs/ben_b1_1').glob('*'):
        if p.suffix in {'.md','.json','.txt'}:add(p,'replay_source/docs/ben_b1_1/'+p.name)
    for n in ['requirements.txt','README.md','ben_b1_1_status.py']:add(REPO/n,'replay_source/'+n)
    blocked=[n for n in items if any(x in n.lower() for x in ('secrets.toml','.env','checkpoint.json','.parquet','.sqlite','.duckdb','private_source_pages'))]
    if blocked:raise ValueError('ARCHIVE_ALLOWLIST_VIOLATION:'+str(blocked))
    from .credentials import BASE, RELATED
    import os, tomllib
    # Inspect only project-authorized local configuration. Never print values.
    values=[]
    def secrets_only(mapping):
        for key,value in mapping.items():
            if isinstance(value,dict):secrets_only(value)
            elif any(word in key.lower() for word in ('key','secret','token','password')) and isinstance(value,str) and len(value)>=12:
                values.append(value.encode())
    for project in RELATED:
        config=BASE/project/'.streamlit/secrets.toml'
        if config.exists():secrets_only(tomllib.loads(config.read_text(encoding='utf-8-sig')))
    for key in ['ALPACA_API_KEY','ALPACA_SECRET_KEY','FINNHUB_API_KEY']:
        if len(os.environ.get(key,''))>=12:values.append(os.environ[key].encode())
    values=list(set(values))
    for name,p in items.items():
        body=p.read_bytes()
        if any(v in body for v in values):raise ValueError('CREDENTIAL_VALUE_DETECTED_IN_ALLOWED_ARTIFACT')
    manifest={'created_at':utc(),'files':{n:{'sha256':sha(p),'size':p.stat().st_size} for n,p in sorted(items.items())},
              'excluded':'Credentials, raw market stores, full checkpoints/quote inventory, databases, private source pages, unrelated project data'}
    write(ROOT/'OUTPUT_MANIFEST.json',manifest);add(ROOT/'OUTPUT_MANIFEST.json')
    with zipfile.ZipFile(target,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for name,p in sorted(items.items()):archive.write(p,name)
    with zipfile.ZipFile(target) as archive:
        bad=archive.testzip()
        if bad:raise ValueError('ZIP_CRC_FAILED:'+bad)
    result={'created_at':utc(),'path':str(target),'size':target.stat().st_size,'sha256':sha(target),
            'entries':len(items),'crc':'PASS','allowlist':'PASS','credential_values_checked':len(values)}
    write(ROOT/'ZIP_SAFETY_CHECK.json',result)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['collect','bundle'])
    parser.add_argument('--a',default='final_v4');parser.add_argument('--b',default='final_v4')
    args=parser.parse_args()
    print(json.dumps(collect(args.a,args.b) if args.action=='collect' else bundle(),ensure_ascii=False,default=str))
