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
from .runtime import root,write,read,utc,digest
from .forward_protocol import freeze
from src.v11.data import Market,NY,classify_fields
from src.v13.accounts import priority
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
        if now<dt(self.config['started_at']) or session_day<dt(self.config['started_at']).astimezone(NY).date():raise ValueError('BEFORE_FORWARD_START')
        opening=schedule(session_day)
        if not opening:return 'NON_SESSION'
        start=datetime.combine(session_day,time(6,30),NY);last=datetime.combine(session_day,time(9,25),NY)
        if not start<=now<=last or now>=opening[0]:return 'MISSED_OR_NOT_YET_DECISION_WINDOW'
        if dt(cutoff)>=now or dt(received_at)>now:raise ValueError('FUTURE_DECISION_INPUT')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');l=self.ledger(c);l.settle(session_index(session_day));l.pay_dividends(str(session_day))
            pending=[json.loads(r[0]) for r in c.execute("SELECT payload FROM orders WHERE status='PENDING_BUY'")]
            reserved=sum(x['reserved_cash'] for x in pending);count=len(pending)
            for x in sorted(candidates,key=lambda x:priority(x['symbol'],x['signal_asof'])):
                symbol=x['symbol'];key=f'{session_day}:{symbol}:BUY'
                if symbol in ('SPY','QQQ','SOXX','DXYZ') or not x.get('identity_verified',False):continue
                if any(o['symbol']==symbol for o in pending):continue
                if c.execute('SELECT 1 FROM intents WHERE unique_key=?',(key,)).fetchone():continue
                estimate=x['known_close']*(1+self.config['experimental']['budget_price_collar'])
                plan=plan_quantity(l.available_cash,l.equity({}),estimate,x['previous_volume'],spec['stop'],
                                   held=symbol in l.positions,position_count=len(l.positions)+count)
                if not plan['quantity']:
                    self.event(c,key+':skip',{'type':'DECISION_SKIP','created_at':now.isoformat(),'symbol':symbol,'reason':plan['reason']});continue
                identity=str(uuid.uuid4());exit_id=identity+':EXIT';end=opening[0]+timedelta(minutes=5)
                intent={'id':identity,'account':self.config['account'],'side':'BUY','symbol':symbol,'created_at':now.isoformat(),
                        'available_at':received_at,'strategy_version':self.config['protocol_hash'],'signal_asof':x['signal_asof'],'known_data_cutoff':cutoff,'data_asof':cutoff,'received_at':received_at,
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
                l.reserve(identity,plan['budget']);reserved+=plan['budget'];count+=1
                pending.append(intent)
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
                    l.release(identity)
                    c.execute("UPDATE orders SET status='REJECTED_ACTION_REVIEW' WHERE id=?",(identity,))
                    c.execute("UPDATE orders SET status='CANCELLED_PARENT_UNFILLED' WHERE id=?",(o['contingent_exit_id'],))
                    continue
                if allowed<start+timedelta(minutes=1):continue
                try:f=market.minutes(o['symbol'],start,min(end,allowed),now=now)
                except Exception as exc:
                    self.event(c,identity+':api:'+now.isoformat(),{'type':'DATA_RETRY','symbol':o['symbol'],'at':now.isoformat(),'error_type':type(exc).__name__});continue
                f=classify_fields(f) if len(f) else f
                f=f[f.execution_eligible] if len(f) else f
                if f.empty:
                    if allowed>=end:
                        l.release(identity)
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
                try:f=market.minutes(symbol,start,end,now=now)
                except Exception as exc:
                    p['execution_status']='DATA_RETRY_'+type(exc).__name__;continue
                f=classify_fields(f) if len(f) else f
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
            return {'account':self.config['account'],'cash':l.cash,'reserved_cash':l.reserved_cash,'available_cash':l.available_cash,'equity':l.equity({}),'positions':l.positions,
                    'unsettled':l.unsettled,'dividend_receivable':l.dividends,'realized_pnl':l.realized,
                    'immutable_intents':c.execute('SELECT count(*) FROM intents').fetchone()[0],
                    'buy_fills':sum(e['type']=='BUY' for e in events),'sell_fills':sum(e['type']=='SELL' for e in events),
                    'settlement_events':sum(e['type']=='CASH_SETTLEMENT' for e in events),
                    'orders':orders,'status':'WAITING_FOR_SIGNAL' if not orders else 'EXPERIMENT_ACTIVE',
                    'strategy_status':'EXPERIMENTAL_UNPROVEN_NO_CHAMPION_NO_BROKER','checked_at':utc()}

    def cancel(self,identity,reason='CANCELLED'):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');l=self.ledger(c)
            row=c.execute("SELECT payload FROM orders WHERE id=? AND status='PENDING_BUY'",(identity,)).fetchone()
            if not row:return
            o=json.loads(row[0]);l.release(identity)
            c.execute('UPDATE orders SET status=? WHERE id=?',(reason,identity))
            c.execute("UPDATE orders SET status='CANCELLED_PARENT_UNFILLED' WHERE id=?",(o['contingent_exit_id'],))
            self.event(c,identity+':cancel',{'type':reason,'at':utc()});self.save(c,l)
