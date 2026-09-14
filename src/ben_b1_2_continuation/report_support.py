"""Read-only coverage counts and existing benchmark evidence validation."""
from collections import defaultdict
from pathlib import Path
import pandas as pd
import pandas_market_calendars as mcal
from .runtime import read,sha

def coverage_summary(records,completed_day):
    groups={};pending=[];all_keys=set()
    for row in records:
        day=row.get('day')
        if not day or day>completed_day:pending.append(row);continue
        key=(row.get('purpose','UNKNOWN'),row.get('status','UNKNOWN'),str(row.get('complete')))
        group=groups.setdefault(key,{'purpose':key[0],'status':key[1],'complete':row.get('complete'),
            'records':0,'keys':set(),'symbols':set(),'reported_rows':0,'unknown_row_count_records':0})
        identity=(row.get('symbol'),day,row.get('purpose'))
        group['records']+=1;group['keys'].add(identity);all_keys.add(identity);group['symbols'].add(row.get('symbol','UNKNOWN'))
        if isinstance(row.get('rows'),int):group['reported_rows']+=row['rows']
        else:group['unknown_row_count_records']+=1
    output=[]
    for _,group in sorted(groups.items()):
        group['unique_symbol_day_purpose']=len(group.pop('keys'));group['symbols']=sorted(group['symbols']);output.append(group)
    return {'completed_day':completed_day,'durable_records':sum(r['records'] for r in output),
        'unique_symbol_day_purpose':len(all_keys),'groups':output,'not_yet_durable_records':pending,
        'reported_row_counts_are_not_independent_coverage_samples':True,
        'complete_means_request_pagination_not_independent_exchange_completeness':True,
        'not_requested_earnings_unknown_is_not_empty_response_or_api_failure':True,
        'unique_counts_across_different_status_groups_may_overlap':True}

def verified_benchmarks(directory,start,end):
    directory=Path(directory).resolve();complete=read(directory/'COMPLETE.json');checks=[]
    for row in complete['outputs']:
        p=(directory/row['path'].replace('\\','/')).resolve()
        if not p.is_relative_to(directory):raise ValueError('BENCHMARK_MANIFEST_PATH_ESCAPE')
        checks.append({'path':str(p),'expected':row['sha256'],'actual':sha(p)})
    if any(r['expected']!=r['actual'] for r in checks):raise ValueError('BENCHMARK_EXISTING_COMPLETION_HASH_MISMATCH')
    summary=read(directory/'SUMMARY.json');by_symbol={r['symbol']:r for r in summary}
    if len(summary)!=2 or set(by_symbol)!={'SPY','QQQ'}:raise ValueError('BENCHMARK_SYMBOLS_NOT_UNIQUE_EXPECTED_TWO')
    expected=[str(d.date()) for d in mcal.get_calendar('NYSE').schedule(start,end).index]
    details=[]
    for symbol in ('SPY','QQQ'):
        value=by_symbol[symbol];daily=pd.read_csv(directory/symbol/'daily.csv')
        if (value['start'],value['end'],value['initial_capital_usd'])!=(start,end,5500):raise ValueError('BENCHMARK_INTERVAL_OR_CAPITAL_MISMATCH')
        if value['status']!='COMPUTED_REAL_INPUT_MODEL_EXECUTION' or value['issues']:raise ValueError('BENCHMARK_NOT_QUALIFIED')
        if list(daily.trade_date)!=expected or set(daily.symbol)!={symbol}:raise ValueError('BENCHMARK_SESSION_CALENDAR_MISMATCH')
        if not pd.to_numeric(daily.equity_usd,errors='coerce').map(lambda x:pd.notna(x) and 0<=x<float('inf')).all():raise ValueError('BENCHMARK_NONFINITE_EQUITY')
        if abs(daily.iloc[-1].equity_usd-value['end_equity_usd'])>.001:raise ValueError('BENCHMARK_FINAL_EQUITY_MISMATCH')
        details.append({'symbol':symbol,'start':start,'end':end,'sessions':len(daily),'initial_capital':5500,'end_equity':value['end_equity_usd'],'status':'PASS'})
    return by_symbol,{'status':'PASS','recomputed':False,'checks':checks,'symbols':details,'complete_manifest_sha256':sha(directory/'COMPLETE.json')}
