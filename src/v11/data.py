"""Freeze V1 object references; field-aware views and minimal SIP minute probes."""
from pathlib import Path
from datetime import datetime,date,timedelta,timezone
from zoneinfo import ZoneInfo
import hashlib
import pandas as pd
import numpy as np
from alpaca.data.requests import StockBarsRequest,StockTradesRequest
from alpaca.data.enums import Adjustment,DataFeed
from alpaca.data.timeframe import TimeFrame
from src.data.alpaca_client import PacedStockClient,AlpacaMarketData,DataAccessError,normalize_bars
from src.data.alpaca_config import load_config
from src.data.alpaca_calendar import finalized_day
from .runtime import root,source,read,write,freeze,utc,digest,state

NY=ZoneInfo('America/New_York')

def snapshot():
    p=root()/'v1_snapshot.json'
    if p.exists():return read(p)
    universe=read(source()/'universe.json');records=universe['records']+universe['references'];entries={}
    for rec in records:
        entries[rec['symbol']]={}
        for adj in ('raw','all'):
            m=read(source()/'datasets'/rec['symbol']/rec['identity_version']/adj/'manifest.json')
            entries[rec['symbol']][adj]=m
    old_outputs={str(p.relative_to(source())):hashlib.sha256(p.read_bytes()).hexdigest() for p in (source()/'research').glob('*') if p.is_file()}
    result={'created_at':utc(),'universe':universe,'manifests':entries,'v1_result_hashes':old_outputs,
            'old_config':read(source()/'preregistration.json'),'mode':'READ_ONLY_IMMUTABLE_OBJECT_REFERENCES'}
    result['hash']=digest(result);write(p,result);return result

def classify_fields(f):
    x=f.copy();prices=x[['open','high','low','close']]
    valid=prices.notna().all(axis=1)&np.isfinite(prices).all(axis=1)&(prices>0).all(axis=1)
    valid&=(x.low<=x.high)&(x.open>=x.low)&(x.open<=x.high)&(x.close>=x.low)&(x.close<=x.high)
    volume_valid=x.volume.notna()&np.isfinite(x.volume)&(x.volume>0)
    x['price_valid']=valid;x['execution_eligible']=valid&volume_valid
    x['auxiliary_warning']=x.vwap.isna()|~np.isfinite(x.vwap)|(x.vwap<=0)
    x['quality_state']=np.where(~valid,'INVALID_OHLC',np.where(~volume_valid,'REFERENCE_ONLY_NO_EXECUTABLE_VOLUME',np.where(x.auxiliary_warning,'VALID_OHLC_AUXILIARY_WARNING','VALID')))
    return x

def load(symbol,adj='raw',include_quarantine=True):
    snap=snapshot();m=snap['manifests'][symbol][adj]
    f=pd.concat([pd.read_parquet(source()/c['path']) for c in m['chunks'].values() if c['status']=='OK'],ignore_index=True)
    if include_quarantine:
        qs=[pd.read_parquet(p) for p in (source()/'quarantine'/symbol/adj).glob('*.parquet')]
        if qs:f=pd.concat([*qs,f],ignore_index=True).drop_duplicates(['symbol','trade_date'],keep='last')
    return classify_fields(f.sort_values('trade_date').reset_index(drop=True))

def feature_input(symbol,adj='all'):
    f=load(symbol,adj);return f[f.execution_eligible].copy()

class Market:
    def __init__(self):self.sdk=PacedStockClient(load_config())
    def minutes(self,symbol,start,end,now=None):
        now=now or datetime.now(timezone.utc)
        if end>now-timedelta(minutes=20):raise DataAccessError('BASIC_20_MINUTE_LAG_REQUIRED')
        request=StockBarsRequest(symbol_or_symbols=[symbol],start=start,end=end-timedelta(microseconds=1),
                                 timeframe=TimeFrame.Minute,feed=DataFeed.SIP,adjustment=Adjustment.RAW,asof='-')
        result=normalize_bars(self.sdk.get_stock_bars(request),(symbol,))
        if len(result) and not ((result.timestamp>=start)&(result.timestamp<end)).all():raise DataAccessError('OUT_OF_RANGE_MINUTES')
        return result.sort_values('timestamp')
    def daily(self,symbols,start,end,adjustment):
        client=AlpacaMarketData(load_config(),sdk=self.sdk);client.asof='-'
        return client.bars(tuple(symbols),start,end,adjustment)

def audit_gaps():
    freeze();snapshot();records=[]
    reasons={'SMCI':('VERIFIED_NASDAQ_SUSPENSION_OTC_INTERVAL','https://ir.supermicro.com/news/news-details/2018/Supermicro-Announces-Suspension-of-Trading-of-Common-Stock-on-Nasdaq-and-its-Intention-to-Appeal/default.aspx'),
             'CLSK':('VERIFIED_NASDAQ_TRADING_HALT','https://www.sec.gov/Archives/edgar/data/827876/000095017024127680/clsk-20241115.htm')}
    for symbol in ('SMCI','CCXI','BATL','CLSK'):
        f=load(symbol);bad=f[f.auxiliary_warning|~f.execution_eligible]
        write(root()/'field_views'/(symbol+'.json'),{'symbol':symbol,'rows':[{'date':r.trade_date,'quality_state':r.quality_state,
            'invalid_fields':[c for c in ['open','high','low','close','vwap'] if not np.isfinite(r[c]) or r[c]<=0],
            'volume':r.volume,'trade_count':r.trade_count,'execution_eligible':bool(r.execution_eligible)} for _,r in bad.iterrows()]})
        records.append({'symbol':symbol,'affected_dates':bad.trade_date.tolist(),'count':len(bad),
                        'invalid_price_field':'VWAP_ONLY','volume_zero':int((bad.volume==0).sum()),'OHLC_numerically_valid':int(bad.price_valid.sum()),
                        'restored_reference_rows':len(bad),'restored_executable_rows':int(bad.execution_eligible.sum()),
                        'verified_cause':reasons.get(symbol,('UNVERIFIED_NO_TRADES_IN_SIP_NOT_ASSUMED_HALT',None))[0],
                        'source':reasons.get(symbol,(None,None))[1],'policy':'KEEP_REFERENCE; no synthetic fill; no zero-volume executions'})
    write(root()/'gap_and_actions_audit.json',{'field_rules_version':freeze()['hash'],'gaps':records,'actions':'PENDING_INTERVAL_AUDIT'})
    return records

def probes():
    p=root()/'market_sample_audit.json'
    if p.exists() and read(p).get('complete'):return read(p)
    config=freeze();market=Market();checks={kind:AlpacaMarketData(load_config(),sdk=market.sdk).probe(kind,finalized_day()) for kind in ['historical','latest']}
    write(root()/'sip_checks.json',checks)
    pairs=[(s,d) for s in config['samples']['symbols'] for d in config['samples']['fixed_dates']]
    pairs+=list(config['samples']['anomalies'].items())+list(config['samples']['split_samples'].items())
    output=[]
    for s,d in pairs:
        try:
            day=date.fromisoformat(d);a=datetime.combine(day,datetime.min.time(),NY)+timedelta(hours=4);b=a+timedelta(hours=16)
            f=market.minutes(s,a,b);path=root()/'samples'/(s+'-'+d+'.parquet');path.parent.mkdir(exist_ok=True);f.to_parquet(path,index=False)
            regular=f[(f.timestamp.dt.tz_convert(NY).dt.time>=datetime.strptime('09:30','%H:%M').time())&(f.timestamp.dt.tz_convert(NY).dt.time<datetime.strptime('16:00','%H:%M').time())]
            daily=load(s);daily=daily[daily.trade_date==d]
            detail={'symbol':s,'date':d,'status':'ACCESS_OK' if len(f) else 'NO_MINUTE_BARS','minutes':len(f),'regular_minutes':len(regular),
                    'received_at':utc(),'data_hash':hashlib.sha256(path.read_bytes()).hexdigest(),
                    'first_regular_open':float(regular.open.iloc[0]) if len(regular) else None,
                    'daily_open':float(daily.open.iloc[0]) if len(daily) else None,
                    'daily_volume':float(daily.volume.iloc[0]) if len(daily) else None,
                    'minute_volume_all_hours':float(f.volume.sum()),'minute_volume_regular':float(regular.volume.sum()),
                    'execution_rule':'First positive-volume regular-session minute open in [09:30,09:35); proxy only, not guaranteed fill'}
            output.append(detail)
        except DataAccessError as e:output.append({'symbol':s,'date':d,'status':e.code,'received_at':utc()})
        write(p,{'complete':False,'samples':output});state('MINIMAL_EXECUTION_PROBES',completed=len(output),total=len(pairs))
    result={'complete':True,'sample_selection':config['samples'],'samples':output,
            'daily_price_semantics':'Conditions T update daily volume but not daily OHLC; minute T may update both. Daily open is not guaranteed executable.',
            'official_sources':['https://docs.alpaca.markets/us/docs/market-data-faq','https://docs.alpaca.markets/us/reference/stockbars']}
    write(p,result);return result
