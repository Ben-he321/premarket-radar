"""Resume only the preserved Q1/B checkpoint; other three are references."""
import gc
import json
import os
import time
import traceback
import sqlite3
import msvcrt
from pathlib import Path
import pandas as pd
from src.ben_b1.ledger import Ledger
from src.ben_b1.b11_research import event
from src.ben_b1_2.portfolio import acquisition_eligible
from src.ben_b1_2.replay import calendar_events
from src.ben_b1_2.runtime import START,END,TAIL_END
from .runtime import *
from .inputs import StreamingInputs,StreamingMarket
from .execution import BoundedReplayEngine as ReplayEngine,EventSpool
from .restore_check import process_memory
from .migration import verify_migrated_engine
NY='America/New_York'
deadline=guard
_engine=None
_last_heartbeat=0.

def heartbeat(progress):
    global _last_heartbeat
    if time.monotonic()-_last_heartbeat<10 and progress.get('phase') not in ('DAY_DURABLE','ARCHIVE_ALL_CHECKS_PASS','DISK_EXACT_SORT_START','DISK_EXACT_SORT_COMPLETE','COMPACTION_DISK_STAGE','COMPACTION_CANONICAL_HASH','COMPACTION_ARCHIVE_COMMITTED'):return
    _last_heartbeat=time.monotonic()
    row={**progress,**guard(),**process_memory(),'pid':os.getpid()}
    write(ROOT/'LIVE_PROGRESS.json',row)
    with (ROOT/'RESOURCE_PROFILE.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(row)+'\n')
    print(json.dumps(row),flush=True)

def check_gate():
    gate=read(active_gate_path())
    if gate['status']!='PASS':raise ValueError('ENGINEERING_GATE_NOT_PASS')
    for path,expected in gate['files'].items():
        if sha(path)!=expected:raise ValueError('ENGINEERING_GATE_SOURCE_OR_PROOF_CHANGED:'+path)
    return gate

def resume():
    global _engine
    check_gate();guard()
    checkpoint=ACCOUNT/'checkpoint.json'
    if not checkpoint.is_file():raise ValueError('ORIGINAL_CHECKPOINT_REQUIRED_NO_NEW_ACCOUNT')
    if (ACCOUNT/'FINAL_ACCOUNT.json').exists():raise ValueError('ALREADY_COMPLETED_DO_NOT_RERUN')
    oldspec=read(SOURCE_ACCOUNT/'RUN_SPEC.json')
    for kind in ('source_files','input_files'):
        for path,expected in oldspec[kind].items():
            if sha(path)!=expected:raise ValueError('ORIGINAL_RUN_SOURCE_CHANGED:'+path)
    earnings=next(path for path in oldspec['input_files'] if 'earnings' in Path(path).name.lower())
    inputs=StreamingInputs(earnings,market=StreamingMarket())
    inputs.market.profile_hook=heartbeat
    inputs.coverage=read(ACCOUNT/'QUOTE_COVERAGE.json')
    for path,expected in read(ACCOUNT/'DYNAMIC_INPUT_HASHES.json').items():
        inputs.market.pin_verified(path,expected);inputs.pinned[path]=expected
    ReplayEngine.verification_hook=staticmethod(heartbeat)
    ReplayEngine.guard_hook=staticmethod(guard)
    ReplayEngine.batch_hook=staticmethod(heartbeat)
    engine=ReplayEngine.restore(checkpoint,inputs.schedule);_engine=engine
    if engine.config.run_id!=RUN_ID or engine._payload()['config']!=oldspec['config']:
        # JSON tuples and lists are equivalent in the frozen specification.
        if json.loads(json.dumps(engine._payload()['config']))!=oldspec['config']:raise ValueError('ORIGINAL_RUN_CONFIG_CHANGED')
    if engine.universe!=inputs.universe:raise ValueError('ORIGINAL_ALL66_UNIVERSE_CHANGED')
    verify_migrated_engine(engine,ROOT)
    if engine.state.get('b12_completed_day',{}).get('day','')<'2026-01-22':raise ValueError('ORIGINAL_PREFIX_MISSING')
    if engine.state['b12_completed_day']['day']=='2026-01-22':
        migration_proof=ROOT/'engineering/STORAGE_MIGRATION_EXPECTED.json'
        proof=read(migration_proof if migration_proof.exists() else ROOT/'engineering/REAL_PREFIX_RESTORE.json')
        if engine.state_digest()!=proof['relocated_wrapper_sha256']:raise ValueError('JAN22_STATE_DIVERGED_BEFORE_RESUME')
    out=ACCOUNT;run_id=RUN_ID;tier='B';final_path=out/'FINAL_ACCOUNT.json';recovery_path=out/'RECOVERY_IDEMPOTENCY.json'
    write(ROOT/'CONTINUATION_RUN_SPEC.json',{'at':utc(),'old_run_spec_sha256':sha(SOURCE_ACCOUNT/'RUN_SPEC.json'),
        'same_account_run_id':RUN_ID,'same_config':True,'same_universe':True,'original_starting_capital_not_reinitialized':True,
        'only_relocation':'Private copied archive path','original_snapshot_6607_69_not_final':True,
        'original_round_results_unmodified':True,'gate':str(active_gate_path()),'deadline':DEADLINE})
    extra=inputs.corporate_events()+inputs.earnings_events(tier)
    by_day={}
    for e in extra:by_day.setdefault(str(pd.Timestamp(e['at']).tz_convert(NY).date()),[]).append(e)
    progress=read(out/'progress.json') if (out/'progress.json').exists() else {}
    if not progress or progress.get('processed_day','')<'2026-01-22':raise ValueError('ORIGINAL_REPORT_CURSOR_REQUIRED_NO_QUARTER_RESTART')
    lag=(pd.Timestamp(engine.state['b12_completed_day']['day'])-pd.Timestamp(progress['processed_day'])).days
    if lag not in (0,1):raise ValueError('CHECKPOINT_REPORT_CURSOR_RECONCILIATION_REQUIRED')
    first=(pd.Timestamp(progress['processed_day'])+pd.Timedelta(days=1)).date().isoformat()
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
            before_spool=EventSpool(out/'_transient_sort',guard)
            before_spool.add(before);before=before_spool
            for s in held:
                try:
                    before.add(inputs.minute_events(s,day))
                    quotes,receipt=inputs.quotes(s,day,True);before.add(quotes)
                    if not receipt.get('complete'):before.append(event('DATA_GAP',s,clock.market_close,{'category':'MARKET_DATA','reason':'HELD_RTH_QUOTES_REQUEST_INCOMPLETE'}))
                except Exception as exc:
                    if isinstance(exc,(MemoryError,TimeoutError,OSError,sqlite3.Error)) or 'HASH' in str(exc) or 'PINNED' in str(exc):raise
                    before.append(event('DATA_GAP',s,clock.market_close,{'category':'MARKET_DATA','reason':'HELD_INPUT_REQUEST_FAILED','exception':type(exc).__name__}))
                    inputs.coverage.append({'symbol':s,'day':day,'purpose':'ACTUAL_HOLDING_RTH','complete':False,'status':'REQUEST_FAILED','error_type':type(exc).__name__})
            engine.checkpoint_path=None
            engine.run(before)
            engine.checkpoint_path=checkpoint
            candidates=[]
            if day<=END:
                for s in inputs.universe:
                    acquisition=acquisition_eligible(engine,s,day,late)
                    if acquisition['allowed']:candidates.append(s)
                    elif acquisition['price_candidate']:
                        inputs.coverage.append({'symbol':s,'day':day,'purpose':'CAUSAL_SIGNAL_ENTRY_WINDOW',
                            'status':'NOT_REQUESTED_EARNINGS_UNVERIFIABLE_THIS_WINDOW','rows':None,'complete':None,
                            'quote_availability':'NOT_CHECKED','acquisition_reason':acquisition['reason'],
                            'as_of_event_time':engine.state['at'],'all66_fixed_decisions_retained':True})
            late_spool=EventSpool(out/'_transient_sort',guard)
            late_spool.add(late);late=late_spool
            # Date then immutable identity only controls downloads. Engine uses
            # actual arrival clock, present spread and only then frozen tie keys.
            for s in sorted(candidates,key=lambda s:inputs.universe[s]['identity_sort_hash']):
                try:
                    quotes,r=inputs.quotes(s,day,False);late.add(quotes)
                    if not r.get('complete'):late.append(event('DATA_GAP',s,clock.market_close+pd.Timedelta(minutes=5),{'category':'MARKET_DATA','reason':'ENTRY_QUOTES_REQUEST_INCOMPLETE'}))
                except Exception as exc:
                    if isinstance(exc,(MemoryError,TimeoutError,OSError,sqlite3.Error)) or 'HASH' in str(exc) or 'PINNED' in str(exc):raise
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
        heartbeat({'phase':'DAY_DURABLE','completed_day':day,'cash':engine.ledger.cash,'positions':sorted(engine.ledger.positions),'events':engine.state['events_processed']})
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
        original=restored.state_digest();restored.run(calendar_events(schedule,TAIL_END,TAIL_END));after=restored.state_digest()
        result={'at':utc(),'before':original,'after':after,'status':'PASS' if original==after else 'FAIL','synthetic':False}
        write(recovery_path,result)
        if result['status']!='PASS':raise ValueError('FINAL_RECOVERY_IDEMPOTENCY_FAILED')
    finally:restored.close()

def main():
    lock=(ROOT/'continuation_runner.lock').open('a+b')
    if lock.tell()==0:lock.write(b'0');lock.flush()
    lock.seek(0)
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        lock.close();raise RuntimeError('CONTINUATION_RUNNER_ALREADY_ACTIVE_NO_DUPLICATE_START')
    started_at=utc()
    failed=False
    def diagnostic(operation,label):
        try:return operation()
        except Exception as diagnostic_error:
            print(json.dumps({'at':utc(),'diagnostic_failure':label,'type':type(diagnostic_error).__name__,'reason':str(diagnostic_error)}),flush=True)
    try:
        write(ROOT/'RUNNER_PROCESS.json',{'pid':os.getpid(),'started_at':started_at,'repo':str(REPO),'account':str(ACCOUNT),'status':'RUNNING'})
        status('CONTINUATION_START',research_running=True,persistent_research_worker_running=True,actual_runner_pid=os.getpid(),error_type=None,reason=None)
        result=resume()
        status('FULL_QUARTER_AND_TAIL_COMPLETE',research_running=False,persistent_research_worker_running=False,result=result)
    except Exception as exc:
        failed=True
        error={'at':utc(),'status':'STOPPED_EVIDENCE_RETAINED','type':type(exc).__name__,'reason':str(exc),
            'traceback':traceback.format_exc(),'no_from_scratch_retry':True,'checkpoint':str(ACCOUNT/'checkpoint.json'),
            'resources':diagnostic(resources,'ERROR_RESOURCE_SNAPSHOT'),'process_memory':diagnostic(process_memory,'ERROR_PROCESS_SNAPSHOT')}
        print(json.dumps({'primary_error':error}),flush=True)
        diagnostic(lambda:write(ROOT/('STOP_RECORD_'+started_at.replace(':','').replace('+','_')+'_'+str(os.getpid())+'.json'),error),'VERSIONED_STOP_RECORD')
        diagnostic(lambda:write(ROOT/'STOP_RECORD.json',error),'LATEST_STOP_RECORD')
        diagnostic(lambda:status('STOPPED_EVIDENCE_RETAINED',research_running=False,persistent_research_worker_running=False,error_type=type(exc).__name__,reason=str(exc)),'STOP_STATUS')
        raise
    finally:
        try:
            if _engine is not None:
                if failed:diagnostic(_engine.close,'ARCHIVE_CLOSE_AFTER_PRIMARY_ERROR')
                else:_engine.close()
        finally:
            stopped={'pid':os.getpid(),'started_at':started_at,'finished_at':utc(),'repo':str(REPO),'account':str(ACCOUNT),'status':'STOPPED'}
            try:
                diagnostic(lambda:write(ROOT/('RUNNER_EXIT_'+str(os.getpid())+'_'+started_at.replace(':','').replace('+','_')+'.json'),stopped),'VERSIONED_PROCESS_EXIT')
                diagnostic(lambda:write(ROOT/'RUNNER_PROCESS.json',stopped),'LATEST_PROCESS_EXIT')
            finally:
                lock.seek(0)
                try:msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
                finally:lock.close()
if __name__=='__main__':main()
