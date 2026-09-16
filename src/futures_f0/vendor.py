"""Bounded Databento CSV inspection and evidence-bound normalization candidates.

This module has no network/client/key dependency. A parsed vendor sample is not
research-qualified. In particular, it never writes VERIFIED_RAW_TO_NORMALIZED
or silently turns receipt time, exchange-date labels or OHLCV closes into
historical publication timestamps or settlements.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

from .data import Request, RAW_SYMBOL, ROOTS, aggregate_session, timestamp
from .runtime import canonical_hash, digest, write_json

NANO = 1_000_000_000
UNDEF_PRICE = 2**63 - 1
UNDEF_TIMESTAMP = 2**64 - 1
MAX_SOURCE_BYTES = 128 * 1024**2
MAX_LINE_BYTES = 64 * 1024
RTYPES = {'definition': 19, 'ohlcv-1h': 34, 'ohlcv-1m': 33,
          'statistics': 24, 'status': 18}
# Confirmed against databento/dbn record.rs and record/impl_default.rs.
# Zero IDs and zero leg counts are intentionally not generalized to null.
DEFINITION_INTEGER_SENTINELS = {
    **{name: 2**31-1 for name in ('contract_multiplier', 'decay_quantity',
        'original_contract_size', 'inst_attrib_value', 'market_depth_implied',
        'market_depth', 'min_lot_size', 'min_lot_size_block', 'min_lot_size_round_lot')},
    **{name: 2**32-1 for name in ('market_segment_id', 'max_trade_vol', 'min_trade_vol')},
    **{name: 2**16-1 for name in ('maturity_year', 'decay_start_date', 'channel_id')},
    **{name: 2**8-1 for name in ('main_fraction', 'price_display_format', 'sub_fraction',
        'underlying_product', 'maturity_month', 'maturity_day', 'maturity_week', 'tick_rule')},
    'appl_id': 2**15-1, 'contract_multiplier_unit': 2**7-1, 'flow_schedule_type': 2**7-1,
}
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
INTEGER = re.compile(r'-?\d+')
ISO_NANOS = re.compile(r'(.+T\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)')


def integer(value, *, nullable=False, sentinel=None):
    if value in (None, '') or (sentinel is not None and str(value) == str(sentinel)):
        if nullable:
            return None
        raise ValueError('REQUIRED_INTEGER_MISSING')
    if isinstance(value, bool) or not INTEGER.fullmatch(str(value)):
        raise ValueError('INVALID_INTEGER')
    return int(value)


def timestamp_ns(value, *, nullable=False):
    """Keep exact integer nanoseconds; Python datetime must not truncate events."""
    if value in (None, '') or str(value) == str(UNDEF_TIMESTAMP):
        if nullable:
            return None
        raise ValueError('REQUIRED_TIMESTAMP_MISSING')
    raw = value.isoformat() if isinstance(value, datetime) else str(value)
    if INTEGER.fullmatch(raw):
        result = int(raw)
    else:
        match = ISO_NANOS.fullmatch(raw)
        if not match:
            raise ValueError('TIMESTAMP_NEEDS_EXPLICIT_ZONE_AND_AT_MOST_NANOSECOND_PRECISION')
        base = timestamp(match[1] + match[3])
        delta = base - EPOCH
        result = (delta.days * 86400 + delta.seconds) * NANO + int((match[2] or '').ljust(9, '0'))
    if not 0 <= result < UNDEF_TIMESTAMP:
        raise ValueError('INVALID_TIMESTAMP_RANGE')
    return result


def iso_ns(value):
    if value is None:
        return None
    seconds, nanos = divmod(value, NANO)
    return (EPOCH + timedelta(seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%S') + f'.{nanos:09d}Z'


def model_time(value):
    """Conservatively round availability upward to the engine's microseconds."""
    return EPOCH + timedelta(microseconds=(value + 999) // 1000)


def price_decimal(value, encoding, *, nullable=False):
    if encoding not in ('fixed_1e9', 'decimal'):
        raise ValueError('EXPLICIT_PRICE_ENCODING_REQUIRED')
    if value in (None, '') or (encoding == 'fixed_1e9' and str(value) == str(UNDEF_PRICE)):
        if nullable:
            return None
        raise ValueError('REQUIRED_PRICE_MISSING')
    try:
        result = (Decimal(integer(value)) / NANO if encoding == 'fixed_1e9' else Decimal(str(value)))
    except (InvalidOperation, ValueError):
        raise ValueError('INVALID_PRICE') from None
    if not result.is_finite():
        raise ValueError('NONFINITE_PRICE')
    return result


def _small_json(path, maximum=1024**2):
    path = Path(path)
    if path.stat().st_size > maximum:
        raise ValueError('EVIDENCE_SIZE_BOUNDARY')
    return json.loads(path.read_text(encoding='utf-8-sig'),
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('NONFINITE_JSON')))


@dataclass(frozen=True)
class RawSource:
    path: Path
    receipt_path: Path
    price_encoding: str

    @classmethod
    def from_receipt(cls, csv_path, receipt_path, *, price_encoding):
        value = cls(Path(csv_path).resolve(), Path(receipt_path).resolve(), price_encoding)
        value.validate()
        return value

    def validate(self):
        if self.price_encoding not in ('fixed_1e9', 'decimal'):
            raise ValueError('EXPLICIT_PRICE_ENCODING_REQUIRED')
        receipt = _small_json(self.receipt_path)
        if not isinstance(receipt, dict):
            raise ValueError('INVALID_VENDOR_RECEIPT')
        p = receipt['request']
        symbols = tuple(p['symbols'].split(',')) if isinstance(p['symbols'], str) else tuple(p['symbols'])
        request = Request(symbols, p['schema'], p['start'], p['end'], p['dataset'], p['stype_in'])
        if request.parameters() != p or canonical_hash(p) != receipt.get('request_sha256'):
            raise ValueError('RECEIPT_REQUEST_MISMATCH')
        if receipt.get('source') != 'https://hist.databento.com/v0/':
            raise ValueError('DATABENTO_SOURCE_RECEIPT_REQUIRED')
        if not self.path.is_file() or not 0 < self.path.stat().st_size <= MAX_SOURCE_BYTES:
            raise ValueError('RAW_SOURCE_SIZE_BOUNDARY')
        if receipt.get('bytes') != self.path.stat().st_size or receipt.get('sha256') != digest(self.path):
            raise ValueError('RAW_SOURCE_HASH_OR_SIZE_MISMATCH')
        timestamp(receipt['received_at'])
        timestamp(receipt['completed_at'])
        if timestamp(receipt['completed_at']) < timestamp(receipt['received_at']):
            raise ValueError('INVALID_DOWNLOAD_RECEIPT_TIMES')
        fmt = receipt.get('csv_format', {})
        # HTTP CSV get_range defaults to pretty_px=false. Legacy receipts from
        # our fixed-format downloader omit csv_format, never implying decimal.
        # Explicit decimal exports must carry their true/false format evidence.
        pretty = fmt.get('pretty_px', False)
        if type(pretty) is not bool or pretty is not (self.price_encoding == 'decimal'):
            raise ValueError('PRICE_ENCODING_DISAGREES_WITH_RECEIPT')
        return receipt

    def records(self, *, guard=None):
        """Yield one parsed record or quarantined record at a time in source order."""
        receipt = self.validate()
        schema, sha = receipt['request']['schema'], receipt['sha256']
        lower, upper = (timestamp_ns(receipt['request'][key]) for key in ('start', 'end'))
        previous = None
        with self.path.open('rb') as stream:
            def lines():
                while True:
                    line = stream.readline(MAX_LINE_BYTES + 1)
                    if not line:
                        return
                    if len(line) > MAX_LINE_BYTES:
                        raise ValueError('RAW_CSV_LINE_SIZE_BOUNDARY')
                    yield line.decode('utf-8-sig')
            reader = csv.DictReader(lines())
            if not reader.fieldnames or len(reader.fieldnames) > 256 or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise ValueError('INVALID_CSV_HEADER')
            if not {'ts_event', 'rtype', 'instrument_id', 'publisher_id'} <= set(reader.fieldnames):
                raise ValueError('MISSING_COMMON_VENDOR_FIELDS')
            for row in reader:
                if guard:
                    guard.check({'stage': 'vendor_stream', 'source_sha256': sha, 'line': reader.line_num})
                result = dict(schema=schema, source_sha256=sha, source_line=reader.line_num,
                              raw_record_sha256=canonical_hash(list(row.items())),
                              is_mock=receipt.get('mock') is True or receipt.get('is_mock') is True,
                              download_received_at=receipt['received_at'],
                              research_qualified=False, issues=[])
                try:
                    if None in row or any(v is None for v in row.values()):
                        raise ValueError('CSV_COLUMN_COUNT_MISMATCH')
                    result.update(_decode_record(row, schema, self.price_encoding))
                    if schema == 'definition' and result['raw_symbol'] not in receipt['request']['symbols'].split(','):
                        raise ValueError('DEFINITION_SYMBOL_OUTSIDE_EXPLICIT_REQUEST')
                    index = result['ts_event_ns'] if schema.startswith('ohlcv-') else result['ts_recv_ns']
                    if index is None or not lower <= index < upper:
                        raise ValueError('RECORD_OUTSIDE_REQUEST_RANGE')
                    if previous is not None and index < previous:
                        raise ValueError('VENDOR_RECORDS_NOT_IN_INDEX_TIME_ORDER')
                    previous = index
                    result['parse_status'] = 'PARSED_VENDOR_RECORD'
                except (ValueError, KeyError, OverflowError) as error:
                    result['parse_status'] = 'QUARANTINED_RAW_RECORD'
                    result['issues'].append(str(error) if not isinstance(error, KeyError) else 'MISSING_FIELD_' + str(error.args[0]))
                yield result
        # Detect a changed source before a completed inspection can be used.
        if digest(self.path) != sha:
            raise ValueError('RAW_SOURCE_CHANGED_DURING_READ')


def _decode_record(row, schema, encoding):
    if integer(row['rtype']) != RTYPES[schema]:
        raise ValueError('SCHEMA_RECORD_TYPE_MISMATCH')
    result = {name: integer(row[name]) for name in ('instrument_id', 'publisher_id')}
    if not 0 < result['instrument_id'] < 2**32 or not 0 < result['publisher_id'] < 2**16:
        raise ValueError('INVALID_INSTRUMENT_OR_PUBLISHER_ID')
    for name in ('ts_event', 'ts_recv', 'ts_ref'):
        result[name + '_ns'] = timestamp_ns(row.get(name), nullable=(name != 'ts_event' or schema == 'statistics'))
        result[name] = iso_ns(result[name + '_ns'])
    result['ts_in_delta_raw'] = row.get('ts_in_delta')
    delta = integer(row.get('ts_in_delta'), nullable=True)
    if delta is not None and not -(2**31) <= delta <= 2**31-1:
        raise ValueError('PUBLISHER_TIME_DELTA_OUTSIDE_INT32')
    clipped = delta in (-(2**31), 2**31-1)
    result['ts_in_delta'] = None if clipped else delta
    result['publisher_send_time_status'] = 'UNKNOWN_CLAMPED_DELTA' if clipped else 'UNKNOWN_MISSING_DELTA_OR_CAPTURE'
    result['publisher_send_at'] = None
    if not clipped and delta is not None and result['ts_recv_ns'] is not None:
        sending = result['ts_recv_ns'] - delta
        if not 0 <= sending < UNDEF_TIMESTAMP:
            raise ValueError('INVALID_RECONSTRUCTED_PUBLISHER_TIME')
        result['publisher_send_at'] = iso_ns(sending)
        result['publisher_send_time_status'] = 'DERIVED_FROM_TS_RECV_MINUS_TS_IN_DELTA'
    result['historical_publication_at'] = None  # ts_recv is retained separately.
    if schema.startswith('ohlcv-'):
        prices = {k: price_decimal(row[k], encoding) for k in ('open', 'high', 'low', 'close')}
        if prices['low'] > min(prices['open'], prices['close']) or prices['high'] < max(prices['open'], prices['close']) or prices['low'] > prices['high']:
            raise ValueError('INVALID_OHLC')
        result.update({k: str(v) for k, v in prices.items()})
        result['volume'] = integer(row.get('volume'), nullable=True, sentinel=UNDEF_TIMESTAMP)
        if result['volume'] is not None and not 0 <= result['volume'] < UNDEF_TIMESTAMP:
            raise ValueError('INVALID_VOLUME')
        result['bucket_seconds'] = 3600 if schema == 'ohlcv-1h' else 60
        if result['ts_event_ns'] % (result['bucket_seconds'] * NANO):
            raise ValueError('UNALIGNED_VENDOR_BAR')
    elif schema == 'definition':
        for name in ('raw_symbol', 'instrument_class', 'security_type', 'security_update_action',
                     'currency', 'settl_currency', 'exchange', 'asset', 'unit_of_measure',
                     'cfi', 'secsubtype', 'user_defined_instrument'):
            result[name] = row.get(name, '')
        for name in ('activation', 'expiration'):
            result[name + '_ns'] = timestamp_ns(row.get(name), nullable=True)
            result[name] = iso_ns(result[name + '_ns'])
        for name in ('min_price_increment', 'display_factor', 'unit_of_measure_qty', 'min_price_increment_amount'):
            value = price_decimal(row.get(name), encoding, nullable=True)
            result[name] = str(value) if value is not None else None
        result['definition_integer_raw'] = {name: row.get(name) for name in DEFINITION_INTEGER_SENTINELS}
        for name, sentinel in DEFINITION_INTEGER_SENTINELS.items():
            result[name] = integer(row.get(name), nullable=True, sentinel=sentinel)
        for name in ('leg_count', 'leg_index'):
            result[name] = integer(row.get(name), nullable=True)
        result['activation_semantics'] = 'INSTRUMENT_ENABLED_TIMESTAMP_NOT_FIRST_TRADED_SESSION_OR_LISTED_DATE'
        result['identity_status'] = ('OUTRIGHT_FUTURE_CANDIDATE' if
            RAW_SYMBOL.fullmatch(result['raw_symbol']) and result['instrument_class'] == 'F'
            and result['security_type'] == 'FUT' and result['leg_count'] in (None, 0)
            and not result['secsubtype'] and result['user_defined_instrument'] in ('', 'N')
            else 'NON_OUTRIGHT_OR_IDENTITY_UNKNOWN')
    elif schema == 'statistics':
        for name in ('stat_type', 'update_action', 'stat_flags', 'sequence', 'channel_id'):
            result[name] = integer(row.get(name), nullable=name in ('sequence', 'channel_id'))
        if result['update_action'] not in (1, 2) or not 0 <= result['stat_flags'] <= 255:
            raise ValueError('UNKNOWN_STATISTIC_ACTION_OR_FLAGS')
        value = price_decimal(row.get('price'), encoding, nullable=True)
        result['price'] = str(value) if value is not None else None
        result['quantity'] = integer(row.get('quantity'), nullable=True, sentinel=UNDEF_PRICE)
        if result['stat_type'] == 3:
            flags = result['stat_flags']
            result['settlement_flags'] = dict(final=bool(flags & 1), actual=bool(flags & 2),
                trading_tick=bool(flags & 4), intraday=bool(flags & 8), unknown_bits=flags & ~15)
            # A date hint is useful for coverage; qualification still requires
            # the GLBX date-semantics evidence and verified exchange calendar.
            ref = result['ts_ref_ns']
            result['reference_session_hint'] = iso_ns(ref)[:10] if ref is not None and ref % (86400 * NANO) == 0 else None
    else:
        for name in ('action', 'reason', 'trading_event'):
            result[name] = integer(row.get(name), nullable=name != 'action')
        for name in ('is_trading', 'is_quoting', 'is_short_sell_restricted'):
            result[name] = row.get(name, '~')
            if result[name] not in ('Y', 'N', '~'):
                raise ValueError('UNKNOWN_STATUS_STATE')
    return result


def inspect_sources(sources, output_dir, *, guard=None):
    """Write streaming records and bounded coverage; never certify research data.

    At most 128 bounded source objects and 18 instrument IDs per object. Empty
    CSVs are explicitly visible in coverage. No row list or dataframe is built.
    """
    sources = tuple(sources)
    if not 1 <= len(sources) <= 128:
        raise ValueError('BOUNDED_SOURCE_OBJECTS_REQUIRED')
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    record_path, coverage_path = output / 'vendor_records.ndjson', output / 'VENDOR_SAMPLE_COVERAGE.csv'
    report_path = output / 'VENDOR_QUALIFICATION.json'
    if any(p.exists() for p in (record_path, coverage_path, report_path)):
        raise ValueError('INSPECTION_OUTPUT_EXISTS_USE_NEW_DIRECTORY')
    coverage, source_objects, total, seen_requests = [], [], 0, set()
    with record_path.open('x', encoding='utf-8', newline='\n') as destination:
        for source in sources:
            receipt = source.validate()
            if receipt['request_sha256'] in seen_requests:
                raise ValueError('DUPLICATE_REQUEST_SOURCE_REQUIRES_REVIEW')
            seen_requests.add(receipt['request_sha256'])
            summary = {}
            source_objects.append(dict(path=str(source.path), sha256=receipt['sha256'],
                receipt_path=str(source.receipt_path), receipt_sha256=digest(source.receipt_path),
                request=receipt['request'], price_encoding=source.price_encoding,
                provenance='ENGINEERING_FIXTURE' if receipt.get('mock') or receipt.get('is_mock') else
                           'HASH_MATCHED_VENDOR_RECEIPT_NOT_INDEPENDENT_SOURCE_CERTIFICATION'))
            for row in source.records(guard=guard):
                total += 1
                key = (row.get('publisher_id'), row.get('instrument_id'))
                if key not in summary:
                    if len(summary) >= 18:
                        raise ValueError('SOURCE_INSTRUMENT_COUNT_BOUNDARY')
                    summary[key] = dict(schema=row['schema'], source_sha256=row['source_sha256'],
                        requested_symbols=receipt['request']['symbols'], observed_raw_symbols=[],
                        publisher_id=key[0], instrument_id=key[1], rows=0, parsed=0, quarantined=0,
                        first_index_ns=None, last_index_ns=None, settlement_rows=0, final_actual_eod_rows=0,
                        delete_rows=0, unknown_volume_rows=0, research_qualified_rows=0,
                        reference_sessions=[], issues=[])
                entry = summary[key]
                entry['rows'] += 1
                if row.get('raw_symbol') and row['raw_symbol'] not in entry['observed_raw_symbols']:
                    if len(entry['observed_raw_symbols']) >= 18:
                        raise ValueError('SOURCE_SYMBOL_COUNT_BOUNDARY')
                    entry['observed_raw_symbols'].append(row['raw_symbol'])
                entry['parsed' if row['parse_status'] == 'PARSED_VENDOR_RECORD' else 'quarantined'] += 1
                index = row.get('ts_event_ns') if row['schema'].startswith('ohlcv-') else row.get('ts_recv_ns')
                if index is not None:
                    entry['first_index_ns'] = index if entry['first_index_ns'] is None else min(entry['first_index_ns'], index)
                    entry['last_index_ns'] = index if entry['last_index_ns'] is None else max(entry['last_index_ns'], index)
                if row.get('stat_type') == 3:
                    entry['settlement_rows'] += 1
                    flags = row.get('settlement_flags', {})
                    entry['final_actual_eod_rows'] += int(flags.get('final') is True and flags.get('actual') is True
                        and flags.get('intraday') is False and flags.get('unknown_bits') == 0 and row.get('update_action') == 1)
                    entry['delete_rows'] += int(row.get('update_action') == 2)
                    hint = row.get('reference_session_hint')
                    if hint and hint not in entry['reference_sessions']:
                        if len(entry['reference_sessions']) >= 128:
                            raise ValueError('STATISTIC_REFERENCE_DATE_BOUNDARY')
                        entry['reference_sessions'].append(hint)
                if row['schema'].startswith('ohlcv-') and row.get('volume') is None:
                    entry['unknown_volume_rows'] += 1
                for issue in row['issues']:
                    if issue not in entry['issues'] and len(entry['issues']) < 20:
                        entry['issues'].append(issue)
                destination.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
            if not summary:
                coverage.append(dict(schema=receipt['request']['schema'], source_sha256=receipt['sha256'],
                    requested_symbols=receipt['request']['symbols'], observed_raw_symbols=[],
                    publisher_id=None, instrument_id=None, rows=0, parsed=0, quarantined=0,
                    first_index_ns=None, last_index_ns=None, settlement_rows=0, final_actual_eod_rows=0,
                    delete_rows=0, unknown_volume_rows=0, research_qualified_rows=0,
                    reference_sessions=[], issues=['EMPTY_SCHEMA_RESPONSE_NOT_COVERAGE']))
            coverage.extend(summary.values())
    with coverage_path.open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(coverage[0]))
        writer.writeheader()
        for row in coverage:
            writer.writerow({k: json.dumps(v) if isinstance(v, list) else v for k, v in row.items()})
    report = dict(status='RAW_SAMPLE_INSPECTED_RESEARCH_BLOCKED', raw_rows=total,
        research_qualified_rows=0, real_futures_backtest_run=False, source_objects=source_objects,
        records=dict(path=record_path.name, sha256=digest(record_path)),
        coverage=dict(path=coverage_path.name, sha256=digest(coverage_path)),
        blockers=['DATED_DEFINITION_VALIDITY_AND_EXCHANGE_UNIT_RECONCILIATION_REQUIRED',
                  'DATED_EXCHANGE_CALENDAR_AND_SESSION_MAPPING_REQUIRED',
                  'OHLCV_HISTORICAL_PUBLICATION_AND_CORRECTION_EVIDENCE_REQUIRED',
                  'SETTLEMENT_REFERENCE_PUBLICATION_STATUS_AND_CORRECTION_REVIEW_REQUIRED',
                  'STATUS_COVERAGE_HALTS_AND_PRICE_LIMIT_REVIEW_REQUIRED',
                  'FIRST_NOTICE_LAST_TRADE_AND_BROKER_BOUNDARIES_REQUIRED',
                  'STANDARD_EXECUTION_MAPPING_AND_INDEPENDENT_INTEGRATION_REVIEW_REQUIRED'])
    write_json(report_path, report, immutable=True)
    return report


@dataclass(frozen=True)
class EvidenceFile:
    """Local source evidence reference, not a user-created verified boolean."""
    path: Path
    sha256: str
    source: str

    def read(self, *, kind, scope=None):
        if not self.source or self.source == 'UNKNOWN' or not re.fullmatch('[0-9a-f]{64}', self.sha256):
            raise ValueError('FILE_BACKED_SOURCE_EVIDENCE_REQUIRED')
        if digest(self.path) != self.sha256:
            raise ValueError('EXTERNAL_EVIDENCE_HASH_MISMATCH')
        value = _small_json(self.path)
        if value.get('kind') != kind or value.get('mock') is not False:
            raise ValueError('EVIDENCE_KIND_OR_REAL_SOURCE_REQUIRED')
        if scope and any(value.get('scope', {}).get(k) != v for k, v in scope.items()):
            raise ValueError('EVIDENCE_SCOPE_MISMATCH')
        # This validates provenance and scope only. It cannot establish that a
        # human's source interpretation is true, so no research gate is opened.
        return value


def definition_at(source, *, contract_id, instrument_id, publisher_id, as_of, guard=None):
    """Select an observed definition version without backdating its receipt."""
    if source.validate()['request']['schema'] != 'definition' or not RAW_SYMBOL.fullmatch(contract_id):
        raise ValueError('SPECIFIC_DEFINITION_SOURCE_REQUIRED')
    limit, selected = timestamp_ns(as_of), None
    for record in source.records(guard=guard):
        if record.get('instrument_id') != instrument_id or record.get('publisher_id') != publisher_id:
            continue
        if record['parse_status'] != 'PARSED_VENDOR_RECORD':
            raise ValueError('QUARANTINED_DEFINITION_REQUIRES_REVIEW')
        if record['ts_recv_ns'] > limit:
            break
        if record['raw_symbol'] != contract_id or record['identity_status'] != 'OUTRIGHT_FUTURE_CANDIDATE':
            raise ValueError('INSTRUMENT_ID_REUSED_OR_NON_OUTRIGHT')
        if record['security_update_action'] not in ('A', 'M', 'D'):
            raise ValueError('DEFINITION_UPDATE_ACTION_UNKNOWN')
        selected = record
    if selected is None or selected['security_update_action'] == 'D':
        raise ValueError('NO_ACTIVE_DEFINITION_KNOWN_AS_OF')
    if selected['activation_ns'] is None or selected['expiration_ns'] is None:
        raise ValueError('DEFINITION_LIFETIME_UNKNOWN')
    if not selected['activation_ns'] <= limit < selected['expiration_ns']:
        raise ValueError('BEFORE_ACTIVATION_OR_AFTER_EXPIRATION')
    return selected


def settlement_at(source, *, instrument_id, publisher_id, session, as_of, reference_evidence, guard=None):
    """Return a causal settlement candidate; later corrections cannot backfill.

    This is conservative: a delete, non-final replacement or unknown flags
    invalidates the selected value. Final/actual/EOD flags alone do not certify
    publication latency, correction history or reference-session coverage.
    """
    if source.validate()['request']['schema'] != 'statistics':
        raise ValueError('STATISTICS_SOURCE_REQUIRED')
    reference_evidence.read(kind='CME_TRADING_REFERENCE_DATE', scope={'dataset': 'GLBX.MDP3'})
    session = date.fromisoformat(str(session))
    if not date(2021, 1, 1) <= session <= date(2025, 12, 31):
        raise ValueError('SETTLEMENT_REFERENCE_OUTSIDE_FROZEN_HISTORY')
    from .settlement import SettlementState
    cutoff, state = timestamp_ns(as_of), SettlementState()
    for row in source.records(guard=guard):
        if row.get('instrument_id') != instrument_id or row.get('publisher_id') != publisher_id:
            continue
        if row['parse_status'] != 'PARSED_VENDOR_RECORD':
            raise ValueError('QUARANTINED_STATISTIC_REQUIRES_REVIEW')
        if row['ts_recv_ns'] > cutoff or row.get('stat_type') != 3:
            continue
        if row.get('reference_session_hint') is None:
            raise ValueError('SETTLEMENT_REFERENCE_NOT_DATE_PRECISION')
        if row['reference_session_hint'] == str(session):
            state.add(row)
    chosen, issue = state.account_candidate()
    if chosen is None:
        issue = {'LATEST_VISIBLE_VERSION_DELETED':'SETTLEMENT_DELETED_AS_OF'}.get(issue,issue)
        return dict(status=issue, settlement=None, session=str(session), updates_as_of=state.updates,
                    research_qualified=False)
    return dict(status='CAUSAL_CLEARING_SETTLEMENT_CANDIDATE_REQUIRES_REVIEW',
        settlement=chosen['price'], session=str(session), reference_at=chosen['ts_ref'],
        capture_available_at=iso_ns(chosen['ts_recv_ns']),
        model_capture_available_at=model_time(chosen['ts_recv_ns']).isoformat(),
        ts_event=chosen['ts_event'], stat_flags=chosen['stat_flags'],
        ts_in_delta=chosen['ts_in_delta'], publisher_send_at=chosen['publisher_send_at'],
        publisher_send_time_status=chosen['publisher_send_time_status'],
        source_sha256=chosen['source_sha256'], raw_record_sha256=chosen['raw_record_sha256'],
        updates_as_of=state.updates, research_qualified=False)



def session_candidate(source, *, definition, contract_id, window, publication_at,
                      publication_evidence, calendar_evidence, quote_evidence,
                      registry_row, next_session=None, guard=None):
    """Build existing SessionBar-shaped *unqualified* records for review.

    Unit conversion is an explicitly evidenced factor, checked against the
    frozen registry and vendor tick/size. The caller must separately resolve
    definition changes during the session, status/limits, settlements,
    delivery/broker boundaries and market mappings before research import.
    """
    receipt = source.validate()
    schema = receipt['request']['schema']
    if schema not in ('ohlcv-1h', 'ohlcv-1m') or definition.get('schema') != 'definition':
        raise ValueError('INTRADAY_BAR_AND_DEFINITION_REQUIRED')
    if definition.get('is_mock') or receipt.get('mock') or receipt.get('is_mock'):
        raise ValueError('MOCK_INPUT_NOT_REAL_NORMALIZATION')
    if definition.get('parse_status') != 'PARSED_VENDOR_RECORD' or definition.get('identity_status') != 'OUTRIGHT_FUTURE_CANDIDATE':
        raise ValueError('OUTRIGHT_DEFINITION_REQUIRED')
    if definition['raw_symbol'] != contract_id or contract_id not in receipt['request']['symbols'].split(','):
        raise ValueError('CONTRACT_IDENTITY_REQUEST_MISMATCH')
    root = next((r for r in sorted(ROOTS, key=len, reverse=True) if RAW_SYMBOL.fullmatch(contract_id) and
                 re.fullmatch(re.escape(r) + r'[FGHJKMNQUVXZ][0-9]{1,4}', contract_id)), None)
    if registry_row['root'] != root:
        raise ValueError('FROZEN_ROOT_REGISTRY_MISMATCH')
    window.validate()
    if window.timezone != 'America/Chicago' or not date(2021, 1, 1) <= window.session <= date(2025, 12, 31):
        raise ValueError('OUTSIDE_FROZEN_EXCHANGE_SESSIONS')
    scope = {'contract_id': contract_id, 'session': str(window.session)}
    cal = calendar_evidence.read(kind='DATED_EXCHANGE_CALENDAR', scope=scope)
    if window.evidence_sha256 != calendar_evidence.sha256:
        raise ValueError('CALENDAR_WINDOW_EVIDENCE_MISMATCH')
    expected_segments = [[timestamp(a).isoformat(), timestamp(b).isoformat()] for a, b in window.segments]
    if cal.get('segments') != expected_segments or cal.get('next_session') != (str(next_session) if next_session else None):
        raise ValueError('DATED_CALENDAR_CONTENT_MISMATCH')
    pub = publication_evidence.read(kind='OHLCV_PUBLICATION', scope=dict(scope, source_sha256=receipt['sha256']))
    if pub.get('available_at') != timestamp(publication_at).isoformat():
        raise ValueError('PUBLICATION_EVIDENCE_TIMESTAMP_MISMATCH')
    quote = quote_evidence.read(kind='CONTRACT_PRICE_CONVENTION', scope={'contract_id': contract_id,
        'definition_record_sha256': definition['raw_record_sha256']})
    factor = price_decimal(quote.get('vendor_decimal_to_quote_units'), 'decimal')
    if factor <= 0 or quote.get('quote_unit') != registry_row['quote_unit']:
        raise ValueError('QUOTE_CONVENTION_MISMATCH')
    if (quote.get('vendor_exchange') != definition['exchange'] or quote.get('exchange') != registry_row['exchange']
            or quote.get('vendor_currency') != definition['currency'] or quote.get('currency') != 'USD'):
        raise ValueError('VENDOR_EXCHANGE_OR_CURRENCY_RECONCILIATION_REQUIRED')
    if price_decimal(quote.get('usd_multiplier_per_quote_unit'), 'decimal') != Decimal(registry_row['usd_multiplier_per_quote_unit']):
        raise ValueError('USD_MULTIPLIER_MISMATCH')
    if price_decimal(definition.get('min_price_increment'), 'decimal') * factor != Decimal(registry_row['tick_in_quote_units']):
        raise ValueError('VENDOR_EXCHANGE_TICK_UNIT_MISMATCH')
    if price_decimal(definition.get('unit_of_measure_qty'), 'decimal') != Decimal(registry_row['contract_size']):
        raise ValueError('VENDOR_EXCHANGE_CONTRACT_SIZE_MISMATCH')
    first, last = (timestamp_ns(timestamp(value).isoformat()) for value in (window.segments[0][0], window.segments[-1][1]))
    if definition.get('activation_ns') is None or definition.get('expiration_ns') is None:
        raise ValueError('DEFINITION_LIFETIME_UNKNOWN')
    if definition['ts_recv_ns'] > first or definition['activation_ns'] > first or definition['expiration_ns'] < last:
        raise ValueError('DEFINITION_UNKNOWN_OR_NOT_ACTIVE_FOR_SESSION')
    bucket_seconds = 3600 if schema == 'ohlcv-1h' else 60

    def rows():
        for record in source.records(guard=guard):
            if (record.get('instrument_id'), record.get('publisher_id')) != (definition['instrument_id'], definition['publisher_id']):
                continue
            if record['parse_status'] != 'PARSED_VENDOR_RECORD':
                raise ValueError('QUARANTINED_OHLCV_REQUIRES_REVIEW')
            at = record['ts_event_ns']
            if first <= at < last:
                # Do not silently drop buckets crossing a break or a partial
                # boundary: aggregate_session must reject them explicitly.
                yield dict(contract_id=contract_id, timestamp=record['ts_event'],
                    **{key: float(Decimal(record[key]) * factor) for key in ('open', 'high', 'low', 'close')},
                    volume=record['volume'], status='VENDOR_RECORD_REVIEW_PENDING', is_mock=False)
    result = aggregate_session(rows(), window, bucket_seconds, contract_id=contract_id,
        publication_at=publication_at, source_hash=receipt['sha256'], received_at=receipt['received_at'], guard=guard)
    diagnostics = {k: result.pop(k) for k in ('expected_buckets', 'actual_buckets', 'calendar_sha256')}
    result.update(status='UNQUALIFIED_VENDOR_SESSION_CANDIDATE', tradable_open=False, tradable_stop=False,
                  next_session=str(next_session) if next_session else None, settlement_reference_at=None)
    diagnostics.update(status='REVIEW_PENDING', first_notice='UNKNOWN', broker_boundary='UNKNOWN',
        required_review=['INTRASESSION_DEFINITION_VERSIONS', 'HALTS_LIMITS_STATUS_COVERAGE',
                         'CAUSAL_SETTLEMENTS_AND_CORRECTIONS', 'DELIVERY_AND_BROKER_BOUNDARIES',
                         'STANDARD_EXECUTION_MAPPING', 'INDEPENDENT_RAW_TO_NORMALIZED_REVIEW'])
    return dict(record=result, qualification=diagnostics)


def manifest_candidate_entry(path, *, manifest_directory, source):
    """Produce the existing manifest entry shape without asserting verification."""
    path, base = Path(path).resolve(), Path(manifest_directory).resolve()
    if not path.is_file() or not path.is_relative_to(base) or source in ('', 'UNKNOWN', None):
        raise ValueError('MANIFEST_CANDIDATE_PATH_OR_SOURCE_INVALID')
    return dict(path=path.relative_to(base).as_posix(), sha256=digest(path), source=source, verified=False)
