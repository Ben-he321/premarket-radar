"""Deterministic synthetic lifecycle tests; SQLite lives only under pytest tmp_path."""
from datetime import datetime,date,timedelta,timezone
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import pytest
from src.v11.paper import Account,session_index

SPEC={'id':'FIXED_LEGACY','family':'LEGACY','hold':1,'stop':.05,'target':None}
CONFIG={'experimental':{'budget_price_collar':.05}}
NOW=datetime(2026,9,11,10,30,tzinfo=timezone.utc)
CUTOFF='2026-09-11T04:00:00+00:00'
def candidate(symbol='NVDA'):return dict(symbol=symbol,known_close=100.,previous_volume=100000.,signal_asof='2026-09-10',identity_verified=True)
def decide(a,now=NOW,items=None):return a.decide(now,date(2026,9,11),items or [candidate()],SPEC,CUTOFF,NOW.isoformat())

class Market:
    def __init__(self,rows):self.rows=rows;self.calls=[]
    def minutes(self,symbol,start,end,now):
        assert end<=now-timedelta(minutes=20)
        self.calls.append((symbol,start,end))
        f=pd.DataFrame([dict(symbol=symbol,timestamp=pd.Timestamp(t),open=o,high=h,low=l,close=c,volume=10000.,vwap=c,trade_date='2026-09-11') for t,o,h,l,c in self.rows])
        return f[(f.timestamp>=start)&(f.timestamp<end)] if len(f) else f

def test_complete_lifecycle_and_restart(tmp_path):
    path=tmp_path/'test.sqlite';a=Account(path,CONFIG);decide(a)
    assert a.status()['immutable_intents']==2
    market=Market([('2026-09-11T13:30:00Z',100,101,99,100),('2026-09-11T19:59:00Z',100,101,99,100)])
    a.match(datetime(2026,9,11,13,56,tzinfo=timezone.utc),market)
    assert a.status()['buy_fills']==1 and len(a.status()['positions'])==1
    a=Account(path,CONFIG);a.match(datetime(2026,9,11,20,21,tzinfo=timezone.utc),market)
    assert a.status()['sell_fills']==1 and a.status()['realized_pnl']==-3
    a.match(datetime(2026,9,14,10,30,tzinfo=timezone.utc),market)
    assert a.status()['cash']==5497 and not a.status()['unsettled']
    a.match(datetime(2026,9,14,10,30,tzinfo=timezone.utc),market)
    assert a.status()['buy_fills']==1 and a.status()['sell_fills']==1

def test_intents_immutable_and_concurrent_reservations(tmp_path):
    a=Account(tmp_path/'test.sqlite',CONFIG)
    with ThreadPoolExecutor(2) as ex:list(ex.map(lambda _:decide(a,items=[candidate(s) for s in ['NVDA','AMD','MSFT','MU','AAPL']]),range(2)))
    assert a.status()['immutable_intents']==8
    with a.connect() as c:
        with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE intents SET payload='{}'")
        with pytest.raises(sqlite3.IntegrityError):c.execute('DELETE FROM intents')

def test_missed_decision_never_backdates(tmp_path):
    a=Account(tmp_path/'test.sqlite',CONFIG)
    assert decide(a,NOW+timedelta(hours=4))=='MISSED_OR_NOT_YET_DECISION_WINDOW'
    assert a.status()['immutable_intents']==0

def test_no_quote_expires_not_halt_claim(tmp_path):
    a=Account(tmp_path/'test.sqlite',CONFIG);decide(a)
    a.match(datetime(2026,9,11,14,tzinfo=timezone.utc),Market([]))
    assert a.status()['orders']['EXPIRED_NO_QUOTE']==1 and a.status()['cash']==5500
    assert a.status()['buy_fills']==0

def test_delayed_data_not_read_early_and_adverse_stop(tmp_path):
    a=Account(tmp_path/'test.sqlite',CONFIG);decide(a)
    m=Market([('2026-09-11T13:30:00Z',100,111,94,99)])
    a.match(datetime(2026,9,11,13,40,tzinfo=timezone.utc),m);assert not m.calls
    a.match(datetime(2026,9,11,13,56,tzinfo=timezone.utc),m)
    assert a.status()['sell_fills']==1 and a.status()['realized_pnl']<0

def test_references_not_ordered(tmp_path):
    a=Account(tmp_path/'test.sqlite',CONFIG);decide(a,items=[candidate(s) for s in ['SPY','QQQ','SOXX','DXYZ']])
    assert a.status()['immutable_intents']==0

def test_future_input_rejected(tmp_path):
    a=Account(tmp_path/'test.sqlite',CONFIG)
    with pytest.raises(ValueError):a.decide(NOW,date(2026,9,11),[candidate()],SPEC,(NOW+timedelta(seconds=1)).isoformat(),NOW.isoformat())

def test_gap_budget_cannot_increase_planned_shares(tmp_path):
    a=Account(tmp_path/'test.sqlite',CONFIG);decide(a)
    m=Market([('2026-09-11T13:30:00Z',200,201,199,200)])
    a.match(datetime(2026,9,11,13,56,tzinfo=timezone.utc),m)
    assert a.status()['positions']['NVDA']['qty']==2
    assert a.status()['cash']>=5500-527

def test_unresolved_action_blocks_only_affected_buy(tmp_path):
    a=Account(tmp_path/'test.sqlite',CONFIG);decide(a,items=[candidate('NVDA'),candidate('AMD')])
    m=Market([('2026-09-11T13:30:00Z',100,101,99,100)])
    a.match(datetime(2026,9,11,13,56,tzinfo=timezone.utc),m,{'2026-09-11':[{'symbol':'NVDA','kind':'BLOCK','id':'unknown'}]})
    assert 'AMD' in a.status()['positions'] and 'NVDA' not in a.status()['positions']
    assert a.status()['orders']['REJECTED_ACTION_REVIEW']==1
