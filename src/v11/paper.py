"""Isolated, delayed real-data paper execution. Contains no broker client.

Immutable buy and contingent-exit intents are authored together before the open.
SQLite transactions protect reservations and all ledger transitions on recovery.
"""
from dataclasses import asdict
from datetime import datetime,date,time,timedelta,timezone
from pathlib import Path
import json
import os
import sqlite3
import uuid
import pandas as pd
import pandas_market_calendars as mcal
from .runtime import root,write,read,utc,freeze,digest
from .data import Market,NY,snapshot,classify_fields
from .kernel import Ledger,plan_quantity,exit_quote,rounded,VERSION
from src.data.alpaca_calendar import sessions,finalized_day

def dt(x):return datetime.fromisoformat(x)
def session_index(day):return len(sessions(date(2016,1,1),day))-1
def schedule(day):
    s=mcal.get_calendar('NYSE').schedule(day,day)
    return None if s.empty else (s.market_open.iloc[0].to_pydatetime(),s.market_close.iloc[0].to_pydatetime())

class Account:
    def __init__(self,path,config,metadata=None):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True);self.config=config;self.metadata=metadata or {}
        with self.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, unique_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY,status TEXT NOT NULL,payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY,payload TEXT NOT NULL);
              CREATE TRIGGER IF NOT EXISTS immutable_intent_update BEFORE UPDATE ON intents BEGIN SELECT RAISE(ABORT,'IMMUTABLE_INTENT'); END;
              CREATE TRIGGER IF NOT EXISTS immutable_intent_delete BEFORE DELETE ON intents BEGIN SELECT RAISE(ABORT,'IMMUTABLE_INTENT'); END;''')
            c.execute('INSERT OR IGNORE INTO state VALUES (1,?)',(self.encode(Ledger(meta=self.metadata)),))

    def connect(self):
        c=sqlite3.connect(self.path,timeout=60);c.execute('PRAGMA busy_timeout=60000');return c
    @staticmethod
    def encode(ledger):
        x=asdict(ledger);x['seen_actions']=sorted(x['seen_actions']);return json.dumps(x,allow_nan=False)
    def ledger(self,c):
        x=json.loads(c.execute('SELECT payload FROM state WHERE id=1').fetchone()[0]);x['seen_actions']=set(x['seen_actions']);return Ledger(**x)
    def save(self,c,l):c.execute('UPDATE state SET payload=? WHERE id=1',(self.encode(l),))
    @staticmethod
    def event(c,key,payload):c.execute('INSERT OR IGNORE INTO events VALUES (?,?)',(key,json.dumps(payload,default=str,allow_nan=False)))

    def roll_cash(self,now):
        """Settlement does not depend on quote/API availability or new signals."""
        day=now.astimezone(NY).date()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');l=self.ledger(c);before=l.cash
            l.settle(session_index(day));l.pay_dividends(str(day))
            if l.cash!=before:self.event(c,f'cash:{day}:{len(l.unsettled)}:{l.cash}',
                {'type':'CASH_SETTLEMENT','amount':rounded(l.cash-before),'booked_at':now.isoformat()})
            self.save(c,l)

    def decide(self,now,session_day,candidates,spec,cutoff,received_at):
        opening=schedule(session_day)
        if not opening:return 'NON_SESSION'
        start=datetime.combine(session_day,time(6,30),NY);last=datetime.combine(session_day,time(9,25),NY)
        if not start<=now<=last or now>=opening[0]:return 'MISSED_OR_NOT_YET_DECISION_WINDOW'
        if dt(cutoff)>=now or dt(received_at)>now:raise ValueError('FUTURE_DECISION_INPUT')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');l=self.ledger(c);l.settle(session_index(session_day));l.pay_dividends(str(session_day))
            pending=[json.loads(r[0]) for r in c.execute("SELECT payload FROM orders WHERE status='PENDING_BUY'")]
            reserved=sum(x['reserved_cash'] for x in pending);count=len(pending)
            for x in sorted(candidates,key=lambda x:x['symbol']):
                symbol=x['symbol'];key=f'{session_day}:{symbol}:BUY'
                if symbol in ('SPY','QQQ','SOXX','DXYZ') or not x.get('identity_verified',False):continue
                if c.execute('SELECT 1 FROM intents WHERE unique_key=?',(key,)).fetchone():continue
                estimate=x['known_close']*(1+self.config['experimental']['budget_price_collar'])
                plan=plan_quantity(l.cash-reserved,l.equity({}),estimate,x['previous_volume'],spec['stop'],
                                   held=symbol in l.positions,position_count=len(l.positions)+count)
                if not plan['quantity']:
                    self.event(c,key+':skip',{'type':'DECISION_SKIP','created_at':now.isoformat(),'symbol':symbol,'reason':plan['reason']});continue
                identity=str(uuid.uuid4());exit_id=identity+':EXIT';end=opening[0]+timedelta(minutes=5)
                intent={'id':identity,'account':'EXPERIMENTAL_PAPER_V1_1','side':'BUY','symbol':symbol,'created_at':now.isoformat(),
                        'signal_asof':x['signal_asof'],'known_data_cutoff':cutoff,'data_asof':cutoff,'received_at':received_at,
                        'planned_execution_time':opening[0].isoformat(),'window_end':end.isoformat(),
                        'planned_quantity':plan['quantity'],'reserved_cash':plan['budget'],'known_close':x['known_close'],
                        'previous_volume':x['previous_volume'],'price_rule':'FIRST_POSITIVE_RTH_MINUTE_OPEN_PROXY_LAG20',
                        'spec':spec,'parameter_version':digest(spec),'engine_version':VERSION,'security_version':self.metadata.get(symbol,'UNKNOWN'),
                        'signal_hash':x.get('signal_hash'),'exit_conditions':{'stop':spec['stop'],'target':spec['target'],'hold_sessions':spec['hold'],
                        'same_bar_rule':'STOP_FIRST'},'contingent_exit_id':exit_id}
                exit_intent={**intent,'id':exit_id,'side':'CONTINGENT_SELL','parent_id':identity,'quantity_rule':'ACTUAL_HELD_SHARES_AFTER_ACTIONS'}
                for key0,obj in [(key,intent),(key+':EXIT',exit_intent)]:
                    c.execute('INSERT INTO intents VALUES (?,?,?)',(obj['id'],key0,json.dumps(obj)))
                c.execute('INSERT INTO orders VALUES (?,?,?)',(identity,'PENDING_BUY',json.dumps(intent)))
                c.execute('INSERT INTO orders VALUES (?,?,?)',(exit_id,'WAITING_FOR_BUY',json.dumps(exit_intent)))
                reserved+=plan['budget'];count+=1
            self.save(c,l)
        return 'DECISION_RECORDED'

    def match(self,now,market,actions=None):
        """Only immutable existing intents may be replayed after a restart."""
        allowed=(now-timedelta(minutes=20)).replace(second=0,microsecond=0);localday=now.astimezone(NY).date()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');l=self.ledger(c);l.settle(session_index(localday));l.pay_dividends(str(localday))
            # Action timestamps are tested against each bar below, never applied early.
            rows=c.execute("SELECT id,payload FROM orders WHERE status='PENDING_BUY'").fetchall()
            for identity,payload in rows:
                o=json.loads(payload);start=dt(o['planned_execution_time']);end=dt(o['window_end'])
                if dt(o['created_at'])>=start:raise ValueError('INTENT_NOT_AUTHORED_BEFORE_EXECUTION')
                if any(a['symbol']==o['symbol'] and a['kind']=='BLOCK' for a in (actions or {}).get(str(start.astimezone(NY).date()),[])):
                    c.execute("UPDATE orders SET status='REJECTED_ACTION_REVIEW' WHERE id=?",(identity,))
                    c.execute("UPDATE orders SET status='CANCELLED_PARENT_UNFILLED' WHERE id=?",(o['contingent_exit_id'],))
                    continue
                if allowed<start+timedelta(minutes=1):continue
                f=market.minutes(o['symbol'],start,min(end,allowed),now=now)
                f=classify_fields(f) if len(f) else f
                f=f[f.execution_eligible] if len(f) else f
                if f.empty:
                    if allowed>=end:
                        c.execute("UPDATE orders SET status='EXPIRED_NO_QUOTE' WHERE id=?",(identity,))
                        c.execute("UPDATE orders SET status='CANCELLED_PARENT_UNFILLED' WHERE id=?",(o['contingent_exit_id'],))
                        self.event(c,identity+':expiry',{'type':'NO_QUOTE_NOT_ASSUMED_HALT','booked_at':now.isoformat(),'intent_id':identity})
                    continue
                bar=f.iloc[0];stamp=bar.timestamp.to_pydatetime();day=stamp.astimezone(NY).date()
                if not start<=stamp<end:raise ValueError('OUT_OF_WINDOW_PRICE')
                reserved_others=sum(json.loads(r[0])['reserved_cash'] for r in c.execute("SELECT payload FROM orders WHERE status='PENDING_BUY' AND id!=?",(identity,)))
                budget=min(o['reserved_cash'],l.cash-reserved_others)
                ok=l.buy(o['symbol'],bar,str(day),session_index(day),o['spec'],l.equity({}),o['previous_volume'],o['signal_asof'],
                         quantity_cap=o['planned_quantity'],budget=budget,intent=o)
                if ok:
                    pos=l.positions[o['symbol']];pos['paper_intent']=identity;pos['paper_exit_intent']=o['contingent_exit_id'];pos['price_timestamp']=stamp.isoformat()
                    o['actual_quantity']=pos['qty'];o['price_timestamp']=stamp.isoformat();o['received_at_fill']=now.isoformat()
                    c.execute("UPDATE orders SET status='FILLED',payload=? WHERE id=?",(json.dumps(o),identity))
                    c.execute("UPDATE orders SET status='ACTIVE_EXIT' WHERE id=?",(o['contingent_exit_id'],))
                    self.event(c,identity+':fill',{'type':'BUY','intent_id':identity,'price_timestamp':stamp.isoformat(),
                         'effective_at':stamp.isoformat(),'received_at':now.isoformat(),'booked_at':now.isoformat(),'quantity':pos['qty'],
                         'fill_price':pos['entry'],'cost':pos['cost'],'model':'DELAYED_REAL_DATA_PAPER_PROXY_NOT_BROKER_FILL'})
                else:
                    c.execute("UPDATE orders SET status='REJECTED_AT_EXECUTION' WHERE id=?",(identity,))
                    c.execute("UPDATE orders SET status='CANCELLED_PARENT_UNFILLED' WHERE id=?",(o['contingent_exit_id'],))
            for symbol in list(l.positions):
                p=l.positions[symbol];start=dt(p.get('cursor',p['price_timestamp']))
                if allowed<=start:continue
                # Process at most one session per call; restart catches up existing intents only.
                day=start.astimezone(NY).date();bounds=schedule(day)
                if not bounds:continue
                blocked=False
                for a in (actions or {}).get(str(day),[]):
                    if a['symbol']!=symbol:continue
                    if a['kind']=='split' and p['entry_date']<str(day):l.split(symbol,a['ratio'],a['id'])
                    elif a['kind']=='dividend' and p['entry_date']<str(day):l.dividend(symbol,a['rate'],a['id'],a.get('pay_date'))
                    elif a['kind']=='BLOCK':
                        p['execution_status']='ACTION_REVIEW_REQUIRED';blocked=True
                if blocked:continue
                end=min(bounds[1],allowed)
                if end<=start:continue
                f=market.minutes(symbol,start,end,now=now);f=classify_fields(f) if len(f) else f
                f=f[f.execution_eligible] if len(f) else f
                p['execution_status']='NO_QUOTE_UNRESOLVED_NOT_ASSUMED_HALT' if f.empty else 'MONITORING'
                for _,b in f.iterrows():
                    timestamp=b.timestamp.to_pydatetime();price,reason=exit_quote(b,p['stop'],p['target'])
                    matured=session_index(day)-p['entry_index']+1>=p['hold']
                    if price is None and session_index(day)-p['entry_index']+1>p['hold']:price,reason=float(b.open),'DELAYED_TIME_EXIT_FIRST_AVAILABLE_QUOTE'
                    if price is None and matured and timestamp+timedelta(minutes=1)>=bounds[1]:price,reason=float(b.close),'TIME_EXIT'
                    if price is not None:
                        exit_id=p['paper_exit_intent'];t=l.sell(symbol,price,str(day),session_index(day),reason)
                        c.execute("UPDATE orders SET status='FILLED' WHERE id=?",(exit_id,))
                        self.event(c,exit_id+':fill',{'type':'SELL','intent_id':exit_id,'price_timestamp':timestamp.isoformat(),
                            'effective_at':timestamp.isoformat(),'received_at':now.isoformat(),'booked_at':now.isoformat(),**t});break
                if symbol in l.positions:
                    if len(f):p['last']=float(f.close.iloc[-1])
                    p['cursor']=end.isoformat()
                    if end>=bounds[1]:
                        nxt=next(d for d in sessions(day+timedelta(days=1),day+timedelta(days=14)))
                        p['cursor']=schedule(nxt)[0].isoformat()
            self.save(c,l)

    def status(self):
        with self.connect() as c:
            l=self.ledger(c);orders=dict(c.execute('SELECT status,count(*) FROM orders GROUP BY status'))
            events=[json.loads(x[0]) for x in c.execute('SELECT payload FROM events')]
            return {'account':'EXPERIMENTAL_PAPER_V1_1','cash':l.cash,'equity':l.equity({}),'positions':l.positions,
                    'unsettled':l.unsettled,'dividend_receivable':l.dividends,'realized_pnl':l.realized,
                    'immutable_intents':c.execute('SELECT count(*) FROM intents').fetchone()[0],
                    'buy_fills':sum(e['type']=='BUY' for e in events),'sell_fills':sum(e['type']=='SELL' for e in events),
                    'settlement_events':sum(e['type']=='CASH_SETTLEMENT' for e in events),
                    'orders':orders,'status':'WAITING_FOR_SIGNAL' if not orders else 'EXPERIMENT_ACTIVE',
                    'strategy_status':'EXPERIMENTAL_UNPROVEN_NO_CHAMPION_NO_BROKER','checked_at':utc()}

def real_decision_inputs(now,market,symbols=None):
    """Fresh finalized raw/all data, only in this version's forward cache."""
    from src.watchlist.features import feature_frame,signal
    snap=snapshot();cfg=freeze();spec=next(x for x in cfg['configs'] if x['id']=='FIXED_LEGACY')
    end=finalized_day(now);start=end-timedelta(days=180);frames={};raw={};issues=[]
    requested=[r for r in snap['universe']['records'] if symbols is None or r['symbol'] in symbols]
    records=requested+snap['universe']['references']
    for offset in range(0,len(records),6):
        batch=[r['symbol'] for r in records[offset:offset+6]]
        for adj,dest in [('raw',raw),('all',frames)]:
            directory=root()/'forward_data'/str(end)/adj;directory.mkdir(parents=True,exist_ok=True)
            missing=[]
            for symbol in batch:
                cached=directory/f'{symbol}.parquet'
                f=pd.read_parquet(cached) if cached.exists() else pd.DataFrame()
                if len(f) and str(end) in f.trade_date.values:dest[symbol]=classify_fields(f)
                else:missing.append(symbol)
            if not missing:continue
            try:result=market.daily(missing,start,end,adj)
            except Exception as exc:
                for symbol in missing:
                    dest[symbol]=pd.DataFrame();issues.append({'symbol':symbol,'adjustment':adj,'reason':'REQUEST_FAILED','error_type':type(exc).__name__})
                continue
            for symbol in missing:
                f=result[result.symbol==symbol].copy();f.to_parquet(directory/f'{symbol}.parquet',index=False)
                dest[symbol]=classify_fields(f) if len(f) else f
    candidates=[];benchmark=frames['SPY'];benchmark=benchmark[benchmark.execution_eligible] if len(benchmark) else benchmark
    for rec in requested:
        symbol=rec['symbol'];f=frames[symbol];r=raw[symbol]
        if f.empty or r.empty:issues.append({'symbol':symbol,'reason':'NO_DATA'});continue
        f=feature_frame(f[f.execution_eligible],benchmark)
        if str(end) not in f.index or str(end) not in r.trade_date.values:issues.append({'symbol':symbol,'reason':'LATEST_SESSION_MISSING'});continue
        b=r[r.trade_date==str(end)].iloc[0]
        if not bool(b.execution_eligible):issues.append({'symbol':symbol,'reason':'NO_EXECUTABLE_LAST_SESSION'});continue
        if bool(signal(f,spec).loc[str(end)]):
            candidates.append({'symbol':symbol,'known_close':float(b.close),'previous_volume':float(b.volume),
                               'signal_asof':str(end),'signal_hash':digest(f.loc[str(end)].fillna('MISSING').to_dict()),
                               'identity_verified':rec['status']=='INCLUDED'})
    received=utc();write(root()/'forward_input_status.json',{'received_at':received,'cutoff':str(end),'issues':issues,'candidate_symbols':[c['symbol'] for c in candidates]})
    return candidates,spec,datetime.combine(end+timedelta(days=1),time(),NY).isoformat(),received

def decision_cycle(account,market,now,records,input_loader=None,actions_loader=None,clock_now=None):
    """Persist per-symbol progress. At most three attempts inside the original window."""
    input_loader=input_loader or real_decision_inputs;actions_loader=actions_loader or forward_actions
    clock_now=clock_now or (lambda:datetime.now(timezone.utc))
    day=now.astimezone(NY).date();directory=account.path.parent
    path=directory/f'decision-progress-{day}.json'
    doc=read(path,{'date':str(day),'symbols':{}});states=doc['symbols']
    for rec in records:
        s=rec['symbol']
        states.setdefault(s,{'status':'PERMANENT_BLOCK' if s=='DXYZ' or rec.get('status')!='INCLUDED' else 'PENDING_RETRY',
                             'attempts':0,'reason':'SCOPE_OR_IDENTITY' if s=='DXYZ' or rec.get('status')!='INCLUDED' else None})
    first=datetime.combine(day,time(6,30),NY);last=datetime.combine(day,time(9,25),NY)
    if not schedule(day) or now<first:return doc
    if now>last:
        for v in states.values():
            if v['status'] in ('PENDING_RETRY','RETRY_LIMIT_REACHED'):v.update(status='MISSED_WINDOW',updated_at=now.isoformat())
        write(path,doc);return doc
    pending=[s for s,v in states.items() if v['status']=='PENDING_RETRY' and v['attempts']<3 and
             (not v.get('next_retry') or dt(v['next_retry'])<=now)]
    if not pending:write(path,doc);return doc
    for s in pending:states[s].update(attempts=states[s]['attempts']+1,next_retry=(now+timedelta(minutes=1)).isoformat())
    write(path,doc) # A crash cannot reset the attempt budget.
    try:
        candidates,spec,cutoff,received=input_loader(now,market,symbols=pending)
        issues=read(root()/'forward_input_status.json',{}).get('issues',[])
        failures={x['symbol']:x.get('reason','REQUEST_FAILED') for x in issues if x['symbol'] in pending}
        actions=actions_loader(now,market)
    except Exception as exc:
        candidates=[];failures={s:type(exc).__name__ for s in pending};actions={};spec=None
    current=clock_now()
    blocked={a['symbol'] for d,rows in actions.items() if d>=str(day) for a in rows if a['kind']=='BLOCK' and 'ACTION_ACCESS_FAILURE' not in a['id']}
    for d,rows in actions.items():
        if d>=str(day):
            for a in rows:
                if 'ACTION_ACCESS_FAILURE' in a['id']:failures[a['symbol']]='ACTION_ACCESS_FAILURE'
    by_symbol={x['symbol']:x for x in candidates}
    # Account.decide atomically preserves alphabetical priority among available signals.
    usable=[x for x in candidates if x['symbol'] not in failures and x['symbol'] not in blocked]
    if usable and current<=last:account.decide(current,day,usable,spec,cutoff,received)
    for s in pending:
        v=states[s];v['updated_at']=current.isoformat()
        if current>last:v.update(status='MISSED_WINDOW',reason='FETCH_FINISHED_AFTER_DEADLINE')
        elif s in blocked:v.update(status='PERMANENT_BLOCK',reason='CORPORATE_ACTION_REVIEW')
        elif s in failures:v.update(status='PENDING_RETRY' if v['attempts']<3 else 'RETRY_LIMIT_REACHED',reason=failures[s])
        elif s in by_symbol:v.update(status='SIGNAL_COMPLETED',reason='ACCOUNT_INTENT_OR_CAPITAL_GATE_RECORDED')
        else:v.update(status='NO_SIGNAL_COMPLETED',reason=None)
    doc['updated_at']=current.isoformat();write(path,doc);return doc

def service():
    import time as clock
    import msvcrt
    if read(root()/'engineering_checks.json',{}).get('status')!='PASS':raise ValueError('ENGINEERING_GATE_NOT_PASSED')
    if read(root()/'sip_checks.json',{}).get('historical',{}).get('status')!='ACCESS_OK':raise ValueError('HISTORICAL_SIP_GATE_NOT_PASSED')
    p=root()/'experimental_paper';p.mkdir(exist_ok=True)
    lock=(p/'service.lock').open('a+b');lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:return
    config=freeze();meta={r['symbol']:r['identity_version'] for r in snapshot()['universe']['records']}
    account=Account(p/'ledger.sqlite',config,meta);market=Market()
    life=read(p/'lifetime.json')
    if not life:
        life={'started_at':utc(),'expires_at':(datetime.now(timezone.utc)+timedelta(days=30)).isoformat()};write(p/'lifetime.json',life)
    while not (p/'STOP').exists() and datetime.now(timezone.utc)<dt(life['expires_at']):
        now=datetime.now(timezone.utc);day=now.astimezone(NY).date();bounds=schedule(day);error=None
        try:
            account.roll_cash(now)
            # No action assumptions for live held positions: daily source audit required first.
            status=account.status()
            if status['positions'] or status['orders'].get('PENDING_BUY'):
                actions=forward_actions(now,market)
                account.match(now,market,actions)
            if bounds:decision_cycle(account,market,now,snapshot()['universe']['records'])
        except Exception as exc:
            error={'type':type(exc).__name__,'http_status':getattr(exc,'status_code',None),'at':utc()}
            write(p/'last_error.json',error)
        nextday=next(d for d in sessions(day,day+timedelta(days=14)) if datetime.combine(d,time(6,30),NY)>now)
        status=account.status();status.update(pid=os.getpid(),heartbeat=utc(),expires_at=life['expires_at'],error=error,
            next_decision=datetime.combine(nextday,time(6,30),NY).isoformat(),next_poll=(now+timedelta(seconds=30)).isoformat(),
            process_command='python -m src.v11 paper-service',singleton_lock=str(p/'service.lock'),
            stop_file=str(p/'STOP'),resume='python -m src.v11 paper-service')
        write(root()/'experimental_paper_status.json',status);clock.sleep(30)
    status=account.status();status.update(service='STOPPED_OR_EXPIRED',stopped_at=utc());write(root()/'experimental_paper_status.json',status)

def forward_actions(now,market):
    """Separately cached read-only corporate actions; unknown complex actions block symbol."""
    from alpaca.data.historical.corporate_actions import CorporateActionsClient
    from alpaca.data.requests import CorporateActionsRequest
    from src.data.alpaca_config import load_config,PROJECT_ROOT
    from src.data.alpaca_rate import SharedRateLimiter
    day=now.astimezone(NY).date();path=root()/'forward_actions'/f'{day}.json'
    if path.exists():return read(path)['calendar']
    cfg=load_config();sdk=CorporateActionsClient(cfg.api_key,cfg.secret_key,raw_data=True);sdk._retry=2
    limiter=SharedRateLimiter(PROJECT_ROOT/'.runtime/alpaca-rate.sqlite');original=sdk._session.request
    def request(method,url,**kw):
        if method!='GET' or not url.startswith('https://data.alpaca.markets/'):raise ValueError('DATA_GET_ONLY')
        limiter.acquire();kw['timeout']=(10,45);return original(method,url,**kw)
    sdk._session.request=request
    result={};records=snapshot()['universe']['records']
    for offset in range(0,len(records),6):
        symbols=[r['symbol'] for r in records[offset:offset+6]]
        try:data=sdk.get_corporate_actions(CorporateActionsRequest(symbols=symbols,start=day-timedelta(days=45),end=day+timedelta(days=7)))
        except Exception as exc:
            for symbol in symbols:result.setdefault(str(day),[]).append({'symbol':symbol,'id':f'{day}:{symbol}:ACTION_ACCESS_FAILURE','kind':'BLOCK','reason':type(exc).__name__})
            continue
        for kind,rows in data.items():
            for a in rows:
                symbol=a.get('symbol') or a.get('acquiree_symbol') or a.get('old_symbol');d=a.get('ex_date') or a.get('effective_date') or a.get('process_date')
                if not symbol or not d:continue
                item={'symbol':symbol,'id':a['id'],'kind':'BLOCK'}
                if kind in ('forward_splits','reverse_splits') and a.get('old_rate') and a.get('new_rate'):item.update(kind='split',ratio=float(a['new_rate'])/float(a['old_rate']))
                elif kind=='cash_dividends':item.update(kind='dividend',rate=float(a['rate']),pay_date=a.get('payable_date'))
                result.setdefault(d,[]).append(item)
    write(path,{'received_at':utc(),'calendar':result,'completeness':'PROVIDER_ONLY'});return result
