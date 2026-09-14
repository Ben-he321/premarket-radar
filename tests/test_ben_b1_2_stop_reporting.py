"""Mock run records: planned delivery stops never rewrite earlier failures."""
import copy
import pytest
from src.ben_b1_2_continuation.report import current_stop_record

PROCESS={'pid':123,'started_at':'2026-09-14T20:03:13+00:00'}
OLD_ERROR={'at':'2026-09-14T17:19:10+00:00','type':'PermissionError'}
PLAN={'at':'2026-09-14T23:35:00+00:00','runner_pid':123,'runner_started_at':PROCESS['started_at'],'type':'PLANNED_DELIVERY_BOUNDARY'}

def test_old_failure_is_not_current_and_planned_record_preserves_it():
    old=copy.deepcopy(OLD_ERROR)
    assert current_stop_record(PROCESS,old,None) is None
    assert current_stop_record(PROCESS,old,PLAN)==PLAN
    assert old==OLD_ERROR

@pytest.mark.parametrize('change',[{'runner_pid':999},{'runner_started_at':'2026-09-14T19:07:00+00:00'},{'at':'2026-09-14T18:00:00+00:00'}])
def test_unrelated_or_stale_planned_stop_is_ignored(change):
    assert current_stop_record(PROCESS,OLD_ERROR,{**PLAN,**change}) is None

def test_later_actual_failure_retains_its_own_cause():
    error={'at':'2026-09-14T23:35:01+00:00','type':'ACTUAL_FAILURE'}
    assert current_stop_record(PROCESS,error,PLAN)==error
