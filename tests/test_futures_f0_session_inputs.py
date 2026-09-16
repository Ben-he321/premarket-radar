"""Artificial real-shaped fixtures stay in pytest temporary directories."""
from datetime import date
import csv
import json

import pytest

from src.futures_f0.data import Request
from src.futures_f0.runtime import canonical_hash, digest
from src.futures_f0.session_inputs import DatedSession, integrate_session, settlement_versions_at
from src.futures_f0.vendor import EvidenceFile, RawSource, timestamp_ns
from src.futures_f0.provenance import derivation_hash, verify_session_derivation


def source(tmp_path, schema, rows, name=None):
    path = tmp_path / ((name or schema) + '.csv')
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    request = Request(('ESZ4',), schema, '2024-10-01T00:00:00Z', '2024-10-02T00:00:00Z').parameters()
    receipt = dict(request=request, request_sha256=canonical_hash(request),
        source='https://hist.databento.com/v0/', sha256=digest(path), bytes=path.stat().st_size,
        received_at='2026-09-15T10:00:00Z', completed_at='2026-09-15T10:01:00Z')
    receipt_path = path.with_suffix('.receipt.json'); receipt_path.write_text(json.dumps(receipt))
    return RawSource.from_receipt(path, receipt_path, price_encoding='fixed_1e9')


def evidence(tmp_path, kind, scope, **values):
    path = tmp_path / (kind + '.json')
    path.write_text(json.dumps(dict(kind=kind, mock=False, scope=scope, **values)))
    return EvidenceFile(path, digest(path), 'ARTIFICIAL_ENGINEERING_FIXTURE_IN_TMP_PATH')


def common(at, rtype):
    return dict(ts_event=timestamp_ns(at), rtype=rtype, publisher_id=1, instrument_id=183748)


def bar(at, rtype=34, volume=60):
    return dict(common(at, rtype), open=100_000_000_000, high=101_000_000_000,
                low=99_000_000_000, close=100_000_000_000, volume=volume)


def statistic(at, *, flags=3, action=1, price=100_000_000_000, seq=1):
    return dict(common(at, 24), ts_recv=timestamp_ns(at) + 123, ts_ref=timestamp_ns('2024-10-01T00:00:00Z'),
        price=price, quantity=2**63 - 1, sequence=seq, channel_id=1, stat_type=3,
        update_action=action, stat_flags=flags)


def fixture(tmp_path, *, segments=None, minutes=False):
    (tmp_path / 'MOCK_FIXTURE_DECLARATION.json').write_text(json.dumps({'mock': True,
        'research_evidence': False, 'purpose': 'Artificial real-shaped integration behavior tests only'}))
    definition = dict(common('2024-10-01T00:00:00Z', 19), ts_recv=timestamp_ns('2024-10-01T00:00:00Z'),
        raw_symbol='ESZ4', instrument_class='F', security_type='FUT', security_update_action='A',
        currency='USD', exchange='XCME', unit_of_measure='IPNT', unit_of_measure_qty=50_000_000_000,
        min_price_increment=250_000_000, activation=timestamp_ns('2023-01-01T00:00:00Z'),
        expiration=timestamp_ns('2024-12-20T14:30:00Z'), leg_count=0)
    sources = [source(tmp_path, 'definition', [definition]),
        source(tmp_path, 'ohlcv-1h', [bar('2024-10-01T14:00:00Z'), bar('2024-10-01T15:00:00Z')]),
        source(tmp_path, 'statistics', [statistic('2024-10-01T16:30:00Z', flags=2),
            statistic('2024-10-01T17:30:00Z', price=101_000_000_000, seq=2)]),
        source(tmp_path, 'status', [dict(common('2024-10-01T13:59:00Z', 18),
            ts_recv=timestamp_ns('2024-10-01T13:59:00Z'), action=2, reason=0, trading_event=0,
            is_trading='Y', is_quoting='Y', is_short_sell_restricted='~')])]
    if minutes:
        rows = [bar(f'2024-10-01T14:{i:02d}:00Z', 33, 1) for i in range(30, 60)]
        sources.append(source(tmp_path, 'ohlcv-1m', rows))
    calendar = evidence(tmp_path, 'DATED_SESSION_INPUT_CANDIDATE', {'contract_id': 'ESZ4', 'session': '2024-10-01'},
        timezone='America/Chicago', segments=segments or [['2024-10-01T14:00:00Z', '2024-10-01T16:00:00Z']],
        next_session='2024-10-02', next_open='2024-10-01T22:00:00Z', source_urls=['https://example.invalid/calendar'])
    boundaries = evidence(tmp_path, 'SPECIFIC_CONTRACT_BOUNDARIES_CANDIDATE', {'contract_id': 'ESZ4'},
        first_notice={'status': 'NOT_APPLICABLE_CASH_SETTLED', 'source_urls': ['https://example.invalid/rule']},
        last_trade={'at': '2024-12-20T14:30:00Z', 'source_urls': ['https://example.invalid/expiry']},
        preceding_exchange_sessions=['2024-12-13', '2024-12-16', '2024-12-17', '2024-12-18', '2024-12-19'],
        session_list_source_urls=['https://example.invalid/dated-sessions'])
    registry = dict(root='ES', market='SP500', exchange='CME', currency='USD', contract_unit='index_point',
        contract_size='50', quote_unit='SP500_index_points', usd_multiplier_per_quote_unit='50',
        tick_in_quote_units='0.25', tick_value_usd='12.5', root_first_trade_date='1997-09-09',
        delivery_type='CASH_SOQ', spec_source_url='https://example.invalid/spec', launch_source_url='https://example.invalid/launch')
    reference = evidence(tmp_path, 'CME_TRADING_REFERENCE_DATE', {'dataset': 'GLBX.MDP3'})
    args = dict(window=DatedSession('ESZ4', date(2024, 10, 1), calendar), decision_at='2024-10-01T17:00:00Z',
        registry_row=registry, boundary_evidence=boundaries, reference_evidence=reference)
    return sources, args


def policy(tmp_path, **updates):
    review = tmp_path/'TEST_ONLY_SOURCE_REVIEW.md'
    review.write_text('Artificial test of document binding; not real vendor certification.')
    values = dict(verdict='DOCUMENTED_CAPTURE_PREFIX_MODEL_SUPPORTED',
        availability_basis='INTERNAL_CAPTURE_PREFIX', input_clock='ts_recv',
        bucket_timestamp_role='INTERVAL_START_NOT_PUBLICATION',
        interval_semantics='START_INCLUSIVE_END_EXCLUSIVE',
        internal_clock_phase='AFTER_INPUT_CUTOFF',
        revision_policy='DROP_TRADE_BUST_AND_CORRECTION_MESSAGES',
        source_urls=['https://databento.com/docs/schemas-and-data-formats/ohlcv'],
        internal_clock_is_observed=False, supplier_published_at=None,
        historical_customer_received_at=None)
    values['companion_documents'] = [review.name]
    values.update(updates)
    return evidence(tmp_path, 'GLBX_CAPTURE_PREFIX_POLICY',
        {'dataset':'GLBX.MDP3','schemas':['ohlcv-1h','ohlcv-1m'],
         'start':'2021-01-01T00:00:00Z','end_exclusive':'2026-01-01T00:00:00Z'}, **values)


def derived_fixture(tmp_path):
    sources, args = fixture(tmp_path)
    args['causality_evidence'] = policy(tmp_path)
    result = integrate_session(sources, **args)
    kwargs = dict(source_root=tmp_path, raw_hashes=[s.validate()['sha256'] for s in sources],
                  registry_row=args['registry_row'])
    return result, kwargs, sources, args


def rehash(value):
    value['derivation_sha256'] = derivation_hash(value)
    value['engine_record_candidate']['source_hash'] = value['derivation_sha256']


def test_real_shaped_derivation_rebuilds_all_sources_and_preserves_unknowns(tmp_path):
    result, kwargs, sources, args = derived_fixture(tmp_path)
    assert verify_session_derivation(result, **kwargs) == result
    clock = result['temporal']
    assert clock['available_at'] == clock['input_cutoff'] == clock['internal_calculated_at']
    assert clock['internal_clock_phase'] == 'AFTER_INPUT_CUTOFF'
    assert clock['supplier_published_at'] is clock['received_at'] is None
    assert result['derivation_sha256'] not in kwargs['raw_hashes']
    assert not result['research_qualified']


@pytest.mark.parametrize('mutation', ['price','line','record_hash','remove_reference','identity','encoding','schema','view'])
def test_rehashed_claim_cannot_replace_raw_recomputation(tmp_path, mutation):
    result, kwargs, sources, args = derived_fixture(tmp_path)
    if mutation == 'price': result['prices']['session_ohlcv']['close'] = '101'
    elif mutation == 'line': result['prices']['records'][0]['source_line'] += 1
    elif mutation == 'record_hash': result['prices']['records'][0]['raw_record_sha256'] = 'a'*64
    elif mutation == 'remove_reference': result['prices']['records'].pop()
    elif mutation == 'identity': result['instrument_id'] += 1
    elif mutation == 'encoding': result['sources'][1]['price_encoding'] = 'decimal'
    elif mutation == 'schema': result['sources'][1]['schema'] = 'ohlcv-1m'
    elif mutation == 'view': result['engine_record_candidate']['close'] = '101'
    rehash(result)
    with pytest.raises(ValueError, match='RECOMPUTATION_MISMATCH|PRICE_ENCODING_DISAGREES'):
        verify_session_derivation(result, **kwargs)


@pytest.mark.parametrize('mutation', ['receipt','raw','policy','outside_root','allowlist'])
def test_external_evidence_or_file_tamper_is_rejected(tmp_path, mutation):
    result, kwargs, sources, args = derived_fixture(tmp_path)
    if mutation == 'receipt': sources[1].receipt_path.write_text(sources[1].receipt_path.read_text()+' ')
    elif mutation == 'raw': sources[1].path.write_text(sources[1].path.read_text()+'\n')
    elif mutation == 'policy': args['causality_evidence'].path.write_text('{}')
    elif mutation == 'outside_root': kwargs['source_root'] = tmp_path/'unrelated'
    elif mutation == 'allowlist': kwargs['raw_hashes'] = [result['derivation_sha256']]
    with pytest.raises(ValueError): verify_session_derivation(result, **kwargs)


@pytest.mark.parametrize('mutation', ['schema','date','observed','publication','revision','phase'])
def test_capture_policy_scope_and_clock_contract_fail_closed(tmp_path, mutation):
    from src.futures_f0.session_inputs import capture_prefix_clock
    ev = policy(tmp_path)
    obj = json.loads(ev.path.read_text())
    if mutation == 'schema': obj['scope']['schemas'] = ['ohlcv-1m']
    elif mutation == 'date': obj['scope']['end_exclusive'] = '2024-01-01T00:00:00Z'
    elif mutation == 'observed': obj['internal_clock_is_observed'] = True
    elif mutation == 'publication': obj['supplier_published_at'] = '2024-10-01T16:00:00Z'
    elif mutation == 'revision': obj['revision_policy'] = 'UNKNOWN'
    elif mutation == 'phase': obj['internal_clock_phase'] = 'PLUS_ONE_MICROSECOND'
    ev.path.write_text(json.dumps(obj)); ev = EvidenceFile(ev.path, digest(ev.path), ev.source)
    with pytest.raises(ValueError, match='CAPTURE_PREFIX_POLICY'):
        capture_prefix_clock(ev, start=timestamp_ns('2024-10-01T14:00:00Z'),
            end=timestamp_ns('2024-10-01T16:00:00Z'), used_schemas={'ohlcv-1h'})


def importer_fixture(tmp_path):
    from src.futures_f0.config import ROOT
    from src.futures_f0.input import QualifiedInputs
    result, kwargs, sources, args = derived_fixture(tmp_path)
    with (ROOT/'docs/futures_f0/contract_registry.csv').open(encoding='utf-8-sig',newline='') as f:
        args['registry_row'] = next(row for row in csv.DictReader(f) if row['root']=='ES')
    result = integrate_session(sources, **args)
    session_path = tmp_path/'derived.json'; session_path.write_text(json.dumps(result))
    key = 'ESZ4|2024-10-01'
    entries = {'definitions':[dict(contract_id='ESZ4',market='SP500',root='ES',multiplier=50,
        tick_size=0.25,listed='2023-01-01',last_trade='2024-12-20',safe_exit_session='2024-12-13',
        exchange='CME',quote_unit='SP500_index_points',verified=True,vendor_definition_verified=True,
        calendar_verified=True,boundary_verified=True,source='ARTIFICIAL_TEST_ONLY')],
        'calendar':{key:dict(timezone='America/Chicago',segments=result['calendar']['segments'],
            source='ARTIFICIAL_TEST_ONLY',evidence_sha256=args['window'].evidence.sha256,
            verified=True,next_session='2024-10-02')},'settlements':[],'status':[],'mapping':[],'bars':[]}
    manifest = dict(kind='EXCHANGE_FUTURES_ACTUAL_CONTRACTS',mock=False,
        integration_review='VERIFIED_RAW_TO_NORMALIZED',source_object_hashes=kwargs['raw_hashes'],
        derivation_source_root=str(tmp_path),
        temporal_evidence=dict(path=args['causality_evidence'].path.name,
            sha256=args['causality_evidence'].sha256,source=args['causality_evidence'].source),
        session_derivations={result['derivation_sha256']:dict(path=session_path.name,sha256=digest(session_path))})
    for name, value in entries.items():
        p=tmp_path/(name+'.json');p.write_text(json.dumps(value))
        manifest[name]=dict(path=p.name,sha256=digest(p),verified=True,source='ARTIFICIAL_TEST_ONLY')
    p=tmp_path/'manifest.json';p.write_text(json.dumps(manifest))
    return QualifiedInputs(p,ROOT/'docs/futures_f0/contract_registry.csv'),result


def test_importer_and_integrator_accept_same_policy_without_qualifying_candidate(tmp_path):
    inputs, result = importer_fixture(tmp_path)
    bar = inputs._bar(result['engine_record_candidate'])
    assert bar.close == 100.0 and bar.source_hash == result['derivation_sha256']
    assert not bar.session_verified and not bar.tradable_open
    assert bar.status == 'UNQUALIFIED_DATED_SESSION_INPUT'
    assert bar.supplier_published_at is bar.received_at is None


@pytest.mark.parametrize('mutation', ['missing_derivation','forged_price','microsecond','fake_receipt','raw_as_derived'])
def test_importer_rejects_unbound_candidate_even_with_policy(tmp_path, mutation):
    inputs, result = importer_fixture(tmp_path)
    item = dict(result['engine_record_candidate'])
    if mutation == 'missing_derivation': inputs.manifest.pop('session_derivations')
    elif mutation == 'forged_price': item['close'] = '101'
    elif mutation == 'microsecond': item['available_at'] = '2024-10-01T16:00:00.000001Z'
    elif mutation == 'fake_receipt': item['received_at'] = item['available_at']
    elif mutation == 'raw_as_derived': item['source_hash'] = inputs.manifest['source_object_hashes'][0]
    with pytest.raises(ValueError): inputs._bar(item)


@pytest.mark.parametrize('mutation', ['intraday_break','calendar_hash'])
def test_importer_cannot_hide_changed_session_segments_behind_same_endpoints(tmp_path, mutation):
    inputs, result = importer_fixture(tmp_path)
    cal=inputs.calendars['ESZ4|2024-10-01']
    if mutation=='intraday_break':
        cal['segments']=[['2024-10-01T14:00:00Z','2024-10-01T14:30:00Z'],
                         ['2024-10-01T15:30:00Z','2024-10-01T16:00:00Z']]
    else: cal['evidence_sha256']='a'*64
    with pytest.raises(ValueError,match='CALENDAR_SEGMENTS_OR_EVIDENCE_MISMATCH'):
        inputs._bar(result['engine_record_candidate'])


def test_derived_manifest_cannot_downgrade_bar_to_raw_supplier_whitelist(tmp_path):
    inputs, result = importer_fixture(tmp_path)
    item=dict(result['engine_record_candidate'])
    item.update(availability_basis='SUPPLIER_PUBLICATION',input_cutoff=None,
        internal_calculated_at=None,temporal_evidence_hash=None,
        supplier_published_at=item['available_at'],close='100.5',
        source_hash=inputs.manifest['source_object_hashes'][0])
    with pytest.raises(ValueError,match='NO_RAW_FALLBACK'): inputs._bar(item)


def test_unattested_decimal_cannot_generate_a_new_consistent_scaled_derivation(tmp_path):
    result, kwargs, sources, args=derived_fixture(tmp_path)
    actual=sources[1]
    sources[1]=RawSource(actual.path,actual.receipt_path,'decimal')
    with pytest.raises(ValueError,match='PRICE_ENCODING_DISAGREES'):
        integrate_session(sources,**args)


def test_source_review_change_invalidates_old_derivation(tmp_path):
    result,kwargs,sources,args=derived_fixture(tmp_path)
    (tmp_path/'TEST_ONLY_SOURCE_REVIEW.md').write_text('Changed reviewed assumptions')
    with pytest.raises(ValueError,match='RECOMPUTATION_MISMATCH'):
        verify_session_derivation(result,**kwargs)


def test_same_packet_settlement_variants_preserved_without_arbitrary_selection(tmp_path):
    sources,args=fixture(tmp_path)
    rows=[statistic('2024-10-01T18:00:00Z',flags=7,price=100_000_000_000),
          statistic('2024-10-01T18:00:00Z',flags=3,price=100_005_000_000),
          statistic('2024-10-01T19:00:00Z',flags=3,price=101_000_000_000,seq=2)]
    stats=source(tmp_path,'statistics',rows,'tick_variants')
    kwargs=dict(contract_id='ESZ4',instrument_id=183748,publisher_id=1,session='2024-10-01',
        reference_evidence=args['reference_evidence'])
    early=settlement_versions_at([stats],decision_at='2024-10-01T18:30:00Z',**kwargs)
    assert early['settlement'] is early['latest_visible_message'] is None
    assert len(early['versions_visible_at_cutoff'])==2 and early['ambiguous_latest_batch']
    later=settlement_versions_at([stats],decision_at='2024-10-01T19:30:00Z',**kwargs)
    assert later['settlement']=='101' and not later['ambiguous_latest_batch']


@pytest.mark.parametrize('different_key', ['sequence','channel_id'])
def test_same_capture_different_key_cannot_clear_prior_settlement_ambiguity(tmp_path,different_key):
    sources,args=fixture(tmp_path)
    rows=[statistic('2024-10-01T18:00:00Z',flags=7),
          statistic('2024-10-01T18:00:00Z',flags=3),
          statistic('2024-10-01T18:00:00Z',flags=3,price=101_000_000_000)]
    rows[2][different_key]=2
    stats=source(tmp_path,'statistics',rows,'same_capture_multiple_keys')
    result=settlement_versions_at([stats],contract_id='ESZ4',instrument_id=183748,publisher_id=1,
        session='2024-10-01',decision_at='2024-10-01T18:30:00Z',reference_evidence=args['reference_evidence'])
    assert result['settlement'] is None and result['ambiguous_latest_batch']
    assert len(result['versions_visible_at_cutoff'])==3


def test_integration_binds_sources_but_never_qualifies_or_backdates_final(tmp_path):
    sources, args = fixture(tmp_path)
    result = integrate_session(sources, **args)
    assert result['prices']['session_ohlcv']['volume'] == 120
    assert result['definition']['units']['unit_comparison'] == 'MATCH'
    assert result['settlement']['settlement'] is None
    assert result['settlement']['latest_visible_message']['price'] == '100'
    assert len(result['settlement']['versions_visible_at_cutoff']) == 1
    assert result['prices']['available_at'] is None
    assert result['execution_status']['opening_capture_state']['is_trading'] == 'Y'
    assert result['execution_status']['tradable_open'] is False
    assert result['boundaries']['five_session_safe_exit_candidate'] == '2024-12-13'
    assert result['boundaries']['safe_exit_session'] is None
    assert result['permitted_assumption_layers']['assumption_layers_blocked_by_missing_broker'] is False
    assert result['research_qualified'] is result['real_futures_backtest_run'] is False


def test_cross_boundary_hour_is_not_included_and_exact_minutes_can_fill_edge(tmp_path):
    segments = [['2024-10-01T14:30:00Z', '2024-10-01T16:00:00Z']]
    sources, args = fixture(tmp_path, segments=segments, minutes=True)
    partial = integrate_session(sources[:-1], **args)['prices']
    assert partial['session_ohlcv'] is None
    assert partial['observed_partial_ohlcv']['volume'] == 60
    assert len(partial['missing_boundary_minutes']) == 30
    complete = integrate_session(sources, **args)['prices']
    assert complete['session_ohlcv']['volume'] == 90  # no double counting hour 14
    assert complete['selected_bucket_count'] == 31
    assert len(complete['excluded_cross_boundary_hours']) == 1


def test_break_segments_omit_closed_minutes_and_preserve_missing_trade_unknown(tmp_path):
    sources, args = fixture(tmp_path, segments=[['2024-10-01T14:00:00Z', '2024-10-01T14:45:00Z'],
        ['2024-10-01T15:30:00Z', '2024-10-01T16:00:00Z']])
    prices = integrate_session(sources, **args)['prices']
    assert prices['session_ohlcv'] is None and prices['observed_partial_ohlcv'] is None
    assert prices['expected_hour_buckets'] == 0 and prices['expected_boundary_minutes'] == 75
    assert '2024-10-01T15:00:00.000000000Z' not in prices['missing_boundary_minutes']


def test_settlement_capture_nanosecond_cutoff_replacement_and_delete(tmp_path):
    sources, args = fixture(tmp_path)
    rows = [statistic('2024-10-01T18:00:00Z', seq=1),
            statistic('2024-10-01T19:00:00Z', flags=2, seq=2),
            statistic('2024-10-01T20:00:00Z', action=2, seq=3)]
    stats = source(tmp_path, 'statistics', rows, 'revisions')
    kwargs = dict(contract_id='ESZ4', instrument_id=183748, publisher_id=1,
                  session='2024-10-01', reference_evidence=args['reference_evidence'])
    early = settlement_versions_at([stats], decision_at='2024-10-01T18:00:00.000000122Z', **kwargs)
    assert early['latest_visible_message'] is None
    final = settlement_versions_at([stats], decision_at='2024-10-01T18:00:00.000000123Z', **kwargs)
    assert final['settlement'] == '100'
    preliminary = settlement_versions_at([stats], decision_at='2024-10-01T19:30:00Z', **kwargs)
    assert preliminary['settlement'] is None
    deleted = settlement_versions_at([stats], decision_at='2024-10-01T20:30:00Z', **kwargs)
    assert deleted['status'] == 'LATEST_VISIBLE_VERSION_DELETED'
    assert deleted['settlement_pricing_reference_at'] is None


def test_future_definition_not_backdated_and_resource_guard_reaches_all_streams(tmp_path):
    sources, args = fixture(tmp_path)
    path = sources[0].path
    text = path.read_text().replace(str(timestamp_ns('2024-10-01T00:00:00Z')), str(timestamp_ns('2024-10-01T14:01:00Z')))
    path.write_text(text)
    receipt = json.loads(sources[0].receipt_path.read_text()); receipt.update(sha256=digest(path), bytes=path.stat().st_size)
    sources[0].receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match='NO_DEFINITION_OBSERVED'):
        integrate_session(sources, **args)


@pytest.mark.parametrize('schema', ['definition', 'ohlcv-1h', 'statistics', 'status'])
def test_guard_propagates_to_every_data_schema(tmp_path, schema):
    sources, args = fixture(tmp_path)
    protected = next(source.validate()['sha256'] for source in sources if source.validate()['request']['schema'] == schema)
    class Stop:
        def check(self, recovery):
            if recovery.get('source_sha256') == protected:
                raise RuntimeError('ENGINEERING_RESOURCE_STOP')
    with pytest.raises(RuntimeError, match='ENGINEERING_RESOURCE_STOP'):
        integrate_session(sources, **args, guard=Stop())


def test_unknown_physical_notice_blocks_boundary_but_not_assumed_cost_layer(tmp_path):
    sources, args = fixture(tmp_path)
    path = args['boundary_evidence'].path
    value = json.loads(path.read_text()); value['first_notice'] = {'status': 'UNKNOWN'}
    path.write_text(json.dumps(value))
    args['boundary_evidence'] = EvidenceFile(path, digest(path), 'ARTIFICIAL_ENGINEERING_FIXTURE')
    result = integrate_session(sources, **args)
    assert result['boundaries']['earliest_boundary_candidate'] is None
    assert any(issue['code'] == 'APPLICABLE_FIRST_NOTICE_BOUNDARY_UNKNOWN' for issue in result['qualification_issues'])
    assert result['permitted_assumption_layers']['commission_usd_per_contract_side'] == '2'


def test_duplicate_source_and_tampered_calendar_fail_closed(tmp_path):
    sources, args = fixture(tmp_path)
    with pytest.raises(ValueError, match='DUPLICATE_SESSION_REQUEST'):
        integrate_session(sources + [sources[1]], **args)
    args['window'].evidence.path.write_text('{}')
    with pytest.raises(ValueError, match='HASH_MISMATCH'):
        integrate_session(sources, **args)
