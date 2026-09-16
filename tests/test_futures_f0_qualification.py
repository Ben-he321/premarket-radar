"""Explicit artificial records for engineering tests; never research inputs."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import pytest

from src.futures_f0.qualification import (realized_calendar,first_clearing_final,execution_evidence,settlement_audit_scope)
from src.futures_f0.session_inputs import _aggregate,capture_prefix_clock
from src.futures_f0.provenance import verify_bar_derivation
from src.futures_f0.vendor import timestamp_ns,iso_ns
from src.futures_f0.engine import FuturesEngine
from test_futures_f0_session_inputs import policy
from test_futures_f0_engine import bar,cfg


class ArtificialCatalog:
    def __init__(self,rows,coverage='COMPLETE_REQUEST_AVAILABLE_DATASET'):
        self.data=rows;self.coverage=coverage;self.calendar_cache={}
    def rows(self,schema,identity=None,before=None,after=None):
        index='ts_event_ns' if schema.startswith('ohlcv-') else 'ts_recv_ns'
        for row in self.data:
            if row['schema']!=schema:continue
            if before is not None and row[index]>before or after is not None and row[index]<after:continue
            yield row
    def interval_coverage(self,*args):return dict(status=self.coverage,request_complete=self.coverage!='NOT_REQUESTED',proofs=['ARTIFICIAL'],dates=[])


def status(event,action,*,recv=None,trading='N',trading_event=0,reason=1,n=1):
    recv=recv or event
    return dict(schema='status',source_sha256='ARTIFICIAL',source_line=n,raw_record_sha256=str(n),
        ts_event=event,ts_event_ns=timestamp_ns(event),ts_recv=recv,ts_recv_ns=timestamp_ns(recv),
        action=action,reason=reason,trading_event=trading_event,is_trading=trading,is_quoting=trading)


def status_fixture():
    return [status('2024-07-03T21:00:00Z',1,trading_event=2),
        status('2024-07-03T22:00:00Z',6,recv='2024-07-03T22:00:00.010Z',trading='Y',n=2),
        status('2024-07-03T22:00:00Z',7,recv='2024-07-03T22:00:00.020Z',trading='Y',n=3),
        status('2024-07-04T17:00:00Z',9,n=4),
        status('2024-07-04T22:00:00Z',7,trading='Y',n=5),
        status('2024-07-05T21:00:00Z',12,recv='2024-07-05T21:00:00.002Z',n=6)]


def calendar(rows):return realized_calendar(ArtificialCatalog(rows),contract_id='ESU4',identity=(1,118))


def test_real_reset_cohort_can_cross_holiday_without_fabricating_daily_bar():
    actual=calendar(status_fixture())
    assert len(actual)==1 and actual[0]['session']=='2024-07-05'
    assert len(actual[0]['segments'])==2
    assert actual[0]['calendar_confirmed_at']=='2024-07-05T21:00:00.002000000Z'


def test_proven_midnight_duplicate_does_not_create_second_reset():
    rows=status_fixture();duplicate=dict(rows[0],ts_recv='2024-07-04T00:00:00Z',ts_recv_ns=timestamp_ns('2024-07-04T00:00:00Z'))
    rows.insert(3,duplicate)
    assert calendar(rows)==calendar(status_fixture())


def test_unknown_late_halt_is_not_silently_ignored_as_snapshot():
    rows=status_fixture();rows.insert(3,status('2024-07-03T21:59:59Z',8,recv='2024-07-04T00:00:00Z',reason=3,n=20))
    with pytest.raises(ValueError,match='LATE_OR_CONFLICTING'):calendar(rows)


def test_same_capture_status_conflict_is_not_sorted_to_convenient_state():
    rows=status_fixture();rows[2]['ts_recv']=rows[1]['ts_recv'];rows[2]['ts_recv_ns']=rows[1]['ts_recv_ns']
    with pytest.raises(ValueError,match='LATE_OR_CONFLICTING'):calendar(rows)


def test_second_reset_cannot_drop_still_open_segment():
    rows=status_fixture();rows.insert(3,status('2024-07-04T00:00:00Z',1,trading_event=2,n=30))
    with pytest.raises(ValueError,match='RESET_WITHOUT'):calendar(rows)


def test_missing_status_request_is_not_a_verified_calendar():
    result=realized_calendar(ArtificialCatalog(status_fixture(),'NOT_REQUESTED'),contract_id='ESU4',identity=(1,118))
    assert not result[0]['coverage_qualified']
    assert result[0]['request_coverage']['status']=='NOT_REQUESTED'


def statistic(at,flags=3,price='100',action=1,n=1):
    return dict(schema='statistics',stat_type=3,source_sha256='ARTIFICIAL',source_line=n,raw_record_sha256=str(n),
        ts_recv=at,ts_event=at,ts_recv_ns=timestamp_ns(at),ts_event_ns=timestamp_ns(at),price=price,
        update_action=action,stat_flags=flags,ts_ref='2024-07-01T00:00:00Z',reference_session_hint='2024-07-01',
        settlement_flags=dict(final=bool(flags&1),actual=bool(flags&2),trading_tick=bool(flags&4),
            intraday=bool(flags&8),unknown_bits=flags&~15))


@pytest.mark.parametrize('flags',[2,6,7,11])
def test_trading_preliminary_or_intraday_cannot_be_account_clearing(flags):
    first,audit=first_clearing_final(ArtificialCatalog([statistic('2024-07-01T23:00:00Z',flags)]),(1,118),'2024-07-01',timestamp_ns('2024-07-02T12:00:00Z'))
    assert first is None and audit['status']!='FIRST_FINAL_WITHOUT_LATER_ECONOMIC_REVISION'


def test_first_final_capture_remains_first_when_trading_precision_arrives_later():
    rows=[statistic('2024-07-01T23:00:00Z',3,'100.005'),statistic('2024-07-02T00:00:00Z',7,'100',n=2)]
    first,audit=first_clearing_final(ArtificialCatalog(rows),(1,118),'2024-07-01',timestamp_ns('2024-07-02T12:00:00Z'))
    assert first==rows[0] and audit['status']=='FIRST_FINAL_WITHOUT_LATER_ECONOMIC_REVISION'


@pytest.mark.parametrize('last',[dict(flags=3,price='101'),dict(flags=3,action=2),dict(flags=2),dict(flags=1)])
def test_later_clearing_revision_is_explicit_account_adapter_gate(last):
    rows=[statistic('2024-07-01T23:00:00Z'),statistic('2024-07-02T00:00:00Z',n=2,**last)]
    first,audit=first_clearing_final(ArtificialCatalog(rows),(1,118),'2024-07-01',timestamp_ns('2024-07-02T12:00:00Z'))
    assert first is not None and audit['status'].startswith('CLEARING_REVISION_AFTER_FIRST_FINAL')


def test_same_time_7_and_3_are_one_batch_and_distinct_precision():
    rows=[statistic('2024-07-01T23:00:00Z',7,'100'),statistic('2024-07-01T23:00:00Z',3,'100.005',n=2)]
    first,audit=first_clearing_final(ArtificialCatalog(rows),(1,118),'2024-07-01',timestamp_ns('2024-07-02T12:00:00Z'))
    assert first['price']=='100.005' and audit['status']=='FIRST_FINAL_WITHOUT_LATER_ECONOMIC_REVISION'


def test_same_time_clearing_conflict_cannot_select_the_first_row():
    rows=[statistic('2024-07-01T23:00:00Z',3,'100'),statistic('2024-07-01T23:00:00Z',3,'101',n=2)]
    first,audit=first_clearing_final(ArtificialCatalog(rows),(1,118),'2024-07-01',timestamp_ns('2024-07-02T12:00:00Z'))
    assert first is None and 'CONFLICTING' in audit['status']


def price_row(at):
    return dict(schema='ohlcv-1h',ts_event=at,ts_event_ns=timestamp_ns(at),open='100',high='101',low='99',close='100',
                volume=5,source_sha256='ARTIFICIAL',source_line=2,raw_record_sha256='ARTIFICIAL')


@pytest.mark.parametrize('coverage,qualified',[('COMPLETE_REQUEST_AVAILABLE_DATASET',True),('NOT_REQUESTED',False),('DATASET_DATE_NOT_AVAILABLE',False)])
def test_missing_bucket_distinguishes_full_response_from_unrequested_or_degraded(coverage,qualified):
    rows=[price_row('2024-07-01T15:00:00Z')];cat=ArtificialCatalog(rows,coverage)
    out=_aggregate([], (1,118),[(timestamp_ns('2024-07-01T14:00:00Z'),timestamp_ns('2024-07-01T16:00:00Z'))],None,catalog=cat,contract_id='ESU4')
    assert (out['session_ohlcv'] is not None)==qualified
    assert len(out['records'])==1 and out['observed_partial_ohlcv']['volume']==5


def test_first_missing_bucket_denies_open_even_when_daily_ohlcv_exists():
    cohort=calendar(status_fixture())[0]
    prices=dict(records=[dict(bucket_start='2024-07-03T23:00:00Z')],session_ohlcv=dict(open='100',high='101',low='99',close='100'))
    out=execution_evidence(ArtificialCatalog([]),(1,118),cohort,prices)
    assert not out['tradable_open'] and 'NO_OBSERVED_FIRST_BUCKET_NO_BACKFILLED_OPEN' in out['open_reasons']


def test_calendar_capture_delays_internal_clock_without_inventing_vendor_time(tmp_path):
    evidence=policy(tmp_path);end=timestamp_ns('2024-10-01T21:00:00Z')
    out=capture_prefix_clock(evidence,start=end-3600*10**9,end=end,used_schemas={'ohlcv-1h'},
        calendar_confirmed_at='2024-10-01T21:00:00.002148521Z')
    assert out['input_cutoff']=='2024-10-01T21:00:00.000000000Z'
    assert out['available_at']==out['calendar_confirmed_at']
    assert out['supplier_published_at'] is None and out['received_at'] is None


def test_engine_accepts_evidence_bound_calendar_clock_and_rejects_backdate():
    b=bar(0);confirmed=b.closes_at+timedelta(microseconds=20)
    b=replace(b,availability_basis='INTERNAL_CAPTURE_PREFIX',input_cutoff=b.closes_at,
        calendar_confirmed_at=confirmed,internal_calculated_at=confirmed,available_at=confirmed,temporal_evidence_hash='e'*64)
    engine=FuturesEngine({},cfg())
    assert engine._bar_problem(b) is None
    assert engine._bar_problem(replace(b,internal_calculated_at=b.closes_at,available_at=b.closes_at))=='CAPTURE_PREFIX_CLOCK_EVIDENCE_INVALID'


def test_qualified_derivation_does_not_accept_hand_edited_trade_permission():
    value=dict(integration_inputs={'qualification_context':{'sha256':'e'*64}},research_qualified=True,
               engine_record_candidate=dict(status='QUALIFIED',tradable_open=False))
    with pytest.raises(ValueError,match='tradable_open'):
        verify_bar_derivation(dict(status='QUALIFIED',tradable_open=True),value)


def test_settlement_negative_revision_assertion_requires_actual_request_range():
    with pytest.raises(ValueError,match='NOT_FULLY_REQUESTED'):
        settlement_audit_scope(ArtificialCatalog([],'NOT_REQUESTED'),'ESU4',(1,118),
                              timestamp_ns('2024-07-01T22:00:00Z'),timestamp_ns('2024-07-03T00:00:00Z'))


def test_settlement_audit_stops_before_degraded_capture_date():
    class Dates(ArtificialCatalog):
        def interval_coverage(self,*args):
            return dict(status='DATASET_DATE_NOT_AVAILABLE',request_complete=True,proofs=['ARTIFICIAL'],
                        dates=[dict(date='2024-09-17',conditions=['available']),dict(date='2024-09-18',conditions=['degraded'])])
    end,proof=settlement_audit_scope(Dates([]),'ESZ4',(1,118),timestamp_ns('2024-09-17T22:00:00Z'),timestamp_ns('2024-09-20T00:00:00Z'))
    assert end==timestamp_ns('2024-09-18T00:00:00Z')
    first,audit=first_clearing_final(ArtificialCatalog([statistic('2024-09-18T01:00:00Z')]),(1,118),'2024-07-01',end-1)
    assert first is None


def test_missing_weekday_cannot_be_excused_as_a_closed_saturday():
    class Dates(ArtificialCatalog):
        def interval_coverage(self,*args):
            return dict(status='DATASET_DATE_NOT_AVAILABLE',request_complete=True,proofs=['ARTIFICIAL'],
                        dates=[dict(date='2024-07-02',conditions=[])])
    end,_=settlement_audit_scope(Dates([]),'ESU4',(1,118),timestamp_ns('2024-07-01T22:00:00Z'),timestamp_ns('2024-07-03T00:00:00Z'))
    assert end==timestamp_ns('2024-07-02T00:00:00Z')


def test_open_fill_padding_includes_frozen_slippage_and_rounding():
    cohort=calendar(status_fixture())[0];at='2024-07-03T21:59:00Z'
    upper=dict(statistic(at,price='101'),stat_type=17);lower=dict(statistic(at,price='50'),stat_type=18)
    prices=dict(records=[dict(bucket_start='2024-07-03T22:00:00Z')],session_ohlcv=dict(open='100',high='100',low='99',close='100'))
    result=execution_evidence(ArtificialCatalog([upper,lower]),(1,118),cohort,prices,tick_size='0.25')
    assert not result['tradable_open'] and result['fill_limit_padding_quote_units']=='1.25'
