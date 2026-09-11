"""Four frozen filters through the same V1.1 quantity and cash kernel."""
from collections import defaultdict
from pathlib import Path
import numpy as np
import pandas as pd
from src.v11.kernel import replay,rounded
from src.watchlist.features import signal
from .runtime import *
from .data import load,frame

def trade_components(t,actions):
    relevant=[a for day,rows in sorted(actions.items()) if t['entry_date']<day<=t['exit_date'] for a in rows if a['symbol']==t['symbol']]
    factor=np.prod([a['ratio'] for a in relevant if a['kind']=='split']);qty=t['qty']/factor
    entitlement=0.;seen=set()
    for a in relevant:
        if a['id'] in seen:continue
        seen.add(a['id'])
        if a['kind']=='split':qty*=a['ratio']
        elif a['kind']=='dividend':entitlement+=qty*a['rate']
    total=rounded(t['net_pnl']+entitlement)
    return {**t,'price_net_pnl':t['net_pnl'],'dividend_entitlement':entitlement,'total_net_pnl':total,
            'price_return_net':t['return_net'],'return_net':total/t['entry_cost'],
            'pnl_definition':'price after commissions/friction; dividend entitlement regardless of payment date; return=(price+dividend)/entry_cost'}

def run():
    p=freeze();snap=read(root()/'input_snapshot.json');benchmark=load('SPY','all');raw={};features={};issues=[]
    spec=next(c for c in read(v11root()/'config_and_engine_version.json')['configs'] if c['id']=='FIXED_LEGACY')
    assert spec['hold']==3 and spec['stop']==.05 and spec['target'] is None
    for r in snap['universe']['records']:
        try:
            f,d,problems=frame(r['symbol'],benchmark)
            if len(d) and r['symbol']!='DXYZ' and r['status']=='INCLUDED':raw[r['symbol']]=d;features[r['symbol']]=f
            if problems:issues.append({'symbol':r['symbol'],'issues':problems})
        except Exception as exc:issues.append({'symbol':r['symbol'],'issues':[type(exc).__name__]})
    actions=read(v11root()/'action_calendar.json');action_blocks=defaultdict(set)
    for r in snap['universe']['records']:
        for kind,rows in read(source()/'actions'/f'{r["symbol"]}.json',{}).get('data',{}).items():
            if kind in ('cash_dividends','forward_splits','reverse_splits'):continue
            for a in rows:
                if a.get('acquirer_symbol')==r['symbol'] and a.get('acquiree_symbol')!=r['symbol']:continue
                day=a.get('ex_date') or a.get('effective_date') or a.get('process_date')
                if day:action_blocks[r['symbol']].add(day)
    raw_index={s:f.set_index('trade_date') for s,f in raw.items()}
    intents={r:defaultdict(list) for r in ['R0','R1','R2','R3']};signal_counts={r:0 for r in intents};blocked=[]
    for s,f in features.items():
        base=signal(f,spec)
        masks={'R0':base,'R1':base&(f.return_20>0),'R2':base&(f.return_60>0),'R3':base&(f.relative20>0)}
        for w in p['windows']:
            start,end=w['evaluation']
            for i in range(start,end-3):
                day=p['days'][i];entry=p['days'][i+1];last=p['days'][i+3]
                if day not in raw_index[s].index:continue
                for version,mask in masks.items():
                    if not bool(mask.iloc[i]):continue
                    signal_counts[version]+=1
                    if any(entry<=a<=last for a in action_blocks[s]):
                        blocked.append({'version':version,'symbol':s,'signal_date':day,'reason':'COMPLEX_ACTION_INTERVAL'});continue
                    intents[version][entry].append({'symbol':s,'spec':spec,'rank':0,'previous_volume':float(raw_index[s].loc[day,'volume']),
                                                     'signal_date':day,'historical_window':w['id']})
    out=root()/'accounts';out.mkdir(exist_ok=True);results=[];metadata={r['symbol']:r['identity_version'] for r in snap['universe']['records']}
    days=p['days'][p['windows'][0]['evaluation'][0]:]
    for version,orders in intents.items():
        write(out/f'{version}-intents.json',dict(orders))
        for case,friction,fee in p['accounts']['costs']:
            check_deadline();path=out/f'{version}-{case}-summary.json'
            if path.exists():results.append(read(path));continue
            ledger,curve=replay(raw,orders,days,actions,fee,friction,metadata)
            trades=pd.DataFrame([trade_components(t,actions) for t in ledger.trades])
            curve.to_parquet(out/f'{version}-{case}-equity.parquet',index=False)
            if len(trades):trades.to_parquet(out/f'{version}-{case}-trades.parquet',index=False)
            # CSV is a report of actual model trades, not a market bar library.
            save_csv(f'accounts/{version}-{case}-trades.csv',trades)
            write(out/f'{version}-{case}-skips.json',ledger.skips)
            gains=trades.total_net_pnl[trades.total_net_pnl>0] if len(trades) else pd.Series(dtype=float)
            losses=trades.total_net_pnl[trades.total_net_pnl<0] if len(trades) else pd.Series(dtype=float)
            market_value=curve.equity-curve.cash-curve.unsettled-curve.dividend_receivable
            row={'version':version,'cost_case':case,'status':'COMPUTED' if len(trades) else 'NO_COMPLETED_TRADES',
                 'ending_equity':float(curve.equity.iloc[-1]),'net_pnl_usd':float(curve.equity.iloc[-1]-5500),
                 'return':float(curve.equity.iloc[-1]/5500-1),'max_drawdown':float(curve.drawdown.min()),
                 'completed_trades':len(trades),'mean_profit_loss_ratio':float(gains.mean()/-losses.mean()) if len(gains) and len(losses) else None,
                 'profit_factor_usd':float(gains.sum()/-losses.sum()) if len(losses) else None,
                 'mean_win_usd':float(gains.mean()) if len(gains) else None,'mean_loss_usd':float(losses.mean()) if len(losses) else None,
                 'capital_utilization':float((market_value/curve.equity).mean()),'invested_sessions':int((curve.positions>0).sum()),
                 'commissions':ledger.fees,'closed_trade_friction_usd':float(trades.friction_usd.sum()) if len(trades) else 0.,
                 'skips':len(ledger.skips),'signal_count':signal_counts[version],'open_positions_at_cutoff':len(ledger.positions),
                 'missing_held_bar_sessions':int((curve.missing_held_bars>0).sum()),'dividend_receivable':float(sum(x['amount'] for x in ledger.dividends)),
                 'intent_hash':digest(orders),'parameters':'UNCHANGED_FIXED_LEGACY_EXCEPT_DECLARED_ENTRY_FILTER'}
            # Exit friction is total round-trip friction minus the closed position entry friction.
            buy_friction=sum(a['friction_usd'] for a in ledger.audit if a['side']=='BUY')
            entry_by={(a['symbol'],a['effective_at']):a['friction_usd'] for a in ledger.audit if a['side']=='BUY'}
            sell_friction=sum(t['friction_usd']-entry_by[(t['symbol'],t['entry_date'])] for t in ledger.trades)
            row['total_friction_usd']=rounded(buy_friction+sell_friction);row['total_cost_usd']=rounded(row['commissions']+row['total_friction_usd'])
            write(path,row);results.append(row);state('FILTER_ACCOUNT_COMPLETE',version=version,cost_case=case,trades=len(trades))
    for row in results:
        base=next(x for x in results if x['version']=='R0' and x['cost_case']==row['cost_case'])
        row.update(delta_pnl_vs_R0=row['net_pnl_usd']-base['net_pnl_usd'],delta_trades_vs_R0=row['completed_trades']-base['completed_trades'],
                   delta_utilization_vs_R0=row['capital_utilization']-base['capital_utilization'],
                   delta_drawdown_vs_R0=row['max_drawdown']-base['max_drawdown'])
    save_csv('momentum_filter_accounts.csv',results);write(root()/'momentum_filter_accounts.json',{'metadata':tags(),'results':results})
    write(root()/'account_data_issues.json',{'data':issues,'complex_action_blocks':blocked})
