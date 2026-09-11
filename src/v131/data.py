"""Pinned real cache, cutoff before features. No download or synthetic fallback."""
from functools import lru_cache
import numpy as np
import pandas as pd
from src.v11.data import classify_fields
from src.watchlist.features import feature_frame
from src.v12.data import label_frame
from .runtime import *

@lru_cache(maxsize=150)
def load(symbol, adjustment):
    snap=read(root()/'input_snapshot.json');parts=[]
    for c in snap['manifests'].get(symbol,{}).get(adjustment,{}).get('chunks',{}).values():
        if c['status']!='OK' or c['start']>CUTOFF:continue
        path=source()/c['path']
        if sha(path)!=c['sha256']:raise ValueError('PINNED_OBJECT_CHANGED_'+symbol)
        f=pd.read_parquet(path,filters=[('trade_date','<=',CUTOFF)])
        if len(f):parts.append(f)
    if not parts:return pd.DataFrame()
    f=pd.concat(parts,ignore_index=True).sort_values('trade_date')
    if f.duplicated(['symbol','trade_date']).any():raise ValueError('DUPLICATE_PINNED_ROWS_'+symbol)
    return classify_fields(f)

def condition(values, name):
    factor, direction=CONDITIONS[name]
    return values[factor]>0 if direction=='gt' else values[factor]<0

def eligibility(f, raw, p):
    """Nothing here reads a future row, future label or corporate-action date."""
    current=f[list({v[0] for v in CONDITIONS.values()})].notna().all(axis=1)&f.observed
    ri=raw.set_index('trade_date').reindex(f.index)
    current &= ri.execution_eligible.fillna(False).astype(bool)
    eligible=np.zeros(len(f),dtype=bool);prob={c:np.full(len(f),np.nan) for c in CONDITIONS};training=[]
    for w in p['windows']:
        a,b=w['train'];start,end=w['evaluation'];train=current.iloc[a:b]
        n=int(train.sum());valid=n>=p['minimum_training_observations']
        if valid:eligible[start:end]=current.iloc[start:end]
        for c in CONDITIONS:
            q=float(condition(f.iloc[a:b],c)[train].mean()) if n else None
            training.append({'window':w['id'],'condition':c,'train_start':p['days'][a],'train_end':p['days'][b-1],
                             'observations':n,'acceptance_probability':q,'qualified':valid})
            if valid:prob[c][start:end]=q
    return eligible,prob,training

def prepare():
    p=protocol();snap=read(root()/'input_snapshot.json');bench=load('SPY','all');rows=[];result={};training=[]
    bench=bench[bench.execution_eligible] if len(bench) else bench
    for rec in snap['universe']['records']:
        check_deadline();s=rec['symbol'];issues=[]
        try:
            raw=load(s,'raw');adj=load(s,'all')
            if raw.empty or adj.empty:raise LookupError('NO_DEVELOPMENT_DATA')
            good=adj[adj.execution_eligible].copy()
            # Known signal-day alignment only; never erase an order using its future path.
            ri=raw.set_index('trade_date');ai=good.set_index('trade_date');joined=ri.index.intersection(ai.index)
            scale=ri.loc[joined,'close']/ai.loc[joined,'close'];bad=pd.Series(False,index=joined)
            for k in ['open','high','low']:
                bad |= ((ri.loc[joined,k]/ai.loc[joined,k])/scale-1).abs()>.01
            bad_dates=set(bad[bad].index)
            if bad_dates:issues.append({'code':'SIGNAL_DATE_RAW_ALL_SCALE_MISMATCH','dates':sorted(bad_dates)})
            good=good[good.trade_date.isin(joined)&~good.trade_date.isin(bad_dates)]
            f=feature_frame(good,bench).reindex(p['days']);f['observed']=f.observed.fillna(False).astype(bool)
            eligible,prob,train=eligibility(f,raw,p)
            for x in train:training.append({'symbol':s,**x})
            result[s]={'features':f,'raw':raw,'eligible':eligible,'probability':prob,'record':rec}
            rows.append({'symbol':s,'type':rec['type'],'identity_status':rec['status'],'identity_version':rec['identity_version'],
                         'raw_rows':len(raw),'start':str(raw.trade_date.min()),'end':str(raw.trade_date.max()),
                         'eligible_signal_days':int(eligible.sum()),'status':'AVAILABLE' if eligible.any() else 'INSUFFICIENT_TRAINING',
                         'scope':'CEF_DIAGNOSTIC_ONLY' if s=='DXYZ' else 'STOCK' if rec['status']=='INCLUDED' else 'IDENTITY_EXCLUDED','issues':issues})
        except Exception as exc:
            rows.append({'symbol':s,'type':rec['type'],'identity_status':rec['status'],'raw_rows':0,'eligible_signal_days':0,
                         'status':'NO_DATA' if isinstance(exc,LookupError) else 'DATA_ERROR','issues':[type(exc).__name__,str(exc)]})
    assert len(rows)==66
    write(root()/'data_coverage.json',rows);save_csv('frozen_training_probabilities.csv',training)
    return result,bench

from .identity import review as complex_actions
