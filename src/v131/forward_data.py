"""Shared two-book cache. All network market calls use the global paced SDK."""
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
import csv,io,requests
import numpy as np
import pandas as pd
from .runtime import *
from .identity import identity_at,registry,resolve_action
from .forward_protocol import freeze
from src.v11.data import Market as BaseMarket,classify_fields,NY
from src.data.alpaca_calendar import finalized_day,sessions
from src.watchlist.features import feature_frame

class Market(BaseMarket):
    def minutes(self,symbol,start,end,now=None):
        now=now or datetime.now(timezone.utc)
        if end>now-timedelta(minutes=20):raise ValueError('BASIC_LAG_20_REQUIRED')
        identity=identity_at(symbol,str(start.astimezone(NY).date()))
        if not identity:raise ValueError('IDENTITY_UNKNOWN')
        key=digest([symbol,identity['symbol'],start.isoformat(),end.isoformat(),'raw','sip'])
        path=root()/'forward_cache/minutes'/f'{key}.parquet';path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():return pd.read_parquet(path)
        f=super().minutes(identity['symbol'],start,end,now)
        if len(f):f['symbol']=symbol
        f.to_parquet(path,index=False);return f

def current_identities(now):
    day=str(now.astimezone(NY).date());path=root()/'forward_cache'/day/'identities.json'
    if path.exists():return read(path)
    index={};errors={}
    for market,name in [('NASDAQ','nasdaqlisted'),('OTHER','otherlisted')]:
        url=f'https://www.nasdaqtrader.com/dynamic/SymDir/{name}.txt'
        try:
            response=requests.get(url,timeout=30);response.raise_for_status()
            for row in csv.DictReader(io.StringIO(response.text),delimiter='|'):
                symbol=row.get('Symbol') or row.get('ACT Symbol')
                if symbol and row.get('Test Issue')!='Y':index[symbol]=row
        except requests.RequestException as exc:errors[market]=type(exc).__name__
    rows=[]
    for rec in read(prior()/'input_snapshot.json')['universe']['records']:
        row=index.get(rec['symbol']);same=bool(row and row.get('Security Name')==rec['name'] and row.get('ETF')=='N' and rec['status']=='INCLUDED' and rec['symbol']!='DXYZ')
        rows.append({'symbol':rec['symbol'],'original_name':rec['name'],'current_name':row.get('Security Name') if row else None,'identity_verified':same,'reason':'CURRENT_EXCHANGE_NAME_MATCH_STABLE_ID_UNKNOWN' if same else 'IDENTITY_OR_SCOPE_BLOCK','identity_version':rec['identity_version'],'sources':rec['sources'],'observed_at':now.isoformat()})
    out={'at':utc(),'rows':rows,'errors':errors};write(path,out);return out

def daily_frame(symbol,adj,end,market):
    destination=root()/'forward_cache'/str(end)/adj/f'{symbol}.parquet';destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists():return pd.read_parquet(destination)
    # Read-only cached historical inputs, not a replay of any historical account.
    snap=read(prior()/'input_snapshot.json');chunks=snap['manifests'].get(symbol,{}).get(adj,{}).get('chunks',{});parts=[]
    begin=end-timedelta(days=1200)
    for c in chunks.values():
        if c['status']=='OK' and c['end']>=str(begin) and c['start']<=str(end):
            p=source()/c['path'];assert sha(p)==c['sha256']
            parts.append(pd.read_parquet(p,filters=[('trade_date','>=',str(begin)),('trade_date','<=',str(end))]))
    # Newest prior forward cache is reused; it is not modified.
    for cachebase in [v11root()/'forward_data',root()/'forward_cache']:
        recent=sorted(p for p in cachebase.glob(f'*/{adj}/{symbol}.parquet') if p.parents[1].name<=str(end))
        if recent:parts.append(pd.read_parquet(recent[-1]))
    f=pd.concat(parts,ignore_index=True).drop_duplicates(['symbol','trade_date'],keep='last').sort_values('trade_date') if parts else pd.DataFrame()
    first=max(begin,date.fromisoformat(str(f.trade_date.max()))+timedelta(days=1)) if len(f) else begin
    if first<=end:
        record=registry().get(symbol,{'segments':[{'symbol':symbol,'start':'2016-01-01','end':'2099-12-31'}]})
        for seg in record['segments']:
            a=max(first,date.fromisoformat(seg['start']));b=min(end,date.fromisoformat(seg['end']))
            if a>b:continue
            z=market.daily([seg['symbol']],a,b,adj)
            if len(z):z['symbol']=symbol;f=pd.concat([f,z],ignore_index=True)
    if len(f):f=classify_fields(f.drop_duplicates(['symbol','trade_date'],keep='last').sort_values('trade_date'));f=f[(f.trade_date>=str(begin))&(f.trade_date<=str(end))]
    f.to_parquet(destination,index=False);return f

def inputs(now,market):
    p=freeze();end=finalized_day(now);cutoff=datetime.combine(end+timedelta(days=1),datetime.min.time(),NY)
    if cutoff>=now:raise ValueError('UNFINALIZED_INPUT')
    path=root()/'forward_cache'/str(end)/'decision_inputs.json'
    if path.exists():return read(path)
    identities=current_identities(now);lookup={r['symbol']:r for r in identities['rows']};frames={};issues={}
    records=read(prior()/'input_snapshot.json')['universe']['records'];calendar=[str(d) for d in sessions(date(2016,1,4),end)]
    idx=len(calendar)-1;start=524+max(0,(idx-524)//126)*126;train_a=start-524;train_b=start-20
    for s in [r['symbol'] for r in records]+['SPY']:
        try:
            frames[s]={adj:daily_frame(s,adj,end,market) for adj in ['raw','all']}
        except Exception as exc:issues[s]='DATA_RETRY_'+type(exc).__name__
    candidates=[];coverage=[];bench=frames.get('SPY',{}).get('all',pd.DataFrame())
    if len(bench):bench=bench[bench.execution_eligible]
    for rec in records:
        s=rec['symbol'];r={'symbol':s,'identity_verified':lookup[s]['identity_verified'],'state':issues.get(s,'PENDING')}
        try:
            if not r['identity_verified']:raise LookupError('IDENTITY_OR_SCOPE_BLOCK')
            raw=frames[s]['raw'];adj=frames[s]['all']
            if raw.empty or adj.empty or bench.empty:raise LookupError('NO_DATA')
            ri=raw.set_index('trade_date');ai=adj[adj.execution_eligible].set_index('trade_date');common=ri.index.intersection(ai.index)
            scale=ri.loc[common,'close']/ai.loc[common,'close'];bad=pd.Series(False,index=common)
            for k in ['open','high','low']:bad|=((ri.loc[common,k]/ai.loc[common,k])/scale-1).abs()>.01
            good=adj[adj.trade_date.isin(common)&~adj.trade_date.isin(bad[bad].index)&adj.execution_eligible]
            feat=feature_frame(good,bench).reindex(calendar)
            eligible=feat[['return_20','relative20','return_5']].notna().all(axis=1)&feat.observed.fillna(False).astype(bool)&ri.reindex(calendar).execution_eligible.fillna(False).astype(bool)
            n=int(eligible.iloc[train_a:train_b].sum());r.update(training_rows=n,signal_date=str(end))
            if n<126:raise LookupError('INSUFFICIENT_TRAINING')
            if not eligible.iloc[-1]:raise LookupError('NO_ELIGIBLE_FINALIZED_SIGNAL_BAR')
            last=feat.iloc[-1];r.update(state='QUALIFIED',M20=bool(last.return_20>0),return_20=float(last.return_20))
            candidates.append({'symbol':s,'identity_verified':True,'signal_asof':str(end),'known_close':float(ri.loc[str(end),'close']),'previous_volume':float(ri.loc[str(end),'volume']),'M20':r['M20'],'signal_hash':digest({'date':str(end),'features':{k:float(last[k]) for k in ['return_20','relative20','return_5']},'identity':lookup[s]['identity_version']})})
        except (LookupError,KeyError,ValueError) as exc:r['state']=str(exc) if isinstance(exc,LookupError) else issues.get(s,type(exc).__name__)
        coverage.append(r)
    out={'at':utc(),'received_at':utc(),'cutoff':cutoff.isoformat(),'signal_date':str(end),'candidates':candidates,'coverage':coverage,'train_start':calendar[train_a],'train_end':calendar[train_b-1],'errors':issues}
    # Transient failures remain retryable: a partial immutable attempt receipt, not a terminal cache.
    if not issues:write(path,out)
    write(root()/'latest_input_status.json',out);return out

def action_calendar(now,market):
    from alpaca.data.historical.corporate_actions import CorporateActionsClient
    from alpaca.data.requests import CorporateActionsRequest
    from src.data.alpaca_config import load_config,PROJECT_ROOT
    from src.data.alpaca_rate import SharedRateLimiter
    day=now.astimezone(NY).date();path=root()/'forward_cache'/str(day)/'actions.json'
    if path.exists():return read(path)['calendar']
    cfg=load_config();sdk=CorporateActionsClient(cfg.api_key,cfg.secret_key,raw_data=True);sdk._retry=2
    original=sdk._session.request;limiter=SharedRateLimiter(PROJECT_ROOT/'.runtime/alpaca-rate.sqlite')
    def request(method,url,**kw):
        if method!='GET' or not url.startswith('https://data.alpaca.markets/'):raise ValueError('DATA_GET_ONLY')
        limiter.acquire();kw['timeout']=(10,45);return original(method,url,**kw)
    sdk._session.request=request;calendar={};reviews=[];symbols=list(registry());failed=[]
    for offset in range(0,len(symbols),6):
        batch=symbols[offset:offset+6]
        try:raw=sdk.get_corporate_actions(CorporateActionsRequest(symbols=batch,start=day-timedelta(days=45),end=day+timedelta(days=7)))
        except Exception as exc:
            for s in batch:calendar.setdefault(str(day),[]).append({'symbol':s,'kind':'BLOCK','id':f'{day}:{s}:API_FAILURE','reason':type(exc).__name__});failed.append(s)
            continue
        for kind,items in raw.items():
            for a in items:
                s=a.get('symbol') or a.get('acquiree_symbol') or a.get('old_symbol')
                if s not in symbols:continue
                x=resolve_action(s,kind,a);reviews.append(x);d=x['day']
                if not d:continue
                event={'symbol':s,'id':x['id'],'kind':'BLOCK','reason':x['status']}
                if x['status']=='NOT_APPLICABLE_OTHER_SECURITY':continue
                if kind in ('forward_splits','reverse_splits') and x['status']=='SUPPORTED_PROVIDER_ACTION' and a.get('old_rate') and a.get('new_rate'):event.update(kind='split',ratio=float(a['new_rate'])/float(a['old_rate']))
                elif kind=='cash_dividends' and x['status']=='SUPPORTED_PROVIDER_ACTION':event.update(kind='dividend',rate=float(a['rate']),pay_date=a.get('payable_date'))
                elif x['status'] in ('VERIFIED_HOLDCO_ONE_FOR_ONE','VERIFIED_SAME_SECURITY_RENAME'):event.update(kind='split',ratio=1.)
                elif x['status']=='ACQUIRER_SHARES_UNCHANGED':continue
                calendar.setdefault(d,[]).append(event)
    out={'at':utc(),'calendar':calendar,'reviews':reviews,'failed_symbols':failed,'completeness':'PROVIDER_ONLY_NOT_COMPLETE_INDEPENDENT_ACTION_AUDIT'}
    if not failed:write(path,out)
    write(root()/'latest_action_status.json',out);return calendar
