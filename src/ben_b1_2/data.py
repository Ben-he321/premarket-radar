"""Bounded real SIP inputs. All new writes stay in B1.2; old caches read only."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import traceback
import pandas as pd
from src.ben_b1.b11_data import B11Market, attach_page_receipts
from src.ben_b1.data_probe import rth_aggregate, schedule, load_universe
from src.ben_b1 import rules
from .runtime import ROOT, B11, B1, START, END, TAIL_END, WARMUP, read, write, sha, utc, deadline

NY='America/New_York'
DATA=ROOT/'data'

class B12Market(B11Market):
    def __init__(self, output=DATA):
        super().__init__(output)
        # Completed local objects are immutable too. Re-entering the legacy
        # normalizer would otherwise hash an already-present quote_id again.
        self.previous+=self.state.get('requests',[])
        for p in [B11/'data/PROBE_STATE.json',B11/'data/exit_quote_supplement_v1/PROBE_STATE.json',
                  DATA/'sample_quotes/PROBE_STATE.json',DATA/'portfolio_quotes/PROBE_STATE.json']:
            if p.exists() and p.resolve()!=self.state_path.resolve():
                self.previous+=read(p).get('requests',[])
        self.used={}

    def acquire(self,symbol,start,end,kind='bars',adjustment='raw',timeframe='1Min',scope='B12_FIXED',**kwargs):
        deadline()
        for prior in self.previous:
            p=prior.get('params',{})
            if (prior.get('symbol')!=symbol or prior.get('kind')!=kind or not prior.get('complete')
                or p.get('symbols')!=kwargs.get('query_symbol',symbol) or p.get('feed')!='sip'):
                continue
            if kind=='bars' and (p.get('adjustment')!=adjustment or p.get('timeframe')!=timeframe):continue
            if pd.Timestamp(p['start'])>pd.Timestamp(start) or pd.Timestamp(p['end'])<pd.Timestamp(end):continue
            path=Path(prior['path'])/'data.parquet'
            if not path.exists():
                if prior.get('rows')!=0:continue
                f=pd.DataFrame()
            else:
                f=pd.read_parquet(path)
                field='timestamp' if kind=='bars' else 't'
                f=f[pd.to_datetime(f[field],utc=True).between(pd.Timestamp(start),pd.Timestamp(end))].copy()
                if len(f) and 'source_received_at' not in f:f=attach_page_receipts(f,prior)
            receipt={**prior,'old_cache_reused_read_only':True,'requested_start':str(start),'requested_end':str(end),
                     'scope_b12':scope,'rows_in_requested_window':len(f),'old_input_sha256':sha(path) if path.exists() else None}
            self.state['cache_reuse_records'].append(receipt);self.save()
            if path.exists():self.used[str(path)]=sha(path)
            return f,receipt
        # Legacy superset reuse did not compare historical query ticker. Our
        # stricter identity match above is authoritative, especially ECHO/SATS.
        previous=self.previous
        self.previous=[]
        try:f,r=super().acquire(symbol,start,end,kind,adjustment,timeframe,scope,**kwargs)
        finally:self.previous=previous
        p=Path(r['path'])/'data.parquet'
        if p.exists():self.used[str(p)]=sha(p)
        if r.get('complete'):self.previous.append(r)
        return f,r

    def quotes(self,symbol,day,holding=False):
        c=schedule(day,day).iloc[0]
        start,end=(c.market_open,c.market_close) if holding else (c.market_close+pd.Timedelta(minutes=5,seconds=-5),c.market_close+pd.Timedelta(minutes=15))
        return self.acquire(symbol,start,end,kind='quotes',scope='B12_HOLDING_RTH' if holding else 'B12_ENTRY_WINDOW',query_symbol=query_ticker(symbol))

    def holding_quotes(self,symbol,day):
        return self.quotes(symbol,day,True)

def query_ticker(symbol):
    # Existing identity registry: same EchoStar security was SATS until June24.
    return 'SATS' if symbol=='ECHO' else symbol

def pin(ref):
    p=Path(ref['path']);actual=sha(p)
    if ref.get('sha256') and actual!=ref['sha256']:raise ValueError('PINNED_OLD_OBJECT_CHANGED:'+str(p))
    return {**ref,'sha256':actual,'reused_read_only':True}

def frame_qc(frame,field='timestamp'):
    if frame.empty:return {'rows':0,'duplicates':0,'invalid_ohlc':0,'missing_volume':0}
    good=(frame[['open','high','low','close']].notna().all(axis=1)&(frame[['open','high','low','close']]>0).all(axis=1)
          &(frame.high>=frame[['open','close','low']].max(axis=1))&(frame.low<=frame[['open','close','high']].min(axis=1)))
    return {'rows':len(frame),'duplicates':int(frame.duplicated(field).sum()),'invalid_ohlc':int((~good).sum()),
            'missing_volume':int(frame.volume.isna().sum()),'negative_volume':int((frame.volume<0).sum())}

def prepare_histories():
    DATA.mkdir(parents=True,exist_ok=True)
    scope=pd.read_csv(B1/'UNIVERSE_POLICY.csv');scope.to_csv(DATA/'ALL66_SCOPE_VERSION.csv',index=False,encoding='utf-8-sig')
    coarse=pd.read_csv(B1/'coarse/all_candidate_events.csv')
    cand=coarse[coarse.trade_date.between(START,END)&coarse.coarse_space_allowed.eq(True)].copy()
    cand=cand.sort_values(['trade_date','identity_sort_hash','symbol'],kind='stable')
    cand.to_csv(DATA/'COARSE_Q1_PREFILTER_ONLY.csv',index=False,encoding='utf-8-sig')
    priority=list(dict.fromkeys(cand.symbol))
    symbols=[s for s in priority if s in scope[scope.scope_policy.eq('KEEP')].symbol.tolist()]
    symbols+=sorted(set(scope[scope.scope_policy.eq('KEEP')].symbol)-set(symbols))
    write(DATA/'HISTORY_REQUEST_FREEZE.json',{'created_at':utc(),'selection':'ALL60_KEEP, sorted by pre-existing coarse earliest event then remaining symbol; excludes are retained in66 table',
        'symbols':symbols,'start':WARMUP,'end':TAIL_END,'old_coarse_is_only_download_order_not_signal':True,'no_outcome_selection':True})
    old={r['symbol']:r for r in read(B11/'data/HISTORY_INPUTS.json')}
    path=DATA/'HISTORY_INPUTS.json';done={r['symbol']:r for r in read(path)} if path.exists() else {}
    market=B12Market(DATA)
    # Vendor-standard corporate actions acquired once for fixed symbols and tail.
    # Existing fetch is bounded through Sep11; effective-date filtering is done
    # by the adapter, never use future ex dates to adjust past signals.
    actions=market.corporate_actions(symbols+['SPY','QQQ'])
    for i,symbol in enumerate(symbols,1):
        deadline()
        if symbol in done and done[symbol].get('complete'):continue
        write(DATA/'WORKER_STATE.json',{'stage':'HISTORIES','pid':os.getpid(),'symbol':symbol,'index':i,'total':len(symbols),'updated_at':utc(),'status':'RUNNING'})
        try:
            if symbol in old:
                row={**old[symbol],'daily':{a:pin(v) for a,v in old[symbol]['daily'].items()},
                     'minutes':pin(old[symbol]['minutes']),'rth':pin(old[symbol]['rth']),
                     'complete':True,'reuse':'B11_IMMUTABLE_SUPERSET','b12_requested_range':[WARMUP,TAIL_END]}
            else:
                row={'symbol':symbol,'query_symbol':query_ticker(symbol),'start':WARMUP,'end':TAIL_END,'daily':{}}
                finish=(pd.Timestamp(TAIL_END)+pd.Timedelta(days=1)).tz_localize(NY)-pd.Timedelta(nanoseconds=1)
                begin=pd.Timestamp(WARMUP,tz=NY)
                for a in ['raw','split','all']:
                    f,r=market.acquire(symbol,begin,finish,adjustment=a,timeframe='1Day',scope='B12_CONTINUOUS_DAILY',query_symbol=query_ticker(symbol))
                    dest=DATA/(symbol+'_daily_'+a+'.parquet');f.to_parquet(dest,index=False)
                    row['daily'][a]={'path':str(dest),'sha256':sha(dest),'receipt':r}
                f,r=market.acquire(symbol,begin,finish,scope='B12_CONTINUOUS_WARMUP_RTH_MINUTES',query_symbol=query_ticker(symbol))
                dest=DATA/(symbol+'_minutes_raw.parquet');f.to_parquet(dest,index=False)
                row['minutes']={'path':str(dest),'sha256':sha(dest),'receipt':r,'qc':frame_qc(f)}
                rth=rth_aggregate(f,WARMUP,TAIL_END) if len(f) else pd.DataFrame()
                daily=pd.read_parquet(row['daily']['raw']['path'])
                if len(rth) and len(daily):
                    daily=daily.set_index('trade_date');rth['last_minute_close']=rth['close'];rth['minute_only_high']=rth.high;rth['minute_only_low']=rth.low
                    rth['vendor_official_close']=rth.trade_date.map(daily.close.to_dict());rth['close']=rth.vendor_official_close
                    rth['high']=rth[['high','close']].max(axis=1);rth['low']=rth[['low','close']].min(axis=1)
                    rth['regular_session_verified']=True;rth['action_units_verified']=True
                    rth['volume_basis']='RTH_MINUTES_WITHOUT_SEPARATE_CLOSING_AUCTION';rth['auction_inclusive_volume_verified']=False
                    mr=f.groupby('trade_date').source_received_at.max().to_dict();dr=daily.source_received_at.to_dict()
                    rth['source_received_at']=[max(mr.get(d,'UNKNOWN'),dr.get(d,'UNKNOWN')) for d in rth.trade_date]
                    rth['historical_network_received_at']='UNKNOWN'
                rd=DATA/(symbol+'_rth.parquet');rth.to_parquet(rd,index=False)
                row['rth']={'path':str(rd),'sha256':sha(rd),'rows':len(rth),'pre_start_valid_sessions':int((rth.trade_date<START).sum()) if len(rth) else 0}
                row['complete']=bool(r.get('complete') and all(v['receipt'].get('complete') for v in row['daily'].values()))
                row['coverage_status']='OBSERVED_HISTORY' if len(rth) else 'EMPTY_RESPONSE_NOT_PROOF_OF_PRE_LISTING'
            done[symbol]=row
        except Exception as exc:
            done[symbol]={'symbol':symbol,'complete':False,'status':'INPUT_FAILED','error_type':type(exc).__name__,'error':str(exc),'at':utc()}
            write(DATA/f'error_{symbol}.json',{**done[symbol],'traceback':traceback.format_exc()})
        write(path,list(done.values()))
        print(json.dumps({'phase':'HISTORIES','symbol':symbol,'index':i,'total':len(symbols),'complete':done[symbol].get('complete'),'rows':done[symbol].get('rth',{}).get('rows'),'reuse':done[symbol].get('reuse')}),flush=True)
    write(DATA/'WORKER_STATE.json',{'stage':'HISTORIES','pid':os.getpid(),'updated_at':utc(),'status':'COMPLETE','completed':len(done)})
    return list(done.values())

if __name__=='__main__':
    prepare_histories()
