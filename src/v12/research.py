"""Five fixed factors, fifteen combinations per candidate; no adaptive search."""
from pathlib import Path
import numpy as np
import pandas as pd
from .runtime import *
from .data import load,frame,label_frame
from .statistics import inference,by_adjust

def conditional():
    p=freeze();snap=read(root()/'input_snapshot.json');benchmark=load('SPY','all')
    bench=benchmark.set_index('trade_date').reindex(p['days']) if len(benchmark) else pd.DataFrame()
    out=root()/'conditional_cache';out.mkdir(exist_ok=True);summaries=[];all_windows=[];coverage=[];evaluation=[]
    for rec in snap['universe']['records']:
        check_deadline();s=rec['symbol'];cache=out/f'{s}.json';event_path=out/f'{s}-evaluation.parquet'
        if cache.exists():
            item=read(cache);summaries.extend(item['summaries']);all_windows.extend(item['windows']);coverage.append(item['coverage'])
            if event_path.exists():evaluation.append(pd.read_parquet(event_path))
            continue
        symbol_rows=[];window_rows=[];ev=[]
        try:d,raw,issues=frame(s,benchmark)
        except Exception as exc:d=pd.DataFrame();raw=pd.DataFrame();issues=[type(exc).__name__]
        coverage_row={'symbol':s,'type':rec['type'],'identity_version':rec['identity_version'],'rows':len(raw),
                      'start':raw.trade_date.min() if len(raw) else None,'end':raw.trade_date.max() if len(raw) else None,'issues':issues,
                      'status':'REUSED_REAL_SIP_CACHE' if len(raw) else 'NO_DEVELOPMENT_DATA'}
        for h in p['horizons']:
            labels=label_frame(d,bench,h) if len(d) else pd.DataFrame()
            for factor in FACTORS:
                test_parts=[]
                if len(d):
                    base=labels.copy();base['factor']=d[factor];base=base.dropna(subset=['label','factor'])
                    for w in p['windows']:
                        a,b=w['train'];train=base[(base.position>=a)&(base.position+h<b)]
                        valid=len(train)>=p['minimum_training_events'] and train.factor.nunique()>1
                        q1,q2=train.factor.quantile([1/3,2/3]).tolist() if valid else (None,None)
                        valid=valid and q1<q2
                        for stage,span in [('TRAIN',w['train']),('EVALUATION',w['evaluation'])]:
                            meta={'symbol':s,'factor':factor,'horizon':h,'window':w['id'],'stage':stage,
                                  'window_start':p['days'][span[0]],'window_end':p['days'][span[1]-1],
                                  'training_q1':q1,'training_q2':q2,'training_events':len(train)}
                            if not valid:
                                window_rows.append({**meta,'bucket':'ALL','status':'INSUFFICIENT_TRAINING','events':0});continue
                            a,b=span;sample=base[(base.position>=a)&(base.position+h<b)].copy()
                            sample['bucket']=np.where(sample.factor<=q1,'weak',np.where(sample.factor<=q2,'medium','strong'))
                            rows,summary=inference(sample,h,span)
                            for row in rows:window_rows.append({**meta,**row,**{'window_'+k:v for k,v in summary.items() if k not in ('events','status')}})
                            if stage=='EVALUATION' and len(sample):
                                sample['symbol']=s;sample['factor_name']=factor;sample['horizon']=h;sample['window']=w['id'];test_parts.append(sample)
                combined=pd.concat(test_parts) if test_parts else pd.DataFrame()
                row={'symbol':s,'security_type':rec['type'],'factor':factor,'horizon':h,'status':'INSUFFICIENT_EVIDENCE','events':0,
                     'time_series_rank_ic':None,'ic_p':None,'strong_minus_weak':None,'spread_p':None,
                     'scope':'CEF_SEPARATE' if s=='DXYZ' else 'SINGLE_STOCK_TIME_SERIES'}
                if len(combined):
                    _,summary=inference(combined,h,[p['windows'][0]['evaluation'][0],len(p['days'])]);row.update(summary)
                    for bucket in ['weak','medium','strong']:
                        a=combined[combined.bucket==bucket];row[bucket+'_events']=len(a);row[bucket+'_mean']=float(a.label.mean()) if len(a) else None
                    ev.append(combined)
                symbol_rows.append(row)
        item={'summaries':symbol_rows,'windows':window_rows,'coverage':coverage_row}
        write(cache,item)
        if ev:
            e=pd.concat(ev);e.to_parquet(event_path,index=True);evaluation.append(e)
        summaries.extend(symbol_rows);all_windows.extend(window_rows);coverage.append(coverage_row)
        state('CONDITIONAL_RESEARCH',symbol=s,completed=len(coverage),total=66)
    assert len(summaries)==990
    pvals=[r.get(k) for r in summaries for k in ['ic_p','spread_p']];adjusted=by_adjust(pvals)
    for i,r in enumerate(summaries):
        r['ic_q_by']=float(adjusted[2*i]);r['spread_q_by']=float(adjusted[2*i+1]);r['multiplicity_family']=1980
        r['fdr_evidence']=r['ic_q_by']<=.05 or r['spread_q_by']<=.05
    save_csv('momentum_per_symbol.csv',summaries);write(root()/'momentum_per_symbol.json',{'metadata':tags(),'results':summaries})
    save_csv('momentum_window_results.csv',all_windows);write(root()/'data_coverage.json',coverage)
    # Same-day securities remain together. Equal weight the available securities per date.
    shared=[]
    if evaluation:
        e=pd.concat(evaluation);e=e[e.symbol!='DXYZ']
        for factor in FACTORS:
            for h in p['horizons']:
                part=e[(e.factor_name==factor)&(e.horizon==h)]
                if part.empty:shared.append({'factor':factor,'horizon':h,'status':'INSUFFICIENT_EVIDENCE'});continue
                # Average within each date/bucket before common-date resampling: no false independent stock count.
                date_bucket=part.groupby(['position','bucket'],as_index=False,observed=True)[['label','factor','excess_spy']].mean()
                date_bucket.index=[p['days'][int(i)] for i in date_bucket.position]
                _,summary=inference(date_bucket,h,[p['windows'][0]['evaluation'][0],len(p['days'])])
                summary['time_series_rank_ic']=None;summary['ic_p']=None;summary['ic_ci_low']=None;summary['ic_ci_high']=None
                shared.append({'factor':factor,'horizon':h,**summary,'status':'DESCRIPTIVE_SHARED_DATE_BUCKET_SUMMARY_NO_CROSS_SECTIONAL_IC'})
    save_csv('momentum_shared_summary.csv',shared)
    state('CONDITIONAL_COMPLETE',comparisons=990)
