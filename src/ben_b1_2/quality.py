"""Read-only audit of actual B1.2 market objects; no imputation or downloads."""
from __future__ import annotations
import gc
import json
from pathlib import Path
import pandas as pd
from .runtime import ROOT, START, END, TAIL_END, WARMUP, read, write, sha, utc
from .data import DATA, frame_qc, query_ticker

def run():
    out=ROOT/'quality';out.mkdir(parents=True,exist_ok=True)
    rows=[];hashes={};mapping=[]
    for h in read(DATA/'HISTORY_INPUTS.json'):
        s=h['symbol']
        for a,v in h.get('daily',{}).items():
            p=Path(v['path']);hashes[str(p)]=sha(p)
            f=pd.read_parquet(p)
            f=f[f.trade_date.between(WARMUP,TAIL_END)] if len(f) else f
            rows.append({'symbol':s,'object':'daily_'+a,'path':str(p),**frame_qc(f),
                         'first':str(f.trade_date.min()) if len(f) else None,'last':str(f.trade_date.max()) if len(f) else None})
            if not len(f):continue
            request_symbol=v['receipt'].get('params',{}).get('symbols')
            mapping.append({'symbol':s,'object':'daily_'+a,'requested_historical_ticker':request_symbol,
                'expected_in_quarter':query_ticker(s),'match':request_symbol==query_ticker(s),
                'asof':v['receipt'].get('params',{}).get('asof'),
                'ticker_rename_tail_gaps_are_not_imputed':True})
        v=h.get('minutes')
        if not v:continue
        p=Path(v['path']);hashes[str(p)]=sha(p)
        f=pd.read_parquet(p)
        f=f[f.trade_date.between(WARMUP,TAIL_END)] if len(f) else f
        rows.append({'symbol':s,'object':'minutes_raw','path':str(p),**frame_qc(f),
                     'first':str(f.trade_date.min()) if len(f) else None,'last':str(f.trade_date.max()) if len(f) else None})
        del f;gc.collect()
    pd.DataFrame(rows).to_csv(out/'MARKET_OBJECT_QUALITY.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(mapping).to_csv(out/'HISTORICAL_QUERY_MAPPING.csv',index=False,encoding='utf-8-sig')
    actions=read(DATA/'corporate_actions.json')
    specific={'symbol':'NOW','action':'5_FOR_1_SPLIT','ex_date':'2025-12-18','old_rate':1,'new_rate':5,
        'issuer_source':'https://investor.servicenow.com/news/news-details/2025/ServiceNow-Shareholders-Approve-5-for-1-Stock-Split/default.aspx',
        'sec_source':'https://www.sec.gov/Archives/edgar/data/1373715/000137371525000321/now-20251205.htm',
        'reviewed_at':utc(),'historical_network_received_at':'UNKNOWN',
        'handling':'Original raw past bars converted by SPLIT event at effective-date boundary; no future split applied before its date.',
        'note':'Corporate effective after Dec17 close versus ex/trading date Dec18 are distinct; vendor ex-date agrees with announced split-adjusted trading start.'}
    write(out/'NOW_SPLIT_UNIT_REVIEW.json',specific)
    summary={'at':utc(),'checked_objects':len(rows),'rows':sum(r['rows'] for r in rows),
       'duplicates':sum(r['duplicates'] for r in rows),'invalid_ohlc':sum(r['invalid_ohlc'] for r in rows),
       'missing_volume':sum(r['missing_volume'] for r in rows),'query_mapping_mismatches':[r for r in mapping if not r['match']],
       'empty_objects':[{'symbol':r['symbol'],'object':r['object']} for r in rows if r['rows']==0],
       'company_action_api_status':actions['status'],'no_data_changed':True,'synthetic':False,
       'limitations':['An absent minute can reflect no eligible trade; it is not automatically classified as a halt.',
                      'Empty API results do not prove listing date; first observed price is not independently verified listing.',
                      'RTH price is reconstructed from actual regular-session minutes plus vendor official close; full per-point auction audit NOT_DONE.',
                      'Complete request does not establish complete vendor historical coverage or exact historical delivery latency.']}
    write(out/'QUALITY_SUMMARY.json',summary);write(out/'INPUT_HASHES.json',hashes)
    print(json.dumps({k:summary[k] for k in ['checked_objects','rows','duplicates','invalid_ohlc','missing_volume','query_mapping_mismatches']}))
    return summary

if __name__=='__main__':run()
