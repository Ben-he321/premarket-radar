"""Actual continuous common-capital replay with bounded real-input acquisition.

No individual sample account is combined into this account. Each mode/tier owns
one original 5500 ledger, all66 eligibility records and a common event clock.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
import gc
import json
import os
from pathlib import Path
import traceback
import pandas as pd
import pandas_market_calendars as mcal
from src.ben_b1 import rules
from src.ben_b1.ledger import Ledger
from src.ben_b1.b11_research import event, digest, clean
from src.ben_b1.events import completed_sessions_since_exit
from .replay import ReplayConfig, calendar_events
from .shared import SharedReplayEngine as ReplayEngine
from .data import B12Market, DATA, NY, query_ticker, frame_qc
from .runtime import ROOT, REPO, B11, B1, START, END, TAIL_END, WARMUP, read, write, sha, utc, deadline, status

def quote_events(symbol,frame,receipt):
    result=[]
    for q in frame.to_dict('records'):
        t=pd.Timestamp(q['t'])
        p={'bid':float(q['bp']),'ask':float(q['ap']),'bid_size':float(q['bs']),'ask_size':float(q['as']),
           'bid_exchange':q.get('bx'),'ask_exchange':q.get('ax'),'conditions':list(q.get('c',[])),
           'timestamp':t.isoformat(),'size_unit':q.get('size_unit','UNKNOWN'),'available_at':t.isoformat(),
           'source_received_at':q.get('source_received_at',receipt.get('last_source_received_at','UNKNOWN')),
           'historical_network_received_at':'UNKNOWN','availability_basis':'HISTORICAL_EVENT_CLOCK_NOT_LIVE_BASIC_RECEIPT'}
        qid=q.get('quote_id') or digest([symbol,{k:v for k,v in p.items() if k!='source_received_at'}])
        result.append(event('QUOTE',symbol,t,p,'quote:'+qid))
    return result

class ContinuousInputs:
    def __init__(self,earnings_path):
        self.schedule=mcal.get_calendar('NYSE').schedule('2025-01-01','2026-12-31')
        self.clocks={str(d.date()):r for d,r in self.schedule.iterrows()}
        self.scope=pd.read_csv(B1/'UNIVERSE_POLICY.csv')
        identities=pd.read_csv(B1/'coarse/all_candidate_events.csv',usecols=['symbol','identity_sort_hash']).drop_duplicates('symbol').set_index('symbol').identity_sort_hash.to_dict()
        self.universe={r['symbol']:{'scope':r['scope_policy'],'security_id':r['stable_local_security_id'],
                       'identity_sort_hash':identities.get(r['symbol'],digest(r['stable_local_security_id']))} for r in self.scope.to_dict('records')}
        self.histories={r['symbol']:r for r in read(DATA/'HISTORY_INPUTS.json')}
        self.earnings_path=Path(earnings_path);self.earnings=read(self.earnings_path)
        self.actions=read(DATA/'corporate_actions.json')
        self.pinned={str(p):sha(p) for p in [B1/'UNIVERSE_POLICY.csv',DATA/'HISTORY_INPUTS.json',self.earnings_path,DATA/'corporate_actions.json',ROOT/'B12_PROTOCOL.json']}
        self.daily={};self.coverage=[];self.market=B12Market(DATA/'portfolio_quotes')
        rank=pd.read_csv(B11/'data/RANKING_VOLUME_PRIOR20.csv')
        for symbol,u in self.universe.items():
            h=self.histories.get(symbol,{})
            if not h.get('rth') or not h.get('complete'):continue
            p=Path(h['rth']['path']);self.pin(p)
            f=pd.read_parquet(p)
            if f.empty:continue
            f=f[f.trade_date.between(WARMUP,TAIL_END)].copy().set_index('trade_date',drop=False)
            # Entry price indicators do not depend on volume. Unverified auction
            # quantity must not quietly become the secondary ranking metric.
            f['observed_minute_volume']=f.volume;f['volume']=float('nan')
            for r in rank[rank.symbol.eq(symbol)].to_dict('records'):
                if r['trade_date'] in f.index:
                    f.loc[r['trade_date'],'volume']=r['rth_plus_observed_auction_volume']
            observed_first=min(f.index)
            expected=[d for d in self.clocks if observed_first<=d<=TAIL_END]
            missing=set(expected)-set(f.index)
            for day in f.index:
                recent=[d for d in expected if d<=day][-100:]
                holes=sorted(set(recent)&missing)
                f.loc[day,'known_recent_session_gaps']=json.dumps(holes)
                if holes:f.loc[day,'regular_session_verified']=False
                if self.actions.get('status')!='ACCESS_OK':f.loc[day,'action_units_verified']=False
            self.daily[symbol]=f

    def pin(self,path):
        p=Path(path);self.pinned[str(p)]=sha(p)

    def daily_event(self,symbol,row):
        d=row['trade_date'];c=self.clocks[d].market_close;at=c+pd.Timedelta(minutes=1)
        p={k:row.get(k) for k in ['open','high','low','close','volume','regular_session_verified','action_units_verified','source_received_at']}
        p.update(session_date=d,available_at=at.isoformat(),historical_network_received_at='UNKNOWN',
                 price_basis='RTH_MINUTES_PLUS_VENDOR_OFFICIAL_CLOSE',volume_basis='OBSERVED_RTH_PLUS_MATCHED_AUCTION_OR_UNKNOWN',
                 availability_basis='FINAL_HISTORICAL_SNAPSHOT_AT_CLOSE_PLUS1_MODEL_ASSUMPTION_NOT_BASIC_LIVE_PROOF',
                 data_basis='VENDOR_STANDARD_FIELDS',independent_point_audit='NOT_INDEPENDENTLY_CHECKED_EACH_POINT',
                 known_recent_session_gaps=row.get('known_recent_session_gaps','[]'))
        return event('DAILY_BAR',symbol,at,p,'daily:'+symbol+':'+d)

    def corporate_events(self):
        result=[];actions=self.actions.get('corporate_actions',{})
        for kind in ('forward_splits','reverse_splits'):
            for a in actions.get(kind,[]):
                s=a['symbol'];d=a['ex_date']
                if s not in self.universe or not WARMUP<=d<=TAIL_END:continue
                ratio=float(a['new_rate'])/float(a['old_rate'])
                result.append(event('SPLIT',s,pd.Timestamp(d,tz=NY),{'action_id':a['id'],'ratio':ratio,
                    'source':'Alpaca corporate actions','new_rate':a['new_rate'],'old_rate':a['old_rate'],
                    'historical_network_received_at':'UNKNOWN','basis':'VENDOR_STANDARD_EFFECTIVE_EX_DATE_RATIO_REVIEWED'},'split:'+a['id']))
        for a in actions.get('cash_dividends',[]):
            s=a['symbol'];ex=a['ex_date'];pay=a.get('payable_date')
            if s not in self.universe or not START<=ex<=TAIL_END:continue
            result.append(event('DIVIDEND_EX',s,pd.Timestamp(ex,tz=NY),{'action_id':a['id'],'amount_per_share':a['rate'],'pay_date':pay or 'UNKNOWN'},'divex:'+a['id']))
            if pay and pay<=TAIL_END:result.append(event('DIVIDEND_PAY',s,pd.Timestamp(pay,tz=NY),{'action_id':a['id']},'divpay:'+a['id']))
        # Unsupported actions affecting the old security invalidate its units.
        # Acquirer stock does not itself split when it buys another issuer.
        for kind in ('stock_mergers','cash_mergers','spin_offs','unit_splits'):
            for a in actions.get(kind,[]):
                s=a.get('acquiree_symbol') or a.get('source_symbol') or a.get('old_symbol')
                d=a.get('ex_date') or a.get('effective_date')
                if s in self.universe and d and WARMUP<=d<=TAIL_END:
                    result.append(event('DATA_GAP',s,pd.Timestamp(d,tz=NY),{'category':'CORPORATE_ACTION_UNITS',
                        'reason':'UNSUPPORTED_COMPLEX_ACTION_UNITS_UNKNOWN','action_id':a['id'],'action_kind':kind},'unknown-action:'+a['id']))
        return result

    def earnings_events(self,tier):
        result=[]
        facts={f['symbol']:f for f in self.earnings['facts']}
        receipts={r['url']:r.get('source_received_at','UNKNOWN') for r in self.earnings.get('source_receipts',[])}
        if tier=='B':
            for s,f in facts.items():
                if s not in self.universe or not f.get('coverage_complete'):continue
                for r in f.get('releases',[]):
                    d=r['release_date_ny']
                    rev={'event_id':s+':'+d,'actual_release_date':d,'source':r['source'],
                         'event_kind':'earnings_release','actual_time_precision':'DATE'}
                    result.append(event('EARNINGS_REVISION',s,pd.Timestamp(START,tz=NY),{'revision':rev,
                        'coverage_complete':True,'coverage_start':f['coverage_start'],'coverage_end':f['coverage_end'],
                        'coverage_source':f['completeness_basis'],'source_received_at':r.get('source_received_at',receipts.get(r['source'],'UNKNOWN')) or 'UNKNOWN',
                        'evidence_file_created_at':self.earnings['created_at'],
                        'historical_network_received_at':'UNKNOWN','availability_basis':'RETROSPECTIVE_EARNINGS_EXCLUSION_NOT_PIT',
                        'original_release_kind':r.get('event_type',r.get('kind',r.get('verified_as')))},'earnings:B:'+s+':'+d))
        else:
            for plan in self.earnings.get('plans',[]):
                s=plan['symbol'];d=plan['planned_release_date']
                if s not in self.universe:continue
                at=pd.Timestamp(plan['conservative_known_at_ny']);at=at.tz_localize(NY) if at.tzinfo is None else at
                actual=next((r for r in facts.get(s,{}).get('releases',[]) if r['release_date_ny']==d),{})
                rev={'event_id':s+':'+d,'planned_date':d,'known_at':at.isoformat(),'source':plan['source'],
                     'event_kind':'earnings_release','actual_time_precision':'DATE'}
                # Actual date is an outcome, not historical confirmation. A
                # release-known receipt is absent; original conservative gate
                # remains UNKNOWN for post-release recovery when required.
                if actual:rev['actual_release_date']=d
                result.append(event('EARNINGS_REVISION',s,at,{'revision':rev,'coverage_complete':True,
                    'availability_basis':plan['availability_basis'],'source_received_at':receipts.get(plan['source'],'UNKNOWN') or 'UNKNOWN',
                    'evidence_file_created_at':self.earnings['created_at'],
                    'historical_network_received_at':'UNKNOWN'},'earnings:A:'+s+':'+digest(plan)))
        return result

    def minute_events(self,symbol,day):
        h=self.histories.get(symbol,{})
        if not h.get('minutes'):return []
        path=Path(h['minutes']['path']);self.pin(path)
        f=pd.read_parquet(path,filters=[('trade_date','==',day)])
        if f.empty:return []
        c=self.clocks[day];times=pd.to_datetime(f.timestamp,utc=True)
        f=f[(times>=c.market_open)&(times<c.market_close)]
        result=[]
        for r in f.to_dict('records'):
            end=pd.Timestamp(r['timestamp'])+pd.Timedelta(minutes=1)
            result.append(event('MINUTE_BAR',symbol,end,{'bar_end':end.isoformat(),**{k:r[k] for k in ['open','high','low','close','volume']},
                'available_at':end.isoformat(),'source_received_at':r.get('source_received_at','UNKNOWN'),
                'historical_network_received_at':'UNKNOWN','availability_basis':'COMPLETED_MINUTE_EVENT_CLOCK_NOT_NETWORK_RECEIPT'},'minute:'+symbol+':'+str(r['timestamp'])))
        return result

    def quotes(self,symbol,day,holding):
        f,r=self.market.quotes(symbol,day,holding)
        path=Path(r['path'])/'data.parquet'
        if path.exists():self.pin(path)
        self.coverage.append({'symbol':symbol,'day':day,'purpose':'ACTUAL_HOLDING_RTH' if holding else 'CAUSAL_SIGNAL_ENTRY_WINDOW',
                              'rows':len(f),'complete':r.get('complete'),'status':r.get('status'),'path':str(path),
                              'query_symbol':r['params'].get('symbols'),'receipt_time':r.get('last_source_received_at')})
        return quote_events(symbol,f,r),r

def eligible_for_quote(engine,symbol,day):
    if engine.universe[symbol]['scope']!='KEEP' or symbol in engine.ledger.positions:return False
    f=engine._features(symbol)
    if len(f)<2 or str(f.index[-1].date())!=day or engine._bar_eligibility(f):return False
    d,p=f.iloc[-1],f.iloc[-2]
    e={n:float(d[f'ema{n}']) for n in rules.EMA_PERIODS};prev={n:float(p[f'ema{n}']) for n in rules.EMA_PERIODS}
    if symbol in engine.state['last_exit']:
        return rules.reentry_eligible(completed_sessions_since_exit(engine.schedule,engine.state['last_exit'][symbol],engine.state['at']),
              engine._post_exit_repairs(symbol),float(d.close),float(p.high),e,engine.state['at'])['allowed']
    return rules.entry_signal(float(d.close),float(p.close),e,prev,int(d.valid_sessions))['allowed']

def run_account(mode,tier,earnings_path,version='base_v1'):
    inputs=ContinuousInputs(earnings_path)
    run_id=f'B12_{mode}_P50_{tier}_{version}';out=ROOT/'portfolio'/version/run_id;out.mkdir(parents=True,exist_ok=True)
    final_path=out/'FINAL_ACCOUNT.json';recovery_path=out/'RECOVERY_IDEMPOTENCY.json'
    final=read(final_path) if final_path.exists() else None
    if final is not None and recovery_path.exists() and read(recovery_path).get('status')!='PASS':
        raise ValueError('INCOMPLETE_FINAL_RECOVERY_FAILED_REVIEW_EXISTING_EVIDENCE')
    entry_dates=tuple(d for d in inputs.clocks if START<=d<=END)
    cfg=ReplayConfig(run_id=run_id,quote_mode=mode,earnings_tier=tier,data_basis='VENDOR_STANDARD_FIELDS',
          start=START,end=TAIL_END,entry_dates=entry_dates,checkpoint_every=0,research_scope='ALL66_CONTINUOUS_COMMON_CAPITAL_COVERAGE_LIMITED')
    source={str(REPO/p):sha(REPO/p) for p in ['src/ben_b1/replay.py','src/ben_b1/ledger.py','src/ben_b1/rules.py',
        'src/ben_b1/events.py','src/ben_b1/b11_research.py','src/ben_b1/b11_data.py','src/ben_b1/data_probe.py',
        'src/ben_b1_2/replay.py','src/ben_b1_2/compact.py','src/ben_b1_2/shared.py','src/ben_b1_2/portfolio.py','src/ben_b1_2/data.py','src/ben_b1_2/runtime.py']}
    spec={'created_at':utc(),'config':asdict(cfg),'tail_end':TAIL_END,'source_files':source,'input_files':inputs.pinned,
          'shared_capital':True,'independent_sample_results_not_used':True,'synthetic':False}
    sp=out/'RUN_SPEC.json'
    if sp.exists():
        old=read(sp)
        if old['source_files']!=source or old['input_files']!=inputs.pinned:raise ValueError('PINNED_RUN_CHANGED_USE_NEW_VERSION')
    else:write(sp,spec)
    dynamic=out/'DYNAMIC_INPUT_HASHES.json'
    if dynamic.exists():
        old_dynamic=read(dynamic)
        for path,expected in old_dynamic.items():
            if not Path(path).is_file() or sha(path)!=expected:raise ValueError('PINNED_DYNAMIC_INPUT_CHANGED:'+path)
        inputs.pinned.update(old_dynamic)
    coverage_path=out/'QUOTE_COVERAGE.json'
    if coverage_path.exists():inputs.coverage=read(coverage_path)
    checkpoint=out/'checkpoint.json'
    if final is not None:
        if not checkpoint.exists():raise ValueError('INCOMPLETE_FINAL_RECOVERY_CHECKPOINT_MISSING')
        if not recovery_path.exists():verify_recovery(checkpoint,inputs.schedule,recovery_path)
        return final
    engine=ReplayEngine.restore(checkpoint,inputs.schedule) if checkpoint.exists() else ReplayEngine(inputs.schedule,cfg,inputs.universe,checkpoint)
    engine.mark_coverage_limited('Unverified PIT/unscheduled earnings, quote model and independently unaudited full-pool inputs; missing candidates may alter ranking',start=START,end=END)
    extra=inputs.corporate_events()+inputs.earnings_events(tier)
    by_day={}
    for e in extra:by_day.setdefault(str(pd.Timestamp(e['at']).tz_convert(NY).date()),[]).append(e)
    if engine.state['at'] is None:
        warm=[inputs.daily_event(s,row) for s,f in inputs.daily.items() for row in f[f.trade_date<START].to_dict('records')]
        warm += [e for e in extra if pd.Timestamp(e['at'])<pd.Timestamp(START,tz=NY)]
        engine.run(warm)
    progress=read(out/'progress.json') if (out/'progress.json').exists() else {}
    first=(pd.Timestamp(progress['processed_day'])+pd.Timedelta(days=1)).date().isoformat() if progress else START
    clocks=read(out/'COMMON_CLOCK.json') if (out/'COMMON_CLOCK.json').exists() else []
    close_values=read(out/'CLOSE_VALUATIONS.json') if (out/'CLOSE_VALUATIONS.json').exists() else []
    def finish_day_batch(events,day,begin_events,begin_fills,held,coverage_start):
        # This marker is committed atomically with the fully processed batch by
        # engine.run/save. Reports can then recover without evaluating another
        # opportunity or requesting an already completed day's market inputs.
        engine.state['b12_completed_day']={'day':day,'begin_events':begin_events,'begin_fills':begin_fills,
            'held_at_day_start':held,'quote_sources':inputs.coverage[coverage_start:]}
        write(out/'DYNAMIC_INPUT_HASHES.json',inputs.pinned);write(out/'QUOTE_COVERAGE.json',inputs.coverage)
        engine.run(events)
    for date in pd.date_range(first,TAIL_END):
        deadline();day=str(date.date());begin_events=engine.state['events_processed'];begin_fills=len(engine.ledger.fills)
        held=sorted(engine.ledger.positions);coverage_start=len(inputs.coverage)
        status('COMMON_P50_REAL_REPLAY',persistent_research_worker_running=True,actual_runner_pid=os.getpid(),account=run_id,
               processed_day=day,checkpoint=str(checkpoint),current_positions=held)
        before=calendar_events(inputs.schedule,day,day)+by_day.get(day,[])
        clock=inputs.clocks.get(day)
        completed_day=engine.state.get('b12_completed_day',{})
        if completed_day.get('day')==day:
            begin_events=completed_day['begin_events'];begin_fills=completed_day['begin_fills'];held=completed_day['held_at_day_start']
        elif clock is None:
            finish_day_batch(before,day,begin_events,begin_fills,held,coverage_start)
        else:
            late=[e for e in before if pd.Timestamp(e['at'])>clock.market_close+pd.Timedelta(minutes=1)]
            before=[e for e in before if pd.Timestamp(e['at'])<=clock.market_close+pd.Timedelta(minutes=1)]
            for s in inputs.universe:
                f=inputs.daily.get(s)
                if f is not None and day in f.index:before.append(inputs.daily_event(s,f.loc[day].to_dict()))
                elif inputs.universe[s]['scope']=='KEEP':before.append(event('DATA_GAP',s,clock.market_close,{'category':'MARKET_DATA','reason':'RTH_DAILY_MISSING_OR_INPUT_UNAVAILABLE'}))
            for s in held:
                try:
                    before += inputs.minute_events(s,day)
                    quotes,receipt=inputs.quotes(s,day,True);before+=quotes
                    if not receipt.get('complete'):before.append(event('DATA_GAP',s,clock.market_close,{'category':'MARKET_DATA','reason':'HELD_RTH_QUOTES_REQUEST_INCOMPLETE'}))
                except Exception as exc:
                    if 'HASH' in str(exc) or 'PINNED' in str(exc):raise
                    before.append(event('DATA_GAP',s,clock.market_close,{'category':'MARKET_DATA','reason':'HELD_INPUT_REQUEST_FAILED','exception':type(exc).__name__}))
                    inputs.coverage.append({'symbol':s,'day':day,'purpose':'ACTUAL_HOLDING_RTH','complete':False,'status':'REQUEST_FAILED','error_type':type(exc).__name__})
            engine.checkpoint_path=None
            engine.run(before)
            engine.checkpoint_path=checkpoint
            candidates=[s for s in inputs.universe if day<=END and eligible_for_quote(engine,s,day)]
            # Date then immutable identity only controls downloads. Engine uses
            # actual arrival clock, present spread and only then frozen tie keys.
            for s in sorted(candidates,key=lambda s:inputs.universe[s]['identity_sort_hash']):
                try:
                    quotes,r=inputs.quotes(s,day,False);late+=quotes
                    if not r.get('complete'):late.append(event('DATA_GAP',s,clock.market_close+pd.Timedelta(minutes=5),{'category':'MARKET_DATA','reason':'ENTRY_QUOTES_REQUEST_INCOMPLETE'}))
                except Exception as exc:
                    if 'HASH' in str(exc) or 'PINNED' in str(exc):raise
                    late.append(event('DATA_GAP',s,clock.market_close+pd.Timedelta(minutes=5),{'category':'MARKET_DATA','reason':'ENTRY_QUOTES_REQUEST_FAILED','exception':type(exc).__name__}))
                    inputs.coverage.append({'symbol':s,'day':day,'purpose':'CAUSAL_SIGNAL_ENTRY_WINDOW','complete':False,'status':'REQUEST_FAILED','error_type':type(exc).__name__})
            finish_day_batch(late,day,begin_events,begin_fills,held,coverage_start)
        if engine.state['errors']:
            write(out/'ENGINE_ERRORS.json',engine.state['errors']);raise ValueError('REAL_ENGINE_EVENT_ERROR_STOP_REVIEW')
        clocks=[r for r in clocks if r['day']!=day]
        clocks.append({'day':day,'session':clock is not None,'entry_allowed':START<=day<=END,
           'event_count':engine.state['events_processed']-begin_events,'new_fills':len(engine.ledger.fills)-begin_fills,
           'held_at_day_start':held,'held_at_day_end':sorted(engine.ledger.positions),'one_shared_cash':engine.ledger.cash,
           'clock_last_at':engine.state['at'],'quote_sources':engine.state['b12_completed_day']['quote_sources']})
        write(out/'COMMON_CLOCK.json',clocks);write(out/'DYNAMIC_INPUT_HASHES.json',inputs.pinned);write(out/'QUOTE_COVERAGE.json',inputs.coverage)
        if clock is not None:
            marks={s:float(inputs.daily[s].loc[day,'close']) for s in engine.ledger.positions
                   if s in inputs.daily and day in inputs.daily[s].index and pd.notna(inputs.daily[s].loc[day,'close'])}
            close_value=Ledger.from_dict(engine.ledger.to_dict()).snapshot(marks)
            if close_value['missing_current_marks']:close_value['net_equity']=None
            close_value.update(trade_date=day,account_state_cutoff=engine.state['at'],
                price_cutoff=clock.market_close.isoformat(),valuation_basis='VENDOR_SESSION_CLOSE_COMMON_WITH_BENCHMARK',
                no_terminal_liquidation_cost=True,after_close_entries_included=True,execution_marks_unchanged=True)
            close_values=[r for r in close_values if r['trade_date']!=day]
            close_values.append(close_value);write(out/'CLOSE_VALUATIONS.json',close_values)
        if day==END:
            write(out/'QUARTER_END_ACCOUNT.json',engine.summary());write(out/'QUARTER_END_LEDGER.json',engine.ledger.to_dict())
            write(out/'QUARTER_END_CLOSE_VALUATION.json',close_value)
        write(out/'progress.json',{'updated_at':utc(),'pid':os.getpid(),'processed_day':day,'events':engine.state['events_processed'],
            'buys':len([f for f in engine.ledger.fills.values() if f['side']=='BUY']),'sells':len([f for f in engine.ledger.fills.values() if f['side']=='SELL']),
            'positions':sorted(engine.ledger.positions),'cash':engine.ledger.cash})
        print(json.dumps({'account':run_id,'day':day,'events':engine.state['events_processed'],'cash':engine.ledger.cash,'positions':sorted(engine.ledger.positions)}),flush=True)
        gc.collect()
    summary=engine.export(out)
    summary.update(account=run_id,interval=[START,END],exit_only_tail_end=TAIL_END,processed_through=TAIL_END,synthetic=False,
                   common_capital=5500,all66_eligibility=True,portfolio_not_stitched=True)
    pd.DataFrame([{k:json.dumps(v) if isinstance(v,(dict,list)) else v for k,v in row.items()} for row in close_values]).to_csv(out/'continuous_close_equity.csv',index=False,encoding='utf-8-sig')
    try:verify_recovery(checkpoint,inputs.schedule,recovery_path)
    finally:engine.close()
    write(final_path,summary)
    return summary

def verify_recovery(checkpoint,schedule,recovery_path):
    restored=ReplayEngine.restore(checkpoint,schedule)
    try:
        original=restored.state_digest();restored.run(calendar_events(schedule,TAIL_END,TAIL_END))
        after=restored.state_digest()
        result={'at':utc(),'before':original,'after':after,'status':'PASS' if original==after else 'FAIL','synthetic':False}
        write(recovery_path,result)
        if result['status']!='PASS':raise ValueError('INCOMPLETE_FINAL_RECOVERY_IDEMPOTENCY_FAILED')
        return result
    finally:restored.close()

def run(earnings_path,version='base_v1',accounts=None):
    results=[]
    for key in accounts or ['Q0:A','Q1:A','Q0:B','Q1:B']:
        mode,tier=key.split(':')
        try:results.append(run_account(mode,tier,earnings_path,version))
        except Exception as exc:
            result={'account':key,'status':'REPLAY_FAILED','error_type':type(exc).__name__,'reason':str(exc),'at':utc()}
            write(ROOT/'portfolio'/version/(key.replace(':','_')+'_ERROR.json'),{**result,'traceback':traceback.format_exc()});results.append(result)
            print(json.dumps(result),flush=True)
        write(ROOT/'portfolio'/version/'RUN_SUMMARY.json',results)
    status('COMMON_P50_BATCH_FINISHED',persistent_research_worker_running=False,portfolio_results=[{k:r.get(k) for k in ('account','status','error_count')} for r in results])
    return results

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--earnings',required=True);p.add_argument('--version',default='base_v1');p.add_argument('--accounts',nargs='*')
    a=p.parse_args();run(a.earnings,a.version,a.accounts)
