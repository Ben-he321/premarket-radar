"""Real-shaped artificial samples only inside tmp_path; never research data."""
from datetime import date
import csv
import json

import pytest

from src.futures_f0.data import Request
from src.futures_f0.reconstruction import CandidateSession, reconstruct_equity_session
from src.futures_f0.runtime import canonical_hash, digest
from src.futures_f0.vendor import EvidenceFile, RawSource, iso_ns, timestamp_ns


def raw_source(tmp_path, schema, rows, name=None):
    path=tmp_path/((name or schema)+'.csv')
    with path.open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    params=Request(('ESZ4',),schema,'2024-10-01T00:00:00Z','2024-10-02T00:00:00Z').parameters()
    receipt=dict(request=params,request_sha256=canonical_hash(params),source='https://hist.databento.com/v0/',
        sha256=digest(path),bytes=path.stat().st_size,received_at='2026-09-15T10:00:00Z',completed_at='2026-09-15T10:01:00Z')
    receipt_path=path.with_suffix('.receipt.json');receipt_path.write_text(json.dumps(receipt))
    return RawSource.from_receipt(path,receipt_path,price_encoding='fixed_1e9')


def bar_row(at, rtype, volume):
    return dict(ts_event=timestamp_ns(at),rtype=rtype,publisher_id=1,instrument_id=183748,
        open=100_000_000_000,high=101_000_000_000,low=99_000_000_000,close=100_000_000_000,volume=volume)


def evidence(tmp_path,kind,scope,**values):
    path=tmp_path/(kind+'.json')
    path.write_text(json.dumps(dict(kind=kind,mock=False,scope=scope,**values)))
    return EvidenceFile(path,digest(path),'EXPLICIT_ENGINEERING_FIXTURE_ONLY')


def fixture(tmp_path, *, missing_hour=False, missing_minute=False, duplicate=False):
    (tmp_path/'MOCK_FIXTURE_DECLARATION.json').write_text(json.dumps({'mock':True,'research_evidence':False,
        'note':'Artificial real-shaped input for bounded nonqualifying reconstruction tests only'}))
    hours=[bar_row('2024-10-01T14:00:00Z',34,60),bar_row('2024-10-01T15:00:00Z',34,60)]
    if missing_hour: hours=hours[1:]
    if duplicate: hours.insert(1,hours[0])
    hourly=raw_source(tmp_path,'ohlcv-1h',hours)
    minute=raw_source(tmp_path,'ohlcv-1m',[bar_row(f'2024-10-01T15:{i:02d}:00Z',33,1)
        for i in range(59 if missing_minute else 60)])
    definition=raw_source(tmp_path,'definition',[dict(ts_event=timestamp_ns('2024-10-01T00:00:00Z'),
        ts_recv=timestamp_ns('2024-10-01T00:00:00Z'),rtype=19,publisher_id=1,instrument_id=183748,
        raw_symbol='ESZ4',instrument_class='F',security_type='FUT',security_update_action='A',leg_count=0,
        currency='USD',exchange='XCME',unit_of_measure='IPNT',unit_of_measure_qty=50_000_000_000,
        min_price_increment=250_000_000,activation=timestamp_ns('2023-01-01T00:00:00Z'),
        expiration=timestamp_ns('2024-12-20T14:30:00Z'))])
    statistics=raw_source(tmp_path,'statistics',[dict(ts_event=timestamp_ns('2024-10-01T20:00:00Z'),
        ts_recv=timestamp_ns('2024-10-01T20:00:00Z')+123,rtype=24,publisher_id=1,instrument_id=183748,
        ts_ref=timestamp_ns('2024-10-01T00:00:00Z'),price=100_500_000_000,quantity=2**63-1,
        sequence=1,channel_id=0,stat_type=3,update_action=1,stat_flags=3)])
    start,end='2024-10-01T14:00:00Z','2024-10-01T16:00:00Z'
    calendar=evidence(tmp_path,'DATED_EXCHANGE_SESSION_CANDIDATE',
        {'contract_id':'ESZ4','session':'2024-10-01'},timezone='America/Chicago',
        segments=[[iso_ns(timestamp_ns(start)),iso_ns(timestamp_ns(end))]])
    reference=evidence(tmp_path,'CME_TRADING_REFERENCE_DATE',{'dataset':'GLBX.MDP3'})
    kwargs=dict(minute_source=minute,definition_source=definition,statistics_source=statistics,
        window=CandidateSession('ESZ4',date(2024,10,1),start,end,calendar),instrument_id=183748,
        publisher_id=1,reference_evidence=reference,settlement_as_of='2024-10-01T21:00:00Z')
    return (hourly,),kwargs


def test_actual_shape_reconstruction_keeps_unknown_publication_and_separate_settlement(tmp_path):
    sources,args=fixture(tmp_path)
    result=reconstruct_equity_session(sources,**args)
    assert result['ohlcv']=={'open':'100','high':'101','low':'99','close':'100','volume':120}
    assert result['coverage']['actual_buckets']==result['coverage']['expected_buckets']==2
    assert result['last_hour_check']['status']=='MATCH'
    assert result['last_hour_check']['minute']['actual_buckets']==60
    assert result['available_at'] is None and result['research_qualified'] is False
    assert result['settlement_reference_at'] is None
    assert result['settlement']['settlement']=='100.5'
    assert result['settlement']['raw_record_sha256']
    assert result['settlement']['capture_available_at']=='2024-10-01T20:00:00.000000123Z'
    assert result['real_futures_backtest_run'] is False


def test_missing_hour_is_visible_not_filled(tmp_path):
    sources,args=fixture(tmp_path,missing_hour=True)
    result=reconstruct_equity_session(sources,**args)
    assert result['ohlcv']['volume']==60
    assert result['coverage']['missing_buckets']==['2024-10-01T14:00:00.000000000Z']
    assert result['coverage']['bucket_coverage']=='MISSING_BUCKETS_OR_VOLUME_UNKNOWN'
    assert result['research_qualified'] is False


def test_missing_minute_fails_independent_hour_check(tmp_path):
    sources,args=fixture(tmp_path,missing_minute=True)
    result=reconstruct_equity_session(sources,**args)
    assert result['last_hour_check']['status']=='MISMATCH_OR_INCOMPLETE_REQUIRES_REVIEW'
    assert result['last_hour_check']['fields_equal']['volume'] is False
    assert result['ohlcv']['volume']==120  # Minute check is never added into day volume.


def test_duplicate_hour_not_double_counted(tmp_path):
    sources,args=fixture(tmp_path,duplicate=True)
    with pytest.raises(ValueError,match='DUPLICATE'):
        reconstruct_equity_session(sources,**args)


def test_explicit_mock_receipt_never_enters_real_candidate(tmp_path):
    sources,args=fixture(tmp_path)
    path=sources[0].receipt_path;receipt=json.loads(path.read_text());receipt['mock']=True
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError,match='MOCK_SOURCE'):
        reconstruct_equity_session(sources,**args)


@pytest.mark.parametrize('protected_source', ['definition_source','statistics_source'])
def test_reconstruction_forwards_guard_into_temporal_source_scan(tmp_path,protected_source):
    sources,args=fixture(tmp_path)
    protected_hash=args[protected_source].validate()['sha256']
    class EngineeringStopGuard:
        def check(self,recovery):
            if recovery.get('stage')=='vendor_stream' and recovery.get('source_sha256')==protected_hash:
                raise RuntimeError('ENGINEERING_TEMPORAL_SCAN_RESOURCE_STOP')
    with pytest.raises(RuntimeError,match='ENGINEERING_TEMPORAL_SCAN_RESOURCE_STOP'):
        reconstruct_equity_session(sources,**args,guard=EngineeringStopGuard())
