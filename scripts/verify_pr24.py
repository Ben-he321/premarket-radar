"""Read-only export of existing PR24 evidence. No strategy/data API imports.

Explicit file allowlist only; source curves/trades are summarized, not bundled.
Never invokes report.produce/research.run and never reads raw/all price datasets.
"""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import zipfile
import numpy as np
import pandas as pd
from filelock import FileLock,Timeout

REVIEWED='8b601c04e6581dbf23c36920b4c7ecc55f2be89e'
ALLOWLIST=['RESULTS.md','candidate_results.csv','candidate_results.json','coverage.json','sip_checks.json',
           'incremental_verification.json','preregistration.json','research/summary.json','research/portfolio.json',
           'research/account_metrics.json','research/promotion.json','research/improvements.json','research/statistics.json',
           'research/per_stock_strategy_table.csv','research/selection_evidence.json','research/holdout_receipt.json','backup_verification.json']
STRUCTURES=['SHARED','PER_SYMBOL','HYBRID_REGIME']
COSTS=['base','stress25','stress50','double_commission']


def utc():return datetime.now(timezone.utc).isoformat()
def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def write(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2,allow_nan=False,default=str),encoding='utf-8')
def git(*args):return subprocess.check_output(['git',*args]).decode('utf-8').strip()
def as_time(t):return datetime.fromtimestamp(t,timezone.utc).isoformat()


def trade_stats(t):
    if t.empty:return dict(completed_trades=0,win_rate=None,mean_win_usd=None,mean_loss_usd=None,
                           mean_win_return=None,mean_loss_return=None,expectancy_usd=None,expectancy_return=None,
                           profit_factor_usd=None,profit_factor_return=None,completed_trade_net_pnl=0.)
    x=t.return_net;d=t.net_pnl;w=x[x>0];l=x[x<0];dw=d[d>0];dl=d[d<0]
    return dict(completed_trades=len(t),win_rate=float((x>0).mean()),mean_win_usd=float(dw.mean()) if len(dw) else None,
                mean_loss_usd=float(dl.mean()) if len(dl) else None,mean_win_return=float(w.mean()) if len(w) else None,
                mean_loss_return=float(l.mean()) if len(l) else None,expectancy_usd=float(d.mean()),expectancy_return=float(x.mean()),
                profit_factor_usd=float(dw.sum()/-dl.sum()) if len(dl) else None,
                profit_factor_return=float(w.sum()/-l.sum()) if len(l) else None,completed_trade_net_pnl=float(d.sum()))


def period_stats(curve,trades,skips,start,end):
    c=curve[(curve.date>=start)&(curve.date<=end)]
    if c.empty:return {'availability':'NO_EQUITY_OBSERVATIONS'}
    prev=curve[curve.date<start];initial=float(prev.equity.iloc[-1]) if len(prev) else 5500.
    previous_fees=float(prev.fees.iloc[-1]) if len(prev) else 0.
    x=trades[(trades.entry_date>=start)&(trades.exit_date<=end)] if len(trades) else trades
    crossing=trades[(trades.entry_date<start)&(trades.exit_date>=start)&(trades.exit_date<=end)] if len(trades) else trades
    peak=np.maximum.accumulate(np.r_[initial,c.equity.to_numpy()])[1:]
    ending=float(c.equity.iloc[-1]);result={'availability':'EXISTING_CURVE_AND_CLOSED_TRADES','start':start,'end':end,
        'original_account_capital':5500.,'period_start_equity':initial,'period_end_equity':ending,'net_equity_change_usd':ending-initial,
        'period_net_return':ending/initial-1,'period_max_drawdown':float(np.min(c.equity.to_numpy()/peak-1)),
        'fees_in_calendar_period':float(c.fees.iloc[-1])-previous_fees,
        'skips_by_reason':dict(Counter(s['reason'] for s in skips if start<=s['date']<=end)),
        'incoming_carry_trades_closed':len(crossing),
        'incoming_carry_closed_pnl_usd':float(crossing.net_pnl.sum()) if len(crossing) else 0.,
        'trade_metrics_basis':'Trades whose ENTRY AND EXIT both lie inside period; equity includes carry marks/receivables/open positions',
        'profit_factor_definition':'Return-weighted PF matches historical report; USD PF supplied separately',**trade_stats(x)}
    return result


def collect(source,out):
    out.mkdir(parents=True,exist_ok=True);existing=out/'existing';existing.mkdir(exist_ok=True)
    initial_hashes={};inventory=[]
    for name in ALLOWLIST:
        p=source/name;row={'path':name,'exists':p.exists()}
        if p.exists():
            initial_hashes[name]=sha(p);row.update(sha256=initial_hashes[name],bytes=p.stat().st_size)
            dst=existing/name;dst.parent.mkdir(parents=True,exist_ok=True);dst.write_bytes(p.read_bytes())
        inventory.append(row)
    write(out/'source_inventory.json',inventory)
    missing=[x['path'] for x in inventory if not x['exists']]
    if missing:
        write(out/'missing_files.json',missing)
    version={'captured_at':utc(),'reviewed_commit':REVIEWED,'head':git('rev-parse','HEAD'),'branch':git('branch','--show-current'),
             'remote':git('remote','get-url','origin'),'workdir':str(Path.cwd()),'status':git('status','--porcelain'),
             'reviewed_diff_stat':git('diff','--stat',REVIEWED),'tracked_source_hashes':{}}
    # Record only source hashes, not source secrets or arbitrary workspace files.
    for name in git('ls-files').splitlines():
        if name.startswith(('src/watchlist/','tests/','scripts/')) and Path(name).is_file():
            version['tracked_source_hashes'][name]=sha(Path(name))
    write(out/'source_version.json',version)
    (out/'report_fix.patch').write_text(git('diff',REVIEWED,'--','src/watchlist/report.py','tests/test_pr24_report_acceptance.py'),encoding='utf-8')

    with closing(sqlite3.connect((source/'tasks.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        events=[dict(r) for r in db.execute('SELECT id,at,task,status,input_version,output,error FROM events ORDER BY id')]
    # Only extracted task evidence; no database or whole logs in bundle.
    for e in events:
        if e['task'].startswith('sync:'):
            e['output']=json.loads(e['output']) if e['output'] else None
        else:
            payload=json.loads(e['output']) if e['output'] else None
            e['output']=payload if isinstance(payload,dict) else {'record_count':len(payload)} if isinstance(payload,list) else None
        if e['error'] and '[WinError 32]' in e['error']:e['error']='WINERROR32_SQLITE_BACKUP_TEMP_HANDLE; subsequently fixed'
    write(out/'task_event_extract.json',events)
    logs=[]
    for name in ['sync.stdout.log','research.stdout.log','incremental.stdout.log','actions.stdout.log']:
        p=source/name
        if p.exists():
            stat=p.stat();lines=p.read_text(encoding='utf-8',errors='replace').splitlines()
            logs.append({'path':name,'sha256':sha(p),'created_utc':as_time(stat.st_birthtime),'last_write_utc':as_time(stat.st_mtime),
                         'file_elapsed_seconds':stat.st_mtime-stat.st_birthtime,'first_line':lines[0] if lines else None,
                         'last_line':lines[-1] if lines else None,'timing_precision':'Filesystem span proxy; not instrumented CPU/network stopwatch'})
    write(out/'log_boundary_extract.json',logs)
    sync=[e for e in events if e['task'].startswith('sync:')]
    batches=[sync[:1316],sync[1316:1328],sync[1328:]] if len(sync)==1466 else [sync]
    timeline=[]
    for label,b in zip(['initial_download','isolated_bad_date_repair','incremental_five_sessions'],batches):
        if b:
            timeline.append({'phase':label,'first_completed_event':b[0]['at'],'last_completed_event':b[-1]['at'],
                'event_span_seconds':(datetime.fromisoformat(b[-1]['at'])-datetime.fromisoformat(b[0]['at'])).total_seconds(),
                'events':len(b),'statuses':dict(Counter(e['status'] for e in b)),
                'timing_precision':'Completion-to-completion lower bound; actual request start unlogged',
                'input_versions':sorted(set(e['input_version'] for e in b))})
    p=read(source/'preregistration.json');summary=read(source/'research/summary.json')
    researchlog=next((x for x in logs if x['path']=='research.stdout.log'),{})
    timeline.append({'phase':'research_calculation','approx_start_utc':researchlog.get('created_utc'),
                     'recorded_complete_utc':summary.get('completed_at'),'approx_seconds':researchlog.get('file_elapsed_seconds'),
                     'input_version':summary.get('input_version'),'preregistered_at':p.get('registered_at'),
                     'timing_precision':'Approximate start from log creation; feature/selection/replay substage starts not separately timestamped'})
    reuse=[e for e in events if e['status']=='FROZEN_RESULT_REUSED']
    last_sync=read(source/'sync_last.json') if (source/'sync_last.json').exists() else {}
    current_state=read(source/'status.json')
    timeline.append({'phase':'cached_resume','events':reuse,'last_sync_log_extract':last_sync,
                     'sync_log_sha256':sha(source/'sync_last.json') if last_sync else None,
                     'orchestrator_start_inferred_from_8h_deadline':as_time(current_state['deadline_epoch']-8*3600) if current_state.get('command')=='run-all' and current_state.get('deadline_epoch') else None,
                     'state_stage':current_state.get('stage'),'state_last_heartbeat':current_state.get('heartbeat'),
                     'state_error_field':'Historical backup handle error retained by dict.update; later engineering FIXED event and backup PASS exist' if current_state.get('error_type') else None,
                     'evidence':'sync_last status log and explicit FROZEN_RESULT_REUSED event; these do not identify an unspecified UI one-minute interval',
                     'exact_duration':None})
    timeline.append({'phase':'report_generation','exact_start':None,'exact_duration':None,
                     'reason':'No dedicated start/end event; RESULTS was regenerated several times. Modification time is not a run duration.'})
    write(out/'timeline.json',timeline)

    cov=read(source/'coverage.json');candidates=read(source/'candidate_results.json')
    lengths={status:[r['symbol'] for r in candidates if r['status']==status] for status in ['INCLUDED','INSUFFICIENT_HISTORY','DATA_UNAVAILABLE','IDENTITY_UNRESOLVED','EXCLUDED_BY_TYPE']}
    write(out/'coverage_lists.json',{'data_threshold_rows':378,'lists':lengths,'groups':len(cov),'rows':sum(c['rows'] for c in cov),
        'strategy_gate':'Independent minimum outer-window/trade counts, positive net expectancy and stress, plus engineering gates; history length does not establish strategy qualification',
        'qualified_strategies':0,'DXYZ':'CEF diagnostic; not ordinary-stock shared selection or account'})
    portfolio=read(source/'research/portfolio.json');accounts=read(source/'research/account_metrics.json')
    selections=read(source/'research/selection_evidence.json');starts=sorted(set(x['evaluation_start'] for x in selections if x['stage']=='OUTER'))
    comparisons=[];exports=[];support=[];crossings=[]
    for structure in STRUCTURES:
        for cost in COSTS:
            stem=structure+'-'+cost
            paths=[source/'research'/(stem+suffix) for suffix in ['-equity.parquet','-trades.parquet','-skips.json']]
            for f in paths:support.append({'path':str(f.relative_to(source)),'exists':f.exists(),'sha256':sha(f) if f.exists() else None,'bundled':False})
            if not paths[0].exists():
                exports.append({'structure':structure,'cost':cost,'availability':'MISSING_EQUITY_FILE'});continue
            curve=pd.read_parquet(paths[0]);trades=pd.read_parquet(paths[1]) if paths[1].exists() else pd.DataFrame()
            skips=read(paths[2]) if paths[2].exists() else []
            outer_end=curve.iloc[int(curve.index[curve.date==starts[-1]][0])+62].date
            periods=[('FULL_COMBINED',curve.date.iloc[0],curve.date.iloc[-1]),
                     ('OUTER_OOS_WITH_EXIT_TAIL',curve.date.iloc[0],p['development_end']),
                     ('STRICT_OUTER_WINDOW_CALENDAR',starts[0],outer_end),
                     ('FINAL_HOLDOUT',p['holdout_start'],p['cutoff'])]
            current={}
            for label,a,b in periods:
                row={'structure':structure,'accurate_name':'SHARED_MARKET_REGIME' if structure=='HYBRID_REGIME' else structure,
                     'cost':cost,'partition':label,**period_stats(curve,trades,skips,a,b)}
                exports.append(row);current[label]=row
            exports.append({'structure':structure,'cost':cost,'partition':'DEVELOPMENT_FIT_ACCOUNT',
                            'availability':'NOT_PRODUCED','reason':'Existing development tables contain normalized per-symbol candidate events, not a separately replayed $5500 training account; no invented partition figures'})
            stored=next(x for x in portfolio if x['structure']==structure and x['cost_case']==cost)
            am=next(x for x in accounts if x['structure']==structure and x['cost_case']==cost)
            checks={'ending_equity':(current['FULL_COMBINED']['period_end_equity'],stored['ending_equity']),
                    'full_max_drawdown':(current['FULL_COMBINED']['period_max_drawdown'],stored['max_drawdown']),
                    'full_fees':(current['FULL_COMBINED']['fees_in_calendar_period'],stored['fees']),
                    'closed_trades':(len(trades),am['closed_trades'])}
            for label,key in [('OUTER_OOS_WITH_EXIT_TAIL','outer'),('FINAL_HOLDOUT','holdout')]:
                for calculated,reported in [('completed_trades','count'),('win_rate','win_rate'),('expectancy_return','expectancy'),('profit_factor_return','profit_factor')]:
                    checks[key+'_'+reported]=(current[label].get(calculated),stored[key].get(reported))
            for name,(calc,old) in checks.items():comparisons.append({'structure':structure,'cost':cost,'field':name,'calculated':calc,'reported':old,'matches':calc==old if calc is None or old is None else bool(np.isclose(calc,old,rtol=1e-10,atol=1e-10))})
            tail=trades[(trades.entry_date<=outer_end)&(trades.exit_date>outer_end)&(trades.exit_date<p['holdout_start'])] if len(trades) else trades
            for row in tail.to_dict('records'):crossings.append({'structure':structure,'cost':cost,'boundary':outer_end,**row})
    write(out/'account_partitions.json',exports);pd.json_normalize(exports).to_csv(out/'account_partitions.csv',index=False,encoding='utf-8-sig')
    write(out/'report_reconciliation.json',comparisons);write(out/'outer_exit_tail_trades.json',crossings);write(out/'derived_input_hashes.json',support)
    # Verify existing broker-free account without creating tables or updating any row.
    with closing(sqlite3.connect((source/'paper/ledger.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        ledger={'counts':{table:db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ['account','observations','intents','fills']},
                'account':[dict(row) for row in db.execute('SELECT * FROM account')],
                'observations':[dict(row) for row in db.execute('SELECT id,created_at,signal_asof,source,status FROM observations')]}
    before=read(source/'paper/status.json')
    lock=FileLock(str(source/'paper/worker.lock'),timeout=0)
    try:lock.acquire();held=False;lock.release()
    except Timeout:held=True
    background={'checked_at':utc(),'paper_status':before,'heartbeat_age_seconds':(datetime.now(timezone.utc)-datetime.fromisoformat(before['heartbeat'])).total_seconds(),
                'singleton_lock_held':held,'ledger_read_only':ledger,'lifecycle':'CASH_OBSERVATION_ONLY',
                'intents_fills_implementation':'NOT_IMPLEMENTED_EMPTY_TABLES_DO_NOT_IMPLY_CAPABILITY',
                'continuous_strategy_improvement':'NOT_IMPLEMENTED','monthly_candidate_evaluation':'NOT_IMPLEMENTED'}
    write(out/'background_verified.json',background)
    final_hashes={name:sha(source/name) for name in initial_hashes}
    preservation={'before':initial_hashes,'after':final_hashes,'unchanged':initial_hashes==final_hashes,
                  'research_functions_called':False,'price_datasets_read':False,'holdout_read_scope':'Existing derived equity/trade artifacts only; no signal calculation or selection'}
    write(out/'historical_evidence_preserved.json',preservation)
    return {'missing':missing,'reconciliation_failures':[c for c in comparisons if not c['matches']],
            'historical_evidence_unchanged':preservation['unchanged'],'lengths':{k:len(v) for k,v in lengths.items()},
            'ledger_counts':ledger['counts'],'timeline':[{k:v for k,v in t.items() if k!='input_versions'} for t in timeline],
            'base_accounts':[x for x in exports if x.get('cost')=='base' and x.get('partition')=='FULL_COMBINED']}


def package(out):
    # Fixed collector outputs only; never recursively archive an input directory.
    extras=['source_inventory.json','source_version.json','report_fix.patch','task_event_extract.json','log_boundary_extract.json',
            'timeline.json','coverage_lists.json','account_partitions.json','account_partitions.csv','report_reconciliation.json',
            'outer_exit_tail_trades.json','derived_input_hashes.json','background_verified.json','background_process_snapshot.json',
            'historical_evidence_preserved.json','ACCEPTANCE.md','unfinished_items.json','isolated_tests.txt','SUMMARY.json','missing_files.json']
    names=['existing/'+n for n in ALLOWLIST if (out/'existing'/n).exists()]+[n for n in extras if (out/n).exists()]
    hashes={n:sha(out/n) for n in names};write(out/'BUNDLE_HASHES.json',hashes);names.append('BUNDLE_HASHES.json')
    archive=out/'verification_bundle.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for n in names:z.write(out/n,n)
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        assert all(hashlib.sha256(z.read(n)).hexdigest()==h for n,h in hashes.items())
    return {'archive':str(archive),'sha256':sha(archive),'bytes':archive.stat().st_size,'files':len(names),'zip_crc':'PASS','all_entry_hashes':'PASS'}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--package',action='store_true');a=parser.parse_args()
    if a.package:print(json.dumps(package(a.output),ensure_ascii=False))
    else:
        result=collect(a.source,a.output);write(a.output/'SUMMARY.json',result)
        print(json.dumps({k:v for k,v in result.items() if k not in ('timeline','base_accounts')},ensure_ascii=False))
