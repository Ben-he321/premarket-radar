"""Artificial CSV fixtures are temporary and are never real-market evidence."""
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import csv
import json

import pytest

from src.futures_f0.data import Request, SessionWindow
from src.futures_f0.runtime import canonical_hash, digest
from src.futures_f0.vendor import (EvidenceFile, RawSource, definition_at, inspect_sources,
    iso_ns, manifest_candidate_entry, model_time, price_decimal, session_candidate,
    settlement_at, timestamp_ns)


def source(tmp_path, schema, rows, *, encoding='fixed_1e9', name=None, mock=True):
    path = tmp_path / ((name or schema) + '.csv')
    headers = list(rows[0]) if rows else ['ts_event', 'rtype', 'publisher_id', 'instrument_id']
    with path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    parameters = Request(('ESZ4',), schema, '2024-10-01T00:00:00+00:00',
                         '2024-10-02T00:00:00+00:00').parameters()
    receipt = dict(request=parameters, request_sha256=canonical_hash(parameters),
        source='https://hist.databento.com/v0/', sha256=digest(path), bytes=path.stat().st_size,
        received_at='2026-09-15T10:00:00+00:00', completed_at='2026-09-15T10:01:00+00:00',
        mock=mock, csv_format={'pretty_px': encoding == 'decimal'})
    receipt_path = path.with_suffix('.receipt.json')
    receipt_path.write_text(json.dumps(receipt), encoding='utf-8')
    return RawSource.from_receipt(path, receipt_path, price_encoding=encoding)


def base(rtype, at='2024-10-01T14:00:00Z'):
    return dict(ts_event=str(timestamp_ns(at)), rtype=rtype, publisher_id=1, instrument_id=183748)


def bars(at='2024-10-01T14:00:00Z'):
    return dict(base(34, at), open=5750250000000, high=5751000000000,
                low=5750000000000, close=5750750000000, volume=200)


def definition(at='2024-10-01T00:00:00Z', **updates):
    return dict(base(19, at), ts_recv=str(timestamp_ns(at)), raw_symbol='ESZ4',
        instrument_class='F', security_type='FUT', security_update_action='A', currency='USD',
        exchange='XCME', asset='ES', unit_of_measure='USD', leg_count=0,
        min_price_increment=250000000, display_factor=10000000,
        unit_of_measure_qty=50000000000, min_price_increment_amount=12500000000,
        activation=str(timestamp_ns('2023-01-01T00:00:00Z')),
        expiration=str(timestamp_ns('2024-12-20T14:30:00Z'))) | updates


def statistic(at, *, price=5750750000000, flags=3, action=1, session='2024-10-01', sequence=1):
    return dict(base(24, at), ts_recv=str(timestamp_ns(at) + 123),
        ts_ref=str(timestamp_ns(session + 'T00:00:00Z')), price=price,
        quantity=9223372036854775807, sequence=sequence, channel_id=1,
        stat_type=3, update_action=action, stat_flags=flags)


def evidence(tmp_path, kind, *, scope, **payload):
    path = tmp_path / (kind + '.json')
    path.write_text(json.dumps(dict(kind=kind, mock=False, scope=scope, **payload)), encoding='utf-8')
    # This entire file remains an artificial fixture inside pytest tmp_path.
    return EvidenceFile(path, digest(path), 'ENGINEERING_FIXTURE_SOURCE')


def test_nanoseconds_are_exact_and_availability_rounds_forward():
    at = '2024-10-01T14:00:00.123456789Z'
    value = timestamp_ns(at)
    assert iso_ns(value) == at
    assert timestamp_ns('2024-10-01T16:00:00.123456789+02:00') == value
    assert model_time(value).isoformat() == '2024-10-01T14:00:00.123457+00:00'
    with pytest.raises(ValueError, match='ZONE'):
        timestamp_ns('2024-10-01T14:00:00')
    assert timestamp_ns(str(2**64-1), nullable=True) is None


def test_explicit_price_encoding_and_cents_multiplier():
    assert price_decimal('432250000000', 'fixed_1e9') == Decimal('432.25')
    assert price_decimal('432.25', 'decimal') == Decimal('432.25')
    assert Decimal(5000) * Decimal('0.01') == 50  # ZC cents per bushel -> USD/quote point.
    assert Decimal('0.25') * 50 == Decimal('12.50')
    assert price_decimal('-1250000000', 'fixed_1e9') == Decimal('-1.25')
    assert price_decimal(str(2**63-1), 'fixed_1e9', nullable=True) is None
    with pytest.raises(ValueError, match='EXPLICIT'):
        price_decimal('432250000000', 'auto')
    with pytest.raises(ValueError, match='NONFINITE'):
        price_decimal('NaN', 'decimal')


def test_raw_stream_preserves_source_and_does_not_invent_publication(tmp_path):
    raw = source(tmp_path, 'ohlcv-1h', [bars()])
    record = next(raw.records())
    assert record['open'] == '5750.25' and record['volume'] == 200
    assert record['historical_publication_at'] is None and record['ts_recv'] is None
    assert record['source_line'] == 2 and record['is_mock'] is True
    assert record['research_qualified'] is False
    with raw.path.open('a') as f:
        f.write('\n')
    with pytest.raises(ValueError, match='HASH_OR_SIZE'):
        list(raw.records())


def test_wrong_schema_missing_volume_and_unsorted_rows_remain_visible(tmp_path):
    raw = source(tmp_path, 'ohlcv-1h', [bars() | {'volume': ''}, bars('2024-10-01T13:00:00Z'),
        bars('2024-10-01T16:00:00Z') | {'rtype': 35}])
    records = list(raw.records())
    assert records[0]['volume'] is None
    assert records[1]['issues'] == ['VENDOR_RECORDS_NOT_IN_INDEX_TIME_ORDER']
    assert records[2]['issues'] == ['SCHEMA_RECORD_TYPE_MISMATCH']


def test_inspection_counts_empty_and_mock_sources_without_qualification(tmp_path):
    raw = source(tmp_path, 'ohlcv-1h', [bars()])
    empty = source(tmp_path, 'status', [])
    report = inspect_sources([raw, empty], tmp_path / 'out')
    assert report['raw_rows'] == 1 and report['research_qualified_rows'] == 0
    assert report['real_futures_backtest_run'] is False
    assert all(s['provenance'] == 'ENGINEERING_FIXTURE' for s in report['source_objects'])
    with (tmp_path/'out'/'VENDOR_SAMPLE_COVERAGE.csv').open(encoding='utf-8-sig') as f:
        counts = list(csv.DictReader(f))
    assert counts[1]['rows'] == '0' and 'EMPTY_SCHEMA_RESPONSE_NOT_COVERAGE' in counts[1]['issues']


def test_definition_versions_are_causal_and_deletes_and_spreads_block(tmp_path):
    raw = source(tmp_path, 'definition', [definition(),
        definition('2024-10-01T15:00:00Z', security_update_action='D')])
    args = dict(contract_id='ESZ4', instrument_id=183748, publisher_id=1)
    assert definition_at(raw, **args, as_of='2024-10-01T14:00:00Z')['raw_symbol'] == 'ESZ4'
    with pytest.raises(ValueError, match='NO_ACTIVE'):
        definition_at(raw, **args, as_of='2024-10-01T16:00:00Z')
    spread = source(tmp_path, 'definition', [definition(instrument_class='S', leg_count=2)], name='spread')
    with pytest.raises(ValueError, match='NON_OUTRIGHT'):
        definition_at(spread, **args, as_of='2024-10-01T14:00:00Z')


def test_definition_dtype_sentinels_preserve_raw_without_inventing_listing(tmp_path):
    raw = source(tmp_path, 'definition', [definition(contract_multiplier=2**31-1,
        original_contract_size=2**31-1,price_display_format=255,maturity_year=65535,
        contract_multiplier_unit=127)])
    record = next(raw.records())
    for field in ('contract_multiplier','original_contract_size','price_display_format',
                  'maturity_year','contract_multiplier_unit'):
        assert record[field] is None
        assert record['definition_integer_raw'][field] is not None
    assert record['definition_integer_raw']['contract_multiplier'] == '2147483647'
    assert record['unit_of_measure_qty']=='50' and record['leg_count']==0
    assert 'listed' not in record
    assert record['activation_semantics']=='INSTRUMENT_ENABLED_TIMESTAMP_NOT_FIRST_TRADED_SESSION_OR_LISTED_DATE'


def test_settlement_reference_does_not_shift_date_and_future_correction_is_excluded(tmp_path):
    raw = source(tmp_path, 'statistics', [statistic('2024-10-01T19:00:00Z', flags=2),
        statistic('2024-10-01T20:00:00Z', sequence=2),
        statistic('2024-10-01T21:00:00Z', price=5751000000000, sequence=3),
        statistic('2024-10-01T22:00:00Z', action=2, sequence=4)])
    reference = evidence(tmp_path, 'CME_TRADING_REFERENCE_DATE', scope={'dataset': 'GLBX.MDP3'})
    args = dict(instrument_id=183748, publisher_id=1, session='2024-10-01', reference_evidence=reference)
    early = settlement_at(raw, **args, as_of='2024-10-01T19:30:00Z')
    assert early['settlement'] is None
    selected = settlement_at(raw, **args, as_of='2024-10-01T20:30:00Z')
    assert selected['settlement'] == '5750.75' and selected['session'] == '2024-10-01'
    assert selected['model_capture_available_at'].endswith('20:00:00.000001+00:00')
    corrected = settlement_at(raw, **args, as_of='2024-10-01T21:30:00Z')
    assert corrected['settlement'] == '5751' and corrected['updates_as_of'] == 3
    deleted = settlement_at(raw, **args, as_of='2024-10-01T22:30:00Z')
    assert deleted['status'] == 'SETTLEMENT_DELETED_AS_OF' and deleted['settlement'] is None


@pytest.mark.parametrize('flags', [1, 2, 0, 11, 19])
def test_theoretical_preliminary_intraday_and_unknown_settlement_flags_block(tmp_path, flags):
    raw = source(tmp_path, 'statistics', [statistic('2024-10-01T20:00:00Z', flags=flags)])
    reference = evidence(tmp_path, 'CME_TRADING_REFERENCE_DATE', scope={'dataset': 'GLBX.MDP3'})
    assert settlement_at(raw, instrument_id=183748, publisher_id=1, session='2024-10-01',
        as_of='2024-10-01T21:00:00Z', reference_evidence=reference)['settlement'] is None


def test_reference_evidence_hash_and_date_precision_are_required(tmp_path):
    raw = source(tmp_path, 'statistics', [statistic('2024-10-01T20:00:00Z') |
        {'ts_ref': str(timestamp_ns('2024-10-01T05:00:00Z'))}])
    reference = evidence(tmp_path, 'CME_TRADING_REFERENCE_DATE', scope={'dataset': 'GLBX.MDP3'})
    args = dict(instrument_id=183748, publisher_id=1, session='2024-10-01', as_of='2024-10-01T21:00:00Z')
    with pytest.raises(ValueError, match='DATE_PRECISION'):
        settlement_at(raw, **args, reference_evidence=reference)
    with pytest.raises(ValueError, match='HASH'):
        settlement_at(raw, **args, reference_evidence=replace(reference, sha256='1'*64))


@pytest.mark.parametrize('delta', [12345,-12345,0])
def test_publisher_sending_timestamp_preserves_exact_delta_and_clock_skew(tmp_path,delta):
    row=statistic('2024-10-01T20:00:00Z') | {'ts_in_delta':delta}
    record=next(source(tmp_path,'statistics',[row]).records())
    assert record['publisher_send_at']==iso_ns(int(row['ts_recv'])-delta)
    assert record['ts_in_delta_raw']==str(delta)
    assert record['historical_publication_at'] is None


@pytest.mark.parametrize('delta', [-(2**31),2**31-1])
def test_clamped_delta_cannot_claim_exact_publisher_timestamp(tmp_path,delta):
    record=next(source(tmp_path,'statistics',[statistic('2024-10-01T20:00:00Z') | {'ts_in_delta':delta}]).records())
    assert record['ts_in_delta'] is None and record['publisher_send_at'] is None
    assert record['ts_in_delta_raw']==str(delta)
    assert record['publisher_send_time_status']=='UNKNOWN_CLAMPED_DELTA'


@pytest.mark.parametrize('schema', ['definition','statistics'])
def test_temporal_lookup_propagates_resource_guard_to_each_record(tmp_path,schema):
    rows=([definition(),definition('2024-10-01T01:00:00Z')] if schema=='definition' else
          [statistic('2024-10-01T20:00:00Z'),statistic('2024-10-01T21:00:00Z',sequence=2)])
    raw=source(tmp_path,schema,rows)
    class EngineeringStopGuard:
        def __init__(self):self.checks=[]
        def check(self,recovery):
            self.checks.append(recovery)
            if len(self.checks)==2:raise RuntimeError('ENGINEERING_GUARD_INTERRUPTED_SECOND_RECORD')
    guard=EngineeringStopGuard()
    with pytest.raises(RuntimeError,match='ENGINEERING_GUARD_INTERRUPTED_SECOND_RECORD'):
        if schema=='definition':
            definition_at(raw,contract_id='ESZ4',instrument_id=183748,publisher_id=1,
                          as_of='2024-10-01T22:00:00Z',guard=guard)
        else:
            reference=evidence(tmp_path,'CME_TRADING_REFERENCE_DATE',scope={'dataset':'GLBX.MDP3'})
            settlement_at(raw,instrument_id=183748,publisher_id=1,session='2024-10-01',
                          as_of='2024-10-01T22:00:00Z',reference_evidence=reference,guard=guard)
    assert [item['stage'] for item in guard.checks]==['vendor_stream','vendor_stream']
    assert [item['line'] for item in guard.checks]==[2,3]


def test_manifest_candidate_cannot_enter_verified_reader(tmp_path):
    p = tmp_path / 'bars.ndjson'
    p.write_text('[]\n')
    entry = manifest_candidate_entry(p, manifest_directory=tmp_path, source='ENGINEERING_FIXTURE')
    assert entry['verified'] is False and entry['sha256'] == digest(p)


def test_extra_csv_columns_are_quarantined_without_crashing_hash(tmp_path):
    raw = source(tmp_path, 'ohlcv-1h', [bars()])
    original = raw.path.read_text()
    raw.path.write_text(original.rstrip('\n') + ',unexpected\n')
    receipt = json.loads(raw.receipt_path.read_text())
    receipt.update(sha256=digest(raw.path), bytes=raw.path.stat().st_size)
    raw.receipt_path.write_text(json.dumps(receipt))
    row = next(raw.records())
    assert row['parse_status'] == 'QUARANTINED_RAW_RECORD'
    assert row['issues'] == ['CSV_COLUMN_COUNT_MISMATCH']


def test_minute_partial_session_is_supported_and_remains_unqualified(tmp_path):
    # Deliberately exercises candidate construction in a temporary engineering
    # fixture. It does not produce or claim a real historical research run.
    raw = source(tmp_path, 'ohlcv-1m', [bars('2024-10-01T14:30:00Z') | {'rtype': 33}], mock=False)
    defs = source(tmp_path, 'definition', [definition()], mock=False)
    defn = definition_at(defs, contract_id='ESZ4', instrument_id=183748, publisher_id=1,
                         as_of='2024-10-01T14:30:00Z')
    start, end = datetime(2024,10,1,14,30,tzinfo=timezone.utc), datetime(2024,10,1,14,31,tzinfo=timezone.utc)
    scope = {'contract_id': 'ESZ4', 'session': '2024-10-01'}
    cal = evidence(tmp_path, 'DATED_EXCHANGE_CALENDAR', scope=scope,
        segments=[[start.isoformat(), end.isoformat()]], next_session='2024-10-02')
    window = SessionWindow(date(2024,10,1), 'America/Chicago', ((start,end),), cal.source, cal.sha256, True)
    available = '2024-10-01T14:31:01+00:00'
    pub = evidence(tmp_path, 'OHLCV_PUBLICATION', scope=dict(scope, source_sha256=raw.validate()['sha256']), available_at=available)
    quote = evidence(tmp_path, 'CONTRACT_PRICE_CONVENTION', scope={'contract_id': 'ESZ4',
        'definition_record_sha256': defn['raw_record_sha256']}, vendor_decimal_to_quote_units='1',
        quote_unit='SP500_index_points', usd_multiplier_per_quote_unit='50',
        vendor_exchange='XCME', exchange='CME', vendor_currency='USD', currency='USD')
    row = dict(root='ES', quote_unit='SP500_index_points', usd_multiplier_per_quote_unit='50',
               tick_in_quote_units='0.25', contract_size='50', exchange='CME')
    args = dict(definition=defn, contract_id='ESZ4', window=window, publication_at=available,
        publication_evidence=pub, calendar_evidence=cal, quote_evidence=quote,
        registry_row=row, next_session=date(2024,10,2))
    candidate = session_candidate(raw, **args)
    assert candidate['record']['close'] == 5750.75
    assert candidate['record']['status'] == 'UNQUALIFIED_VENDOR_SESSION_CANDIDATE'
    assert candidate['record']['tradable_open'] is False
    assert candidate['qualification']['first_notice'] == 'UNKNOWN'
    with pytest.raises(ValueError, match='TICK_UNIT'):
        session_candidate(raw, **(args | {'registry_row': row | {'tick_in_quote_units': '25'}}))
