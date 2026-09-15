"""Artificial engineering inputs only. No real research prices or API calls."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import json
from zoneinfo import ZoneInfo

import pytest

from src.futures_f0.config import Config, load_config
from src.futures_f0.data import (Request, MetadataClient, SessionWindow,
    aggregate_session, safe_exit_session, verify_input_manifest)
from src.futures_f0.runtime import Guard, ResourceBoundary, GIB, canonical_hash, digest, utcnow


def request(**overrides):
    return Request(**(dict(symbols=('MESH2',), schema='ohlcv-1h',
                          start='2022-01-01T00:00:00Z', end='2022-02-01T00:00:00Z') | overrides))


@pytest.mark.parametrize('symbols', [(), ('ALL_SYMBOLS',), ('ES.FUT',), ('SPY',),
    ('ESH2-ESM2',), ('AAPL220121C00100000',), ('MESH2', 'MESH2')])
def test_narrow_instrument_scope(symbols):
    with pytest.raises(ValueError):
        request(symbols=symbols).parameters()


@pytest.mark.parametrize('kwargs', [dict(schema='trades'), dict(schema='ohlcv-1d'),
    dict(start='2020-12-01T00:00:00Z'), dict(end='2026-02-01T00:00:00Z'),
    dict(end='2022-02-02T00:00:00Z'), dict(start='2022-01-01')])
def test_frozen_date_and_resolution_scope(kwargs):
    with pytest.raises(ValueError):
        request(**kwargs).parameters()


class Response:
    status_code = 200
    headers = {}
    def __init__(self, value=None, payload=b'contract,open\nMESH2,100\n'):
        self.value, self.payload, self.closed = value, payload, False
    def json(self): return self.value
    def close(self): self.closed = True
    def iter_content(self, chunk_size): yield self.payload


class Session:
    def __init__(self): self.calls = []; self.values = iter([3, 300, 0, 3, 300, 0])
    def post(self, url, **kwargs):
        self.calls.append(url)
        return Response(next(self.values)) if 'metadata.' in url else Response()


def test_missing_key_makes_no_network_request(tmp_path):
    http = Session()
    with pytest.raises(ValueError, match='KEY_MISSING'):
        MetadataClient(Config(tmp_path), session=http).estimate(request())
    assert not http.calls


def test_free_estimate_not_credit_authority_and_cached_download(tmp_path):
    http = Session()
    client = MetadataClient(Config(tmp_path, 'MOCK_ONLY_NOT_A_KEY'), session=http)
    plan = request(); estimate = client.estimate(plan)
    assert len(http.calls) == 3
    with pytest.raises(ValueError, match='POSITIVE_QUOTE'):
        client.download_zero_quote(plan, estimate | {'get_cost': 0.01}, exact_contracts_verified=True)
    assert len(http.calls) == 3
    with pytest.raises(ValueError, match='IDENTITIES'):
        client.download_zero_quote(plan, estimate)
    receipt = client.download_zero_quote(plan, estimate, exact_contracts_verified=True)
    assert receipt['research_qualified'] is False
    assert len(http.calls) == 7
    assert client.download_zero_quote(plan, estimate, exact_contracts_verified=True) == receipt
    assert len(http.calls) == 7
    cache = next((tmp_path / 'vendor_cache').glob('*.csv'))
    cache.write_bytes(b'CHANGED')
    with pytest.raises(ValueError, match='HASH_MISMATCH'):
        client.download_zero_quote(plan, estimate, exact_contracts_verified=True)
    assert len(http.calls) == 7


def test_stale_quote_and_partial_are_not_rebilled(tmp_path):
    client = MetadataClient(Config(tmp_path, 'MOCK'), session=Session())
    plan = request(); estimate = client.estimate(plan)
    stale = estimate | {'estimated_at': (utcnow() - timedelta(hours=2)).isoformat()}
    with pytest.raises(ValueError, match='FRESH_COST'):
        client.download_zero_quote(plan, stale, exact_contracts_verified=True)
    target = tmp_path / 'vendor_cache' / (canonical_hash(plan.parameters()) + '.partial')
    target.parent.mkdir(); target.write_bytes(b'PARTIAL')
    with pytest.raises(ValueError, match='NO_AUTO_REBILL'):
        client.download_zero_quote(plan, estimate, exact_contracts_verified=True)
    assert len(client.session.calls) == 3


def window(first, end):
    return SessionWindow(end.astimezone(ZoneInfo('America/Chicago')).date(), 'America/Chicago', ((first, end),),
                         'MOCK_TEST_CALENDAR', '1' * 64, True)


def rows(first, count, seconds=3600):
    for i in range(count):
        yield dict(timestamp=first + timedelta(seconds=i * seconds), contract_id='MESH2',
                   open=100, high=102, low=99, close=101, volume=20, status='QUALIFIED')


def aggregate(data, w, width=3600):
    return aggregate_session(data, w, width, contract_id='MESH2',
                             publication_at=w.segments[-1][1] + timedelta(seconds=1), source_hash='1' * 64)


def test_exchange_session_dst_is_timezone_based():
    chi = ZoneInfo('America/Chicago')
    first = datetime(2022, 3, 13, 17, tzinfo=chi)
    end = datetime(2022, 3, 14, 16, tzinfo=chi)
    result = aggregate(rows(first, 23), window(first, end))
    assert result['status'] == 'QUALIFIED'
    assert result['opens_at'] == '2022-03-13T22:00:00+00:00'
    assert result['closes_at'] == '2022-03-14T21:00:00+00:00'
    assert result['volume'] == 460 and result['received_at'] is None


def test_missing_bar_or_volume_remains_unknown():
    first = datetime(2022, 3, 14, tzinfo=timezone.utc); w = window(first, first + timedelta(hours=2))
    assert aggregate(rows(first, 1), w)['status'] == 'MISSING_BUCKETS_UNKNOWN'
    values = list(rows(first, 2)); values[1]['volume'] = None
    result = aggregate(values, w)
    assert result['volume'] is None and result['status'] == 'MISSING_VOLUME_UNKNOWN'


def test_duplicates_or_ohlc_or_break_crossing_rejected():
    first = datetime(2022, 3, 14, tzinfo=timezone.utc); w = window(first, first + timedelta(hours=2))
    data = list(rows(first, 2))
    with pytest.raises(ValueError, match='DUPLICATE'):
        aggregate([data[0], data[0]], w)
    with pytest.raises(ValueError, match='INVALID_OHLC'):
        aggregate([data[0] | {'low': 105}], w)
    with pytest.raises(ValueError, match='FINER_BARS'):
        aggregate(data, window(first + timedelta(minutes=30), first + timedelta(hours=2)))


def test_incomplete_day_and_unknown_calendar_blocked():
    first = datetime(2022, 3, 14, tzinfo=timezone.utc); w = window(first, first + timedelta(hours=2))
    with pytest.raises(ValueError, match='LOOKAHEAD'):
        aggregate_session(rows(first, 2), w, 3600, contract_id='MESH2', publication_at=first, source_hash='1' * 64)
    with pytest.raises(ValueError, match='CALENDAR_REQUIRED'):
        aggregate(rows(first, 2), SessionWindow(w.session, w.timezone, w.segments, 'UNKNOWN', '1' * 64, False))


def test_mock_halted_lineage_and_calendar_relabel_cannot_become_real_qualified():
    first = datetime(2022, 3, 14, tzinfo=timezone.utc); w = window(first, first + timedelta(hours=2))
    data = list(rows(first, 2))
    with pytest.raises(ValueError, match='MOCK_INPUT'):
        aggregate([data[0] | {'is_mock': True}], w)
    bad = aggregate([data[0] | {'status': 'HALTED'}, data[1]], w)
    assert bad['status'] != 'QUALIFIED' and not bad['tradable_open']
    with pytest.raises(ValueError, match='SOURCE_SHA256'):
        aggregate_session(data, w, 3600, contract_id='MESH2', publication_at=w.segments[-1][1], source_hash='')
    with pytest.raises(ValueError, match='SESSION_DATE'):
        aggregate(data, SessionWindow(date(2022,3,20), w.timezone, w.segments, 'MOCK', '1'*64, True))


def test_boolean_fake_quote_does_not_authorize_download(tmp_path):
    http = Session(); client = MetadataClient(Config(tmp_path, 'MOCK'), session=http)
    fake = {'request_sha256':canonical_hash(request().parameters()), 'get_cost':False,
            'estimated_at':utcnow().isoformat()}
    with pytest.raises(ValueError, match='POSITIVE_QUOTE'):
        client.download_zero_quote(request(), fake, exact_contracts_verified=True)
    assert not http.calls


def test_late_statistics_window_never_permits_2026_bars():
    p = request(schema='statistics', start='2025-12-30T00:00:00Z', end='2026-01-03T00:00:00Z')
    assert p.parameters()['schema'] == 'statistics'
    with pytest.raises(ValueError, match='OUTSIDE_FROZEN'):
        request(start=p.start, end=p.end).parameters()


def test_delivery_roll_uses_supplied_trading_calendar_and_earliest_boundary():
    days = [date(2022, 1, d) for d in (3,4,5,6,7,10,11,12,13,14,18,19,20,21,24,25)]
    assert safe_exit_session(days, first_notice=date(2022,1,20), last_trade=date(2022,1,25),
                             broker_boundary_not_applicable=True) == date(2022,1,12)
    with pytest.raises(ValueError, match='UNKNOWN'):
        safe_exit_session(days, first_notice=None, last_trade=date(2022,1,25))


@pytest.mark.parametrize('reading,expired,reason', [
    ({'rss_bytes':2*GIB,'peak_rss_bytes':2*GIB,'system_available_bytes':4*GIB},False,'RSS_LIMIT'),
    ({'rss_bytes':GIB,'peak_rss_bytes':GIB,'system_available_bytes':GIB},False,'SYSTEM_AVAILABLE'),
    ({'rss_bytes':GIB//5,'peak_rss_bytes':2*GIB,'system_available_bytes':4*GIB},False,'RSS_LIMIT'),
    ({'rss_bytes':GIB,'peak_rss_bytes':GIB,'system_available_bytes':4*GIB},True,'AUTHORIZATION_EXPIRED')])
def test_resource_boundary_writes_recovery_without_retry(tmp_path, reading, expired, reason):
    deadline = utcnow() + timedelta(hours=-1 if expired else 1)
    guard = Guard(deadline.isoformat(), tmp_path, snapshot=lambda: reading)
    with pytest.raises(ResourceBoundary, match=reason): guard.check({'next': 'TEST_ONLY'})
    proof = json.loads((tmp_path / 'RESOURCE_STOP.json').read_text())
    assert not proof['stopped_other_services'] and not proof['auto_retry']


def test_manifest_refuses_mock_and_source_hash_tamper(tmp_path):
    path = tmp_path / 'manifest.json'; path.write_text(json.dumps({'mock':True}))
    with pytest.raises(ValueError, match='REAL_EXCHANGE'): verify_input_manifest(path)
    data = {'kind':'EXCHANGE_FUTURES_ACTUAL_CONTRACTS','mock':False}
    for name in ('bars','definitions','calendar','settlements','status','mapping'):
        p = tmp_path / (name + '.csv'); p.write_text('TEST_INPUT')
        data[name] = {'path':p.name,'sha256':digest(p),'source':'MOCK_TEST_PROVENANCE','verified':True}
    path.write_text(json.dumps(data)); assert verify_input_manifest(path) == data
    (tmp_path / 'bars.csv').write_text('CORRUPTION')
    with pytest.raises(ValueError, match='HASH_MISMATCH'): verify_input_manifest(path)


def test_config_reads_only_designated_key_and_rejects_stock_output(tmp_path, monkeypatch):
    secret = tmp_path/'secrets.toml'; secret.write_text('DATABENTO_API_KEY = "TEST_ONLY"\n')
    monkeypatch.delenv('DATABENTO_API_KEY', raising=False)
    cfg = load_config(tmp_path/'futures-f0-test', secret)
    assert cfg.api_key == 'TEST_ONLY' and 'TEST_ONLY' not in repr(cfg)
    with pytest.raises(ValueError, match='OWN_FUTURES'): load_config(tmp_path/'old-stock-data', secret)
