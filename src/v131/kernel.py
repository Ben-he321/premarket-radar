"""Exit-fee collateral; no early fees, cash injection or change to T+1.

Reservations are a restriction on existing settled cash, never another asset.
After selling below commission, the negative unsettled liability remains reserved
until settlement. This closes the release-before-settlement spending loophole.
"""
from dataclasses import dataclass,field
from src.v11.kernel import Ledger as OldLedger,plan_quantity as old_plan,fill_amounts,rounded,exit_quote

VERSION='EXECUTION_V1_3_1_EXIT_RESERVE'

def plan_quantity(cash,equity,price,previous_volume,stop=.05,max_weight=.2,risk=.005,commission=1.,friction=.001,
                  held=False,position_count=0,max_positions=4,quantity_cap=None,budget=None,exit_fee=None):
    fee=commission if exit_fee is None else exit_fee
    available=min(cash,budget) if budget is not None else cash
    p=old_plan(available-fee,equity,price,previous_volume,stop,max_weight,risk,commission,friction,held,position_count,max_positions,quantity_cap)
    p.update(buy_budget=p['budget'],exit_fee_reserve=fee if p['quantity'] else 0.,engine_version=VERSION)
    p['budget']=rounded(p['budget']+p['exit_fee_reserve']);return p

@dataclass
class Ledger(OldLedger):
    pending:dict=field(default_factory=dict)

    @property
    def reserved_cash(self):
        return rounded(sum(p['exit_fee_reserve'] for p in self.positions.values())+sum(self.pending.values())+
                       sum(max(0.,-x['amount']) for x in self.unsettled))
    @property
    def available_cash(self):return rounded(self.cash-self.reserved_cash)

    def reserve(self,key,amount):
        amount=rounded(amount)
        if key in self.pending:
            if self.pending[key]!=amount:raise ValueError('IMMUTABLE_RESERVATION_CHANGED')
            return
        if amount<0 or amount>self.available_cash:raise ValueError('INSUFFICIENT_RESERVABLE_CASH')
        self.pending[key]=amount

    def release(self,key):self.pending.pop(key,None)

    def buy(self,symbol,bar,day,day_index,spec,equity,previous_volume,signal_date,quantity_cap=None,budget=None,intent=None):
        key=(intent or {}).get('id');own=self.pending.get(key,0.)
        available=rounded(self.available_cash+own)
        total_budget=min(available,budget) if budget is not None else available
        plan=plan_quantity(available,equity,float(bar['open']),previous_volume,spec['stop'],self.max_weight,self.risk,
                           self.commission,self.friction,symbol in self.positions,len(self.positions),self.max_positions,quantity_cap,total_budget)
        if key:self.release(key)
        if not plan['quantity']:
            self.skips.append({'symbol':symbol,'date':day,'reason':plan['reason'],'engine_version':VERSION});return False
        # Keep the original available-money bound through the shared planner.
        # Feeding its rounded purchase cost back as a new tight bound can floor
        # one extra share from fractional cents even when cash is plentiful.
        ok=super().buy(symbol,bar,day,day_index,spec,equity,previous_volume,signal_date,plan['quantity'],total_budget-self.commission,intent)
        if ok:
            p=self.positions[symbol];p['exit_fee_reserve']=self.commission;p['context']['engine_version']=VERSION
            self.audit[-1]['engine_version']=VERSION
        self.assert_cash();return ok

    def sell(self,symbol,price,day,day_index,reason):
        original=self.commission;self.commission=self.positions[symbol]['exit_fee_reserve']
        try:result=super().sell(symbol,price,day,day_index,reason)
        finally:self.commission=original
        self.assert_cash();return result

    def settle(self,day_index):
        super().settle(day_index);self.assert_cash()

    def assert_cash(self):
        if self.cash<0 or self.available_cash<0:raise ValueError('CASH_RESERVATION_INVARIANT')
