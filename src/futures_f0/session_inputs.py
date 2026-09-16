"""Date-scoped, bounded raw-cache integration, deliberately below the import gate.

These candidates bind session segments, concrete contract boundaries and the
settlement message prefix visible at a decision cutoff. Neither a price match
nor a file-backed human interpretation certifies historical causality. No
network, credentials, broker, calendar guessing or research manifest is used.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import heapq
from pathlib import Path
from zoneinfo import ZoneInfo

from .contract_snapshot import compare_definition
from .data import RAW_SYMBOL, timestamp
from .runtime import canonical_hash, digest
from .vendor import EvidenceFile, NANO, iso_ns, timestamp_ns

HOUR = 3600 * NANO
MINUTE = 60 * NANO


def _ref(row):
    return {k: row[k] for k in ('source_sha256', 'source_line', 'raw_record_sha256')}


def _evidence_ref(evidence):
    return dict(path=str(evidence.path), sha256=evidence.sha256, source=evidence.source)


@dataclass(frozen=True)
class DatedSession:
    contract_id: str
    session: date
    evidence: EvidenceFile

    def read(self):
        if not RAW_SYMBOL.fullmatch(self.contract_id) or not date(2021, 1, 1) <= self.session <= date(2025, 12, 31):
            raise ValueError('SPECIFIC_CONTRACT_AND_FROZEN_DATE_REQUIRED')
        value = self.evidence.read(kind='DATED_SESSION_INPUT_CANDIDATE',
            scope={'contract_id': self.contract_id, 'session': str(self.session)})
        if value.get('timezone') != 'America/Chicago' or not value.get('source_urls'):
            raise ValueError('DATED_CALENDAR_SOURCE_AND_EXCHANGE_TIMEZONE_REQUIRED')
        raw = value.get('segments')
        if not isinstance(raw, list) or not 1 <= len(raw) <= 4:
            raise ValueError('BOUNDED_EXPLICIT_SESSION_SEGMENTS_REQUIRED')
        segments = tuple((timestamp_ns(a), timestamp_ns(b)) for a, b in raw)
        previous = None
        for start, end in segments:
            if start >= end or start % MINUTE or end % MINUTE or previous is not None and start <= previous:
                raise ValueError('INVALID_OR_OVERLAPPING_SESSION_SEGMENTS')
            previous = end
        if segments[-1][1] - segments[0][0] > 24 * HOUR:
            raise ValueError('SESSION_ENVELOPE_EXCEEDS_ONE_DAY')
        if timestamp(iso_ns(segments[-1][1])).astimezone(ZoneInfo('America/Chicago')).date() != self.session:
            raise ValueError('EXCHANGE_TRADE_DATE_MISMATCH')
        next_session = value.get('next_session')
        next_open = value.get('next_open')
        if (next_session is None) != (next_open is None):
            raise ValueError('NEXT_SESSION_AND_OPEN_MUST_BE_SUPPLIED_TOGETHER')
        if next_session is not None:
            if date.fromisoformat(next_session) <= self.session or timestamp_ns(next_open) <= segments[-1][1]:
                raise ValueError('INVALID_NEXT_DATED_SESSION')
        return value, segments


def _sources(sources, contract_id):
    sources = tuple(sources)
    if not 1 <= len(sources) <= 64:
        raise ValueError('BOUNDED_SESSION_SOURCE_COUNT_REQUIRED')
    output, seen = [], set()
    for source in sources:
        receipt = source.validate()
        if receipt.get('mock') or receipt.get('is_mock'):
            raise ValueError('MOCK_SOURCE_NOT_REAL_SESSION_INPUT')
        if contract_id not in receipt['request']['symbols'].split(','):
            continue
        key = receipt['request_sha256']
        if key in seen:
            raise ValueError('DUPLICATE_SESSION_REQUEST_SOURCE')
        seen.add(key)
        output.append((source, receipt))
    if not output:
        raise ValueError('NO_EXPLICIT_CONTRACT_SOURCES')
    return output


def _rows(sources, schema, *, identity=None, guard=None):
    def stream(source):
        for row in source.records(guard=guard):
            if row['parse_status'] != 'PARSED_VENDOR_RECORD':
                raise ValueError('QUARANTINED_SOURCE_REQUIRES_REVIEW')
            if identity is None or (row['publisher_id'], row['instrument_id']) == identity:
                yield row
    streams = [stream(source) for source, receipt in sources if receipt['request']['schema'] == schema]
    index = 'ts_event_ns' if schema.startswith('ohlcv-') else 'ts_recv_ns'
    yield from heapq.merge(*streams, key=lambda row: (row[index], row['source_sha256'], row['source_line']))


def _definition(sources, contract_id, start, end, cutoff, guard):
    chosen, versions, identity, issues = None, [], None, []
    for row in _rows(sources, 'definition', guard=guard):
        if row['raw_symbol'] != contract_id or row['ts_recv_ns'] > cutoff:
            continue
        current = row['publisher_id'], row['instrument_id']
        if identity is not None and identity != current:
            raise ValueError('DATED_CONTRACT_IDENTITY_CHANGED_REQUIRES_REVIEW')
        identity = current
        if len(versions) >= 256:
            raise ValueError('DEFINITION_VERSION_COUNT_BOUNDARY')
        versions.append(dict(**_ref(row), observed_at=row['ts_recv'], action=row['security_update_action'],
                             activation=row['activation'], expiration=row['expiration']))
        if row['ts_recv_ns'] <= start:
            chosen = row
        elif row['ts_recv_ns'] < end:
            issues.append('INTRASESSION_DEFINITION_VERSION_REVIEW_REQUIRED')
    if chosen is None:
        raise ValueError('NO_DEFINITION_OBSERVED_BY_SESSION_OPEN')
    if chosen['security_update_action'] not in ('A', 'M') or chosen['identity_status'] != 'OUTRIGHT_FUTURE_CANDIDATE':
        raise ValueError('NO_ACTIVE_OUTRIGHT_DEFINITION_AT_SESSION_OPEN')
    if chosen['activation_ns'] is None or chosen['expiration_ns'] is None:
        issues.append('VENDOR_LIFETIME_UNKNOWN')
    elif chosen['activation_ns'] > start or chosen['expiration_ns'] < end:
        issues.append('SESSION_OUTSIDE_OBSERVED_VENDOR_LIFETIME')
    return chosen, versions, sorted(set(issues))


def _aggregate(sources, identity, segments, guard):
    """Use whole hours inside segments; only minute data may cover cut hours."""
    expected_hours, expected_minutes, partial = set(), set(), []
    for start, end in segments:
        first_hour = ((start + HOUR - 1) // HOUR) * HOUR
        last_hour = (end // HOUR) * HOUR
        expected_hours.update(range(first_hour, last_hour, HOUR))
        edges = [(start, min(first_hour, end)), (max(first_hour, last_hour), end)]
        for a, b in edges:
            if a < b:
                expected_minutes.update(range(a, b, MINUTE))
                partial.append([iso_ns(a), iso_ns(b)])
    selected, crossed = {}, []
    for schema, expected, width in (('ohlcv-1h', expected_hours, HOUR), ('ohlcv-1m', expected_minutes, MINUTE)):
        for row in _rows(sources, schema, identity=identity, guard=guard):
            at = row['ts_event_ns']
            if at not in expected:
                if schema == 'ohlcv-1h' and any(at < b and at + width > a for a, b in segments):
                    if len(crossed) >= 16:
                        raise ValueError('CROSS_BOUNDARY_BUCKET_COUNT_LIMIT')
                    crossed.append(dict(**_ref(row), bucket_start=row['ts_event'],
                                        reason='WHOLE_HOUR_CROSSES_DATED_SEGMENT_BOUNDARY'))
                continue
            if at in selected:
                raise ValueError('DUPLICATE_SESSION_BUCKET_REQUIRES_REVIEW')
            if len(selected) >= 1440:
                raise ValueError('SESSION_AGGREGATION_ROW_LIMIT')
            selected[at] = row
    missing_hours = sorted(expected_hours - selected.keys())
    missing_minutes = sorted(expected_minutes - selected.keys())
    prices, volume, references = None, 0, []
    for at, row in sorted(selected.items()):
        p = {key: Decimal(row[key]) for key in ('open', 'high', 'low', 'close')}
        if prices is None:
            prices = p
        else:
            prices.update(high=max(prices['high'], p['high']), low=min(prices['low'], p['low']), close=p['close'])
        volume = None if volume is None or row['volume'] is None else volume + row['volume']
        references.append(dict(**_ref(row), bucket_start=row['ts_event'], schema=row['schema']))
    observed = None if prices is None else dict(**{k: str(v) for k, v in prices.items()}, volume=volume)
    complete = not missing_hours and not missing_minutes and observed is not None and volume is not None
    return dict(session_ohlcv=observed if complete else None, observed_partial_ohlcv=observed,
        price_coverage='COMPLETE_OBSERVED_BUCKETS' if complete else 'INCOMPLETE_NOT_FILLED',
        expected_hour_buckets=len(expected_hours), expected_boundary_minutes=len(expected_minutes),
        selected_bucket_count=len(selected), missing_hour_buckets=[iso_ns(x) for x in missing_hours],
        missing_boundary_minutes=[iso_ns(x) for x in missing_minutes], boundary_minute_intervals=partial,
        excluded_cross_boundary_hours=crossed, records=references,
        available_at=None, causal_prefix_status='UNKNOWN_HISTORICAL_BAR_CORRECTIONS_AND_AVAILABILITY')


def settlement_versions_at(sources, *, contract_id, instrument_id, publisher_id,
                           session, decision_at, reference_evidence, guard=None):
    """Select the latest visible version; deletes/nonfinal replacements invalidate.

    ts_ref is only a UTC date label. ts_recv bounds an observed capture prefix,
    never customer delivery. Later messages are absent from this result.
    """
    reference_evidence.read(kind='CME_TRADING_REFERENCE_DATE', scope={'dataset': 'GLBX.MDP3'})
    session = date.fromisoformat(str(session))
    if not date(2021, 1, 1) <= session <= date(2025, 12, 31):
        raise ValueError('SETTLEMENT_OUTSIDE_FROZEN_SESSION')
    sources = _sources(sources, contract_id)
    cutoff, versions, latest, order = timestamp_ns(decision_at), [], None, None
    ambiguous_latest_batch = False
    for row in _rows(sources, 'statistics', identity=(publisher_id, instrument_id), guard=guard):
        if row['ts_recv_ns'] > cutoff:
            continue
        if row['stat_type'] != 3:
            continue
        if row['reference_session_hint'] is None:
            raise ValueError('SETTLEMENT_REFERENCE_DATE_AMBIGUOUS')
        if row['reference_session_hint'] != str(session):
            continue
        key = row['ts_recv_ns'], row['sequence'], row['channel_id']
        if order is not None and key[0] == order[0]:
            # Trading-tick and clearing-tick variants can share a packet. Keep
            # both records, but never pick by CSV order or the favorable value.
            # A different sequence/channel at the same capture time cannot
            # clear ambiguity: merged file order is not cross-channel order.
            ambiguous_latest_batch = True
        else:
            ambiguous_latest_batch = False
        order = key
        if len(versions) >= 512:
            raise ValueError('SETTLEMENT_VERSION_COUNT_BOUNDARY')
        versions.append(dict(**_ref(row), capture_at=row['ts_recv'], event_at=row['ts_event'],
            reference_date=row['reference_session_hint'], ts_ref=row['ts_ref'], price=row['price'],
            action=row['update_action'], flags=row['settlement_flags'], sequence=row['sequence'],
            channel_id=row['channel_id'], publisher_send_at=row['publisher_send_at'],
            publisher_send_time_status=row['publisher_send_time_status']))
        latest = row
    value, status = None, 'NO_SETTLEMENT_MESSAGE_VISIBLE_AT_CUTOFF'
    if latest is not None:
        flags = latest['settlement_flags']
        status = 'LATEST_VISIBLE_VERSION_NOT_FINAL_ACTUAL_EOD'
        if ambiguous_latest_batch:
            status = 'MULTIPLE_SETTLEMENT_VARIANTS_REQUIRE_EXPLICIT_REVIEW'
        elif latest['update_action'] == 2:
            status = 'LATEST_VISIBLE_VERSION_DELETED'
        elif (latest['price'] is not None and flags['final'] and flags['actual'] and
              not flags['intraday'] and not flags['unknown_bits']):
            if latest['ts_event_ns'] is None or latest['ts_event_ns'] > latest['ts_recv_ns']:
                status = 'SETTLEMENT_EVENT_CAPTURE_ORDER_UNKNOWN'
            else:
                value, status = latest['price'], 'FINAL_ACTUAL_EOD_CAPTURE_PREFIX_CANDIDATE'
    return dict(session=str(session), decision_at=iso_ns(cutoff), status=status, settlement=value,
        latest_visible_message=versions[-1] if versions and not ambiguous_latest_batch else None,
        versions_visible_at_cutoff=versions, ambiguous_latest_batch=ambiguous_latest_batch,
        capture_available_at=latest['ts_recv'] if value is not None else None,
        historical_customer_available_at=None, settlement_pricing_reference_at=None,
        reference_date_semantics=_evidence_ref(reference_evidence), research_qualified=False)


def _status(sources, identity, start, end, cutoff, guard):
    opening, events = None, []
    for row in _rows(sources, 'status', identity=identity, guard=guard):
        if row['ts_recv_ns'] > min(cutoff, end):
            continue
        event = dict(**_ref(row), capture_at=row['ts_recv'], event_at=row['ts_event'],
                     **{k: row[k] for k in ('action', 'reason', 'trading_event', 'is_trading', 'is_quoting')})
        if row['ts_recv_ns'] <= start:
            opening = event
        else:
            if len(events) >= 1024:
                raise ValueError('STATUS_EVENT_COUNT_BOUNDARY')
            events.append(event)
    return dict(opening_capture_state=opening, events_captured_during_session=events,
        status='OBSERVED_EVENTS_NOT_COMPLETE_HALT_LIMIT_OR_EXECUTION_PROOF',
        tradable_open=False, tradable_stop=False)


def boundary_candidate(evidence, *, contract_id, definition, registry_row):
    value = evidence.read(kind='SPECIFIC_CONTRACT_BOUNDARIES_CANDIDATE', scope={'contract_id': contract_id})
    notice, last = value.get('first_notice', {}), value.get('last_trade', {})
    issues, boundaries = [], []
    if last.get('at') and last.get('source_urls'):
        at = timestamp_ns(last['at'])
        boundaries.append(timestamp(last['at']).astimezone(ZoneInfo('America/Chicago')).date())
        if definition['expiration_ns'] != at:
            issues.append('RULE_LAST_TRADE_DIFFERS_FROM_VENDOR_EXPIRATION_REVIEW_REQUIRED')
    else:
        issues.append('SPECIFIC_LAST_TRADE_SOURCE_UNKNOWN')
    if notice.get('status') == 'NOT_APPLICABLE_CASH_SETTLED':
        if registry_row['delivery_type'] not in ('CASH', 'CASH_SOQ') or not notice.get('source_urls'):
            raise ValueError('CASH_NOTICE_INAPPLICABILITY_SOURCE_MISMATCH')
    elif notice.get('date') and notice.get('source_urls'):
        boundaries.append(date.fromisoformat(notice['date']))
    else:
        issues.append('APPLICABLE_FIRST_NOTICE_BOUNDARY_UNKNOWN')
    earliest = min(boundaries) if boundaries and not issues else None
    dates = value.get('preceding_exchange_sessions', [])
    if not isinstance(dates, list) or len(dates) > 64:
        raise ValueError('BOUNDARY_SESSION_LIST_SIZE_LIMIT')
    days = [date.fromisoformat(x) for x in dates]
    if days != sorted(set(days)):
        raise ValueError('BOUNDARY_SESSIONS_MUST_BE_UNIQUE_AND_ORDERED')
    prior = [day for day in days if earliest is not None and day < earliest]
    candidate = str(prior[-5]) if len(prior) >= 5 and value.get('session_list_source_urls') else None
    issues.append('DATED_BOUNDARY_CALENDAR_AND_HISTORICAL_EFFECTIVE_RULE_REVIEW_PENDING')
    if candidate is None:
        issues.append('FIVE_PRIOR_EXCHANGE_SESSIONS_NOT_EVIDENCED')
    return dict(first_notice=notice, last_trade=last, vendor_expiration=definition['expiration'],
        earliest_boundary_candidate=str(earliest) if earliest else None,
        five_session_safe_exit_candidate=candidate, safe_exit_session=None,
        preceding_exchange_sessions=dates, evidence=_evidence_ref(evidence), issues=issues,
        broker_boundary_status='NOT_APPLICABLE_RESEARCH_WITHOUT_SPECIFIED_BROKER',
        live_execution_eligible=False, research_qualified=False)


def capture_prefix_clock(evidence, *, start, end, used_schemas):
    """Validate documented input-clock semantics and bind the policy hash.

    The cutoff has a separate AFTER_INPUT_CUTOFF logical phase. No numerical
    processing delay, supplier publication or historical receipt is invented.
    Calendar, contract, status and independent integration gates are separate.
    """
    value = evidence.read(kind='GLBX_CAPTURE_PREFIX_POLICY')
    scope = value.get('scope', {})
    required = dict(verdict='DOCUMENTED_CAPTURE_PREFIX_MODEL_SUPPORTED',
        availability_basis='INTERNAL_CAPTURE_PREFIX', input_clock='ts_recv',
        bucket_timestamp_role='INTERVAL_START_NOT_PUBLICATION',
        interval_semantics='START_INCLUSIVE_END_EXCLUSIVE',
        internal_clock_phase='AFTER_INPUT_CUTOFF',
        revision_policy='DROP_TRADE_BUST_AND_CORRECTION_MESSAGES')
    if any(value.get(key) != expected for key, expected in required.items()):
        raise ValueError('CAPTURE_PREFIX_POLICY_SEMANTICS_NOT_ESTABLISHED')
    if (scope.get('dataset') != 'GLBX.MDP3' or not value.get('source_urls') or
        value.get('internal_clock_is_observed') is not False or
        value.get('supplier_published_at', 'MISSING') is not None or
        value.get('historical_customer_received_at', 'MISSING') is not None):
        raise ValueError('CAPTURE_PREFIX_POLICY_SOURCE_OR_CLOCK_SCOPE_INVALID')
    if (not set(used_schemas) <= set(scope.get('schemas', [])) or
        not timestamp_ns(scope['start']) <= start < end <= timestamp_ns(scope['end_exclusive'])):
        raise ValueError('CAPTURE_PREFIX_POLICY_OUTSIDE_SCHEMA_OR_DATE_SCOPE')
    documents = value.get('companion_documents', [])
    if not isinstance(documents, list) or not 1 <= len(documents) <= 10:
        raise ValueError('CAPTURE_PREFIX_BOUNDED_SOURCE_REVIEW_DOCUMENTS_REQUIRED')
    source_review = []
    for name in documents:
        path = (evidence.path.parent / name).resolve()
        if (not path.is_relative_to(evidence.path.parent.resolve()) or not path.is_file()
                or not 0 < path.stat().st_size <= 2 * 1024**2):
            raise ValueError('CAPTURE_PREFIX_SOURCE_REVIEW_MISSING_OR_OUTSIDE_POLICY_DIRECTORY')
        source_review.append(dict(path=str(path), sha256=digest(path)))
    calculated = iso_ns(end)
    return dict(availability_basis='INTERNAL_CAPTURE_PREFIX', input_cutoff=iso_ns(end),
        internal_calculated_at=calculated, available_at=calculated,
        supplier_published_at=None, received_at=None,
        temporal_evidence_hash=evidence.sha256, temporal_evidence=_evidence_ref(evidence),
        source_review_documents=source_review,
        source_review_proof='HASH_BOUND_ARCHIVED_REVIEW_AND_URLS_NOT_VENDOR_PER_FILE_CERTIFICATION',
        internal_clock_is_observed=False,
        internal_clock_phase='AFTER_INPUT_CUTOFF',
        internal_clock_convention='CUTOFF_WITH_SEPARATE_LOGICAL_PHASE_NO_NUMERICAL_DELAY',
        proof_scope='DOCUMENTED_VENDOR_POLICY_WITH_SOURCE_HASH_BOUND_SESSION_AGGREGATION')


def integrate_session(sources, *, window, decision_at, registry_row, boundary_evidence,
                      reference_evidence, causality_evidence=None, guard=None):
    """Build fields for independent import review, never self-approve eligibility.

    A documented causal-prefix policy can resolve the temporal layer. Remaining
    gates are returned individually so a caller can complete qualification rather
    than being forced to treat every source as permanently unqualifiable.
    """
    if guard:
        guard.check({'stage': 'integrate_dated_session', 'contract_id': window.contract_id})
    calendar, segments = window.read()
    cutoff, start, end = timestamp_ns(decision_at), segments[0][0], segments[-1][1]
    if cutoff < start:
        raise ValueError('DECISION_BEFORE_SESSION_OPEN')
    source_objects = tuple(sources)
    selected_sources = _sources(source_objects, window.contract_id)
    definition, versions, definition_issues = _definition(selected_sources, window.contract_id, start, end, cutoff, guard)
    units = compare_definition(definition, registry_row)
    identity = definition['publisher_id'], definition['instrument_id']
    aggregation = _aggregate(selected_sources, identity, segments, guard)
    temporal = None
    if causality_evidence is not None:
        temporal = capture_prefix_clock(causality_evidence, start=start, end=end,
            used_schemas={record['schema'] for record in aggregation['records']})
        aggregation['available_at'] = temporal['available_at']
        aggregation['causal_prefix_status'] = 'DOCUMENTED_CAPTURE_PREFIX_POLICY_BOUND_TO_SOURCE_RECORDS'
    settlement = settlement_versions_at(source_objects, contract_id=window.contract_id,
        instrument_id=identity[1], publisher_id=identity[0], session=window.session,
        decision_at=decision_at, reference_evidence=reference_evidence, guard=guard)
    boundaries = boundary_candidate(boundary_evidence, contract_id=window.contract_id,
        definition=definition, registry_row=registry_row)
    issues = [
        dict(layer='CALENDAR', code='DATED_HOLIDAY_AND_SPECIAL_EVENT_RECONCILIATION_PENDING'),
        dict(layer='EXECUTION', code='STATUS_HALT_LIMIT_COVERAGE_REVIEW_PENDING'),
        dict(layer='DEFINITION', code='SPECIFIC_FIRST_TRADE_DATE_UNKNOWN_ACTIVATION_IS_NOT_LISTING'),
        dict(layer='SETTLEMENT', code='SETTLEMENT_PRICING_REFERENCE_TIME_AND_DELIVERY_CAUSALITY_UNKNOWN'),
        dict(layer='INTEGRATION', code='INDEPENDENT_RAW_TO_ACCOUNT_INPUT_REVIEW_PENDING')]
    if temporal is None:
        issues.append(dict(layer='CAUSALITY', code='OHLCV_AS_OF_PREFIX_AND_CORRECTION_POLICY_UNPROVEN'))
    elif timestamp_ns(temporal['available_at']) > cutoff:
        issues.append(dict(layer='CAUSALITY', code='INTERNAL_LOGICAL_CALCULATION_AFTER_DECISION_CUTOFF'))
    issues.extend(dict(layer='DEFINITION', code=code) for code in definition_issues)
    issues.extend(dict(layer='BOUNDARY', code=code) for code in boundaries['issues'])
    if units['unit_comparison'] != 'MATCH':
        issues.append(dict(layer='UNITS', code='ACTUAL_VENDOR_FROZEN_REGISTRY_UNIT_MISMATCH'))
    if aggregation['session_ohlcv'] is None:
        issues.append(dict(layer='PRICES', code='INCOMPLETE_BUCKET_COVERAGE_NOT_A_FULL_SESSION'))
    if end > cutoff:
        issues.append(dict(layer='CAUSALITY', code='SESSION_NOT_COMPLETE_AT_DECISION_CUTOFF'))
    if settlement['settlement'] is None:
        issues.append(dict(layer='SETTLEMENT', code=settlement['status']))
    result = dict(status='DATED_RAW_SESSION_CANDIDATE_RESEARCH_BLOCKED', research_qualified=False,
        real_futures_backtest_run=False, contract_id=window.contract_id, session=str(window.session),
        market=registry_row['market'], root=registry_row['root'], instrument_id=identity[1], publisher_id=identity[0],
        opens_at=iso_ns(start), closes_at=iso_ns(end), decision_at=iso_ns(cutoff),
        next_session=calendar.get('next_session'), next_open=calendar.get('next_open'),
        calendar=dict(evidence=_evidence_ref(window.evidence), segments=calendar['segments'],
                      source_status=calendar.get('source_status', 'UNKNOWN')),
        definition=dict(selected_record=_ref(definition), versions_observed_by_cutoff=versions, units=units),
        prices=aggregation, temporal=temporal, settlement=settlement,
        execution_status=_status(selected_sources, identity, start, end, cutoff, guard),
        boundaries=boundaries, qualification_issues=issues,
        permitted_assumption_layers=dict(initial_margin_fractions=['0.10', '0.20'],
            maintenance_fraction_of_initial='0.75', commission_usd_per_contract_side='2',
            historical_margin_status='UNKNOWN_SEPARATE_FROM_ALLOWED_ASSUMED_LAYER',
            historical_commission_status='UNKNOWN_SEPARATE_FROM_ALLOWED_ASSUMED_LAYER',
            assumption_layers_blocked_by_missing_broker=False),
        derivation_contract_version=1,
        integration_inputs=dict(registry_row=dict(registry_row),
            reference_evidence=_evidence_ref(reference_evidence)),
        sources=[dict(source_sha256=r['sha256'], receipt_sha256=digest(s.receipt_path),
                      path=str(s.path.resolve()), receipt_path=str(s.receipt_path.resolve()),
                      price_encoding=s.price_encoding, request_sha256=r['request_sha256'],
                      schema=r['request']['schema'], request_start=r['request']['start'],
                      request_end=r['request']['end'], download_received_at=r['received_at'])
                 for s, r in selected_sources])
    result['derivation_sha256'] = canonical_hash(result)
    # The importer must validate the session derivation against its raw sources;
    # a many-file session has no honest single raw-object hash. Retain both.
    ohlcv = aggregation['session_ohlcv']
    result['engine_record_candidate'] = None if ohlcv is None else dict(
        contract_id=window.contract_id, session=str(window.session),
        opens_at=iso_ns(start), closes_at=iso_ns(end), available_at=aggregation['available_at'],
        **ohlcv, source_hash=result['derivation_sha256'], received_at=None,
        settlement=settlement['settlement'], settlement_available_at=settlement['capture_available_at'],
        settlement_reference_at=None, status='UNQUALIFIED_DATED_SESSION_INPUT',
        tradable_open=False, tradable_stop=False, session_verified=False, is_mock=False,
        next_session=calendar.get('next_session'),
        **{key: temporal[key] if temporal else None for key in
           ('availability_basis', 'input_cutoff', 'internal_calculated_at', 'supplier_published_at', 'temporal_evidence_hash')})
    result['raw_source_object_hashes'] = sorted({record['source_sha256'] for record in aggregation['records']})
    return result
