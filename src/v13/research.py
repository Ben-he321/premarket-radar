import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from .runtime import *
from .data import label_frame, condition
from .statistics import distribution, nonoverlap

def interval(boot):
    valid=boot[np.isfinite(boot)]
    return tuple(float(x) for x in np.quantile(valid,[.025,.975])) if len(valid)>=9000 else (None,None)

def compare(sample, h):
    if sample.empty:return [{'group':g,'status':'INSUFFICIENT_EVIDENCE','events':0} for g in ['CONDITION','NOT_CONDITION','UNCONDITIONAL']]
    matrix=[];selected=sample.accepted.to_numpy(bool)
    for mask in [selected,~selected,np.ones(len(sample),bool)]:
        matrix.extend([np.where(mask,sample.label,np.nan),np.where(mask,sample.excess_spy,np.nan)])
    estimates,boot,_=distribution(np.array(matrix).T,sample.position.to_numpy())
    rows=[]
    for j,(group,mask) in enumerate(zip(['CONDITION','NOT_CONDITION','UNCONDITIONAL'],[selected,~selected,np.ones(len(sample),bool)])):
        part=sample[mask];blocks=int((part.position//60).nunique());enough=len(part)>=20 and blocks>=8
        lo,hi=interval(boot[:,2*j]) if enough else (None,None)
        elo,ehi=interval(boot[:,2*j+1]) if enough else (None,None)
        dlo,dhi=interval(boot[:,2*j]-boot[:,4]) if enough else (None,None)
        rows.append({'group':group,'events':len(part),'spy_paired_events':int(part.excess_spy.notna().sum()),
                     'nonoverlap_labels':nonoverlap(part.position.tolist(),h),'date_blocks':blocks,
                     'mean_label':float(part.label.mean()) if len(part) else None,'mean_spy_excess':float(part.excess_spy.mean()) if part.excess_spy.notna().any() else None,
                     'ci_low':lo,'ci_high':hi,'spy_ci_low':elo,'spy_ci_high':ehi,
                     'mean_minus_unconditional':float(estimates[2*j]-estimates[4]) if len(part) else None,'delta_ci_low':dlo,'delta_ci_high':dhi,
                     'status':'DESCRIPTIVE_ONLY' if enough else 'INSUFFICIENT_EVIDENCE'})
    return rows

def run(data, benchmark):
    p=protocol();dest=root()/'conditional_cache';dest.mkdir(exist_ok=True)
    summaries=[];descriptive=[];events=[];exclusions=[]
    bench=benchmark.set_index('trade_date').reindex(p['days']) if len(benchmark) else pd.DataFrame()
    for rec in read(root()/'input_snapshot.json')['universe']['records']:
        check_deadline();s=rec['symbol'];cache=dest/f'{s}.json';eventpath=dest/f'{s}.parquet'
        if cache.exists():
            item=read(cache);summaries+=item['conditions'];descriptive+=item['descriptive'];exclusions+=item['exclusions']
            if eventpath.exists():events.append(pd.read_parquet(eventpath))
            continue
        rows=[];desc=[];ev=[];excluded=[];item=data.get(s)
        for h in p['horizons']:
            base=pd.DataFrame()
            if item:
                f=item['features'];labels=label_frame(f,bench,h);eligible=item['eligible'].copy()
                mature=np.zeros(len(f),bool)
                for w in p['windows']:
                    a,b=w['evaluation'];mature[a:b-h]=True
                known=eligible&mature
                excluded.append({'symbol':s,'horizon':h,'eligible_mature_signals':int(known.sum()),
                                 'future_incomplete_label_excluded':int((known&labels.label.isna()).sum()),'account_intents_affected':False})
                base=labels[known&labels.label.notna()].copy()
                for factor in FACTORS:
                    base[factor]=f[factor]
            for factor in FACTORS:
                part=base.dropna(subset=[factor,'label']) if len(base) else pd.DataFrame()
                desc.append({'symbol':s,'factor':factor,'horizon':h,'events':len(part),
                             'time_series_rank_ic':float(spearmanr(part[factor],part.label).statistic) if len(part)>=20 and part[factor].nunique()>1 else None,
                             'nonoverlap_labels':nonoverlap(part.position.tolist(),h) if len(part) else 0,
                             'date_blocks':int((part.position//60).nunique()) if len(part) else 0,
                             'status':'DESCRIPTIVE_RANK_NOT_CALIBRATED' if len(part)>=20 else 'INSUFFICIENT_EVIDENCE',
                             'scope':'CEF_SEPARATE' if s=='DXYZ' else 'SINGLE_STOCK_NO_PROMOTION'})
            for c in CONDITIONS:
                part=base.copy()
                if len(part):
                    part['accepted']=condition(part,c);part['symbol']=s;part['condition']=c;part['horizon']=h;ev.append(part.reset_index(names='signal_date'))
                for row in compare(part,h):rows.append({'symbol':s,'condition':c,'horizon':h,'scope':'CEF_SEPARATE' if s=='DXYZ' else 'SINGLE_STOCK',**row})
        # Pandas may emit NaN from constant rank correlation; JSON must remain strict.
        for row in desc:
            if row['time_series_rank_ic'] is not None and not np.isfinite(row['time_series_rank_ic']):row['time_series_rank_ic']=None
        write(cache,{'conditions':rows,'descriptive':desc,'exclusions':excluded})
        if ev:
            e=pd.concat(ev,ignore_index=True);e.to_parquet(eventpath,index=False);events.append(e)
        summaries+=rows;descriptive+=desc;exclusions+=excluded
        state('REAL_PER_STOCK_CONDITIONS',symbol=s,completed=len(summaries)//27,total=66)
    save_csv('per_stock_conditions.csv',summaries);save_csv('descriptive_factors_990.csv',descriptive)
    save_csv('statistical_label_exclusions.csv',exclusions)
    shared=[];daily=[]
    if events:
        e=pd.concat(events,ignore_index=True)
        allowed={s for s,x in data.items() if s!='DXYZ' and x['record']['status']=='INCLUDED'}
        e=e[e.symbol.isin(allowed)]
        for (c,h),g in e.groupby(['condition','horizon']):
            a=g[g.accepted].groupby('position')[['label','excess_spy']].mean()
            b=g[~g.accepted].groupby('position')[['label','excess_spy']].mean()
            u=g.groupby('position')[['label','excess_spy']].mean()
            common=a.index.intersection(b.index).intersection(u.index)
            x=np.column_stack([a.loc[common,'label'],b.loc[common,'label'],u.loc[common,'label'],a.loc[common,'excess_spy'],b.loc[common,'excess_spy'],u.loc[common,'excess_spy']])
            est,boot,blocks=distribution(x,common.to_numpy())
            for j,name in enumerate(['CONDITION','NOT_CONDITION','UNCONDITIONAL']):
                lo,hi=interval(boot[:,j]);dlo,dhi=interval(boot[:,j]-boot[:,2]);elo,ehi=interval(boot[:,j+3])
                shared.append({'condition':c,'horizon':h,'group':name,'paired_dates':len(common),'date_blocks':blocks,
                               'mean_label':float(est[j]),'mean_spy_excess':float(est[j+3]),'ci_low':lo,'ci_high':hi,
                               'spy_ci_low':elo,'spy_ci_high':ehi,'delta_unconditional':float(est[j]-est[2]),'delta_ci_low':dlo,'delta_ci_high':dhi,
                               'status':'DESCRIPTIVE_EQUAL_DATE_EQUAL_AVAILABLE_STOCK_WEIGHT_NO_ACCOUNT'})
            for i,pos in enumerate(common):daily.append({'condition':c,'horizon':h,'signal_date':p['days'][pos],
                'condition_label':x[i,0],'not_condition_label':x[i,1],'unconditional_label':x[i,2],
                'condition_spy_excess':x[i,3],'not_condition_spy_excess':x[i,4],'unconditional_spy_excess':x[i,5]})
    save_csv('shared_conditions.csv',shared);save_csv('shared_condition_daily.csv',daily)
    assert len(summaries)==1782 and len(descriptive)==990
