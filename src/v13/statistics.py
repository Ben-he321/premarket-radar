"""Shared-calendar inference. No IID treatment of overlapping labels."""
from functools import lru_cache
import numpy as np
from src.v12.statistics import by_adjust, nonoverlap

@lru_cache(maxsize=16)
def weights(blocks, repetitions=10000, seed=131729):
    return np.random.default_rng(seed).multinomial(blocks,np.full(blocks,1/blocks),size=repetitions).astype(float)

def distribution(values, positions=None, repetitions=10000, seed=131729, block=60):
    x=np.asarray(values,dtype=float)
    if x.ndim==1:x=x[:,None]
    pos=np.arange(len(x)) if positions is None else np.asarray(positions,dtype=int)
    ids=pos//block; full=np.arange(ids.min(),ids.max()+1) if len(ids) else np.array([])
    if not len(full):return np.full(x.shape[1],np.nan),np.full((repetitions,x.shape[1]),np.nan),0
    sums=np.array([np.nansum(x[ids==i],axis=0) for i in full]);counts=np.array([np.isfinite(x[ids==i]).sum(axis=0) for i in full])
    w=weights(len(full),repetitions,seed); num=w@sums;den=w@counts
    estimate=np.divide(sums.sum(axis=0),counts.sum(axis=0),out=np.full(x.shape[1],np.nan),where=counts.sum(axis=0)>0)
    boot=np.divide(num,den,out=np.full_like(num,np.nan),where=den>0)
    return estimate,boot,len(full)

def summary(values,positions=None,**kw):
    x=np.asarray(values,dtype=float);est,b,n=distribution(x,positions,**kw)
    observed=int(np.isfinite(x).sum());usable=b[:,0];usable=usable[np.isfinite(usable)]
    if observed<20 or n<8 or len(usable)<9000:
        return {'mean':float(est[0]) if np.isfinite(est[0]) else None,'n':observed,'blocks':n,'ci_low':None,'ci_high':None,'p':None,'status':'INSUFFICIENT_EVIDENCE'}
    lo,hi=np.quantile(usable,[.025,.975]);p=(1+np.sum(np.abs(usable-est[0])>=abs(est[0])))/(len(usable)+1)
    return {'mean':float(est[0]),'n':observed,'blocks':n,'ci_low':float(lo),'ci_high':float(hi),'p':float(p),'status':'DESCRIPTIVE_BLOCK_UNCERTAINTY'}

def resolution(m,replications,alpha=.05):
    harmonic=sum(1/i for i in range(1,m+1));minimum=1/(replications+1)
    return [{'rank':k,'family':m,'replications':replications,'minimum_p':minimum,'by_threshold':alpha*k/(m*harmonic),
             'minimum_p_reachable_at_rank':minimum<=alpha*k/(m*harmonic)} for k in range(1,m+1)]
