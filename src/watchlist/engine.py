"""RESEARCH_ENGINE_V1: one deterministic daily fill kernel and cash ledger.

All-adjusted event returns are diagnostics only. Account replay uses raw prices,
explicit share actions and separate dividend receivables; never broker orders.
"""
from dataclasses import dataclass,field
import math
import numpy as np
import pandas as pd


def exit_quote(bar, stop, target=None):
    if bar['open']<=stop:return float(bar['open']),'GAP_STOP'
    hit_stop=bar['low']<=stop
    hit_target=target is not None and bar['high']>=target
    if hit_stop:return float(stop),'STOP_AND_TARGET_ADVERSE' if hit_target else 'STOP'
    if hit_target:return float(target),'TARGET'
    return None,None


def diagnostics(d,spec,signal_mask):
    events=[]; next_allowed=0
    for i in np.flatnonzero(signal_mask.to_numpy()):
        entry_i=i+1;last_i=entry_i+spec['hold']-1
        if entry_i<next_allowed or last_i>=len(d):continue
        path=d.iloc[entry_i:last_i+1]
        if path[['open','high','low','close','volume']].isna().any().any() or (path.volume<=0).any():continue
        opening=float(path.open.iloc[0]); stop=opening*(1-spec['stop'])
        target=opening*(1+spec['target']) if spec['target'] else None
        price=None;reason='TIME_EXIT';exit_i=last_i
        for offset,(_,bar) in enumerate(path.iterrows()):
            price,reason0=exit_quote(bar,stop,target)
            if price is not None:
                exit_i=entry_i+offset;reason=reason0;break
        if price is None:price=float(d.iloc[last_i].close)
        actual=d.iloc[entry_i:exit_i+1]
        gross=price/opening-1
        events.append({'signal_date':str(d.index[i]),'entry_date':str(d.index[entry_i]),'exit_date':str(d.index[exit_i]),
                       'return_gross':gross,'return_net':price*(1-.001)/(opening*(1+.001))-1-2/1100,
                       'stress25':price*(1-.0025)/(opening*(1+.0025))-1-2/1100,
                       'stress50':price*(1-.005)/(opening*(1+.005))-1-2/1100,
                       'holding':exit_i-entry_i+1,'mfe':float(actual.high.max()/opening-1),'mae':float(actual.low.min()/opening-1),
                       'market_up':bool(d.iloc[i].market_up),'exit_reason':reason,'config':spec['id'],
                       'basis':'ALL_ADJUSTED_EVENT_DIAGNOSTIC_STANDARD_1100_NOTIONAL_NOT_ACCOUNT_FILL'})
        next_allowed=exit_i+1
    return pd.DataFrame(events)


def metrics(trades, column='return_net'):
    if trades.empty:return {'count':0,'win_rate':None,'payoff_ratio':None,'expectancy':None,'profit_factor':None}
    x=trades[column];wins=x[x>0];losses=x[x<0]
    return {'count':len(x),'win_rate':float((x>0).mean()),'expectancy':float(x.mean()),'median':float(x.median()),
            'q05':float(x.quantile(.05)),'q95':float(x.quantile(.95)),
            'payoff_ratio':float(wins.mean()/-losses.mean()) if len(wins) and len(losses) else None,
            'profit_factor':float(wins.sum()/-losses.sum()) if len(losses) else None}


@dataclass
class Ledger:
    cash:float=5500.0
    commission:float=1.0
    friction:float=.001
    max_positions:int=4
    max_weight:float=.20
    risk:float=.005
    delay:int=1
    positions:dict=field(default_factory=dict)
    unsettled:list=field(default_factory=list)
    dividends:list=field(default_factory=list)
    trades:list=field(default_factory=list)
    skips:list=field(default_factory=list)
    seen_actions:set=field(default_factory=set)
    fees:float=0.0
    realized:float=0.0

    def equity(self,marks):
        return self.cash+sum(x['amount'] for x in self.unsettled)+sum(x['amount'] for x in self.dividends)+sum(p['qty']*marks.get(s,p['last']) for s,p in self.positions.items())

    def settle(self,day_index):
        due=[x for x in self.unsettled if x['due']<=day_index]
        self.cash+=sum(x['amount'] for x in due)
        self.unsettled=[x for x in self.unsettled if x['due']>day_index]

    def buy(self,symbol,bar,day,day_index,spec,equity,previous_volume,signal_date):
        reason=None;price=float(bar['open'])*(1+self.friction)
        liquidity=previous_volume if np.isfinite(previous_volume) and previous_volume>0 else 0
        qty=max(0,math.floor(min(equity*self.max_weight/price,(self.cash-self.commission)/price,
                                   equity*self.risk/(price*spec['stop']),liquidity*.001)))
        if symbol in self.positions:reason='ALREADY_HELD'
        elif len(self.positions)>=self.max_positions:reason='MAX_POSITIONS'
        elif not np.isfinite(previous_volume) or previous_volume<=0:reason='LIQUIDITY_UNKNOWN'
        elif qty<1:reason='CAPITAL_CONSTRAINT'
        if reason:
            self.skips.append({'date':day,'symbol':symbol,'reason':reason});return False
        cost=qty*price+self.commission
        self.cash-=cost;self.fees+=self.commission
        self.positions[symbol]={'qty':qty,'entry':price,'cost':cost,'last':price,'stop':float(bar['open'])*(1-spec['stop']),
                                'target':float(bar['open'])*(1+spec['target']) if spec['target'] else None,
                                'entry_index':day_index,'entry_date':day,'signal_date':signal_date,'hold':spec['hold'],'strategy':spec['id']}
        assert self.cash>=-1e-7
        return True

    def sell(self,symbol,price,day,day_index,reason):
        p=self.positions.pop(symbol);fill=float(price)*(1-self.friction)
        proceeds=p['qty']*fill-self.commission
        self.unsettled.append({'due':day_index+self.delay,'amount':proceeds})
        self.fees+=self.commission;net=proceeds-p['cost'];self.realized+=net
        self.trades.append({'symbol':symbol,'signal_date':p['signal_date'],'entry_date':p['entry_date'],'exit_date':day,'qty':p['qty'],
                            'entry':p['entry'],'exit':fill,'net_pnl':net,'return_net':net/p['cost'],'reason':reason,'strategy':p['strategy']})

    def split(self,symbol,ratio,event_id):
        if event_id in self.seen_actions:return
        self.seen_actions.add(event_id)
        if symbol not in self.positions:return
        p=self.positions[symbol];p['qty']*=ratio;p['entry']/=ratio;p['last']/=ratio;p['stop']/=ratio
        if p['target']:p['target']/=ratio
        # Corporate actions can create fractions; these are held, never new fractional orders.

    def dividend(self,symbol,rate,event_id,pay_date=None):
        if event_id in self.seen_actions:return
        self.seen_actions.add(event_id)
        if symbol in self.positions:self.dividends.append({'amount':self.positions[symbol]['qty']*rate,'pay_date':pay_date,'id':event_id})

    def pay_dividends(self,day):
        due=[x for x in self.dividends if x['pay_date'] and x['pay_date']<=day]
        self.cash+=sum(x['amount'] for x in due)
        self.dividends=[x for x in self.dividends if x not in due]


def replay(frames,intents,days,actions=None,commission=1.,friction=.001):
    ledger=Ledger(commission=commission,friction=friction);curve=[];marks={};actions=actions or {}
    indexed={s:d.set_index('trade_date') for s,d in frames.items()}
    for i,day in enumerate(days):
        ledger.settle(i);ledger.pay_dividends(day)
        bars={s:d.loc[day] for s,d in indexed.items() if day in d.index}
        for action in actions.get(day,[]):
            if action['kind']=='split':ledger.split(action['symbol'],action['ratio'],action['id'])
            elif action['kind']=='dividend':ledger.dividend(action['symbol'],action['rate'],action['id'],action.get('pay_date'))
        # Overnight holdings may exit at open/stop first; cash remains unsettled.
        for s in list(ledger.positions):
            if s not in bars:continue
            p=ledger.positions[s]
            if bars[s]['open']<=p['stop']:ledger.sell(s,float(bars[s]['open']),day,i,'GAP_STOP')
        equity=ledger.equity({s:float(b['open']) for s,b in bars.items()})
        for order in sorted(intents.get(day,[]),key=lambda x:(-x.get('rank',0),x['symbol'])):
            s=order['symbol']
            if s not in bars or not np.isfinite(bars[s]['open']) or bars[s]['open']<=0:
                ledger.skips.append({'date':day,'symbol':s,'reason':'NO_EXECUTABLE_BAR'});continue
            ledger.buy(s,bars[s],day,i,order['spec'],equity,order['previous_volume'],order['signal_date'])
        for s in list(ledger.positions):
            if s not in bars:continue
            p=ledger.positions[s];bar=bars[s]
            price,reason=exit_quote(bar,p['stop'],p['target'])
            if price is None and i-p['entry_index']+1>=p['hold']:price,reason=float(bar.close),'TIME_EXIT'
            if price is not None:ledger.sell(s,price,day,i,reason)
            else:p['last']=float(bar.close)
        marks.update({s:float(b.close) for s,b in bars.items()})
        eq=ledger.equity(marks)
        curve.append({'date':day,'equity':eq,'cash':ledger.cash,'unsettled':sum(x['amount'] for x in ledger.unsettled),
                      'dividend_receivable':sum(x['amount'] for x in ledger.dividends),'realized_pnl':ledger.realized,
                      'unrealized_pnl':sum(p['qty']*marks.get(s,p['last'])-p['cost'] for s,p in ledger.positions.items()),
                      'fees':ledger.fees,'positions':len(ledger.positions)})
    result=pd.DataFrame(curve)
    if len(result):
        peak=np.maximum.accumulate(np.r_[5500.,result.equity.to_numpy()])[1:]
        result['drawdown']=result.equity/peak-1
    return ledger,result
