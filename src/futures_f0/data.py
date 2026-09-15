"""Bounded input qualification, exchange-session aggregation and zero-cash preflight.

There is no market-data fallback. Public prices/credits are not account rights.
Positive list-price downloads additionally require an archived account-credit
proof and a durable reservation under the separately authorized cash-zero cap.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo

import requests

from .protocol import MARKETS
from .runtime import canonical_hash, digest, utcnow, write_json

ROOTS = tuple(r for m in MARKETS.values() for r in [m['signal'], *m['execution']])
SCHEMAS = ('definition', 'ohlcv-1h', 'ohlcv-1m', 'statistics', 'status')
RAW_SYMBOL = re.compile('(' + '|'.join(sorted(ROOTS, key=len, reverse=True)) + r')[FGHJKMNQUVXZ][0-9]{1,4}')


def timestamp(value):
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('NAIVE_TIMESTAMP_REJECTED')
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class Request:
    symbols: tuple[str, ...]
    schema: str
    start: str
    end: str
    dataset: str = 'GLBX.MDP3'
    stype_in: str = 'raw_symbol'

    def parameters(self):
        if self.dataset != 'GLBX.MDP3' or self.stype_in != 'raw_symbol':
            raise ValueError('ONLY_EXPLICIT_CME_FUTURES_RAW_SYMBOLS')
        if not self.symbols or len(self.symbols) > 18 or len(set(self.symbols)) != len(self.symbols):
            raise ValueError('INVALID_BOUNDED_CONTRACT_SELECTION')
        if any(not RAW_SYMBOL.fullmatch(s) for s in self.symbols):
            raise ValueError('ROOT_PARENT_SPREAD_OPTION_OR_WILDCARD_REJECTED')
        if self.schema not in SCHEMAS:
            raise ValueError('TICK_OR_UTC_DAILY_SCHEMA_NOT_AUTHORIZED')
        first, last = timestamp(self.start), timestamp(self.end)
        # Publication tail is not a 2026 trading sample. Statistics remain raw
        # until ts_ref is reconciled to a <=2025-12-31 reference session.
        ceiling = datetime(2026, 1, 4 if self.schema == 'statistics' else 1, tzinfo=timezone.utc)
        if not (datetime(2021, 1, 1, tzinfo=timezone.utc) <= first < last <= ceiling):
            raise ValueError('OUTSIDE_FROZEN_HISTORY')
        if last - first > timedelta(days=31):
            raise ValueError('REQUEST_OVER_31_DAYS')
        return dict(dataset=self.dataset, symbols=','.join(self.symbols), schema=self.schema,
                    start=first.isoformat(), end=last.isoformat(), stype_in=self.stype_in)


class MetadataClient:
    """Free metadata endpoints only, with redacted failure messages."""
    base = 'https://hist.databento.com/v0/'

    def __init__(self, config, *, session=None, guard=None):
        self.config, self.guard = config, guard
        self.session = session or requests.Session()

    def _metadata(self, method, parameters):
        if method not in ('get_cost', 'get_record_count', 'get_billable_size'):
            raise ValueError('METADATA_METHOD_NOT_ALLOWED')
        if not self.config.api_key:
            raise ValueError('DATABENTO_API_KEY_MISSING')
        for attempt in range(3):
            if self.guard:
                self.guard.check({'stage': 'metadata', 'method': method})
            try:
                response = self.session.post(self.base + 'metadata.' + method, data=parameters,
                    auth=(self.config.api_key, ''), timeout=(10, 30))
            except requests.RequestException:
                if attempt == 2:
                    raise RuntimeError('METADATA_NETWORK_FAILURE_REDACTED') from None
                time.sleep(attempt + 1)
                continue
            try:
                if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    try:
                        pause = min(10, max(1, float(response.headers.get('Retry-After', attempt + 1))))
                    except ValueError:
                        pause = attempt + 1
                    time.sleep(pause)
                    continue
                if response.status_code != 200:
                    raise RuntimeError('METADATA_HTTP_' + str(response.status_code))
                return response.json()
            finally:
                response.close()
        raise RuntimeError('METADATA_RETRY_LIMIT')

    def estimate(self, request):
        parameters = request.parameters()
        result = {'request': parameters, 'request_sha256': canonical_hash(parameters),
                  'source': self.base, 'estimated_at': utcnow().isoformat()}
        for name in ('get_record_count', 'get_billable_size', 'get_cost'):
            value = self._metadata(name, parameters)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError('INVALID_METADATA_ESTIMATE')
            result[name] = value
        result['status'] = 'REAL_METADATA_ESTIMATE_NOT_DOWNLOAD_OR_CREDIT_PROOF'
        return result

    def download_zero_quote(self, request, estimate, *, exact_contracts_verified=False, budget=None):
        """Stream only freshly quoted zero-cash requests; no positive-cost bypass.

        Completed content hashes are reusable. Interrupted responses are retained
        as .partial; no automatic rebilling/retry. A successful download is raw
        vendor data, never automatically qualified for research.
        """
        if budget is not None:
            return self.download_with_credit(request, budget,
                exact_contracts_verified=exact_contracts_verified)
        parameters = request.parameters()
        request_hash = canonical_hash(parameters)
        target = self.config.data_dir / 'vendor_cache' / (request_hash + '.csv')
        receipt = target.with_suffix('.receipt.json')
        if target.exists() and receipt.exists():
            old = json.loads(receipt.read_text(encoding='utf-8'))
            if old.get('request_sha256') != request_hash or old.get('sha256') != digest(target):
                raise ValueError('CACHED_SOURCE_HASH_MISMATCH')
            return old
        if estimate.get('request_sha256') != request_hash:
            raise ValueError('ESTIMATE_REQUEST_MISMATCH')
        if not 0 <= (utcnow() - timestamp(estimate['estimated_at'])).total_seconds() <= 3600:
            raise ValueError('FRESH_COST_ESTIMATE_REQUIRED')
        if type(estimate.get('get_cost')) not in (int, float) or estimate['get_cost'] != 0:
            raise ValueError('POSITIVE_QUOTE_REQUIRES_VERIFIED_EXISTING_CREDIT_AND_AUTHORIZATION')
        if not exact_contracts_verified:
            raise ValueError('EXACT_OUTRIGHT_CONTRACT_IDENTITIES_REQUIRED')
        if not self.config.api_key:
            raise ValueError('DATABENTO_API_KEY_MISSING')
        if target.exists() or receipt.exists() or target.with_suffix('.partial').exists():
            raise ValueError('INCOMPLETE_CACHE_REQUIRES_REVIEW_NO_AUTO_REBILL')
        # Never authorize a billable endpoint using a caller-created estimate.
        # Check the authenticated vendor again, even for a claimed zero quote.
        fresh = self.estimate(request)
        if type(fresh.get('get_cost')) not in (int, float) or fresh['get_cost'] != 0:
            raise ValueError('FRESH_VENDOR_QUOTE_NOT_ZERO_DOWNLOAD_BLOCKED')
        partial = target.with_suffix('.partial')
        target.parent.mkdir(parents=True, exist_ok=True)
        acquired_at = utcnow().isoformat()
        response = None
        try:
            if self.guard:
                self.guard.check({'stage': 'before_download', 'request_sha256': request_hash})
            response = self.session.post(self.base + 'timeseries.get_range',
                data=dict(parameters, stype_out='instrument_id', encoding='csv', compression='none'),
                auth=(self.config.api_key, ''), timeout=(10, 30), stream=True)
            if response.status_code != 200:
                raise RuntimeError('DOWNLOAD_HTTP_' + str(response.status_code))
            written = 0
            with partial.open('xb') as f:
                for chunk in response.iter_content(chunk_size=65536):
                    if self.guard:
                        self.guard.check({'stage': 'download', 'request_sha256': request_hash,
                                          'bytes_retained': written})
                    written += len(chunk)
                    if written > 128 * 1024 ** 2:
                        raise ValueError('BATCH_DOWNLOAD_SIZE_LIMIT_SPLIT_REQUEST_AFTER_REVIEW')
                    f.write(chunk)
            if written == 0:
                raise ValueError('EMPTY_RESPONSE_NOT_MARKET_COVERAGE')
            partial.replace(target)
            value = dict(request=parameters, request_sha256=request_hash, source=self.base,
                         received_at=acquired_at, completed_at=utcnow().isoformat(),
                         historical_publication_time='UNKNOWN_UNTIL_RECORD_QUALIFICATION',
                         sha256=digest(target), bytes=written, quoted_cost_usd=0,
                         authenticated_estimate=fresh, research_qualified=False)
            write_json(receipt, value, immutable=True)
            return value
        except requests.RequestException:
            raise RuntimeError('DOWNLOAD_FAILED_NO_AUTOMATIC_RETRY_REDACTED') from None
        finally:
            if response is not None:
                response.close()

    def download_with_credit(self, request, budget, *, exact_contracts_verified=False):
        """Refresh the real quote, reserve its full price, then request once.

        Caller-created estimates never authorize this endpoint. All exceptions,
        including interrupts and unknown server outcomes, retain the reservation.
        ``quoted_cost_usd`` is the vendor's list-price estimate, not a statement
        that a cash payment or a settled credit debit has been observed.
        """
        from .budget import BudgetGate

        if not isinstance(budget, BudgetGate) or budget.data_dir != self.config.data_dir.resolve():
            raise ValueError('BUDGET_GATE_MUST_USE_SAME_RUN_DIRECTORY')
        parameters = request.parameters()
        request_hash = canonical_hash(parameters)
        target = self.config.data_dir / 'vendor_cache' / (request_hash + '.csv')
        receipt = target.with_suffix('.receipt.json')
        partial = target.with_suffix('.partial')
        # A complete content-verified batch is reused before metadata or billing
        # work. An interrupted batch is never downloaded again automatically.
        if target.exists() and receipt.exists():
            old = json.loads(receipt.read_text(encoding='utf-8'))
            if old.get('request_sha256') != request_hash or old.get('sha256') != digest(target):
                raise ValueError('CACHED_SOURCE_HASH_MISMATCH')
            return old
        if target.exists() or receipt.exists() or partial.exists():
            raise ValueError('INCOMPLETE_CACHE_REQUIRES_REVIEW_NO_AUTO_REBILL')
        if not exact_contracts_verified:
            raise ValueError('EXACT_OUTRIGHT_CONTRACT_IDENTITIES_REQUIRED')
        if not self.config.api_key:
            raise ValueError('DATABENTO_API_KEY_MISSING')
        if self.guard:
            self.guard.check({'stage': 'before_credit_quote', 'request_sha256': request_hash})
        fresh = self.estimate(request)
        reservation = budget._reserve_fresh_quote(parameters, fresh, api_key=self.config.api_key)
        response = None
        acquired_at = utcnow().isoformat()
        try:
            if self.guard:
                self.guard.check({'stage': 'before_download', 'request_sha256': request_hash})
            # Reserve/flush first and mark the attempt before even opening the
            # connection, so pre-response failures cannot erase a possible bill.
            target.parent.mkdir(parents=True, exist_ok=True)
            with partial.open('xb') as stream:
                budget._assert_send_allowed(reservation, api_key=self.config.api_key)
                response = self.session.post(self.base + 'timeseries.get_range',
                    data=dict(parameters, stype_out='instrument_id', encoding='csv', compression='none'),
                    auth=(self.config.api_key, ''), timeout=(10, 30), stream=True)
                if response.status_code != 200:
                    raise RuntimeError('DOWNLOAD_HTTP_' + str(response.status_code))
                written = 0
                for chunk in response.iter_content(chunk_size=65536):
                    if self.guard:
                        self.guard.check({'stage': 'download', 'request_sha256': request_hash,
                                          'bytes_retained': written})
                    written += len(chunk)
                    if written > 128 * 1024 ** 2:
                        raise ValueError('BATCH_DOWNLOAD_SIZE_LIMIT_SPLIT_REQUEST_AFTER_REVIEW')
                    stream.write(chunk)
                stream.flush()
                import os
                os.fsync(stream.fileno())
            if written == 0:
                raise ValueError('EMPTY_RESPONSE_NOT_MARKET_COVERAGE')
            partial.replace(target)
            value = dict(request=parameters, request_sha256=request_hash, source=self.base,
                received_at=acquired_at, completed_at=utcnow().isoformat(),
                historical_publication_time='UNKNOWN_UNTIL_RECORD_QUALIFICATION',
                sha256=digest(target), bytes=written, quoted_cost_usd=fresh['get_cost'],
                authenticated_estimate=fresh, research_qualified=False,
                credit_reservation_id=reservation['reservation_id'],
                reserved_list_price_usd=reservation['reserved_cost_usd'],
                budget_reservation_sha256=reservation['sha256'],
                evidence_manifest_sha256=reservation['evidence_manifest_sha256'],
                credit_applicability=reservation['credit_applicability'],
                cash_authorization_usd=0, cash_payment_usd=None, credit_debit_usd=None,
                billing_reconciliation='NOT_INDEPENDENTLY_RECONCILED')
            write_json(receipt, value, immutable=True)
            budget._finish(reservation, receipt_path=receipt)
            return value
        except BaseException as error:
            try:
                budget._finish(reservation, failure=type(error).__name__)
            except Exception:
                # A durable RESERVE still occupies its entire price when even
                # recording the failure is interrupted or the lock is busy.
                pass
            if isinstance(error, requests.RequestException):
                raise RuntimeError('DOWNLOAD_FAILED_NO_AUTOMATIC_RETRY_REDACTED') from None
            raise
        finally:
            if response is not None:
                response.close()


def write_capability(config, registry_file=None):
    output = config.data_dir
    value = dict(checked_at=utcnow().isoformat(), source='GLBX.MDP3', key_present=bool(config.api_key),
        key_source=config.key_source, real_futures_history_run=False,
        local_futures_source='NONE_FOUND_IN_SCOPED_PROJECT_INVENTORY',
        read_only_broker_history='NOT_CONNECTED_NOT_TESTED',
        authenticated_metadata='NOT_RUN_KEY_MISSING' if not config.api_key else 'PENDING_EXACT_CONTRACT_REQUEST',
        historical_permission='UNVERIFIED', credits_balance='UNKNOWN', credits_expiry='UNKNOWN',
        credits_authorization='UNKNOWN', paid_downloads=0, cash_spend_usd=0,
        blockers=['NO_QUALIFIED_FUTURES_SOURCE', 'VENDOR_DEFINITIONS_NOT_ACQUIRED',
                  'HISTORICAL_SESSION_CALENDARS_NOT_ACQUIRED', 'COST_AND_CREDIT_UNVERIFIED'],
        acquisition_can_continue_with='Existing authorized actual futures contract data, or configured key + verified account credit/cost',
        equity_alpaca_is_futures_permission=False)
    if not config.api_key:
        value['blockers'].insert(0, 'DATABENTO_API_KEY_MISSING')
    write_json(output / 'DATA_CAPABILITY.json', value)
    plans = []
    for market, item in MARKETS.items():
        for root in [item['signal'], *item['execution']]:
            plans.append(dict(market=market, root=root, exact_instruments='UNKNOWN_PENDING_DEFINITIONS',
                 schema_sequence=['definition', 'ohlcv-1h', 'statistics', 'status'],
                 hour_rows_upper_per_contract_31_calendar_days=31 * 24,
                 minute_rows_upper_per_contract_31_calendar_days=31 * 24 * 60,
                 minimum_schema_for_partial_session_boundaries='ohlcv-1m, only necessary months/contracts',
                 selected_rows=None, vendor_quote_usd=None, status='NOT_REQUESTED',
                 root_parent_download=False))
    write_json(output / 'COST_ESTIMATE.json', dict(at=utcnow().isoformat(), currency='USD',
        procurement_cash_budget=0, authenticated_quote_total=None, actual_cash_spend=0,
        credit_balance=None, credit_expiry=None, credit_authorization='UNVERIFIED',
        estimates_are_sizing_formulas_not_vendor_quotes=True, maximum_batch_days=31,
        maximum_batch_response_bytes=128 * 1024 ** 2, plans=plans,
        no_api_calls_made_by_this_inventory=True,
        note='Definitions/statistics/status are event schemas: record counts must come from metadata. UTC daily bars are not the main session layer.'))
    fields = ['market', 'root', 'role', 'requested_start', 'requested_end', 'first_actual_session',
              'last_actual_session', 'qualified_rows', 'status', 'reason']
    with (output / 'COVERAGE.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader()
        for market, item in MARKETS.items():
            for root in [item['signal'], *item['execution']]:
                writer.writerow(dict(market=market, root=root, role='signal' if root == item['signal'] else 'execution',
                    requested_start='2021-01-01_WARMUP_2022-01-01_TRADING', requested_end='2025-12-31',
                    first_actual_session='NA', last_actual_session='NA', qualified_rows='NA',
                    status='DATA_BLOCKED_NOT_RUN', reason='NO_QUALIFIED_ACTUAL_CONTRACT_DATA'))
    return value


@dataclass(frozen=True)
class SessionWindow:
    session: date
    timezone: str
    segments: tuple[tuple[datetime, datetime], ...]
    source: str
    evidence_sha256: str
    verified: bool

    def validate(self):
        zone = ZoneInfo(self.timezone)
        if not self.verified or self.source in ('', 'UNKNOWN') or not re.fullmatch('[0-9a-f]{64}', self.evidence_sha256):
            raise ValueError('VERIFIED_DATED_EXCHANGE_CALENDAR_REQUIRED')
        if not self.segments:
            raise ValueError('CLOSED_SESSION_HAS_NO_BAR')
        previous = None
        for start, end in self.segments:
            start, end = timestamp(start), timestamp(end)
            if start >= end or (previous is not None and start < previous):
                raise ValueError('INVALID_SESSION_SEGMENTS')
            previous = end
        start, end = timestamp(self.segments[0][0]), timestamp(self.segments[-1][1])
        if end.astimezone(zone).date() != self.session:
            raise ValueError('SESSION_DATE_DOES_NOT_MATCH_EXCHANGE_CLOSE')
        if start.astimezone(zone).date() < self.session - timedelta(days=1) or end-start > timedelta(hours=25):
            raise ValueError('INVALID_MULTI_DAY_SESSION_RELABEL')


def aggregate_session(rows, window, bucket_seconds, *, contract_id, publication_at, source_hash,
                      received_at=None, guard=None):
    """Aggregate one actual session only; missing/no-trade bins are never filled.

    Requires every scheduled bucket to exist for QUALIFIED. Missing intervals
    retain an UNKNOWN coverage status, even when the market might simply have
    had no trades. Closed breaks are excluded by explicit dated segments.
    """
    window.validate()
    if not re.fullmatch('[0-9a-f]{64}', source_hash):
        raise ValueError('SOURCE_SHA256_REQUIRED')
    if bucket_seconds not in (60, 3600):
        raise ValueError('UNAPPROVED_BUCKET_WIDTH')
    expected = 0
    for start, end in window.segments:
        seconds = (timestamp(end) - timestamp(start)).total_seconds()
        if seconds % bucket_seconds or timestamp(start).timestamp() % bucket_seconds:
            raise ValueError('BUCKET_CROSSES_SESSION_BOUNDARY_REQUEST_FINER_BARS')
        expected += int(seconds / bucket_seconds)
    n, previous, result, volume = 0, None, None, 0
    input_statuses = set()
    for row in rows:
        if row.get('is_mock') is True:
            raise ValueError('MOCK_INPUT_NOT_ALLOWED_IN_REAL_AGGREGATOR')
        input_statuses.add(row.get('status', 'UNKNOWN'))
        if guard:
            guard.check({'stage': 'aggregate', 'contract': contract_id, 'session': str(window.session)})
        at = timestamp(row['timestamp'])
        if previous is not None and at <= previous:
            raise ValueError('DUPLICATE_OR_UNSORTED_BAR')
        previous = at
        if row.get('contract_id') != contract_id:
            raise ValueError('MIXED_ACTUAL_CONTRACTS')
        if not any(timestamp(a) <= at and at + timedelta(seconds=bucket_seconds) <= timestamp(b)
                   for a, b in window.segments):
            raise ValueError('BAR_OUTSIDE_EXCHANGE_SESSION_OR_BREAK')
        if at.timestamp() % bucket_seconds:
            raise ValueError('UNALIGNED_BAR_BUCKET')
        o, h, l, c = (float(row[k]) for k in ('open', 'high', 'low', 'close'))
        if not all(math.isfinite(x) for x in (o, h, l, c)) or l > min(o, c) or h < max(o, c) or h < l:
            raise ValueError('INVALID_OHLC')
        v = row.get('volume')
        if v is None or v == '':
            volume = None
        else:
            v = float(v)
            if not math.isfinite(v) or v < 0 or v != int(v):
                raise ValueError('INVALID_VOLUME')
            if volume is not None:
                volume += int(v)
        if result is None:
            result = dict(open=o, high=h, low=l, close=c)
        else:
            result.update(high=max(result['high'], h), low=min(result['low'], l), close=c)
        n += 1
    if not n:
        raise ValueError('NO_DATA_NOT_ZERO_RETURN')
    published = timestamp(publication_at)
    if published < timestamp(window.segments[-1][1]):
        raise ValueError('UNFINISHED_SESSION_OR_PUBLICATION_LOOKAHEAD')
    return dict(result, contract_id=contract_id, session=str(window.session), volume=volume,
                opens_at=timestamp(window.segments[0][0]).isoformat(),
                closes_at=timestamp(window.segments[-1][1]).isoformat(), available_at=published.isoformat(),
                received_at=timestamp(received_at).isoformat() if received_at else None,
                source_hash=source_hash, expected_buckets=expected, actual_buckets=n,
                status='INPUT_STATUS_' + '|'.join(sorted(input_statuses)) if input_statuses != {'QUALIFIED'} else
                       'QUALIFIED' if n == expected and volume is not None else
                       'MISSING_VOLUME_UNKNOWN' if volume is None else 'MISSING_BUCKETS_UNKNOWN',
                tradable_open=n == expected and input_statuses == {'QUALIFIED'},
                tradable_stop=n == expected and input_statuses == {'QUALIFIED'},
                session_verified=True, calendar_sha256=window.evidence_sha256,
                is_mock=False, settlement=None, settlement_available_at=None)


def safe_exit_session(session_dates, *, first_notice, last_trade, broker_boundary=None,
                      first_notice_not_applicable=False, broker_boundary_not_applicable=False):
    if last_trade is None or (first_notice is None and not first_notice_not_applicable):
        raise ValueError('DELIVERY_BOUNDARY_UNKNOWN')
    if broker_boundary is None and not broker_boundary_not_applicable:
        raise ValueError('BROKER_BOUNDARY_UNKNOWN')
    earliest = min(x for x in (first_notice, last_trade, broker_boundary) if x is not None)
    dates = tuple(session_dates)
    if tuple(sorted(set(dates))) != dates:
        raise ValueError('SESSION_CALENDAR_NOT_STRICTLY_ORDERED')
    before = [d for d in dates if d < earliest]
    if len(before) < 5 or not dates or dates[-1] < earliest:
        raise ValueError('INSUFFICIENT_DATED_CALENDAR_FOR_ROLL')
    return before[-5]


def verify_input_manifest(path):
    """Evidence hashes must match before a research importer can read bars."""
    path = Path(path)
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if data.get('kind') != 'EXCHANGE_FUTURES_ACTUAL_CONTRACTS' or data.get('mock') is not False:
        raise ValueError('REAL_EXCHANGE_FUTURES_MANIFEST_REQUIRED')
    required = ('bars', 'definitions', 'calendar', 'settlements', 'status', 'mapping')
    for name in required:
        entry = data.get(name, {})
        p = (path.parent / entry.get('path', '')).resolve()
        if not p.is_file() or not p.is_relative_to(path.parent.resolve()):
            raise ValueError('MISSING_OR_ESCAPING_INPUT_' + name.upper())
        if entry.get('verified') is not True or entry.get('source') in (None, '', 'UNKNOWN'):
            raise ValueError('UNVERIFIED_INPUT_' + name.upper())
        if digest(p) != entry.get('sha256'):
            raise ValueError('SOURCE_HASH_MISMATCH_' + name.upper())
    return data
