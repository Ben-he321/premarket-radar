"""Explicit mock-only engineering fixtures; never research data."""
import json
import pytest
from test_ben_b1_replay import schedule
from src.ben_b1.replay import _hash
from src.ben_b1_2.compact import stream_hash
from src.ben_b1_2_continuation.engine import mapping_digest, StreamingSharedEngine
from src.ben_b1_2_continuation.execution import EventSpool, BoundedReplayEngine

@pytest.mark.parametrize('value',[{}, {'z':1,'a':2}, {'é':{'at':'2026-01-22T21:05:27.534978322+00:00','n':1.0,'f':None,'list':[True,1,-0.0]}}, {'😀':1,'中':2,'a\\b':'quoted"\n'}, {'a':{'nan':float('nan'),'inf':float('inf')}}])
def test_streamed_mapping_is_exact_original_canonical_digest(value):
    count,digest=mapping_digest(sorted(value.items()))
    assert count==len(value) and digest==stream_hash(value)==_hash(value)

@pytest.mark.parametrize('rows',[[('b',1),('a',2)],[('a',1),('a',2)],[(1,'bad')]])
def test_bad_key_order_or_duplicates_not_silently_accepted(rows):
    with pytest.raises(ValueError,match='SORTED_UNIQUE'):mapping_digest(iter(rows))

def test_same_shared_checkpoint_version_not_new_account(schedule,tmp_path):
    from src.ben_b1_2.replay import ReplayConfig
    from src.ben_b1_2.shared import SharedReplayEngine
    from test_ben_b1_2_compact import initial
    original=SharedReplayEngine(schedule,ReplayConfig(run_id='EXPLICIT_MOCK',quote_mode='Q1',synthetic_test=True,checkpoint_every=0),{'X':{'scope':'KEEP','security_id':'MOCK_X'}},tmp_path/'checkpoint.json')
    original.run(initial(schedule)); expected=original.state_digest(); original.close()
    loaded=StreamingSharedEngine.restore(tmp_path/'checkpoint.json',schedule)
    assert loaded.state_digest()==expected
    loaded.close()

CASES=[
 ('test_compacted_daily_blocks_preserve_every_economic_field',{'mode':'Q0'}),
 ('test_compacted_daily_blocks_preserve_every_economic_field',{'mode':'Q1'}),
 ('test_duplicate_archived_event_and_repeated_consumed_display_never_add_fee_or_quantity',{}),
 ('test_archived_event_id_content_conflict_is_checked_even_if_timestamp_is_changed',{'move_time':False}),
 ('test_archived_event_id_content_conflict_is_checked_even_if_timestamp_is_changed',{'move_time':True}),
 ('test_unclosed_same_timestamp_group_is_not_ranked_or_lost_when_saved',{}),
 ('test_archive_committed_before_old_checkpoint_replays_unaccounted_fill_exactly_once',{}),
 ('test_same_session_close_and_entry_blocks_resume_from_last_durable_checkpoint',{}),
 ('test_committed_future_generation_cannot_be_claimed_by_different_replayed_input',{}),
 ('test_obsolete_old_timestamp_cannot_revive_displayed_inventory_or_create_entry',{}),
 ('test_corrupt_or_missing_archive_is_explicit_failure_not_empty_recovery',{'tamper':'event_digest'}),
 ('test_corrupt_or_missing_archive_is_explicit_failure_not_empty_recovery',{'tamper':'archive_missing'})]

@pytest.mark.parametrize('name,extra',CASES)
def test_original_adversarial_cases_actually_use_new_class(name,extra,schedule,tmp_path,monkeypatch):
    import test_ben_b1_2_compact as cases
    from src.ben_b1_2.shared import SharedReplayEngine
    monkeypatch.setattr(cases,'CompactReplayEngine',BoundedReplayEngine)
    # Both sides use the frozen shared-cash semantics; only storage differs.
    monkeypatch.setattr(cases,'ReplayEngine',SharedReplayEngine)
    getattr(cases,name)(schedule=schedule,tmp_path=tmp_path,**extra)

@pytest.mark.parametrize('name,extra',[
 ('test_no_competing_remainders_preserves_original_ledger_and_financial_fields',{'quote_mode':'Q0'}),
 ('test_no_competing_remainders_preserves_original_ledger_and_financial_fields',{'quote_mode':'Q1'}),
 ('test_each_later_partial_fill_preserves_other_fixed_intent_cash_through_recovery',{}),
 ('test_shared_checkpoint_cannot_be_silently_restored_without_cash_guard',{}),
 ('test_this_round_shared_guard_does_not_enable_unrequested_position_or_margin_modes',{'mode':'P100_CONCENTRATED'}),
 ('test_this_round_shared_guard_does_not_enable_unrequested_position_or_margin_modes',{'mode':'P200_MARGIN_RESEARCH'})])
def test_shared_cash_regressions_actually_use_new_class(name,extra,schedule,tmp_path,monkeypatch):
    import test_ben_b1_2_shared as cases
    import src.ben_b1_2.shared as module
    monkeypatch.setattr(module,'SharedReplayEngine',BoundedReplayEngine)
    getattr(cases,name)(schedule=schedule,tmp_path=tmp_path,**extra)

def test_external_order_preserves_nanoseconds_priority_and_stability_over_chunks(tmp_path):
    from src.ben_b1.replay import PRIORITY, _clean
    from src.ben_b1 import rules
    events=[{'event_id':str(i%7),'kind':'QUOTE' if i%2 else 'ENTRY_DECISION','at':'2026-01-22T21:05:27.534978322+00:00','payload':{'ordinal':i}} for i in range(8201)]
    spool=EventSpool(tmp_path)
    for index in range(0,len(events),37):spool.add(iter(events[index:index+37]))
    expected=sorted(events,key=lambda e:(rules.aware(e['at']),PRIORITY[e['kind']],str(e['event_id'])))
    assert list(spool.ordered())==_clean(expected)
    spool.close(discard=True)

def test_external_order_mixed_timezones_nanoseconds_unicode(tmp_path):
    from src.ben_b1.replay import PRIORITY
    from src.ben_b1 import rules
    at=['2026-01-23T14:30:00.000000001+00:00','2026-01-23T09:30:00-05:00',
        '2026-01-23T15:30:00+01:00','2026-01-23T14:29:59.999999999Z']
    events=[{'event_id':k,'kind':kind,'at':t,'payload':{'ordinal':i}} for i,(t,k,kind) in enumerate(
        (t,k,kind) for t in at for k in ['中','é','😀','a','a'] for kind in ['QUOTE','ENTRY_DECISION','QUOTE'])]
    spool=EventSpool(tmp_path)
    for e in reversed(events):spool.add([e])
    expected=sorted(reversed(events),key=lambda e:(rules.aware(e['at']),PRIORITY[e['kind']],str(e['event_id'])))
    assert list(spool.ordered())==expected
    spool.close(discard=True)

@pytest.mark.parametrize('side',['BUY','SELL'])
def test_actual_mutation_then_error_rolls_back_identically(side,schedule,tmp_path,monkeypatch):
    from test_ben_b1_2_shared import create,initial
    from test_ben_b1_replay import quote
    from src.ben_b1.ledger import Ledger
    from src.ben_b1_2.shared import SharedReplayEngine
    from src.ben_b1_2_continuation.dense_check import component_proof
    proofs=[]
    for cls in (SharedReplayEngine,BoundedReplayEngine):
        eng=create(schedule,tmp_path/cls.__name__,cls=cls)
        eng.run(initial(schedule)+[quote('2026-05-01 16:05:01','FIRST',ask_size=10)])
        before=len(eng.ledger.fills);oldassert=Ledger.assert_invariants;injected=[]
        def fail_after_mutation(book):
            oldassert(book)
            if len(book.fills)>before and not injected:injected.append(True);raise ValueError('MOCK_POST_FILL_ROLLBACK')
        at='2026-05-01 16:05:02' if side=='BUY' else '2026-05-04 09:30:00'
        q=quote(at,'FIRST',bid=100.95 if side=='BUY' else 95.50,ask=101. if side=='BUY' else 95.55,ask_size=10)
        with monkeypatch.context() as m:
            m.setattr(Ledger,'assert_invariants',fail_after_mutation)
            eng.process(q)
        assert injected and len(eng.ledger.fills)==before and eng.state['errors'][-1]['reason']=='MOCK_POST_FILL_ROLLBACK'
        failed=component_proof(eng)
        eng.run([{**q,'event_id':q['event_id']+':NEW_ID'}])
        proofs.append((failed,component_proof(eng)));eng.close()
    assert proofs[0]==proofs[1]

def test_non_quote_revision_rollback_retains_original_semantics(schedule,tmp_path,monkeypatch):
    from test_ben_b1_2_shared import create,initial
    from test_ben_b1_replay import quote,ev
    from src.ben_b1.ledger import Ledger
    from src.ben_b1_2.shared import SharedReplayEngine
    from src.ben_b1_2_continuation.dense_check import component_proof
    results=[]
    for cls in (SharedReplayEngine,BoundedReplayEngine):
        eng=create(schedule,tmp_path/cls.__name__,cls=cls)
        eng.run(initial(schedule)+[quote('2026-05-01 16:05:01','FIRST',ask_size=10)])
        oldassert=Ledger.assert_invariants;seen=[]
        def fail(book):
            oldassert(book)
            if not seen:seen.append(True);raise ValueError('MOCK_REVISION_ROLLBACK')
        e=ev('EARNINGS_REVISION','2026-05-01 16:05:02','FIRST',revision={
            'event_id':'MOCK_ADDED','planned_date':'2026-05-20','known_at':'2026-05-01T16:05:02-04:00','source':'MOCK_TEST'},coverage_complete=True)
        before=_hash(eng.state['earnings'])
        with monkeypatch.context() as m:m.setattr(Ledger,'assert_invariants',fail);eng.process(e)
        assert seen and _hash(eng.state['earnings'])==before
        results.append(component_proof(eng));eng.close()
    assert results[0]==results[1]

def test_clock_cache_ny_midnight_dst_and_session_edges(schedule,tmp_path):
    import pandas as pd
    from test_ben_b1_2_shared import create
    from src.ben_b1_2.shared import SharedReplayEngine
    engines=[create(schedule,tmp_path/c.__name__,cls=c) for c in (SharedReplayEngine,BoundedReplayEngine)]
    for day in ('2026-03-06','2026-03-09','2026-11-02'):
        row=schedule.loc[day]
        values=[pd.Timestamp(day,tz='America/New_York')]+[x+d for x in (row.market_open,row.market_close) for d in (pd.Timedelta(nanoseconds=-1),pd.Timedelta(0))]
        for at in values:
            for e in engines:e.state['at']=at.isoformat()
            assert engines[0]._day()==engines[1]._day()
            assert engines[0]._is_rth()==engines[1]._is_rth()
            assert engines[0]._day('2026-01-23T00:00:00Z')==engines[1]._day('2026-01-23T00:00:00Z')
            assert engines[0]._day()==engines[1]._day()
    for e in engines:e.close()

def test_generator_failure_rolls_back_entire_appended_prefix(tmp_path):
    spool=EventSpool(tmp_path)
    first={'event_id':'old','kind':'QUOTE','at':'2026-01-23T14:30:00Z','payload':{}}
    spool.add([first]);before=spool.input_digest.hexdigest()
    def failing():
        for i in range(5000):yield {**first,'event_id':str(i)}
        raise ValueError('MOCK_BROKEN_BATCH')
    with pytest.raises(ValueError,match='MOCK_BROKEN_BATCH'):spool.add(failing())
    assert spool.count==1 and spool.input_digest.hexdigest()==before and list(spool.ordered())==[first]
    spool.close(discard=True)

def test_saved_sql_guard_reason_is_not_lost_as_data_gap(tmp_path):
    import sqlite3
    spool=EventSpool(tmp_path)
    def failing():
        spool.sql_failure=MemoryError('MOCK_RESOURCE_BOUNDARY')
        raise sqlite3.OperationalError('interrupted')
        yield
    with pytest.raises(MemoryError,match='MOCK_RESOURCE_BOUNDARY'):spool.add(failing())
    spool.close(discard=True)

def test_global_quote_schema_and_ids_match_whole_response_with_late_fields(tmp_path):
    import pandas as pd
    import pyarrow.parquet as pq
    from src.ben_b1.data_probe import digest
    from src.ben_b1_2_continuation.inputs import normalize_pages
    from src.ben_b1_2_continuation.runtime import write,sha
    rows=[{'t':f'2026-01-23T14:30:00.{i:09d}Z','bp':10,'ap':11,'bs':3,'as':4} for i in range(5)]
    rows[3].update(optional_text='late',optional_codes=['A']);rows[4].update(optional_text='last',optional_codes=['B'],bs=None)
    for page,part in enumerate((rows[:3],rows[3:])):
        p=tmp_path/f'page_{page:05d}.json';write(p,{'quotes':{'MOCK':part}})
        write(tmp_path/f'page_{page:05d}_receipt.json',{'sha256':sha(p),'source_received_at':f'MOCK_PAGE_{page}'})
    receipt={'path':str(tmp_path),'pages':2,'rows':5,'params':{'symbols':'MOCK'},'last_source_received_at':'MOCK_LAST'}
    path=normalize_pages('MOCK',receipt,batch_size=2)
    expected=pd.DataFrame(rows)
    expected['quote_id']=[digest({'symbol':'MOCK','original_record':r}) for r in expected.to_dict('records')]
    actual=pd.read_parquet(path)
    assert actual.quote_id.tolist()==expected.quote_id.tolist()
    assert actual.optional_text.tolist()==[None,None,None,'late','last']
    assert actual.optional_codes.iloc[3].tolist()==['A']
    assert actual.source_received_at.tolist()==['MOCK_PAGE_0']*3+['MOCK_PAGE_1']*2
