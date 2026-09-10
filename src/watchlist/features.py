"""22 causal, explicit daily features; no filling missing market sessions."""
import numpy as np
import pandas as pd
from src.data.alpaca_calendar import sessions


def feature_frame(frame, benchmark=None):
    if frame.empty:return frame.copy()
    d=frame.copy().set_index('trade_date').sort_index()
    calendar=[str(x) for x in sessions(pd.Timestamp(d.index.min()).date(),pd.Timestamp(d.index.max()).date())]
    d=d.reindex(calendar)
    c,h,l,o,v=[d[k] for k in ('close','high','low','open','volume')]
    r=c.pct_change(fill_method=None)
    for n in (1,5,20,60):d[f'return_{n}']=c.pct_change(n,fill_method=None)
    for n in (5,10,20,60):d[f'ma{n}_gap']=c/c.rolling(n).mean()-1
    d['ma20_slope']=c.rolling(20).mean().pct_change(5,fill_method=None)
    tr=pd.concat([(h-l),(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1,skipna=False)
    d['atr14_ratio']=tr.rolling(14).mean()/c
    d['vol20']=r.rolling(20).std()
    d['downside_vol20']=r.clip(upper=0).rolling(20).std()
    for n in (5,20):d[f'volume_ratio{n}']=v/v.shift().rolling(n).mean()
    d['dollar_volume20']=(c*v).rolling(20).mean()
    d['breakout20']=c/h.shift().rolling(20).max()-1
    d['range_position20']=(c-l.rolling(20).min())/(h.rolling(20).max()-l.rolling(20).min())
    d['gap']=o/c.shift()-1
    d['intraday']=c/o-1
    up=c.diff().clip(lower=0).rolling(14).mean()
    down=(-c.diff().clip(upper=0)).rolling(14).mean()
    d['rsi14']=100-100/(1+up/down.replace(0,np.nan))
    d.loc[(down==0)&(up>0),'rsi14']=100
    d['drawdown60']=c/c.rolling(60).max()-1
    if benchmark is not None and not benchmark.empty:
        bc=benchmark.set_index('trade_date').close.reindex(d.index)
        d['relative20']=d.return_20-bc.pct_change(20,fill_method=None)
        d['market_up']=bc>bc.rolling(60).mean()
    else:
        d['relative20']=np.nan;d['market_up']=False
    d['observed']=c.notna()
    return d.replace([np.inf,-np.inf],np.nan)


def signal(d, spec):
    volume=d.volume_ratio5>spec['volume_threshold']
    if spec['family']=='LEGACY':
        ma5=d.close.rolling(5).mean();ma10=d.close.rolling(10).mean()
        result=((d.low-ma5).abs()/ma5<=.02)&(d.close>=ma10)&(d.close>d.close.shift())&volume
    elif spec['family']=='A':
        result=(d.ma20_gap>0)&(d.ma60_gap>0)&(d.low<=d.close.rolling(5).mean()*1.01)&(d.return_1>0)&volume
    elif spec['family']=='B':
        result=(d.breakout20>0)&(d.ma60_gap>0)&volume
    elif spec['family']=='C':
        result=d.market_up&(d.return_5<-.03)&(d.rsi14<40)&(d.return_1>0)&volume
    else:raise ValueError('UNKNOWN_STRATEGY')
    return result.fillna(False)&d.observed
