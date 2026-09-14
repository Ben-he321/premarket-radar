"""Freeze observed continuous inputs and all66 daily coverage before results."""
from __future__ import annotations
import json
import pandas as pd
from .portfolio import ContinuousInputs
from .runtime import ROOT, START, END, TAIL_END, WARMUP, read, write, sha, utc
from .data import DATA, frame_qc

def build(rebuild=False):
    target=ROOT/'INPUT_CAPABILITY.json'
    if target.exists() and not rebuild:return read(target)
    inputs=ContinuousInputs(ROOT/'earnings/PRIMARY_EARNINGS_B12_V2.json')
    earnings=pd.read_csv(ROOT/'earnings/EARNINGS_DAILY_COVERAGE_V2.csv')
    days=[d for d in inputs.clocks if START<=d<=TAIL_END]
    rows=[];all66=[]
    for s,u in inputs.universe.items():
        f=inputs.daily.get(s,pd.DataFrame());h=inputs.histories.get(s,{})
        first=str(f.index.min()) if len(f) else None
        last=str(f.index.max()) if len(f) else None
        for d in days:
            observed=len(f)>0 and d in f.index
            history=int((f.index<=d).sum()) if len(f) else 0
            if u['scope']!='KEEP':reason='BUSINESS_SCOPE_EXCLUDED' if u['scope'].startswith('EXCLUDE') else 'IDENTITY_OR_SCOPE_UNKNOWN'
            elif not observed:reason='BEFORE_FIRST_OBSERVED_PRICE_NOT_VERIFIED_LISTING' if first and d<first else 'PRICE_MISSING_UNKNOWN_NOT_ZERO'
            elif history<100:reason='INDICATOR_WARMUP_INSUFFICIENT'
            elif not bool(f.loc[d,'regular_session_verified']):reason='RECENT_SESSION_GAP_RTH_BASIS_UNKNOWN'
            elif not bool(f.loc[d,'action_units_verified']):reason='ACTION_UNITS_UNKNOWN'
            else:reason='VENDOR_STANDARD_PRICE_INPUT_AVAILABLE_NOT_INDEPENDENT_PIT'
            rows.append({'symbol':s,'trade_date':d,'scope_policy':u['scope'],'quarter_entry_window':d<=END,
                         'price_present':bool(observed),'valid_observed_warmup_sessions':history,'price_coverage_reason':reason,
                         'price_input_eligible':reason=='VENDOR_STANDARD_PRICE_INPUT_AVAILABLE_NOT_INDEPENDENT_PIT',
                         'historical_network_received_at':'UNKNOWN','final_daily_availability':'CLOSE_PLUS1_MODEL_ASSUMPTION',
                         'next_real_quote_and_cash_qualification':'ENGINE_RECOMPUTES_AT_ACTUAL_EVENT_TIME'})
        all66.append({'symbol':s,'scope_policy':u['scope'],'first_observed_price':first,'last_observed_price':last,
                      'observed_first_is_verified_listing':False,'quarter_rows':int(f.index.to_series().between(START,END).sum()) if len(f) else 0,
                      'warmup_rows_before_start':int((f.index<START).sum()) if len(f) else 0,
                      'history_request_complete':h.get('complete',False),'query_symbol':h.get('query_symbol',s),
                      'new_or_reused':h.get('reuse','NEW_B12_TARGETED'),'history_path':h.get('rth',{}).get('path'),
                      'price_basis':'VENDOR_STANDARD_RTH_MINUTES_PLUS_OFFICIAL_CLOSE','scope_is_historical_pit':False})
    table=pd.DataFrame(rows)
    # Preserve separate earnings columns; no unknown factual date is promoted.
    datecol=next((c for c in ('trade_date','trade_date_ny','date','day') if c in earnings),None)
    if datecol is None:raise ValueError('EARNINGS_COVERAGE_DATE_COLUMN_UNKNOWN')
    if datecol:
        e=earnings.rename(columns={datecol:'trade_date'})
        e=e.rename(columns={c:'earnings_'+c for c in e if c not in ('symbol','trade_date')})
        table=table.merge(e,on=['symbol','trade_date'],how='left',validate='one_to_one')
    table.to_csv(ROOT/'ALL66_DAILY_INPUT_QUALIFICATION.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(all66).to_csv(ROOT/'ALL66_INPUT_COVERAGE.csv',index=False,encoding='utf-8-sig')
    result={'frozen_at':utc(),'status':'COVERAGE_LIMITED','candidate_count':len(inputs.universe),'keep_count':sum(u['scope']=='KEEP' for u in inputs.universe.values()),
       'quarter':[START,END],'tail_end':TAIL_END,'daily_qualification_rows':len(table),
       'all66_retained':len(all66)==66,'histories_completed':sum(h.get('complete',False) for h in inputs.histories.values()),
       'securities_with_quarter_prices':sum(r['quarter_rows']>0 for r in all66),'quarter_session_count':sum(START<=d<=END for d in days),
       'price_qualified_symbol_sessions':int(table[table.trade_date<=END].price_input_eligible.sum()),
       'empty_response_not_no_earnings_or_no_trades':True,'earnings_evidence_path':str(inputs.earnings_path),'earnings_sha256':sha(inputs.earnings_path),
       'actual_corporate_action_access':inputs.actions['status'],'source_files':inputs.pinned,
       'limitations':['Not independently verified per-point close/auction quantity; secondary rank volume unknown outside inherited checked dates.',
          'A only documented public plan versions; B bounded retrospective ordinary earnings exclusions; unscheduled completeness not established.',
          'HistoricalL1 model and close+1 data assumption do not prove Basic realtime execution; no forward launched.',
          'Current business scope applied to historical dates; missing candidates can change shared-capital competition.'],
       'study_allowed':'ACTUAL_COVERAGE_LIMITED_COMMON_P50_DIAGNOSTIC','full_pool_performance_certified':False}
    write(target,result)
    return result

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--rebuild',action='store_true');args=parser.parse_args()
    result=build(args.rebuild);print(json.dumps({k:result[k] for k in ['status','candidate_count','histories_completed','securities_with_quarter_prices','price_qualified_symbol_sessions']}))
