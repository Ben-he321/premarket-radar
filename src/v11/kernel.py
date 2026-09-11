"""One versioned quantity/fee/PnL kernel for candidates, replay and paper fills."""
from dataclasses import dataclass,field
from decimal import Decimal,ROUND_HALF_UP
import math
import numpy as np
import pandas as pd
from src.watchlist.engine import Ledger as V1Ledger,exit_quote
from .runtime import digest

VERSION='EXECUTION_V1_1_1'
def rounded(x,places=2):return float(Decimal(str(x)).quantize(Decimal(10)**-places,rounding=ROUND_HALF_UP))

def fill_amounts(raw_price,quantity,side,commission=1.,friction=.001):
    if side not in ('BUY','SELL') or commission<0 or not 0<=friction<1:raise ValueError('INVALID_COST_OR_SIDE')
    if not np.isfinite(raw_price) or raw_price<=0 or quantity<=0:raise ValueError('INVALID_FILL_INPUT')
    price=rounded(raw_price*(1+friction if side=='BUY' else 1-friction),4)
    gross=rounded(quantity*price);fee=rounded(commission)
    return {'fill_price':price,'quantity':quantity,'gross':gross,'fee':fee,
            'cash_amount':rounded(gross+fee if side=='BUY' else gross-fee),
            'friction_usd':rounded(abs(price-raw_price)*quantity),'raw_price':float(raw_price)}

def plan_quantity(cash,equity,price,previous_volume,stop=.05,max_weight=.2,risk=.005,commission=1.,friction=.001,
                  held=False,position_count=0,max_positions=4,quantity_cap=None,budget=None):
    reason=None
    if held:reason='ALREADY_HELD'
    elif position_count>=max_positions:reason='MAX_POSITIONS'
    elif not np.isfinite(price) or price<=0:reason='INVALID_PRICE'
    elif not np.isfinite(previous_volume) or previous_volume<=0:reason='LIQUIDITY_UNKNOWN'
    if reason:return {'quantity':0,'reason':reason,'budget':0.}
    px=rounded(price*(1+friction),4);available=min(cash,budget) if budget is not None else cash
    qty=max(0,math.floor(min(equity*max_weight/px,(available-commission)/px,equity*risk/(px*stop),previous_volume*.001)))
    if quantity_cap is not None:qty=min(qty,int(quantity_cap))
    while qty and fill_amounts(price,qty,'BUY',commission,friction)['cash_amount']>available:qty-=1
    return {'quantity':qty,'reason':None if qty else 'CAPITAL_CONSTRAINT',
            'budget':fill_amounts(price,qty,'BUY',commission,friction)['cash_amount'] if qty else 0.,
            'risk_budget':equity*risk,'engine_version':VERSION}

@dataclass
class Ledger(V1Ledger):
    audit:list=field(default_factory=list)
    meta:dict=field(default_factory=dict)

    def settle(self,day_index):
        super().settle(day_index);self.cash=rounded(self.cash)

    def pay_dividends(self,day):
        super().pay_dividends(day);self.cash=rounded(self.cash)

    def buy(self,symbol,bar,day,day_index,spec,equity,previous_volume,signal_date,quantity_cap=None,budget=None,intent=None):
        plan=plan_quantity(self.cash,equity,float(bar['open']),previous_volume,spec['stop'],self.max_weight,self.risk,
                           self.commission,self.friction,symbol in self.positions,len(self.positions),self.max_positions,quantity_cap,budget)
        context={'symbol':symbol,'security_version':self.meta.get(symbol,'UNKNOWN'),'signal_time':signal_date,
                 'intent_time':(intent or {}).get('created_at'),'data_asof':(intent or {}).get('data_asof'),
                 'received_at':(intent or {}).get('received_at'),'parameter_version':digest(spec),'engine_version':VERSION,
                 'timestamp_evidence':'RECORDED_FORWARD_INTENT' if (intent or {}).get('created_at') else 'HISTORICAL_SIGNAL_DATE_ONLY_INTENT_RECEIPT_UNKNOWN',
                 'planned_quantity':(intent or {}).get('planned_quantity',plan['quantity'])}
        if plan['reason']:
            self.skips.append({'date':day,**context,'actual_quantity':0,'reason':plan['reason']});return False
        q=plan['quantity'];fill=fill_amounts(float(bar['open']),q,'BUY',self.commission,self.friction)
        self.cash=rounded(self.cash-fill['cash_amount']);self.fees=rounded(self.fees+fill['fee'])
        self.positions[symbol]={'qty':q,'entry':fill['fill_price'],'entry_raw':float(bar['open']),'cost':fill['cash_amount'],
            'last':float(bar['open']),'stop':float(bar['open'])*(1-spec['stop']),'target':float(bar['open'])*(1+spec['target']) if spec['target'] else None,
            'entry_index':day_index,'entry_date':day,'signal_date':signal_date,'hold':spec['hold'],'strategy':spec['id'],
            'entry_fee':fill['fee'],'entry_friction':fill['friction_usd'],'context':context}
        self.audit.append({'side':'BUY','effective_at':day,**context,'actual_quantity':q,'capital_used':fill['cash_amount'],**fill})
        return True

    def sell(self,symbol,price,day,day_index,reason):
        p=self.positions.pop(symbol);fill=fill_amounts(float(price),p['qty'],'SELL',self.commission,self.friction)
        proceeds=fill['cash_amount'];self.unsettled.append({'due':day_index+self.delay,'amount':proceeds})
        self.fees=rounded(self.fees+fill['fee']);net=rounded(proceeds-p['cost']);self.realized=rounded(self.realized+net)
        t={'symbol':symbol,'signal_date':p['signal_date'],'entry_date':p['entry_date'],'exit_date':day,'qty':p['qty'],
           'entry':p['entry'],'exit':fill['fill_price'],'net_pnl':net,'return_net':net/p['cost'],'reason':reason,'strategy':p['strategy'],
           'entry_cost':p['cost'],'exit_proceeds':proceeds,'entry_fee':p['entry_fee'],'exit_fee':fill['fee'],
           'friction_usd':rounded(p['entry_friction']+fill['friction_usd']),**p['context']}
        self.trades.append(t);self.audit.append({'side':'SELL','effective_at':day,**t});return t

def replay(frames,intents,days,actions=None,commission=1.,friction=.001,metadata=None):
    ledger=Ledger(commission=commission,friction=friction,meta=metadata or {});curve=[];marks={};actions=actions or {}
    indexed={s:d.set_index('trade_date') for s,d in frames.items()}
    for i,day in enumerate(days):
        ledger.settle(i);ledger.pay_dividends(day)
        bars={s:d.loc[day] for s,d in indexed.items() if day in d.index}
        # Eligibility describes whether a traded bar exists, not a same-day liquidity budget.
        # Quantity participation always uses the previous known session's volume.
        valid={s:b for s,b in bars.items() if bool(b.get('execution_eligible',True)) and all(np.isfinite(b[k]) and b[k]>0 for k in ('open','high','low','close'))}
        for a in actions.get(day,[]):
            if a['kind']=='split':ledger.split(a['symbol'],a['ratio'],a['id'])
            elif a['kind']=='dividend':ledger.dividend(a['symbol'],a['rate'],a['id'],a.get('pay_date'))
        for s in list(ledger.positions):
            if s in valid and valid[s]['open']<=ledger.positions[s]['stop']:ledger.sell(s,float(valid[s]['open']),day,i,'GAP_STOP')
        equity=ledger.equity({s:float(b['open']) for s,b in valid.items()})
        for order in sorted(intents.get(day,[]),key=lambda x:(-x.get('rank',0),x['symbol'])):
            s=order['symbol']
            if s not in valid:ledger.skips.append({'date':day,'symbol':s,'reason':'NO_EXECUTABLE_BAR'});continue
            ledger.buy(s,valid[s],day,i,order['spec'],equity,order['previous_volume'],order['signal_date'],intent=order)
        for s in list(ledger.positions):
            if s not in valid:continue
            pos=ledger.positions[s];bar=valid[s];price,reason=exit_quote(bar,pos['stop'],pos['target'])
            if price is None and i-pos['entry_index']+1>=pos['hold']:price,reason=float(bar['close']),'TIME_EXIT'
            if price is not None:ledger.sell(s,price,day,i,reason)
            else:pos['last']=float(bar['close'])
        marks.update({s:float(b['close']) for s,b in valid.items()});eq=ledger.equity(marks)
        curve.append({'date':day,'equity':eq,'cash':ledger.cash,'unsettled':sum(x['amount'] for x in ledger.unsettled),
                      'dividend_receivable':sum(x['amount'] for x in ledger.dividends),'realized_pnl':ledger.realized,
                      'fees':ledger.fees,'positions':len(ledger.positions),'missing_held_bars':sum(s not in valid for s in ledger.positions)})
    c=pd.DataFrame(curve)
    if len(c):c['drawdown']=c.equity/np.maximum.accumulate(np.r_[5500.,c.equity.to_numpy()])[1:]-1
    return ledger,c

def candidate_event(symbol,path,spec,previous_volume,signal_date,metadata=None,reference_cash=5500.,actions=None):
    """Fixed reference state, same execution functions; no all-adjusted fill prices."""
    ledger=Ledger(cash=reference_cash,meta=metadata or {})
    if path.empty:return {'status':'NO_PATH','symbol':symbol}
    first=path.iloc[0]
    if not bool(first.get('execution_eligible',True)) or first.volume<=0:return {'status':'NO_EXECUTABLE_BAR','symbol':symbol}
    if not ledger.buy(symbol,first,str(first.trade_date),0,spec,reference_cash,previous_volume,signal_date):
        return {'status':ledger.skips[-1]['reason'],'symbol':symbol}
    for i,(_,bar) in enumerate(path.iterrows()):
        if i:
            for a in (actions or {}).get(str(bar.trade_date),[]):
                if a['kind']=='split':ledger.split(a['symbol'],a['ratio'],a['id'])
                elif a['kind']=='dividend':ledger.dividend(a['symbol'],a['rate'],a['id'],a.get('pay_date'))
        if not bool(bar.get('execution_eligible',True)) or bar.volume<=0:continue
        p=ledger.positions[symbol];px,reason=exit_quote(bar,p['stop'],p['target'])
        if px is None and i+1>=spec['hold']:px,reason=float(bar['close']),'TIME_EXIT'
        if px is not None:
            t=ledger.sell(symbol,px,str(bar.trade_date),i,reason)
            dividend=sum(x['amount'] for x in ledger.dividends)
            return {'status':'COMPLETED',**t,'dividend_entitlement':dividend,
                    'total_net_pnl':rounded(t['net_pnl']+dividend),'return_net':(t['net_pnl']+dividend)/t['entry_cost']}
    return {'status':'UNMATURED_OR_NO_EXIT_QUOTE','symbol':symbol}
