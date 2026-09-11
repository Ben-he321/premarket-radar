"""Mock minute fixtures; SQLite files stay in pytest temporary directories."""
from datetime import datetime,timezone
import json
import pandas as pd
import pytest
from src.v131.paper import Account
from src.v131.kernel import Ledger
from src.v131.identity import registry,identity_at,resolve_action

SPEC={'id':'EXP_M20_H20','stop':.05,'target':None,'hold':20}
NOW=datetime.fromisoformat('2026-09-14T10:30:00+00:00')
def config():return {'account':'MOCK','started_at':'2026-09-11T14:00:00+00:00','protocol_hash':'MOCK','experimental':{'budget_price_collar':.05}}
def candidates():return [{'symbol':s,'identity_verified':True,'known_close':10.,'previous_volume':1e6,'signal_asof':'2026-09-11'} for s in ['NVDA','AMD']]
def decide(a):return a.decide(NOW,NOW.date(),candidates(),SPEC,'2026-09-12T04:00:00+00:00',NOW.isoformat())
class Minutes:
    def __init__(self,fail=None,empty=False):self.fail=fail;self.empty=empty
    def minutes(self,symbol,start,end,now=None):
        if symbol==self.fail:raise ConnectionError('MOCK_API_FAILURE')
        if self.empty:return pd.DataFrame()
        return pd.DataFrame([{'symbol':symbol,'timestamp':pd.Timestamp(start),'trade_date':str(start.date()),'open':10.,'high':10.1,'low':9.9,'close':10.,'volume':1000.,'vwap':10.}])

def test_two_books_isolation_reservations_restart_and_immutability(tmp_path):
    a=Account(tmp_path/'a.sqlite',config());b=Account(tmp_path/'b.sqlite',config());decide(a);decide(a)
    assert a.status()['immutable_intents']==4 and b.status()['immutable_intents']==0
    assert a.status()['reserved_cash']>0 and a.status()['cash']==5500
    restart=Account(tmp_path/'a.sqlite',config());decide(restart)
    with restart.connect() as c:
        with pytest.raises(Exception):c.execute("UPDATE intents SET payload='{}'")
        intents=[json.loads(x[0]) for x in c.execute('SELECT payload FROM intents')]
    assert all(x['available_at']==NOW.isoformat() for x in intents)
    restart.match(datetime.fromisoformat('2026-09-14T13:51:00+00:00'),Minutes())
    status=restart.status();assert status['buy_fills']==2 and status['reserved_cash']==2
    restart.match(datetime.fromisoformat('2026-09-14T13:51:00+00:00'),Minutes())
    assert restart.status()['buy_fills']==2
    # Same actual price / equity / known liquidity / quantity cap as historical kernel.
    for symbol,pos in status['positions'].items():
        l=Ledger();l.buy(symbol,{'open':10},'2026-09-14',0,SPEC,5500,1e6,'2026-09-11',quantity_cap=pos['qty'],budget=pos['cost']+1)
        assert l.positions[symbol]['cost']==pos['cost'] and l.positions[symbol]['entry']==pos['entry']

def test_expiry_cancel_release_and_api_failure_does_not_expire_others(tmp_path):
    a=Account(tmp_path/'a.sqlite',config());decide(a)
    a.match(datetime.fromisoformat('2026-09-14T14:00:00+00:00'),Minutes(fail='NVDA',empty=True))
    s=a.status();assert s['orders']['PENDING_BUY']==1 and s['orders']['EXPIRED_NO_QUOTE']==1 and s['reserved_cash']>0
    with a.connect() as c:identity=c.execute("SELECT id FROM orders WHERE status='PENDING_BUY'").fetchone()[0]
    a.cancel(identity);a.cancel(identity);assert a.status()['reserved_cash']==0

def test_no_backdated_or_late_intent(tmp_path):
    a=Account(tmp_path/'a.sqlite',config())
    with pytest.raises(ValueError):a.decide(datetime.fromisoformat('2026-09-10T10:30:00+00:00'),NOW.date(),candidates(),SPEC,'2026-09-09T04:00:00+00:00',NOW.isoformat())
    assert a.decide(datetime.fromisoformat('2026-09-14T14:00:00+00:00'),NOW.date(),candidates(),SPEC,'2026-09-12T04:00:00+00:00',NOW.isoformat())=='MISSED_OR_NOT_YET_DECISION_WINDOW'
    assert a.status()['immutable_intents']==0

def test_contingent_exit_gap_settlement_and_reverse_split_restart(tmp_path):
    a=Account(tmp_path/'a.sqlite',config());decide(a)
    a.match(datetime.fromisoformat('2026-09-14T13:51:00+00:00'),Minutes())
    class Gap:
        def minutes(self,symbol,start,end,now=None):
            return pd.DataFrame([{'symbol':symbol,'timestamp':pd.Timestamp(start),'trade_date':str(start.date()),'open':.01,'high':.02,'low':.005,'close':.01,'volume':1000.,'vwap':.01}])
    # A provider reverse split changes shares, never multiplies flat exit fees.
    with a.connect() as c:
        l=a.ledger(c)
        for s in l.positions:l.split(s,.01,'mock-split-'+s)
        a.save(c,l)
    a=Account(tmp_path/'a.sqlite',config());a.match(datetime.fromisoformat('2026-09-14T14:00:00+00:00'),Gap())
    status=a.status();assert status['sell_fills']==2 and not status['positions'] and status['reserved_cash']>0
    a.roll_cash(datetime.fromisoformat('2026-09-15T10:30:00+00:00'))
    status=a.status();assert not status['unsettled'] and status['reserved_cash']==0 and status['cash']>=0
    before=status['cash'];a.roll_cash(datetime.fromisoformat('2026-09-15T10:30:00+00:00'));assert a.status()['cash']==before
    with a.connect() as c:assert a.ledger(c).fees==4

def test_h20_uses_twentieth_trading_session_close(tmp_path):
    a=Account(tmp_path/'a.sqlite',config());decide(a)
    a.match(datetime.fromisoformat('2026-09-14T13:51:00+00:00'),Minutes())
    # Explicit mock cursor representing 19 already-observed holding sessions.
    with a.connect() as c:
        l=a.ledger(c)
        for pos in l.positions.values():pos['cursor']='2026-10-09T19:59:00+00:00'
        a.save(c,l)
    a.match(datetime.fromisoformat('2026-10-09T20:21:00+00:00'),Minutes())
    assert a.status()['sell_fills']==2
    with a.connect() as c:assert all(t['reason']=='TIME_EXIT' for t in a.ledger(c).trades)

def test_identity_effective_boundary_and_new_event_not_whitelisted(monkeypatch):
    from src.v131 import identity
    records=[{'symbol':s,'name':s,'status':'INCLUDED','identity_version':'MOCK','research_start':'2016-01-01'} for s in ['META','ECHO','RKLB']+[f'MOCK{i}' for i in range(63)]]
    monkeypatch.setattr(identity,'read',lambda _: {'universe':{'records':records}})
    book=registry();assert len(book)==66
    assert identity_at('ECHO','2021-11-23',book)['symbol']=='SATS'
    assert identity_at('ECHO','2026-06-24',book)['symbol']=='ECHO'
    a={'effective_date':'2021-11-23','acquiree_cusip':'27875T101','id':'old','rate':48.25}
    assert resolve_action('ECHO','cash_mergers',a,book)['status']=='NOT_APPLICABLE_OTHER_SECURITY'
    a={'process_date':'2025-05-27','old_cusip':'773122106','new_cusip':'773121108','id':'rk'}
    assert resolve_action('RKLB','name_changes',a,book)['status']=='VERIFIED_HOLDCO_ONE_FOR_ONE'
    a['process_date']='2026-09-15';assert resolve_action('RKLB','name_changes',a,book)['status']!='VERIFIED_HOLDCO_ONE_FOR_ONE'
