"""Synthetic engineering fixtures only; never write to a research ledger."""
import pandas as pd
import pytest
from src.v11.kernel import Ledger,fill_amounts,plan_quantity,candidate_event,replay
from src.v11.data import classify_fields
from src.watchlist.engine import exit_quote,metrics

SPEC={'id':'FIXED_LEGACY','family':'LEGACY','hold':1,'stop':.05,'target':None}
def frame():return pd.DataFrame([dict(trade_date='2020-01-02',open=100.,high=101.,low=99.,close=100.,volume=100000.,vwap=100.)])

def test_known_answer_and_candidate_replay_parity():
    f=frame();event=candidate_event('X',f,SPEC,100000.,'2020-01-01')
    ledger,curve=replay({'X':f},{'2020-01-02':[dict(symbol='X',spec=SPEC,previous_volume=100000.,signal_date='2020-01-01')]},['2020-01-02'])
    t=ledger.trades[0]
    assert t['intent_time'] is None and t['received_at'] is None
    assert t['timestamp_evidence']=='HISTORICAL_SIGNAL_DATE_ONLY_INTENT_RECEIPT_UNKNOWN'
    assert t['qty']==5 and t['entry_cost']==501.50 and t['exit_proceeds']==498.50 and t['net_pnl']==-3
    for key in ('qty','entry_cost','exit_proceeds','net_pnl','return_net'):assert event[key]==t[key]
    assert ledger.cash==4998.5 and ledger.unsettled[0]['amount']==498.5
    ledger.settle(0);assert ledger.cash==4998.5
    ledger.settle(1);assert ledger.cash==5497.
    ledger.settle(1);assert ledger.cash==5497.

@pytest.mark.parametrize('kw,reason',[({'price':10000},'CAPITAL_CONSTRAINT'),({'held':True},'ALREADY_HELD'),
 ({'cash':.5},'CAPITAL_CONSTRAINT'),({'previous_volume':float('nan')},'LIQUIDITY_UNKNOWN'),({'position_count':4},'MAX_POSITIONS')])
def test_rejections(kw,reason):
    args=dict(cash=5500,equity=5500,price=100,previous_volume=100000);args.update(kw)
    assert plan_quantity(**args)['reason']==reason

def test_gap_and_double_hit():
    assert exit_quote(dict(open=90,low=89,high=112),95,110)==(90.,'GAP_STOP')
    assert exit_quote(dict(open=100,low=94,high=112),95,110)==(95.,'STOP_AND_TARGET_ADVERSE')

def test_auxiliary_warning_and_zero_activity_distinct():
    f=frame();f['vwap']=float('nan');x=classify_fields(f)
    assert x.execution_eligible.iloc[0] and x.auxiliary_warning.iloc[0]
    f['volume']=0;x=classify_fields(f)
    assert x.price_valid.iloc[0] and not x.execution_eligible.iloc[0]
    assert candidate_event('X',x,SPEC,100000,'2020-01-01')['status']=='NO_EXECUTABLE_BAR'

def test_action_idempotence_and_receivable():
    l=Ledger();l.buy('X',frame().iloc[0],'2020-01-02',0,SPEC,5500,100000,'2020-01-01')
    l.split('X',2,'s');l.split('X',2,'s');assert l.positions['X']['qty']==10
    l.dividend('X',.5,'d',None);l.dividend('X',.5,'d',None)
    l.pay_dividends('2020-02-01');assert len(l.dividends)==1 and l.dividends[0]['amount']==5
    l.split('X',.1,'r');assert l.positions['X']['qty']==1

def test_empty_no_winners_no_losers():
    assert metrics(pd.DataFrame())['count']==0
    assert metrics(pd.DataFrame({'return_net':[1.,2.]}))['profit_factor'] is None
    assert metrics(pd.DataFrame({'return_net':[-1.,-2.]}))['payoff_ratio'] is None

def test_cash_cap_and_quantity_reservation():
    p=plan_quantity(5500,5500,120,100000,quantity_cap=3,budget=250)
    assert p['quantity']==2 and p['budget']<=250

def test_bad_side():
    with pytest.raises(ValueError):fill_amounts(100,5,'ORDER')
