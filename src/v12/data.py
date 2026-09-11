"""Pinned cached data only. Never invoke downloads or V1/V1.1 report writers."""
from functools import lru_cache
import hashlib
import pandas as pd
import numpy as np
from src.v11.data import classify_fields
from src.watchlist.features import feature_frame
from .runtime import freeze,root,source,read,write,CUTOFF

@lru_cache(maxsize=150)
def load(symbol,adj):
    snap=read(root()/'input_snapshot.json');m=snap['manifests'][symbol][adj];parts=[]
    for c in m['chunks'].values():
        if c['status']!='OK' or c['start']>CUTOFF:continue
        path=source()/c['path']
        if hashlib.sha256(path.read_bytes()).hexdigest()!=c['sha256']:raise ValueError('PINNED_OBJECT_HASH_MISMATCH')
        f=pd.read_parquet(path,filters=[('trade_date','<=',CUTOFF)])
        if len(f):parts.append(f)
    if not parts:return pd.DataFrame()
    f=pd.concat(parts,ignore_index=True).sort_values('trade_date')
    if f.duplicated(['symbol','trade_date']).any():raise ValueError('DUPLICATE_PINNED_ROWS')
    assert f.trade_date.max()<=CUTOFF
    f=classify_fields(f);return f[f.execution_eligible].copy()

def aligned(symbol):
    raw=load(symbol,'raw');all_=load(symbol,'all');issues=[]
    if raw.empty or all_.empty:return raw,all_,['NO_DEVELOPMENT_DATA']
    shared=set(raw.trade_date)&set(all_.trade_date)
    if set(raw.trade_date)!=set(all_.trade_date):issues.append('RAW_ALL_DATE_MISMATCH_EXCLUDED')
    raw=raw[raw.trade_date.isin(shared)].copy();all_=all_[all_.trade_date.isin(shared)].copy()
    a=all_.set_index('trade_date');r=raw.set_index('trade_date');scale=r.close/a.close
    mismatch=pd.Series(False,index=a.index)
    for key in ['open','high','low']:
        mismatch|=((r[key]/a[key])/scale-1).abs()>.01
    if mismatch.any():
        issues.append('RAW_ALL_INTRABAR_SCALE_MISMATCH_EXCLUDED')
        bad=set(mismatch[mismatch].index);raw=raw[~raw.trade_date.isin(bad)];all_=all_[~all_.trade_date.isin(bad)]
    return raw,all_,issues

def frame(symbol,benchmark):
    raw,all_,issues=aligned(symbol)
    if all_.empty:return pd.DataFrame(),raw,issues
    d=feature_frame(all_,benchmark).reindex(freeze()['days'])
    d['observed']=d.observed.fillna(False).astype(bool)
    return d,raw,issues

def label_frame(d,benchmark,h):
    """t+1 open to t+H close; complete path and no immature labels."""
    out=pd.DataFrame(index=d.index);out['position']=np.arange(len(d))
    out['label']=d.close.shift(-h)/d.open.shift(-1)-1
    present=d[['open','high','low','close','volume']].notna().all(axis=1)&(d.volume>0)
    complete=pd.concat([present.shift(-j,fill_value=False) for j in range(1,h+1)],axis=1).all(axis=1)
    out.loc[~complete,'label']=np.nan
    if benchmark is not None and not benchmark.empty:
        b=benchmark.reindex(d.index);out['spy_label']=b.close.shift(-h)/b.open.shift(-1)-1
        bp=b[['open','close','volume']].notna().all(axis=1)&(b.volume>0)
        valid=pd.concat([bp.shift(-j,fill_value=False) for j in range(1,h+1)],axis=1).all(axis=1)
        out.loc[~valid,'spy_label']=np.nan
    else:out['spy_label']=np.nan
    out['excess_spy']=out.label-out.spy_label
    out['entry_date']=pd.Series(d.index,index=d.index).shift(-1)
    out['exit_date']=pd.Series(d.index,index=d.index).shift(-h)
    return out
