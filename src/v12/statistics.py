"""Single-stock time-series ranks and common-calendar block resampling."""
from functools import lru_cache
import numpy as np
from scipy.stats import rankdata

@lru_cache(maxsize=512)
def draws(block_ids,seed=1729,repetitions=500):
    rng=np.random.default_rng(seed);k=len(block_ids)
    counts=np.zeros((repetitions,k),dtype=float)
    if k:
        picked=rng.integers(0,k,size=(repetitions,k))
        for j in range(repetitions):counts[j]=np.bincount(picked[j],minlength=k)
    return counts

def block_sums(values,ids,unique):
    idx=np.searchsorted(unique,ids)
    return np.bincount(idx,weights=values,minlength=len(unique))

def ratio(a,b):
    return np.divide(a,b,out=np.full_like(np.asarray(a,dtype=float),np.nan),where=np.asarray(b)>0)

def ci(values):
    x=np.asarray(values);x=x[np.isfinite(x)]
    return (float(np.quantile(x,.025)),float(np.quantile(x,.975))) if len(x)>=400 else (None,None)

def centered_p(values,point):
    if point is None or not np.isfinite(point):return None
    x=np.asarray(values);x=x[np.isfinite(x)]
    return float((1+np.sum(np.abs(x-point)>=abs(point)))/(len(x)+1)) if len(x)>=400 else None

def nonoverlap(positions,h):
    n=0;next_allowed=-1
    for p in sorted(set(positions)):
        if p>=next_allowed:n+=1;next_allowed=p+h
    return n

def inference(d,h,calendar_span):
    """All symbols sharing a calendar span use the exact same bootstrap draws."""
    blocks=tuple(range(calendar_span[0]//20,(calendar_span[1]-1)//20+1))
    unique=np.array(blocks);weights=draws(blocks);ids=d.position.to_numpy(dtype=int)//20
    y=d.label.to_numpy(float);x=d.factor.to_numpy(float);b=d.bucket.to_numpy();rows=[]
    counts_all=block_sums(np.ones(len(d)),ids,unique);sum_all=block_sums(y,ids,unique)
    uncond_boot=ratio(weights@sum_all,weights@counts_all)
    boots={};unconditional=float(np.mean(y)) if len(y) else None
    for bucket in ['ALL','weak','medium','strong']:
        mask=np.ones(len(d),dtype=bool) if bucket=='ALL' else b==bucket
        a=y[mask];positions=d.position.to_numpy()[mask];nblocks=len(set(ids[mask]));effective=nonoverlap(positions,h)
        count=block_sums(mask.astype(float),ids,unique);sums=block_sums(np.where(mask,y,0),ids,unique)
        boot=ratio(weights@sums,weights@count);boots[bucket]=boot
        sufficient=len(a)>=20 and nblocks>=5 and effective>=5
        low,high=ci(boot) if sufficient else (None,None)
        sub=d.loc[mask];excess=sub.excess_spy.dropna()
        row={'bucket':bucket,'status':'DESCRIPTIVE_WITH_BLOCK_CI' if sufficient else 'INSUFFICIENT_EVIDENCE',
             'events':len(a),'nonoverlap_events':effective,'date_blocks':nblocks,
             'first_signal':str(sub.index.min()) if len(sub) else None,'last_signal':str(sub.index.max()) if len(sub) else None,
             'mean':float(np.mean(a)) if len(a) else None,'median':float(np.median(a)) if len(a) else None,
             'up_fraction':float(np.mean(a>0)) if len(a) else None,
             'q05':float(np.quantile(a,.05)) if len(a) else None,'q95':float(np.quantile(a,.95)) if len(a) else None,
             'mean_ci_low':low,'mean_ci_high':high,'unconditional_same_dates_mean':unconditional,
             'bucket_minus_unconditional':float(np.mean(a)-unconditional) if len(a) else None,
             'spy_excess_mean':float(excess.mean()) if len(excess) else None,'spy_paired_events':len(excess)}
        dl,dh=ci(boot-uncond_boot) if sufficient else (None,None)
        row.update(bucket_minus_unconditional_ci_low=dl,bucket_minus_unconditional_ci_high=dh)
        rows.append(row)
    weak=y[b=='weak'];strong=y[b=='strong']
    spread=float(strong.mean()-weak.mean()) if len(weak) and len(strong) else None
    adequate=all(r['status']=='DESCRIPTIVE_WITH_BLOCK_CI' for r in rows if r['bucket'] in ('weak','strong'))
    spreadboot=boots['strong']-boots['weak'];sl,sh=ci(spreadboot) if adequate else (None,None)
    ic=None;ic_boot=[]
    if len(d)>=60 and len(set(ids))>=5 and len(set(x))>1 and len(set(y))>1:
        xr=rankdata(x);yr=rankdata(y);ic=float(np.corrcoef(xr,yr)[0,1])
        arrays=[block_sums(v,ids,unique) for v in [np.ones(len(d)),xr,yr,xr*xr,yr*yr,xr*yr]]
        n,sx,sy,sxx,syy,sxy=[weights@a for a in arrays]
        vx=sxx-ratio(sx*sx,n);vy=syy-ratio(sy*sy,n)
        ic_boot=ratio(sxy-ratio(sx*sy,n),np.sqrt(np.maximum(0,vx*vy)))
    il,ih=ci(ic_boot)
    summary={'events':len(d),'nonoverlap_events':nonoverlap(d.position,h),'date_blocks':len(set(ids)),
             'time_series_rank_ic':ic,'ic_ci_low':il,'ic_ci_high':ih,'ic_p':centered_p(ic_boot,ic),
             'strong_minus_weak':spread,'spread_ci_low':sl,'spread_ci_high':sh,
             'spread_p':centered_p(spreadboot,spread) if adequate else None,
             'unconditional_same_dates_mean':unconditional,'status':'DESCRIPTIVE_WITH_BLOCK_CI' if adequate and ic is not None else 'INSUFFICIENT_EVIDENCE'}
    return rows,summary

def by_adjust(pvalues):
    """Benjamini-Yekutieli controls FDR under arbitrary dependence."""
    x=np.array([1 if p is None or not np.isfinite(p) else p for p in pvalues]);m=len(x)
    order=np.argsort(x);ranked=x[order]*m*np.sum(1/np.arange(1,m+1))/np.arange(1,m+1)
    result=np.empty(m);result[order]=np.minimum(1,np.minimum.accumulate(ranked[::-1])[::-1]);return result
