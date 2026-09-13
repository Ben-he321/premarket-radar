"""Isolated read-only B1 market-data acquisition and honest evidence coverage.

Only data.alpaca.markets GET requests; original research objects are read only.
The original operational rate-quota database is deliberately shared (120/min).
Page objects and request receipts are resumable and never contain credentials.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
import tomllib
import time
import pandas as pd
import pandas_market_calendars as mcal
import requests
from src.data.alpaca_config import load_config
from src.data.alpaca_client import PacedStockClient, normalize_bars, DataAccessError
from src.data.alpaca_rate import SharedRateLimiter

OLD = Path(r'C:\Users\benhe\OneDrive\Documentos\GITHUB\premarket-radar-ai-m1')
SOURCE = Path(r'C:\Users\benhe\BenAITradingData\watchlist-research-v1')
OUT = Path(r'C:\Users\benhe\BenAITradingData\ben-b1-research-20260913\data_probe')
CASES = [('WULF','2026-09-11'),('AAOI','2026-09-11'),('AMZN','2026-09-11'),
         ('SKHY','2026-08-31'),('SKHY','2026-09-03'),('BE','2026-09-02'),('BE','2026-09-08')]
NY = 'America/New_York'

def utc(): return datetime.now(timezone.utc).isoformat()
def read(p): return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def digest(x): return hashlib.sha256(json.dumps(x,sort_keys=True,default=str).encode()).hexdigest()
def write(p,x):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    q=p.with_suffix(p.suffix+'.tmp');q.write_text(json.dumps(x,ensure_ascii=False,indent=2,default=str),encoding='utf-8');q.replace(p)
def load_universe(): return read(SOURCE/'universe.json')
def raw_daily(symbol,adjustment='raw'):
    rec=next(r for r in load_universe()['records']+load_universe()['references'] if r['symbol']==symbol)
    manifest=read(SOURCE/'datasets'/symbol/rec['identity_version']/adjustment/'manifest.json')
    frames=[pd.read_parquet(SOURCE/c['path']) for c in manifest['chunks'].values() if c['status']=='OK' and c['rows']]
    return pd.concat(frames,ignore_index=True).sort_values('trade_date').drop_duplicates(['symbol','trade_date'],keep='last') if frames else pd.DataFrame()

class ProbeMarket:
    def __init__(self,output=OUT):
        self.output=Path(output);self.output.mkdir(parents=True,exist_ok=True)
        os.environ.setdefault('ALPACA_SECRETS_FILE',str(OLD/'.streamlit/secrets.toml'))
        self.config=load_config();self.sdk=PacedStockClient(self.config)
        self.sdk.shared_limiter=SharedRateLimiter(OLD/'.runtime/alpaca-rate.sqlite',120)
        self.state_path=self.output/'PROBE_STATE.json'
        self.state=read(self.state_path) if self.state_path.exists() else {'started_at':utc(),'api_pages':0,'cache_pages':0,'requests':[],'errors':[]}
        self.state['credentials_present']=self.config.has_credentials;self.state['status']='RUNNING';self.save()
    def save(self): self.state['updated_at']=utc();write(self.state_path,self.state)
    def acquire(self,symbol,start,end,kind='bars',adjustment='raw',timeframe='1Min',scope='RTH_AND_EXTENDED',max_pages=300,query_symbol=None):
        rec=next(r for r in load_universe()['records']+load_universe()['references'] if r['symbol']==symbol)
        query_symbol=query_symbol or symbol
        params={'symbols':query_symbol,'start':pd.Timestamp(start).isoformat(),'end':pd.Timestamp(end).isoformat(),'feed':'sip','asof':'-','limit':10000,'sort':'asc'}
        if pd.Timestamp(end)>pd.Timestamp.now(tz='UTC')-pd.Timedelta(minutes=20): raise ValueError('BASIC_DELAY_REQUIRED')
        if kind=='bars':params.update(timeframe=timeframe,adjustment=adjustment)
        key={'symbol':symbol,'identity_version':rec['identity_version'],'kind':kind,'params':params,'session':scope,'version':'BEN_B1_PROBE_V1'}
        folder=self.output/'cache'/digest(key);folder.mkdir(parents=True,exist_ok=True)
        write(folder/'request.json',key)
        page_token=None;allrows=[];complete=False;pages=0;received=[];error=None
        for page in range(max_pages):
            datafile=folder/f'page_{page:05d}.json';receiptfile=folder/f'page_{page:05d}_receipt.json'
            if datafile.exists() and receiptfile.exists():
                receipt=read(receiptfile)
                if receipt['sha256']!=hashlib.sha256(datafile.read_bytes()).hexdigest():raise ValueError('CACHE_HASH_MISMATCH')
                result=read(datafile);self.state['cache_pages']+=1
            else:
                args=dict(params)
                if page_token:args['page_token']=page_token
                requested=utc()
                try: result=self.sdk.get('/stocks/'+kind,data=args)
                except DataAccessError as exc:
                    error=exc.code;self.state['errors'].append({'symbol':symbol,'kind':kind,'code':error,'at':utc()});break
                received_at=utc();write(datafile,result)
                receipt={'request_started_at':requested,'source_received_at':received_at,'retrieved_at':received_at,
                    'source_published_at':'UNKNOWN','historical_network_received_at':'UNKNOWN',
                    'sha256':hashlib.sha256(datafile.read_bytes()).hexdigest(),'rows':sum(len(v) for v in (result.get(kind) or {}).values()),'next_page_token_present':bool(result.get('next_page_token'))}
                write(receiptfile,receipt);self.state['api_pages']+=1;self.save()
            pages+=1;received.append(receipt['source_received_at'])
            payload=result.get(kind) or {};allrows.extend(payload.get(query_symbol,[]));page_token=result.get('next_page_token')
            if not page_token:complete=True;break
        summary={**key,'cache_key':folder.name,'path':str(folder),'rows':len(allrows),'pages':pages,'complete':complete,
                 'status':error or ('ACCESS_OK' if allrows else 'EMPTY_RESPONSE') if complete or error else 'PAGE_BUDGET_INCOMPLETE',
                 'last_source_received_at':received[-1] if received else 'UNKNOWN','known_at_historical':'UNKNOWN',
                 'size_unit':('SHARES_SINCE_2025_11_03;PRE_CHANGE_ROUND_LOTS_100_SHARES_ASSUMPTION' if kind=='quotes' else 'VOLUME_SHARES')}
        write(folder/'summary.json',summary)
        self.state['requests']=[r for r in self.state['requests'] if r.get('cache_key')!=folder.name]+[summary];self.save()
        frame=normalize_bars({symbol:allrows},(symbol,)) if kind=='bars' else pd.DataFrame(allrows)
        if not frame.empty:frame.to_parquet(folder/'data.parquet',index=False)
        return frame,summary

def latest_daily_overlay():
    market=ProbeMarket();records=load_universe()['records']+load_universe()['references'];symbols=[r['symbol'] for r in records]
    results=[]
    for adjustment in ['raw','all','split']:
        params={'symbols':','.join(symbols),'start':'2026-09-11T04:00:00Z','end':'2026-09-12T03:59:59Z','feed':'sip','asof':'-','limit':10000,'sort':'asc','timeframe':'1Day','adjustment':adjustment}
        key={'params':params,'identity_versions':{r['symbol']:r['identity_version'] for r in records},'schema':'BEN_B1_LATEST_OVERLAY_1'};folder=OUT/'cache'/digest(key);folder.mkdir(parents=True,exist_ok=True);write(folder/'request.json',key)
        page_token=None;collected={s:[] for s in symbols};receipts=[]
        for page in range(300):
            dest=folder/f'page_{page:05d}.json';rp=folder/f'page_{page:05d}_receipt.json'
            if dest.exists() and rp.exists():
                result=read(dest);receipt=read(rp)
                if receipt['sha256']!=hashlib.sha256(dest.read_bytes()).hexdigest():raise ValueError('CACHE_HASH_MISMATCH')
                market.state['cache_pages']+=1
            else:
                query=dict(params)
                if page_token:query['page_token']=page_token
                started=utc();result=market.sdk.get('/stocks/bars',data=query);write(dest,result)
                receipt={'source_received_at':utc(),'request_started_at':started,'sha256':hashlib.sha256(dest.read_bytes()).hexdigest(),'historical_network_received_at':'UNKNOWN'};write(rp,receipt);market.state['api_pages']+=1
            receipts.append(receipt)
            for s,bars in (result.get('bars') or {}).items():
                if s not in symbols:raise ValueError('UNEXPECTED_SYMBOL')
                collected[s]+=bars
            page_token=result.get('next_page_token');market.save()
            if not page_token:break
        else:raise ValueError('LATEST_OVERLAY_INCOMPLETE')
        frame=normalize_bars(collected,tuple(symbols));frame.to_parquet(OUT/f'latest_2026-09-11_{adjustment}.parquet',index=False)
        summary={'adjustment':adjustment,'date':'2026-09-11','rows':len(frame),'symbols_with_data':sorted(frame.symbol.unique()),'requested_symbols':symbols,'missing_symbols':sorted(set(symbols)-set(frame.symbol.unique())),
                 'cache_key':folder.name,'receipts':receipts,'complete':True,'source':'ALPACA_SIP','source_received_at':receipts[-1]['source_received_at'],'no_old_objects_overwritten':True};results.append(summary);write(folder/'summary.json',summary)
    write(OUT/'latest_overlay_manifest.json',results);market.state['status']='LATEST_OVERLAY_COMPLETE';market.save();return results

def first10_probes(path):
    f=pd.read_csv(path);market=ProbeMarket();rows=[]
    for _,event in f.iterrows():
        symbol=event['symbol'];day=str(event.get('trade_date',event.get('date')));query_symbol='FB' if symbol=='META' and day<'2022-06-09' else symbol
        s=schedule(day,day).iloc[0];close=s.market_close
        minutes,mr=market.acquire(symbol,s.market_open,close+pd.Timedelta(minutes=15),scope='FIRST10_EVENT_RTH_AND_ORDER_WINDOW',query_symbol=query_symbol)
        quotes,qr=market.acquire(symbol,close+pd.Timedelta(minutes=5)-pd.Timedelta(seconds=5),close+pd.Timedelta(minutes=15),kind='quotes',scope='FIRST10_EVENT_ORDER_WINDOW',query_symbol=query_symbol)
        q1550,q15r=market.acquire(symbol,close-pd.Timedelta(minutes=10,seconds=5),close-pd.Timedelta(minutes=10),kind='quotes',scope='FIRST10_EVENT_MINUS10',query_symbol=query_symbol)
        rth=rth_aggregate(minutes,day,day) if len(minutes) else pd.DataFrame()
        rows.append({'symbol':symbol,'query_symbol':query_symbol,'date':day,'selection':'FROZEN_COARSE_CHRONOLOGICAL_AND_ID_HASH_FIRST10_NOT_OUTCOME_SELECTED',
            'minute_rows':len(minutes),'rth_minutes':int(rth.minutes.iloc[0]) if len(rth) else 0,'rth_close':float(rth.close.iloc[0]) if len(rth) else None,'quote_rows':len(quotes),'minus10_quote_rows':len(q1550),'minute_complete':mr['complete'],'quote_complete':qr['complete'],
            'minutes_cache_key':mr['cache_key'],'quotes_cache_key':qr['cache_key'],'earnings_PIT':'UNKNOWN','actual_release_calendar':'UNKNOWN','quote_size_units':'PRE_2025_11_03_HISTORICAL_ROUND_LOT_NORMALIZATION_NOT_API_VERIFIED_STRICT_GATE',
            'source_received_at':qr['last_source_received_at'],'historical_network_received_at':'UNKNOWN','strict_execution_status':'DATA_GATED_EARNINGS_CALENDAR_AND_100_RTH_WARMUP_NOT_FULLY_QUALIFIED'})
        pd.DataFrame(rows).to_csv(OUT/'FIRST10_PROBE_RESULTS.csv',index=False,encoding='utf-8-sig')
    market.state['status']='FIRST10_PROBES_COMPLETE';market.save();return rows

def close_reconciliation():
    market=ProbeMarket();rows=[]
    for symbol,day in CASES:
        s=schedule(day,day).iloc[0];close=s.market_close
        trades,tr=market.acquire(symbol,close-pd.Timedelta(seconds=2),close+pd.Timedelta(seconds=5),kind='trades',scope='CASE_CLOSING_AUCTION_RECONCILIATION')
        daily_req=next(r for r in market.state['requests'] if r['symbol']==symbol and r['params'].get('timeframe')=='1Day' and r['params'].get('adjustment')=='raw')
        daily=pd.read_parquet(Path(daily_req['path'])/'data.parquet');daily=daily[daily.trade_date==day].iloc[0]
        rth=pd.read_parquet(OUT/f'{symbol}_rth.parquet');rth=rth[rth.trade_date==day].iloc[0]
        auction=trades[trades.c.apply(lambda codes:'6' in codes)] if len(trades) else trades
        official=trades[trades.c.apply(lambda codes:'M' in codes)] if len(trades) else trades
        selected=[]
        for _,t in pd.concat([auction,official]).drop_duplicates('i').iterrows():selected.append({k:t[k] for k in ['t','p','s','c','x','i'] if k in t})
        rows.append({'symbol':symbol,'date':day,'vendor_raw_daily_close':float(daily.close),'rth_1559_minute_close':float(rth.close),
                     'difference':float(daily.close-rth.close),'trade_rows':len(trades),'auction_condition6_count':len(auction),'official_conditionM_count':len(official),
                     'auction_prices':json.dumps(sorted(set(auction.p.tolist()))) if len(auction) else '[]',
                     'daily_close_matches_any_auction':bool(len(auction) and any(abs(float(p)-float(daily.close))<1e-8 for p in auction.p)),
                     'minute_close_equals_vendor':abs(float(daily.close)-float(rth.close))<1e-8,
                     'selected_trade_evidence':json.dumps(selected,default=str),'source_received_at':tr['last_source_received_at'],'trades_cache_key':tr['cache_key'],
                     'status':'CASE_ONLY_AUCTION_PRICE_CHECK_NOT_FULL_WARMUP_CLOSE_VALIDATION'})
        pd.DataFrame(rows).to_csv(OUT/'CLOSING_AUCTION_RECONCILIATION.csv',index=False,encoding='utf-8-sig')
    market.state['status']='DATA_PROBES_COMPLETE';market.save();return rows

def schedule(start,end):return mcal.get_calendar('NYSE').schedule(start_date=start,end_date=end)
def rth_aggregate(frame,start,end):
    cal=schedule(start,end);rows=[]
    for label,s in cal.iterrows():
        f=frame[(frame.timestamp>=s.market_open)&(frame.timestamp<s.market_close)].sort_values('timestamp')
        if f.empty:continue
        rows.append({'trade_date':str(label.date()),'open':float(f.open.iloc[0]),'high':float(f.high.max()),'low':float(f.low.min()),'close':float(f.close.iloc[-1]),'volume':float(f.volume.sum()),'minutes':len(f),
            'expected_calendar_minutes':int((s.market_close-s.market_open).total_seconds()/60),'last_bar_end':(f.timestamp.iloc[-1]+pd.Timedelta(minutes=1)).isoformat(),
            'session_close':s.market_close.isoformat(),'session_close_minute_present':bool(f.timestamp.iloc[-1]+pd.Timedelta(minutes=1)==s.market_close),'source':'ALPACA_SIP_RTH_MINUTE_AGGREGATE'})
    return pd.DataFrame(rows)

def finnhub_probe(output=OUT):
    secrets={}
    for p in [Path.home()/'.streamlit/secrets.toml',OLD/'.streamlit/secrets.toml']:
        if p.exists():secrets.update(tomllib.loads(p.read_text(encoding='utf-8-sig')))
    key=str(secrets.get('FINNHUB_API_KEY') or os.environ.get('FINNHUB_API_KEY') or '').strip()
    if not key and (OLD/'.env').exists():
        for line in (OLD/'.env').read_text(encoding='utf-8-sig').splitlines():
            clean=line.strip()
            if clean and not clean.startswith('#') and '=' in clean and clean.split('=',1)[0].strip()=='FINNHUB_API_KEY':key=clean.split('=',1)[1].strip().strip('\"\'')
    result={'checked_at':utc(),'credential_present':bool(key),'calls':[],'earnings_pit':'NOT_ESTABLISHED','no_subscription_purchase':True}
    if key:
        for label,begin,end in [('recent','2026-08-01','2026-10-31'),('historical','2018-01-01','2018-03-31')]:
            params={'from':begin,'to':end,'symbol':'AMZN'};a=utc()
            try:
                r=requests.get('https://finnhub.io/api/v1/calendar/earnings',params=params,headers={'X-Finnhub-Token':key},timeout=(10,30))
                rows=r.json().get('earningsCalendar',[]) if r.ok else []
                entry={'range':label,'request':params,'requested_at':a,'received_at':utc(),'http_status':r.status_code,'rows':len(rows),'status':'ACCESS_OK' if r.ok and rows else 'EMPTY_NOT_NO_EARNINGS' if r.ok else 'PERMISSION_OR_REQUEST_FAILED'}
                if r.ok:write(Path(output)/('finnhub_'+label+'.json'),{'receipt':entry,'earningsCalendar':rows,'level':'ACTUAL_RELEASE_ONLY_UNLESS_INDEPENDENT_PLANNED_VERSION'})
            except (requests.RequestException,ValueError):entry={'range':label,'status':'NETWORK_OR_RESPONSE_ERROR','received_at':utc()}
            result['calls'].append(entry)
    else:result['status']='MISSING_PROJECT_FINNHUB_CREDENTIAL'
    write(Path(output)/'finnhub_capability.json',result);return result

def run_cases():
    market=ProbeMarket();finnhub_probe();rows=[];symbol_frames={}
    for symbol in dict(CASES):
        # Fixed >300-session target, independent of subsequent outcomes. SKHY listing history remains short.
        frame,receipt=market.acquire(symbol,'2025-06-01T00:00:00Z','2026-09-12T03:59:59Z',adjustment='raw',scope='CASE_WARMUP_RTH_WITH_EXTENDED_CONTEXT')
        if len(frame):
            rth=rth_aggregate(frame,'2025-06-01','2026-09-11');rth.to_parquet(OUT/f'{symbol}_rth.parquet',index=False)
        else:rth=pd.DataFrame()
        symbol_frames[symbol]=(frame,rth,receipt)
        # Separate split/current all daily source allows action-unit investigation; does not substitute for RTH.
        for adj in ['raw','split','all']:
            market.acquire(symbol,'2025-06-01T00:00:00Z','2026-09-12T03:59:59Z',timeframe='1Day',adjustment=adj,scope='ACTION_UNIT_REFERENCE')
    for symbol,day in CASES:
        cal=schedule(day,day);s=cal.iloc[0];close=s.market_close;decision=close+pd.Timedelta(minutes=5)
        quotes,qr=market.acquire(symbol,decision-pd.Timedelta(seconds=5),close+pd.Timedelta(minutes=15),kind='quotes',scope='CASE_AFTERHOURS_DECISION_AND_LIMIT_WINDOW')
        q1550,q15r=market.acquire(symbol,close-pd.Timedelta(minutes=10,seconds=5),close-pd.Timedelta(minutes=10),kind='quotes',scope='CASE_CLOSE_MINUS_10_MIN_SNAPSHOT')
        frame,rth,mr=symbol_frames[symbol]
        hist=rth[rth.trade_date<=day] if len(rth) else rth
        today=hist[hist.trade_date==day] if len(hist) else hist
        eligible=quotes.copy()
        if len(eligible):
            eligible['ts']=pd.to_datetime(eligible.t,utc=True);eligible=eligible[(eligible.ts<=decision)&(eligible.ts>=decision-pd.Timedelta(seconds=5))]
        latest=eligible.iloc[-1].to_dict() if len(eligible) else {}
        rows.append({'symbol':symbol,'date':day,'case_source':'USER_TASKBOOK_DEVELOPMENT_SEMANTIC_SAMPLE','screenshot_original':'NOT_AVAILABLE_IN_THIS_ATTACHMENT',
            'rth_history_sessions':len(hist),'warmup_status':'AVAILABLE_GE_100_RTH' if len(hist)>=100 else 'INDICATOR_WARMUP_INSUFFICIENT',
            'rth_close':float(today.close.iloc[0]) if len(today) else None,'rth_high':float(today.high.iloc[0]) if len(today) else None,
            'case_rth_minutes':int(today.minutes.iloc[0]) if len(today) else 0,'case_full_close_minute':bool(today.session_close_minute_present.iloc[0]) if len(today) else False,
            'case_quote_rows':len(quotes),'quote_complete_window':qr['complete'],'decision_time_ny':decision.tz_convert(NY).isoformat(),'decision_time_madrid':decision.tz_convert('Europe/Madrid').isoformat(),
            'quote_at_decision':json.dumps({k:v for k,v in latest.items() if k!='ts'},default=str),'minus10_quote_rows':len(q1550),
            'earnings_status':'EARNINGS_UNKNOWN_PENDING_TARGETED_OFFICIAL_EVIDENCE','formal_signal_status':'PENDING_RULE_ENGINE_AND_CORPORATE_ACTION_UNIT_CHECK',
            'source_received_at':qr['last_source_received_at'],'historical_network_received_at':'UNKNOWN','minute_cache_key':mr['cache_key'],'quotes_cache_key':qr['cache_key']})
        pd.DataFrame(rows).to_csv(OUT/'CASE_DATA_RECONCILIATION.csv',index=False,encoding='utf-8-sig')
    market.state['status']='CASE_PROBES_COMPLETE';market.save();return rows

def inventory():
    records=load_universe()['records'];state=read(OUT/'PROBE_STATE.json') if (OUT/'PROBE_STATE.json').exists() else {'requests':[]};rows=[]
    scope=pd.read_csv(OUT.parent/'UNIVERSE_POLICY.csv').set_index('symbol').to_dict('index') if (OUT.parent/'UNIVERSE_POLICY.csv').exists() else {}
    official=read(OUT/'official_earnings_evidence.json')['events'] if (OUT/'official_earnings_evidence.json').exists() else []
    latest=pd.read_parquet(OUT/'latest_2026-09-11_raw.parquet') if (OUT/'latest_2026-09-11_raw.parquet').exists() else pd.DataFrame()
    for rec in records:
        f=raw_daily(rec['symbol']);requests_for=[x for x in state['requests'] if x['symbol']==rec['symbol']]
        rows.append({'symbol':rec['symbol'],'identity_version':rec['identity_version'],'stable_security_id':rec.get('security_id','UNKNOWN'),'historical_mapping':rec.get('historical_mapping','UNKNOWN'),
            'security_type':rec['type'],'daily_cached_rows':len(f),'daily_start':f.trade_date.min() if len(f) else None,'daily_end':f.trade_date.max() if len(f) else None,
            'daily_semantics':'SIP_VENDOR_DAILY_COARSE_ONLY_RTH_PRICE_FIELDS_NOT_ALL_VOLUME','raw':'CACHED','all':'CACHED','split':'TARGETED_CASE_PROBE_ONLY',
            'minute_rows_new':sum(x['rows'] for x in requests_for if x['kind']=='bars' and x['params'].get('timeframe')=='1Min'),
            'quote_rows_new':sum(x['rows'] for x in requests_for if x['kind']=='quotes'),
            'quote_full_historical_coverage':'NOT_AVAILABLE','earnings_PIT':'UNKNOWN','actual_release':'TARGETED_EVIDENCE_ONLY_NOT_FULL_HISTORY',
            'industry_scope':scope.get(rec['symbol'],{}).get('scope_policy','UNKNOWN'),'stable_local_security_id':scope.get(rec['symbol'],{}).get('stable_local_security_id','UNKNOWN'),
            'latest_isolated_overlay':'2026-09-11' if len(latest) and rec['symbol'] in latest.symbol.values else 'UNKNOWN',
            'targeted_earnings_evidence':next((e['level'] for e in official if e['symbol']==rec['symbol']),'UNKNOWN'),
            'security_historical_margin':'UNKNOWN','historical_borrow_rate':'UNKNOWN','margin_scenario':'HYPOTHETICAL_50_INITIAL_30_MAINTENANCE_50_STRESS','rate_scenario':'FIXED_8PCT_12PCT_STRESS_ACT360_NOT_REAL_CURVE'})
    pd.DataFrame(rows).to_csv(OUT/'DATA_CAPABILITY.csv',index=False,encoding='utf-8-sig');return rows

def official_evidence():
    """Bounded issuer-page facts reviewed against primary sources, never a full calendar."""
    events=[
      {'symbol':'WULF','level':'VERIFIED_PIT','planned_release_date_ny':'2026-08-05','actual_date_ny':'2026-08-05','actual_time_ny':'07:00','release_session':'BMO',
       'planned_publication_date':'2026-07-22','planned_publication_time_ny':'09:24','conservative_known_from_date':'2026-07-22',
       'planned_source':'https://investors.terawulf.com/news-events/press-releases/detail/143/terawulf-schedules-conference-call-for-second-quarter-2026-financial-results',
       'actual_source':'https://investors.terawulf.com/news-events/press-releases/detail/144/terawulf-reports-second-quarter-2026-results',
       'time_source':'https://investors.terawulf.com/news-events/press-releases',
       'fact_summary':'Issuer advance notice explicitly says financial release before 08:00 call on August 5; issuer index dates actual publication 07:00 EDT.'},
      {'symbol':'BE','level':'VERIFIED_PIT','planned_release_date_ny':'2026-07-28','actual_date_ny':'2026-07-28','actual_time_ny':'UNKNOWN','release_session':'AMC_PLANNED_ACTUAL_HOUR_UNKNOWN',
       'planned_publication_date':'2026-07-06','planned_publication_time_ny':'UNKNOWN','conservative_known_from_date':'2026-07-07',
       'planned_source':'https://www.bloomenergy.com/news/bloom-energy-to-announce-second-quarter-2026-financial-results-on-july-28-2026/',
       'actual_source':'https://investor.bloomenergy.com/press-releases/press-release-details/2026/Bloom-Energy-Reports-Record-Second-Quarter-2026-Financial-Results-and-Raises-Full-Year-2026-Guidance/default.aspx',
       'fact_summary':'Issuer July 6 notice explicitly schedules financial release after July 28 close. Actual issuer release and SEC 8-K confirm July 28 date; no call-time substitution.'},
      {'symbol':'AAOI','level':'VERIFIED_PIT','planned_release_date_ny':'2026-08-06','actual_date_ny':'2026-08-06','actual_time_ny':'UNKNOWN','release_session':'UNKNOWN',
       'planned_publication_date':'2026-07-16','planned_publication_time_ny':'UNKNOWN','conservative_known_from_date':'2026-07-17',
       'planned_source':'https://investors.ao-inc.com/news-releases/news-release-details/applied-optoelectronics-announces-date-second-quarter-2026',
       'actual_source':'https://investors.ao-inc.com/news-releases/news-release-details/applied-optoelectronics-reports-second-quarter-2026-results',
       'fact_summary':'Advance release text explicitly identifies August 6 financial release date. The 16:30 conference call is not recorded as release time.'},
      {'symbol':'AMZN','level':'ACTUAL_RELEASE_ONLY','planned_release_date_ny':'UNKNOWN','actual_date_ny':'2026-07-30','actual_time_ny':'UNKNOWN','release_session':'UNKNOWN',
       'planned_publication_date':'2026-07-16','planned_publication_time_ny':'UNKNOWN','conservative_known_from_date':'UNKNOWN',
       'planned_source':'https://ir.aboutamazon.com/news-release/news-release-details/2026/Amazon-com-to-Webcast-Second-Quarter-2026-Financial-Results-Conference-Call/default.aspx',
       'actual_source':'https://www.aboutamazon.com/news/company-news/amazon-earnings-q2-2026-report',
       'fact_summary':'Advance item schedules only conference call; it is not silently promoted into advance release-date evidence.'},
      {'symbol':'SKHY','level':'ACTUAL_RELEASE_ONLY','planned_release_date_ny':'UNKNOWN','actual_date_ny':'UNKNOWN','actual_date_issuer':'2026-07-29','actual_time_ny':'UNKNOWN','release_session':'UNKNOWN',
       'planned_publication_date':'UNKNOWN','planned_publication_time_ny':'UNKNOWN','conservative_known_from_date':'UNKNOWN',
       'actual_source':'https://news.skhynix.com/en/q2-2026-business-results/',
       'fact_summary':'Issuer Seoul-dated July 29 results; exact release time and corresponding New York date not verified. Never substitute Korean price history for Nasdaq ADR.'}]
    urls=sorted({v for e in events for k,v in e.items() if k.endswith('_source')})
    urls += ['https://news.skhynix.com/en/skhynix-lists-adrs-on-nasdaq/',
             'https://docs.alpaca.markets/us/v1.1/changelog/marketdata-bid-and-ask-size-display-change',
             'https://docs.alpaca.markets/us/reference/stockquotes-1',
             'https://docs.alpaca.markets/us/docs/market-data-faq',
             'https://finnhub.io/docs/api/earnings-calendar','https://finnhub.io/pricing-stock-estimates']
    receipts=[]
    for url in urls:
        dest=OUT/'public_source_receipts'/(digest(url)+'.json')
        if dest.exists():receipts.append(read(dest));continue
        started=utc()
        try:
            r=requests.get(url,timeout=(10,30));entry={'url':url,'http_status':r.status_code,'request_started_at':started,'source_received_at':utc(),'body_sha256':hashlib.sha256(r.content).hexdigest(),'body_bytes':len(r.content),'content_stored':False}
        except requests.RequestException:entry={'url':url,'status':'NETWORK_ERROR','source_received_at':utc()}
        write(dest,entry);receipts.append(entry)
    for event in events:
        event['retrieved_at']=utc();event['source_received_at']=next((r['source_received_at'] for r in receipts if r['url']==event['actual_source']),'UNKNOWN')
        event['historical_network_received_at']='UNKNOWN';event['next_earnings_after_actual']='UNKNOWN';event['coverage']='SINGLE_QUARTER_TARGETED_NOT_COMPLETE_CALENDAR'
    write(OUT/'official_earnings_evidence.json',{'events':events,'retrieved_at':utc(),'receipts':receipts,
       'important':'VERIFIED_PIT applies to public source-publication evidence for one scheduled event, not actual historical API receipt or a complete continuous future calendar. Date-only notices become usable next NY date by conservative research policy, not fabricated time.',
       'finnhub':'No FINNHUB_API_KEY in supported original-project configuration; permissions and historical span NOT_TESTED. Public JS pricing/docs did not expose usable text; no unsupported numeric free-history claim.',
       'SKHY_identity':'Issuer explicitly verifies Nasdaq ADR trading debut 2026-07-10; security warmup remains below 100 sessions for both cases.'})
    monthly=[];cal=schedule('2016-01-01','2026-09-11')
    for rec in load_universe()['records']:
        matches=[e for e in events if e['symbol']==rec['symbol']]
        for month in pd.period_range('2016-01','2026-09',freq='M'):
            days=cal[(cal.index>=month.start_time)&(cal.index<=month.end_time)].index
            known=set()
            for e in matches:
                if e['level']=='VERIFIED_PIT':known.update(d.strftime('%Y-%m-%d') for d in days if e['conservative_known_from_date']<=d.strftime('%Y-%m-%d')<=e['actual_date_ny'])
            actual=[e for e in matches if (e.get('actual_date_ny','')!='UNKNOWN' and e['actual_date_ny'].startswith(str(month))) or e.get('actual_date_issuer','').startswith(str(month))]
            monthly.append({'symbol':rec['symbol'],'month':str(month),'exchange_sessions':len(days),'verified_next_event_known_sessions':len(known),
                'verified_next_event_known_coverage':len(known)/len(days) if len(days) else None,'actual_events_verified':len(actual),
                'level':'PARTIAL_VERIFIED_PIT' if known else 'ACTUAL_RELEASE_ONLY' if actual else 'UNKNOWN',
                'complete_calendar':False,'strict_new_entries_on_unknown_dates':'REJECT','retrospective_exclusion_eligible':'INCOMPLETE_EVENT_CALENDAR_NOT_FULL_HISTORY',
                'source_urls':' | '.join(e['actual_source'] for e in actual)})
    pd.DataFrame(monthly).to_csv(OUT/'EARNINGS_COVERAGE.csv',index=False,encoding='utf-8-sig')
    return events

def data_review():
    """Add explicit row timestamps in new derived files; preserve raw pages and data.parquet."""
    cache_manifest=[];quality=[];action_rows=[]
    for folder in sorted((OUT/'cache').iterdir()):
        if not (folder/'request.json').exists():continue
        request=read(folder/'request.json');summary=read(folder/'summary.json');kind=request.get('kind','bars');rows=[];receipts=[]
        for page in sorted(folder.glob('page_?????.json')):
            receipt=read(page.with_name(page.stem+'_receipt.json'))
            if receipt['sha256']!=hashlib.sha256(page.read_bytes()).hexdigest():raise ValueError('RAW_PAGE_HASH_MISMATCH')
            receipts.append({'path':str(page),'sha256':receipt['sha256'],'source_received_at':receipt['source_received_at']})
            payload=read(page).get(kind) or {}
            for returned_symbol,data in payload.items():
                for raw in data:
                    r=dict(raw);r.update(symbol=request.get('symbol',returned_symbol),query_symbol=returned_symbol,
                         source_received_at=receipt['source_received_at'],retrieved_at=receipt['source_received_at'],historical_network_received_at='UNKNOWN',
                         source_published_at='UNKNOWN',available_at='UNKNOWN_IN_HISTORICAL_DECISION_CLOCK',decision_time='NOT_APPLICABLE_DATA_RECORD',
                         order_created_at='NOT_APPLICABLE_NO_ORDER',fill_effective_at='NOT_APPLICABLE_NO_FILL',source='ALPACA_SIP',currency='USD',page_sha256=receipt['sha256'])
                    stamp=pd.Timestamp(raw['t']);r['event_time']=stamp.isoformat();r['bar_start']=stamp.isoformat() if kind=='bars' else None
                    if kind=='bars':r['bar_end']=(stamp+pd.Timedelta(minutes=1 if request['params']['timeframe']=='1Min' else 1440)).isoformat()
                    else:r['bar_end']=None
                    r['size_unit']='SHARES' if kind!='quotes' or stamp.tz_convert(NY).date().isoformat()>='2025-11-03' else 'HISTORICAL_ROUND_LOT_API_NORMALIZATION_UNKNOWN'
                    rows.append(r)
        if rows:
            timed=pd.DataFrame(rows);timed.to_parquet(folder/'timed_records.parquet',index=False)
        cache_manifest.append({'cache_key':folder.name,'request':request,'summary':summary,'raw_pages':receipts,
           'data_parquet_sha256':hashlib.sha256((folder/'data.parquet').read_bytes()).hexdigest() if (folder/'data.parquet').exists() else None,
           'timed_records_sha256':hashlib.sha256((folder/'timed_records.parquet').read_bytes()).hexdigest() if rows else None,
           'timed_rows':len(rows),'complete':summary.get('complete',False)})
        if (folder/'data.parquet').exists():
            f=pd.read_parquet(folder/'data.parquet')
            q={'cache_key':folder.name,'kind':kind,'symbol':request.get('symbol','MULTI_69'),'rows':len(f),'exact_duplicate_rows':int(f.astype(str).duplicated().sum()),'complete':summary.get('complete',False)}
            if kind=='bars':
                p=f[['open','high','low','close']];q.update(missing_price_rows=int(p.isna().any(axis=1).sum()),nonpositive_price_rows=int((p<=0).any(axis=1).sum()),
                    invalid_ohlc_rows=int(((f.low>f.high)|(f.open<f.low)|(f.open>f.high)|(f.close<f.low)|(f.close>f.high)).sum()),zero_volume_rows=int((f.volume==0).sum()),missing_volume_rows=int(f.volume.isna().sum()))
            elif kind=='quotes':q.update(zero_or_invalid_quote_rows=int(((f.bp<=0)|(f.ap<=0)|(f.bp>f.ap)).sum()),empty_response_is_no_fill_evidence=False)
            quality.append(q)
    state=read(OUT/'PROBE_STATE.json');write(OUT/'data_cache_manifest.json',{'created_at':utc(),'items':cache_manifest,'successful_alpaca_http_pages':state['api_pages'],
        'http_attempts_including_legacy_sdk_retries':'UNKNOWN_NOT_SEPARATELY_INSTRUMENTED','public_get_receipts':len(list((OUT/'public_source_receipts').glob('*.json'))),
        'credentials_exported':False,'raw_pages_immutable':True,'old_data_modified':False})
    pd.DataFrame(quality).to_csv(OUT/'PROBE_DATA_QUALITY.csv',index=False,encoding='utf-8-sig')
    for symbol in dict(CASES):
        frames={}
        for adj in ['raw','split','all']:
            r=next(r for r in state['requests'] if r['symbol']==symbol and r['params'].get('timeframe')=='1Day' and r['params'].get('adjustment')==adj)
            frames[adj]=pd.read_parquet(Path(r['path'])/'data.parquet').set_index('trade_date')
        x=frames['raw'].close.to_frame('raw').join(frames['split'].close.rename('split')).join(frames['all'].close.rename('all'))
        action_rows.append({'symbol':symbol,'dates':len(x),'start':x.index.min(),'end':x.index.max(),
             'raw_split_close_different_rows':int((abs(x.raw-x.split)>1e-8).sum()),'split_all_close_different_rows':int((abs(x.split-x['all'])>1e-8).sum()),
             'source_status':'CURRENT_VENDOR_ADJUSTMENT_COMPARISON_NOT_INDEPENDENT_COMPLETE_ACTION_VERIFICATION','historical_corporate_action_known_at':'UNKNOWN'})
    pd.DataFrame(action_rows).to_csv(OUT/'ACTION_UNIT_CHECK.csv',index=False,encoding='utf-8-sig')
    (OUT/'TIME_FIELDS_AND_LIMITATIONS.md').write_text('''# B1 定向数据证据口径

原始分页 JSON 保留供应商字段；各页 receipt 保存本次真实接收时间与 SHA256。新生成的 timed_records.parquet 为每一条数据写入事件时间、bar_start/bar_end、接收时间、取回时间、原始页哈希。

历史当时的网络接收时间、供应商发布时刻、当时可用时刻均为 UNKNOWN，不能从本次读取时间或文件修改时间倒推。无订单的数据记录的意图/成交时间为 NOT_APPLICABLE。历史日线 bar_end 是供应商纽约自然日桶边界，不是官方收盘时刻。

分钟聚合排除 16:00 时刻的交易，因此不代表已验证官方常规收盘。7 个案例均另用真实 condition 6 收盘竞价成交核对；5 个案例的 15:59 分钟末价不同于正式竞价/供应商日线收盘。尚未核实所有暖机日的收盘竞价；原分钟聚合仅为 RTH_MINUTE_BAR_PROXY，不宣称完整严格日线验收。

报价2025-11-03起官方改为股数，之前资料为整手。旧历史API是否统一回写单位缺少明确证据，2016样本保留原值并阻止严格成交数量验收，不盲乘100。2026案例单位采用股数。盘口仅为研究执行代理，未发生真实下单。

财报只核验了5家公司各一个季度，3个事件有事前计划公告；这不构成66股完整PIT日历，过去季度已发布也不能证明下一次日期已知。Finnhub项目凭证未找到，权限和跨度没有实际接口验收。没有关闭财报过滤，没有付费或调用模型。

成功HTTP页数可实数核对；公共SDK内部重试的额外尝试次数未独立记录，标UNKNOWN。所有请求使用SIP，120/min配额由原项目操作限速数据库共享；唯一写入原工作树的对象为既有操作配额数据库，无源代码、行情对象、服务或账本改动。
''',encoding='utf-8')
    return {'cache_requests':len(cache_manifest),'quality_files':len(quality),'raw_pages':sum(len(i['raw_pages']) for i in cache_manifest)}

def audit_full66_existing():
    """Read-only full candidate manifest/QC check, plus isolated Sep11 overlay."""
    universe=load_universe();details=[];groups=[]
    for rec in universe['records']:
        for adj in ['raw','all']:
            path=SOURCE/'datasets'/rec['symbol']/rec['identity_version']/adj/'manifest.json';manifest=read(path)
            identity_match=manifest.get('symbol')==rec['symbol'] and manifest.get('identity_version')==rec['identity_version'] and manifest.get('adjustment')==adj
            frames=[]
            for key,c in manifest['chunks'].items():
                if c.get('status')!='OK':continue
                obj=SOURCE/c['path'];file_hash=hashlib.sha256(obj.read_bytes()).hexdigest();f=pd.read_parquet(obj)
                details.append({'symbol':rec['symbol'],'adjustment':adj,'chunk':key,'path':str(obj),'sha256':file_hash,'declared_sha256':c.get('sha256'),
                    'hash_matches':file_hash==c.get('sha256'),'declared_rows':c.get('rows'),'actual_rows':len(f),'rows_match':len(f)==c.get('rows'),
                    'object_symbol_matches':f.empty or set(f.symbol)=={rec['symbol']},'manifest_identity_matches':identity_match,
                    'feed':c.get('feed'),'source_symbol':c.get('source_symbol'),'asof':c.get('asof'),'adjustment_matches':c.get('adjustment')==adj,
                    'manifest_retrieved_at':c.get('retrieved_at'),'historical_per_row_network_receipt':'UNKNOWN_NOT_INFERRED_FROM_MANIFEST_TIME'})
                if len(f):frames.append(f)
            frame=pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()
            tail=pd.read_parquet(OUT/f'latest_2026-09-11_{adj}.parquet');tail=tail[tail.symbol==rec['symbol']]
            combined=pd.concat([frame,tail],ignore_index=True)
            p=combined[['open','high','low','close']];dates=sorted(set(combined.trade_date));cal=schedule(dates[0],dates[-1]).index.strftime('%Y-%m-%d') if dates else []
            gaps=sorted(set(cal)-set(dates))
            groups.append({'symbol':rec['symbol'],'identity_version':rec['identity_version'],'adjustment':adj,'manifest_version':manifest.get('version'),'manifest_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'manifest_identity_matches':identity_match,'old_rows':len(frame),'new_tail_rows':len(tail),'combined_rows':len(combined),
                'duplicate_symbol_dates':int(combined.duplicated(['symbol','trade_date']).sum()),'first_available_not_listing_date':dates[0] if dates else None,'effective_end':dates[-1] if dates else None,
                'invalid_ohlc':int(((combined.low>combined.high)|(combined.open<combined.low)|(combined.open>combined.high)|(combined.close<combined.low)|(combined.close>combined.high)).sum()),
                'missing_price_rows':int(p.isna().any(axis=1).sum()),'nonpositive_price_rows':int((p<=0).any(axis=1).sum()),'missing_volume_rows':int(combined.volume.isna().sum()),'nonpositive_volume_rows':int((combined.volume<=0).sum()),
                'calendar_gap_count_within_observed_range':len(gaps),'gap_dates':'|'.join(gaps),'gap_reason':'UNKNOWN_OR_PRIOR_QUARANTINE_NOT_ASSUMED_HALT','source':'EXISTING_ALPACA_SIP_PLUS_ISOLATED_SEP11_OVERLAY',
                'manifest_rowhash_source_matches':all(x['hash_matches'] and x['rows_match'] and x['object_symbol_matches'] for x in details if x['symbol']==rec['symbol'] and x['adjustment']==adj)})
    pd.DataFrame(groups).to_csv(OUT/'FULL66_EXISTING_PLUS_TAIL_QC.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(details).to_csv(OUT/'FULL66_INPUT_OBJECT_LINEAGE.csv',index=False,encoding='utf-8-sig')
    ec=pd.read_csv(OUT/'EARNINGS_COVERAGE.csv');summary={'checked_at':utc(),'candidates':len(universe['records']),'groups':len(groups),'input_objects':len(details),'old_file_hash_mismatches':sum(not r['hash_matches'] for r in details),
        'row_count_mismatches':sum(not r['rows_match'] for r in details),'identity_mismatches':sum(not r['manifest_identity_matches'] or not r['object_symbol_matches'] for r in details),
        'combined_rows_raw':sum(r['combined_rows'] for r in groups if r['adjustment']=='raw'),'combined_rows_all':sum(r['combined_rows'] for r in groups if r['adjustment']=='all'),
        'duplicate_symbol_dates':sum(r['duplicate_symbol_dates'] for r in groups),'groups_with_calendar_gaps':sum(r['calendar_gap_count_within_observed_range']>0 for r in groups),
        'all_effective_ends_sep11':all(r['effective_end']=='2026-09-11' for r in groups),'earnings_matrix_rows':len(ec),'earnings_matrix_candidates':ec.symbol.nunique(),
        'earnings_complete_calendar_claims':int(ec.complete_calendar.sum()),'earnings_known_coverage_out_of_range':int((~ec.verified_next_event_known_coverage.between(0,1)).sum()),
        'earnings_known_window_sessions':int(ec.verified_next_event_known_sessions.sum()),'scope':'No APIs; existing manifests/objects plus already fetched isolated overlay; no original data mutated'}
    write(OUT/'FULL66_AUDIT_SUMMARY.json',summary);return summary

def main():
    p=argparse.ArgumentParser();p.add_argument('--cases',action='store_true');p.add_argument('--inventory',action='store_true');p.add_argument('--finnhub',action='store_true');p.add_argument('--official',action='store_true');p.add_argument('--first10');p.add_argument('--latest',action='store_true');p.add_argument('--auction',action='store_true');p.add_argument('--review',action='store_true');p.add_argument('--audit-existing',action='store_true');args=p.parse_args()
    if args.cases:run_cases()
    if args.finnhub:finnhub_probe()
    if args.inventory:inventory()
    if args.official:official_evidence()
    if args.first10:first10_probes(args.first10)
    if args.latest:latest_daily_overlay()
    if args.auction:close_reconciliation()
    if args.review:data_review()
    if args.audit_existing:audit_full66_existing()
    print(json.dumps({'status':'COMPLETE','output':str(OUT)},ensure_ascii=False))
if __name__=='__main__':main()
