"""Explicit mock engineering fixtures, never written into research data."""
from dataclasses import asdict
import pytest
from src.v11.kernel import Ledger as OldLedger
from src.v131.kernel import Ledger,plan_quantity

SPEC={'id':'MOCK','stop':.05,'target':None,'hold':20}
def buy(l,s='A',price=10,cap=None,intent=None,budget=None):
    return l.buy(s,{'open':price},'2020-01-02',0,SPEC,5500,1e6,'2020-01-01',cap,budget,intent)

@pytest.mark.parametrize('price,deficit',[(1.65,-.35),(1.71,-.29)])
def test_original_two_deficits_and_fixed_reserve(price,deficit):
    old=OldLedger(cash=price+2,commission=2,friction=0)
    assert buy(old,price=price,cap=1)
    old.sell('A',price,'2020-01-03',1,'MOCK_GAP');old.settle(2)
    assert old.cash==deficit
    new=Ledger(cash=price+2,commission=2,friction=0)
    assert not buy(new,price=price,cap=1)
    assert new.fees==0 and new.cash==price+2

def test_low_price_exit_and_liability_stays_reserved_until_settled():
    l=Ledger(cash=6,commission=2,friction=0)
    assert buy(l,price=1,cap=1)
    assert (l.cash,l.reserved_cash,l.available_cash,l.fees)==(3,2,1,2)
    t=l.sell('A',.01,'2020-01-03',1,'GAP_STOP')
    assert t['exit_fee']==2 and l.reserved_cash==1.99 and l.available_cash==1.01
    assert not buy(l,'B',.01,cap=1)
    l.settle(2);assert l.cash==1.01 and l.fees==4 and l.equity({})==1.01
    l.settle(2);assert l.cash==1.01

def test_four_holdings_pending_and_fee_not_asset():
    l=Ledger();before=l.equity({});p=plan_quantity(l.available_cash,before,10,1e6)
    l.reserve('A',p['budget']);assert l.equity({})==before and l.fees==0
    assert buy(l,intent={'id':'A'},budget=p['budget'])
    for s in 'BCD':assert buy(l,s)
    assert not buy(l,'E');assert len(l.positions)==4 and l.reserved_cash==4

@pytest.mark.parametrize('reason',['CANCELLED','EXPIRED','PARTIAL_QUANTITY_REDUCTION'])
def test_release_restart_and_partial_reduction(reason):
    l=Ledger();l.reserve('x',500);l.reserve('x',500)
    restored=Ledger(**asdict(l));assert restored.reserved_cash==500
    if reason=='PARTIAL_QUANTITY_REDUCTION':
        assert buy(restored,cap=1,intent={'id':'x'},budget=500)
        assert restored.reserved_cash==1 and restored.fees==1
    else:
        restored.release('x');restored.release('x');assert restored.reserved_cash==0 and restored.fees==0

def test_dividend_receivable_not_spendable_and_reverse_split():
    l=Ledger(cash=30);assert buy(l,cap=1)
    l.dividend('A',1000,'d',None);available=l.available_cash
    assert l.equity({})>1000 and not buy(l,'B',100,cap=1)
    l.split('A',.1,'r');l.split('A',.1,'r')
    assert l.positions['A']['qty']==.1 and l.reserved_cash==1 and l.available_cash==available
    l.split('A',10,'f');assert l.positions['A']['qty']==1 and l.reserved_cash==1

def test_zero_trade_and_shared_plan_same_inputs():
    l=Ledger();assert not buy(l,price=10000);assert l.cash==5500 and l.fees==0
    p=plan_quantity(l.available_cash,5500,10,1e6)
    assert buy(l,price=10)
    assert l.positions['A']['qty']==p['quantity'] and l.positions['A']['cost']==p['buy_budget']

@pytest.mark.parametrize('price',[.13,.27,1.65,3.19,7.43,30.33,56.13,137.43,212.57])
def test_reserve_must_not_reduce_quantity_when_cash_bound_not_active(price):
    old=OldLedger();new=Ledger()
    assert buy(old,price=price) and buy(new,price=price)
    assert old.positions['A']['qty']==new.positions['A']['qty']
    assert old.positions['A']['cost']==new.positions['A']['cost']
