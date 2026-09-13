"""Finite B1 evidence replay and case calculations; incomplete data never create fills.

This runner is intentionally a qualification/diagnostic runner. The pure event
and accounting components are tested separately. A fully qualified multi-session
quote replay must be integrated before any trading performance can be published.
"""
from dataclasses import asdict
from pathlib import Path
import json
import math
import pandas as pd
import pandas_market_calendars as mcal
from .freeze import ROOT, REPO, sha, write, now
from .rules import (daily_features, nearest_overhead, initial_stop_plan, net_reward_risk,
                    constrained_entry_limit, Quote, capped_quote_fill, floor_to_tick,
                    provisional_ema, deviation_reduction)
from .events import EarningsRevision, earnings_gate, earnings_window, session_clock
from .ledger import Ledger, FinanceConfig

PROBE=ROOT/'data_probe'


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def read_frame(request, expected_hashes):
    if not request['complete']:
        raise ValueError('INCOMPLETE_REQUEST')
    path=Path(request['path'])/'data.parquet'
    if expected_hashes.get(str(path)) != sha(path):
        raise ValueError('CONSUMED_INPUT_HASH_MISMATCH')
    f=pd.read_parquet(path)
    if 'symbol' in f and set(f.symbol.unique()) != {request['symbol']}:
        raise ValueError('IDENTITY_MISMATCH')
    return f


def case_inputs(symbol, requests, expected_hashes):
    result={}
    for adjustment in ['raw','split','all']:
        request=next(r for r in requests if r['symbol']==symbol and r['params'].get('timeframe')=='1Day'
                     and r['params'].get('adjustment')==adjustment and r['session']=='ACTION_UNIT_REFERENCE')
        result[adjustment]=read_frame(request,expected_hashes).set_index('trade_date').sort_index()
    f=result['split']
    # Official SIP conditions make daily price fields a useful reference; volume
    # is not RTH-only. Full warmup auctions/version availability remain unverified.
    result['features']=daily_features(f,regular_session_verified=False)
    minute_path=PROBE/f'{symbol}_rth.parquet'
    if expected_hashes.get(str(minute_path)) != sha(minute_path):
        raise ValueError('CONSUMED_RTH_INPUT_HASH_MISMATCH')
    result['minute']=pd.read_parquet(minute_path).set_index('trade_date')
    result['raw_equals_split']=bool(result['raw'][['open','high','low','close']].equals(result['split'][['open','high','low','close']]))
    return result


def make_revision(event):
    known_date=event.get('conservative_known_from_date')
    if known_date in [None,'UNKNOWN']:
        return None
    known_time=event.get('planned_publication_time_ny','UNKNOWN')
    # Date-only publication uses the explicitly documented next-date availability
    # boundary. It is not mislabeled as an observed source publication timestamp.
    when=pd.Timestamp(f"{known_date} {known_time if known_time!='UNKNOWN' else '00:00'}",tz='America/New_York')
    actual=event['actual_date_ny']
    actual_time=event.get('actual_time_ny','UNKNOWN')
    fields={'actual_release_at':pd.Timestamp(f'{actual} {actual_time}',tz='America/New_York')} if actual_time!='UNKNOWN' else {'actual_release_date':actual}
    planned=event.get('planned_release_date_ny')
    if planned in [None,'UNKNOWN']:
        return None
    return EarningsRevision(event_id=event['symbol']+'_Q2_2026',planned_date=planned,known_at=when,
        source=event['planned_source'],source_received_at=None,time_class=event['release_session'],**fields)


def screen(row):
    emas={n:float(row[f'ema{n}']) for n in [5,10,20,50,100]}
    return emas, bool(row.daily_screen_candidate)


def run():
    root=ROOT/'research'
    root.mkdir(parents=True,exist_ok=True)
    config=load_json(ROOT/'frozen_config.json')
    requests=load_json(PROBE/'PROBE_STATE.json')['requests']
    cases=pd.read_csv(PROBE/'CASE_DATA_RECONCILIATION.csv')
    auction=pd.read_csv(PROBE/'CLOSING_AUCTION_RECONCILIATION.csv').set_index(['symbol','date'])
    consumed=[Path(r['path'])/'data.parquet' for r in requests if r['params'].get('timeframe')=='1Day' and r['session']=='ACTION_UNIT_REFERENCE']
    consumed += [PROBE/f'{s}_rth.parquet' for s in cases.symbol.unique()]
    consumed += [PROBE/'CASE_DATA_RECONCILIATION.csv',PROBE/'CLOSING_AUCTION_RECONCILIATION.csv',PROBE/'official_earnings_evidence.json']
    pinned_path=root/'consumed_input_hashes.json'
    expected={str(p):sha(p) for p in consumed}
    if pinned_path.exists():
        pinned=load_json(pinned_path)
        if pinned['sha256'] != expected:
            raise ValueError('INPUT_CHANGED_REQUIRE_EXPLICIT_VERSIONED_RUN')
    else:
        write(pinned_path,{'at':now(),'sha256':expected,'source_pages_manifest_sha256':sha(PROBE/'data_cache_manifest.json')})
    inputs={s:case_inputs(s,requests,expected) for s in cases.symbol.unique()}
    calendar=mcal.get_calendar('NYSE').schedule('2025-01-01','2026-12-31')
    evidence=load_json(PROBE/'official_earnings_evidence.json')['events']
    records=[]; quote_diagnostics=[]
    for c in cases.to_dict('records'):
        symbol=c['symbol']; day=c['date']; data=inputs[symbol]
        row=data['features'].loc[day]; emas,fresh=screen(row)
        clock=session_clock(calendar,day)
        C=float(data['raw'].loc[day,'close'])
        quote=json.loads(c['quote_at_decision'])
        # No actual order is placed: these calculations explain what data/rules fail.
        ask=float(quote['ap']) if quote else None
        E=(ask if ask is not None else C)*1.001
        qty=max(0,math.floor((2750-3)/E))
        stop=initial_stop_plan(C,E,emas,qty)
        overhead=nearest_overhead(C,emas,float(row.prior_high30))
        complete=overhead['status'] in ['KNOWN_OVERHEAD','NO_KNOWN_OVERHEAD_IN_DEFINED_SET']
        rr=net_reward_risk(E,stop.legs,overhead['price'],overhead_inputs_complete=complete) if stop.status=='VALID' else {'rr':None,'allowed':False,'status':stop.reason}
        reasons=[]
        if int(row.valid_sessions)<100:reasons.append('INDICATOR_WARMUP_INSUFFICIENT')
        if not fresh:reasons.append('NO_FRESH_CLOSE_BREAKOUT')
        if stop.status!='VALID':reasons.append(stop.reason)
        if not rr.get('allowed'):reasons.append('NET_2R_OR_OVERHEAD_INPUT_GATE')
        if not quote:reasons.append('NO_FRESH_QUOTE_AT_FIXED_1605_DECISION')
        reasons.extend(['EARNINGS_NEXT_RELEASE_UNKNOWN','FULL_WARMUP_PRICE_AND_ACTION_AUDIT_NOT_COMPLETE'])
        age=(pd.Timestamp(clock['entry_decision'])-pd.Timestamp(quote['t'])).total_seconds() if quote else None
        record={'symbol':symbol,'date':day,'case_source':c['case_source'],
                'original_screenshot':'NOT_ATTACHED; taskbook is semantic development sample',
                'user_manual_support':161.37 if symbol=='SKHY' else None,
                'manual_support_is_not_ema':symbol=='SKHY','official_close':C,
                'auction_price_match':bool(auction.loc[(symbol,day),'daily_close_matches_any_auction']),
                'last_rth_minute_close':float(data['minute'].loc[day,'close']),
                'close_limit_1pct':floor_to_tick(C*1.01),
                'valid_same_security_sessions':int(row.valid_sessions),
                **{f'ema{n}_daily_split_snapshot':emas[n] for n in emas},
                'atr14_prev':float(row.atr14_prev),'prior_high30':float(row.prior_high30),
                'fresh_cross_and_above_short_group':fresh,'stop_plan':json.dumps([asdict(x) for x in stop.legs]),
                'nearest_overhead':overhead['price'],'overhead_status':overhead['status'],'indicative_net_rr':rr.get('rr'),
                'indicative_price_basis':'actual ask+friction' if ask is not None else 'C+friction for structure diagnostic ONLY; NOT a fill',
                'raw_split_equal_entire_warmup':data['raw_equals_split'],
                'quote_bid':float(quote['bp']) if quote else None,'quote_ask':ask,'quote_age_seconds':age,
                'displayed_ask_shares':float(quote['as']) if quote else None,
                'quote_units':'SHARES under post-2025-11-03 SIP size convention',
                'decision_time_ny':clock['new_york']['entry_decision'],'decision_time_madrid':clock['madrid']['entry_decision'],
                'historical_network_received_at':'UNKNOWN','source_received_at':c['source_received_at'],
                'order_created_at':None,'fill_effective_at':None,'status':'REJECTED_NO_ORDER',
                'reasons':';'.join(dict.fromkeys(reasons))}
        records.append(record)
        for cost in ['base','stress25','double_commission']:
            result={'status':'NOT_EVALUABLE','quantity':None,'reason':'NO_FRESH_DECISION_QUOTE_OR_STOP_PLAN'}
            if quote and stop.status=='VALID' and complete:
                friction=.0025 if cost=='stress25' else .001
                commission=2 if cost=='double_commission' else 1
                limit=constrained_entry_limit(C,stop.legs,overhead['price'],commission=commission,exit_friction=friction)
                if limit is not None:
                    result=capped_quote_fill(Quote(float(quote['bp']),ask,float(quote['as']),quote['t'],size_unit='shares'),
                        clock['entry_decision'],clock['entry_decision'],clock['entry_expiry'],limit,qty,2750,
                        max(emas[n] for n in [5,10,20]),max(x.stop for x in stop.legs),commission=commission,
                        exit_reserve=len(stop.legs)*commission,friction=friction)
            quote_diagnostics.append({'symbol':symbol,'date':day,'cost':cost,'diagnostic_only':True,
                                      'strategy_order_created':False,'earnings_overridden':False,
                                      'post_partial_strategy_eligibility':'NOT_EVALUATED_COMPONENT_ONLY; actual legs and net RR need recomputation before any order',
                                      **{f'quote_component_{k}':v for k,v in result.items()}})
    pd.DataFrame(records).to_csv(ROOT/'CASE_RECONCILIATION.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(quote_diagnostics).to_csv(root/'quote_component_diagnostics.csv',index=False,encoding='utf-8-sig')
    # Evaluate every available day in each independently sourced announcement
    # window, without looking for better windows when no signal occurs.
    window_rows=[]
    for event in evidence:
        if event['level']!='VERIFIED_PIT':continue
        symbol=event['symbol']; revision=make_revision(event)
        if revision is None:continue
        start=event['conservative_known_from_date']; end=event['actual_date_ny']
        frame=inputs[symbol]['features'].loc[start:end]
        for day,row in frame.iterrows():
            clock=session_clock(calendar,day)
            gate=earnings_gate(clock['entry_decision'],calendar,[revision])
            window_rows.append({'symbol':symbol,'date':day,'decision_time':clock['entry_decision'],
                'earnings_level':gate['tier'],'earnings_status':gate['status'],'earnings_reason':gate['reason'],
                'earnings_new_entry_allowed':gate['new_entry_allowed'],'required_exit_time':gate.get('required_exit_time'),
                'first_entry_signal_on_vendor_daily':bool(row.daily_screen_candidate),'close':float(row.close),
                'ema5':float(row.ema5),'ema10':float(row.ema10),'ema20':float(row.ema20),
                'valid_sessions':int(row.valid_sessions),
                'source_published_at':str(revision.known_at) if event['planned_publication_time_ny']!='UNKNOWN' else 'UNKNOWN',
                'source_publication_date':event['planned_publication_date'],
                'conservative_available_at_bound':str(revision.known_at),
                'publication_precision':'EXACT' if event['planned_publication_time_ny']!='UNKNOWN' else 'DATE_ONLY_CONSERVATIVE_NEXT_DATE_BOUNDARY',
                'actual_release_time':event['actual_time_ny'],'historical_network_received_at':'UNKNOWN',
                'source_received_at':event['source_received_at'],'decision_price_version':'CURRENT_VENDOR_SPLIT_SNAPSHOT',
                'strict_full_execution_eligible':False,'strict_missing':'FULL_WARMUP_RTH_ACTIONS_AND_FULL_QUOTE_PATH',
                'order_created_at':None,'fill_effective_at':None})
    windows=pd.DataFrame(window_rows)
    windows.to_csv(root/'earnings_window_decisions.csv',index=False,encoding='utf-8-sig')
    before=windows[windows.earnings_new_entry_allowed]
    candidates=before[before.first_entry_signal_on_vendor_daily]
    # Explicitly refuse to turn unexecuted/missing research paths into flat,
    # zero-drawdown strategy performance.
    summaries=[]; snapshots=[]
    for trial in config['matrix']:
        account_id=f"{trial['rule']}__{trial['position']}__{trial['cost']}"
        account=Ledger(FinanceConfig(mode=trial['position'],commission=trial['commission'],friction_bps=trial['friction_bps']))
        snapshot=account.snapshot({})
        snapshots.append({'account_id':account_id,'state':'INITIALIZED_DIAGNOSTIC_NO_TRADE_QUALIFIED_PERIOD',**snapshot})
        summaries.append({'account_id':account_id,**trial,'initial_equity':5500,
            'status':'DATA_GATED_PARTIAL_EXECUTION','full_historical_return_evaluated':False,
            'end_equity':None,'net_profit':None,'cagr':None,'max_drawdown':None,
            'observed_announcement_window_symbol_sessions':len(windows),
            'preblackout_symbol_sessions':len(before),'fresh_signals_in_preblackout_windows':len(candidates),
            'orders_created':0,'fills_observed':0,'completed_campaigns':0,
            'win_rate':None,'expectancy':None,'reason':'NO_FULL_TRADE_QUALIFIED_HISTORY; diagnostic windows do not establish strategy performance'})
    pd.DataFrame(summaries).to_csv(ROOT/'account_summary.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(snapshots).to_csv(root/'initial_account_states.csv',index=False,encoding='utf-8-sig')
    for name,columns in {
        'orders.csv':['account_id','order_id','decision_time','created_at','symbol','side','quantity','limit','status'],
        'fills.csv':['account_id','order_id','fill_id','symbol','side','quantity','price','commission','effective_at'],
        'campaigns.csv':['account_id','campaign_id','symbol','entry','exit','net_profit','realized_R'],
        'daily_equity.csv':['account_id','date','cash','debt','accrued_interest','reserve','equity'],
        'annual_returns.csv':['account_id','year','return','status'],
        'rolling_12m.csv':['account_id','date','return','status'],
    }.items():
        pd.DataFrame(columns=columns).to_csv(root/name,index=False,encoding='utf-8-sig')
    pd.DataFrame([{'symbol':s,'initial_equity':5500,'status':'NOT_COMPUTED_NO_QUALIFIED_COMMON_CAPITAL_DEPLOYMENT_DATE',
                   'start':None,'end':'2026-09-11','net_return':None,'cash_interest':0,
                   'dividend_rule':'integer reinvestment after actual payment; do not buy adjusted prices',
                   'liquidation_fees':'must apply symmetrically once research path is eligible',
                   'old_index_audit_preserved':True} for s in ['SPY','QQQ']]).to_csv(root/'benchmarks.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame([{'monthly_budget':fee,'payment':'external','actual_new_expense':0,
                   'historical_diagnostic_fixed_fees':None,'all_cost_profit':None,'status':'NO_EVALUATED_TRADING_PERIOD',
                   'existing_shared_subscription_cost':'UNKNOWN','research_budget_not_purchase_authorization':True} for fee in [0,150]]).to_csv(root/'fixed_cost_diagnostic.csv',index=False,encoding='utf-8-sig')
    write(root/'run_summary.json',{'at':now(),'status':'DATA_GATED_PARTIAL_EXECUTION','cases_calculated':len(records),
        'case_orders':0,'matrix_paths_registered_and_gate_evaluated':len(summaries),'matrix_paths_strictly_qualified':0,'matrix_profit_paths_completed':0,
        'announcement_window_symbol_sessions':len(windows),'preblackout_symbol_sessions':len(before),
        'preblackout_fresh_signals':len(candidates),'full_strictly_executable_history':False,
        'limitations':['Current complete historical quote replay not integrated','Full warmup RTH/company-action audit incomplete',
                       'Only three targeted PIT event windows, not a full next-earnings calendar'],
        'empty_execution_files_are_explicitly_not_evaluated_performance':True,
        'frozen_config_sha256':sha(ROOT/'frozen_config.json'),
        'consumed_input_manifest_sha256':sha(pinned_path),
        'source_code_sha256':{str(p.relative_to(REPO)):sha(p) for p in (REPO/'src/ben_b1').glob('*.py')}})
    print(json.dumps(load_json(root/'run_summary.json'),ensure_ascii=False))


if __name__=='__main__':run()
