"""Real Jan5 MRVL dense block, interrupted after archive commit then replayed."""
import time
import pandas as pd
from src.ben_b1.b11_research import Inputs
from src.ben_b1_2.samples import PinnedHoldingQuoteProvider,deduplicate_events
from src.ben_b1_2.replay import ReplayConfig,calendar_events
from .dense_check import VERSION,NAME,MANIFEST,component_proof
from .execution import BoundedReplayEngine
from .runtime import *

def run():
    out=ROOT/'engineering/real_crash';cp=out/'checkpoint.json'
    if cp.exists():raise ValueError('EXISTING_CRASH_EVIDENCE_NO_SILENT_RETRY')
    started=time.monotonic();guard();inputs=Inputs();spec=read(OLD/'samples'/VERSION/'tier_B'/NAME/'RUN_SPEC.json')
    cfg=ReplayConfig(**spec['config']);sample=inputs.first[1];symbol='MRVL';first=sample['trade_date']
    universe={symbol:{'security_id':sample['local_security_id'],'scope':'KEEP','identity_sort_hash':sample['identity_sort_hash']}}
    daily,minutes,quotes=inputs.symbol_data(symbol);extra=inputs.earnings_events(symbol,'B',first)+inputs.corporate_events(symbol,first,'2026-04-30')
    engine=BoundedReplayEngine(inputs.schedule,cfg,universe,cp);engine.guard_hook=guard
    warmup=[inputs.daily_event(symbol,row) for row in daily[daily.trade_date<first].to_dict('records')]
    engine.run(warmup+[e for e in extra if pd.Timestamp(e['at'])<pd.Timestamp(first,tz='America/New_York')])
    def events(day,held=False):
        result=calendar_events(inputs.schedule,day,day)+[e for e in extra if str(pd.Timestamp(e['at']).tz_convert('America/New_York').date())==day]
        result+=inputs.day_events(symbol,day,daily,minutes,quotes)
        if held:result+=PinnedHoldingQuoteProvider(MANIFEST)(symbol,day)['events']
        return deduplicate_events(result)
    old=read(ROOT/'engineering/dense_original/BLOCK_PROOFS.json')
    expected=lambda at:next({k:v for k,v in r.items() if k!='elapsed'} for r in old if r['at']==at)
    for day in ('2026-01-02','2026-01-03','2026-01-04'):engine.run(events(day))
    assert component_proof(engine)==expected(engine.state['at'])
    prefix=engine.state_digest();prefix_file=sha(cp);block=events('2026-01-05',True)
    def interrupt(committed):raise RuntimeError('INTENTIONAL_ENGINEERING_CRASH_AFTER_DURABLE_ARCHIVE_BEFORE_CHECKPOINT')
    engine.after_archive_commit_hook=interrupt
    try:engine.run(block)
    except RuntimeError as exc:
        if 'INTENTIONAL_ENGINEERING_CRASH' not in str(exc):raise
    else:raise AssertionError('CRASH_NOT_INJECTED')
    engine.close();assert sha(cp)==prefix_file
    resumed=BoundedReplayEngine.restore(cp,inputs.schedule);resumed.guard_hook=guard
    assert resumed.state_digest()==prefix
    future=resumed._db.execute('SELECT COUNT(*) FROM archive_blocks WHERE sequence>?',(resumed.state['archive']['applied_sequence'],)).fetchone()[0]
    assert future==1
    resumed.run(block);after=component_proof(resumed)
    assert after==expected(resumed.state['at'])
    completed=resumed.state_digest();resumed.run(block);assert resumed.state_digest()==completed
    result={'status':'PASS','at':utc(),'synthetic':False,'real_symbol':symbol,'real_day':'2026-01-05',
        'real_block_events_submitted':len(block),'unchanged_checkpoint_during_crash':True,'future_unclaimed_generations':future,
        'original_prefix_restored_exactly':True,'actual_dense_block_replayed_after_crash':True,
        'all_financial_and_nonfinancial_component_hashes_equal_uninterrupted_original':True,
        'entire_real_dense_block_duplicate_idempotency':True,'proof':after,'elapsed_seconds':time.monotonic()-started,
        'scope':'EXISTING_FIXED_MRVL_ENGINEERING_FIXTURE_NO_Q1_B_NEW_DATE_EXECUTED'}
    write(ROOT/'engineering/REAL_DENSE_CRASH_RECOVERY.json',result);resumed.close();print(result,flush=True)
if __name__=='__main__':run()
