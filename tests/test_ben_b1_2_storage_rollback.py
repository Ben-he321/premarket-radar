"""Explicit engineering fixtures for immutable-record rollback snapshots."""
import copy
import pytest
from test_ben_b1_replay import schedule,quote
from test_ben_b1_2_shared import create,initial
from src.ben_b1.ledger import Ledger
from src.ben_b1_2.shared import SharedReplayEngine
from src.ben_b1_2_continuation.execution import BoundedReplayEngine
from src.ben_b1_2_continuation.rollback import quote_ledger_snapshot,quote_state_memo
from src.ben_b1_2_continuation.dense_check import component_proof

def test_historical_records_retain_aliases_but_mutable_containers_are_independent():
    book=Ledger();old={'type':'EXPLICIT_ENGINEERING_FIXTURE'};book.fills['old']=old;book.events.append(old)
    backup=quote_ledger_snapshot(book)
    assert backup==book.to_dict()
    assert backup['state']['fills'] is not book.fills and backup['state']['events'] is not book.events
    assert backup['state']['fills']['old'] is backup['state']['events'][0] is old
    book.fills['new']={'type':'NEW'};book.events.append(book.fills['new'])
    assert list(backup['state']['fills'])==['old'] and len(backup['state']['events'])==1
    book.cash=0;assert backup['state']['cash']==5500

def test_quote_and_closed_order_values_only_are_shared_not_their_containers():
    state={'quotes':{'X':{'bid':100}},'pending':{'closed':{'status':'FILLED'},'open':{'status':'OPEN'},'unknown':{'status':'UNKNOWN'}},'marks':{'X':100}}
    backup=copy.deepcopy(state,quote_state_memo(state,set()))
    assert backup['quotes'] is not state['quotes'] and backup['pending'] is not state['pending']
    assert backup['quotes']['X'] is state['quotes']['X']
    assert backup['pending']['closed'] is state['pending']['closed']
    for key in ('open','unknown'):assert backup['pending'][key] is not state['pending'][key]
    state['quotes']['X']={'bid':200};state['pending']['new']={'status':'OPEN'};state['marks']['X']=200
    assert backup['quotes']['X']['bid']==100 and 'new' not in backup['pending'] and backup['marks']['X']==100

@pytest.mark.parametrize('failure',['EARNINGS_CANCEL','CROSS_POSITION_PLAN'])
def test_quote_fault_preserves_full_legacy_rollback_after_cancel_or_mark_mutation(schedule,tmp_path,monkeypatch,failure):
    results=[]
    for cls in (SharedReplayEngine,BoundedReplayEngine):
        eng=create(schedule,tmp_path/cls.__name__,cls=cls)
        eng.run(initial(schedule)+[quote('2026-05-01 16:05:01','FIRST',ask_size=10)])
        if failure=='CROSS_POSITION_PLAN':eng.run([quote('2026-05-01 16:05:02','SECOND',ask_size=10)])
        called=[];snapshots=[];original_snapshot=eng._rollback_ledger
        def record_snapshot(kind):snapshots.append(kind);return original_snapshot(kind)
        with monkeypatch.context() as patch:
            patch.setattr(eng,'_rollback_ledger',record_snapshot)
            if failure=='EARNINGS_CANCEL':
                cancel=Ledger.cancel_order
                def fail_after_cancel(book,*args,**kwargs):
                    result=cancel(book,*args,**kwargs);called.append(result)
                    raise ValueError('MOCK_POST_EARNINGS_CANCEL')
                patch.setattr(Ledger,'cancel_order',fail_after_cancel)
                patch.setattr(eng,'_gate',lambda symbol:{'new_entry_allowed':False,'reason':'MOCK_UNVERIFIED_EARNINGS','required_exit_time':'2026-05-04T13:30:00+00:00'})
                event=quote('2026-05-04 09:30:00','FIRST',bid=101,ask=101.05)
            else:
                original=eng._quote
                def fail_after_other_position_mark(symbol,payload):
                    original(symbol,payload)
                    result=eng.ledger.plan_entry('UNTRADED_MOCK',10,{'FIRST':111},displayed_size=1)
                    assert result['status']=='CURRENT_EQUITY_UNKNOWN'
                    assert eng.ledger.positions['FIRST']['last_mark']==111
                    called.append(result);raise ValueError('MOCK_POST_REJECTED_PLAN_MARK_MUTATION')
                patch.setattr(eng,'_quote',fail_after_other_position_mark)
                event=quote('2026-05-01 16:05:03','FIRST',bid=130,ask=130.05)
            eng.process(event)
        assert called and snapshots==['QUOTE'] and eng.state['errors']
        results.append(component_proof(eng));eng.close()
    assert results[0]==results[1]
