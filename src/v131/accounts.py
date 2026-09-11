"""486 finite shared-cash replays. V1.1 fills/quantity/cash, no broker imports."""
from collections import Counter, defaultdict
import hashlib
import numpy as np
import pandas as pd
from .kernel import Ledger, rounded
from src.watchlist.engine import exit_quote
from src.v12.accounts import trade_components
from .runtime import *
from .data import condition
from .identity import review as complex_actions
import gzip,json

def uniform(seed,symbol,day):
    return int.from_bytes(hashlib.sha256(f'activity|{seed}|{symbol}|{day}'.encode()).digest()[:8],'big')/2**64

def priority(symbol,day):return hashlib.sha256(f'priority|{symbol}|{day}'.encode()).digest()

def replay_indexed(bars,orders,days,actions,complex_calendar,fee,friction,metadata):
    """Equivalent event order to V1.1 replay, with additional exposure/action diagnostics."""
    ledger=Ledger(commission=fee,friction=friction,meta=metadata);curve=[];marks={};issues=[];skips=Counter();unverified=False
    for i,day in enumerate(days):
        ledger.settle(i);ledger.pay_dividends(day);valid=bars.get(day,{})
        for a in actions.get(day,[]):
            if a['kind']=='split':
                ledger.split(a['symbol'],a['ratio'],a['id'])
                # A stale mark must use the new share unit on a missing split-day quote.
                if a['symbol'] in marks:marks[a['symbol']]/=a['ratio']
            elif a['kind']=='dividend':ledger.dividend(a['symbol'],a['rate'],a['id'],a.get('pay_date'))
        for a in complex_calendar.get(day,[]):
            if a['symbol'] in ledger.positions:
                issues.append({**a,'held_quantity':ledger.positions[a['symbol']]['qty']})
                if a['status']=='UNVERIFIED_COMPLEX_ACTION':unverified=True
        for s in list(ledger.positions):
            if s in valid and valid[s]['open']<=ledger.positions[s]['stop']:ledger.sell(s,float(valid[s]['open']),day,i,'GAP_STOP')
        opens={s:float(b['open']) for s,b in valid.items()};equity=ledger.equity(opens)
        for order in orders.get(day,[]):
            s=order['symbol']
            if s not in valid:skips['NO_EXECUTABLE_BAR']+=1;continue
            if s in ledger.positions:skips['ALREADY_HELD']+=1;continue
            if len(ledger.positions)>=ledger.max_positions:skips['MAX_POSITIONS']+=1;continue
            ledger.buy(s,valid[s],day,i,order['spec'],equity,order['previous_volume'],order['signal_date'],intent=order)
        post_open_value=sum(pos['qty']*opens.get(s,pos['last']) for s,pos in ledger.positions.items())
        post_open_equity=ledger.equity(opens)
        missing_before=sum(s not in valid for s in ledger.positions)
        for s in list(ledger.positions):
            if s not in valid:continue
            pos=ledger.positions[s];bar=valid[s];price,reason=exit_quote(bar,pos['stop'],pos['target'])
            if price is None and i-pos['entry_index']+1>=pos['hold']:price,reason=float(bar['close']),'TIME_EXIT'
            if price is not None:
                entry_index=pos['entry_index'];t=ledger.sell(s,price,day,i,reason);t['holding_sessions']=i-entry_index+1
            else:pos['last']=float(bar['close'])
        marks.update({s:float(b['close']) for s,b in valid.items()});eq=ledger.equity(marks)
        close_value=sum(pos['qty']*marks.get(s,pos['last']) for s,pos in ledger.positions.items())
        curve.append({'date':day,'equity':eq,'cash':ledger.cash,'unsettled':sum(x['amount'] for x in ledger.unsettled),
                      'reserved_cash':ledger.reserved_cash,'available_cash':ledger.available_cash,'dividend_receivable':sum(x['amount'] for x in ledger.dividends),'realized_pnl':ledger.realized,'fees':ledger.fees,
                      'positions':len(ledger.positions),'missing_held_bars':missing_before,
                      'close_utilization':close_value/eq if eq>0 else 0.,
                      'post_entry_open_utilization':post_open_value/post_open_equity if post_open_equity>0 else 0.,
                      'unverified_complex_action':unverified})
    for x in ledger.skips:skips[x['reason']]+=1
    c=pd.DataFrame(curve)
    c['drawdown']=c.equity/np.maximum.accumulate(np.r_[5500.,c.equity.to_numpy()])[1:]-1
    return ledger,c,issues,dict(skips)

def build_inputs(data):
    p=protocol();days=p['days'];bars=defaultdict(dict);candidates=defaultdict(list);training_hashes={}
    included={s:x for s,x in data.items() if s!='DXYZ' and x['record']['status']=='INCLUDED'}
    for s,item in included.items():
        raw=item['raw'];f=item['features'];ri=raw.set_index('trade_date')
        for b in raw[raw.execution_eligible].to_dict('records'):
            bars[b['trade_date']][s]={k:b[k] for k in ['open','high','low','close','volume']}
        for i in np.flatnonzero(item['eligible']):
            day=days[i];window=next(w for w in p['windows'] if w['evaluation'][0]<=i<w['evaluation'][1])
            candidates[i].append({'symbol':s,'signal_date':day,'previous_volume':float(ri.loc[day,'volume']),
                'accepted':{c:bool(condition(f.iloc[[i]],c).iloc[0]) for c in CONDITIONS},
                'probabilities':{c:float(item['probability'][c][i]) for c in CONDITIONS},'window_end':window['evaluation'][1]})
    for i,rows in candidates.items():rows.sort(key=lambda x:priority(x['symbol'],x['signal_date']))
    return bars,candidates,{s:x['record']['identity_version'] for s,x in included.items()}

def make_orders(candidates,days,name,h,seed=None):
    orders={};spec={'id':f'V13_{name}_H{h}','hold':h,'stop':.05,'target':None}
    for i,rows in candidates.items():
        for x in rows:
            if i+h>=x['window_end']:continue
            accepted=True if name=='U' else (uniform(seed,x['symbol'],x['signal_date'])<x['probabilities'][name] if seed is not None else x['accepted'][name])
            if accepted:
                orders.setdefault(days[i+1],[]).append({'symbol':x['symbol'],'spec':spec,'previous_volume':x['previous_volume'],'signal_date':x['signal_date']})
    return orders

def cases():
    p=protocol();result=[]
    for h in p['horizons']:
        for name in [*CONDITIONS,'U']:
            for cost,friction,fee in p['accounts']['costs']:
                result.append({'id':f'{name}-H{h}-{cost}','condition':name,'horizon':h,'seed':None,'kind':'UNCONDITIONAL' if name=='U' else 'CONDITION','cost_case':cost,'friction':friction,'fee':fee})
        for name in CONDITIONS:
            for seed in p['random_seeds']:
                result.append({'id':f'{name}-H{h}-random-{seed}','condition':name,'horizon':h,'seed':seed,'kind':'ACTIVITY_RANDOM','cost_case':'base','friction':.001,'fee':1.})
    return result

def run(data):
    p=protocol();bars,candidates,metadata=build_inputs(data);actions=read(root()/'actions.json');complex_calendar=complex_actions()
    write(root()/'complex_action_calendar.json',complex_calendar)
    days=p['days'][p['windows'][0]['evaluation'][0]:];dest=root()/'accounts';dest.mkdir(exist_ok=True)
    action_by_symbol=defaultdict(dict)
    for day,items in actions.items():
        for a in items:action_by_symbol[a['symbol']].setdefault(day,[]).append(a)
    result=[];all_cases=cases();write(root()/'expected_accounts.json',all_cases)
    for case in all_cases:
        check_deadline();name=case['id'];summary_path=dest/f'{name}.json'
        if summary_path.exists():
            row=read(summary_path)
            if all(sha(dest/f'{name}-{suffix}.csv')==row['output_hashes'][suffix] for suffix in ['equity','trades']):result.append(row);continue
            raise ValueError('ACCOUNT_CHECKPOINT_HASH_MISMATCH_'+name)
        started=utc();orders=json.loads(gzip.decompress((root()/'fixed_intents'/f'{name}.json.gz').read_bytes())); assert digest(orders)==read(prior()/f'accounts/{name}.json')['intent_hash']
        ledger,curve,issues,skips=replay_indexed(bars,orders,days,actions,complex_calendar,case['fee'],case['friction'],metadata)
        trades=[]
        for t in ledger.trades:
            t.setdefault('holding_sessions',days.index(t['exit_date'])-days.index(t['entry_date'])+1)
            components=trade_components(t,{day:a for day,a in action_by_symbol[t['symbol']].items() if t['entry_date']<day<=t['exit_date']})
            trades.append({k:components[k] for k in ['symbol','signal_date','entry_date','exit_date','qty','entry','exit','reason','holding_sessions',
                'price_net_pnl','dividend_entitlement','total_net_pnl','entry_cost','return_net','entry_fee','exit_fee','friction_usd']})
        t=pd.DataFrame(trades,columns=['symbol','signal_date','entry_date','exit_date','qty','entry','exit','reason','holding_sessions',
                'price_net_pnl','dividend_entitlement','total_net_pnl','entry_cost','return_net','entry_fee','exit_fee','friction_usd'])
        t['account_id']=name;curve['account_id']=name
        t.to_csv(dest/f'{name}-trades.csv',index=False);curve.to_csv(dest/f'{name}-equity.csv',index=False)
        buy_friction=sum(x['friction_usd'] for x in ledger.audit if x['side']=='BUY')
        open_friction=sum(x['entry_friction'] for x in ledger.positions.values())
        friction=float(t.friction_usd.sum())+open_friction
        row={**case,'started_at':started,'completed_at':utc(),'status':'COMPUTED_PROXY_UNVERIFIED_COMPLEX_ACTION' if any(x['status']=='UNVERIFIED_COMPLEX_ACTION' for x in issues) else 'COMPUTED',
             'minimum_cash':float(curve.cash.min()),'minimum_available_cash':float(curve.available_cash.min()),'ending_equity':float(curve.equity.iloc[-1]),'net_pnl_usd':float(curve.equity.iloc[-1]-5500),'return':float(curve.equity.iloc[-1]/5500-1),
             'max_drawdown':float(curve.drawdown.min()),'completed_trades':len(t),'net_expectancy_per_trade_usd':float(t.total_net_pnl.mean()) if len(t) else None,
             'mean_holding_sessions':float(t.holding_sessions.mean()) if len(t) else None,'commissions':ledger.fees,'friction_usd':rounded(friction),
             'total_cost_usd':rounded(ledger.fees+friction),'close_utilization':float(curve.close_utilization.mean()),
             'post_entry_open_utilization':float(curve.post_entry_open_utilization.mean()),'skips':skips,'intent_count':sum(map(len,orders.values())),
             'missing_held_bar_sessions':int((curve.missing_held_bars>0).sum()),'open_positions':len(ledger.positions),'open_position_details':ledger.positions,
             'complex_action_hits':issues,'intent_hash':digest(orders),'output_hashes':{suffix:sha(dest/f'{name}-{suffix}.csv') for suffix in ['equity','trades']}}
        write(summary_path,row);result.append(row)
        state('REAL_SHARED_CASH_ACCOUNTS',completed=len(result),total=486,account=name)
    save_csv('account_summary.csv',[{k:v for k,v in r.items() if k not in ('open_position_details','complex_action_hits','output_hashes','skips')} for r in result])
    write(root()/'account_summaries.json',result)
    assert len(result)==486
