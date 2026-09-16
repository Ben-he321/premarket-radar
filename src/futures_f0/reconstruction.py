"""Small real-source session reconstructions with explicitly unknown availability.

Independent of the research importer: complete price buckets do not certify
historical publication, execution, calendars, listing dates or accounting. No
network access, synthetic data fallback, trading account or verified manifest.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re
from zoneinfo import ZoneInfo

from .data import timestamp
from .runtime import canonical_hash
from .vendor import NANO, EvidenceFile, definition_at, iso_ns, settlement_at, timestamp_ns


@dataclass(frozen=True)
class CandidateSession:
    contract_id: str
    session: date
    opens_at: str
    closes_at: str
    calendar_evidence: EvidenceFile

    def validate(self):
        start, end = timestamp_ns(self.opens_at), timestamp_ns(self.closes_at)
        if not date(2021,1,1) <= self.session <= date(2025,12,31):
            raise ValueError('OUTSIDE_FROZEN_CANDIDATE_SESSION')
        if not 0 < end-start <= 24*3600*NANO or start % (3600*NANO) or end % (3600*NANO):
            raise ValueError('ONE_CONTINUOUS_HOUR_ALIGNED_SESSION_REQUIRED')
        if timestamp(self.closes_at).astimezone(ZoneInfo('America/Chicago')).date()!=self.session:
            raise ValueError('EXCHANGE_SESSION_LABEL_MISMATCH')
        evidence=self.calendar_evidence.read(kind='DATED_EXCHANGE_SESSION_CANDIDATE',
            scope={'contract_id':self.contract_id,'session':str(self.session)})
        if (evidence.get('timezone')!='America/Chicago' or
            evidence.get('segments')!=[[iso_ns(start),iso_ns(end)]]):
            raise ValueError('CANDIDATE_CALENDAR_CONTENT_MISMATCH')
        return start,end


def _aggregate(sources, *, instrument_id, publisher_id, start, end, schema,
               max_rows, guard=None):
    """Fold records incrementally, retaining bounded evidence and bucket times."""
    width=3600*NANO if schema=='ohlcv-1h' else 60*NANO
    sources=tuple(sources)
    if not 1 <= len(sources) <= 4 or (end-start) % width:
        raise ValueError('BOUNDED_ALIGNED_SOURCES_REQUIRED')
    sources=sorted(sources,key=lambda s:timestamp_ns(s.validate()['request']['start']))
    previous=None; values=None; volume=0; references=[]; times=[]; source_hashes=[]
    for source in sources:
        receipt=source.validate()
        if receipt['request']['schema']!=schema:
            raise ValueError('RECONSTRUCTION_SCHEMA_MISMATCH')
        if receipt.get('mock') is True or receipt.get('is_mock') is True:
            raise ValueError('MOCK_SOURCE_NOT_REAL_RECONSTRUCTION')
        source_hashes.append(receipt['sha256'])
        for row in source.records(guard=guard):
            if row['parse_status']!='PARSED_VENDOR_RECORD':
                raise ValueError('QUARANTINED_SOURCE_REQUIRES_REVIEW')
            if (row['instrument_id'],row['publisher_id'])!=(instrument_id,publisher_id):
                continue
            at=row['ts_event_ns']
            if not start<=at<end:
                continue
            if at+width>end or at%width or previous is not None and at<=previous:
                raise ValueError('DUPLICATE_UNSORTED_OR_CROSS_BOUNDARY_BUCKET')
            if len(references)>=max_rows:
                raise ValueError('RECONSTRUCTION_ROW_BOUNDARY')
            previous=at; times.append(at)
            price={name:Decimal(row[name]) for name in ('open','high','low','close')}
            if values is None:
                values=price
            else:
                values.update(high=max(values['high'],price['high']),low=min(values['low'],price['low']),close=price['close'])
            if row['volume'] is None:
                volume=None
            elif volume is not None:
                volume+=row['volume']
            references.append(dict(source_sha256=row['source_sha256'],source_line=row['source_line'],
                raw_record_sha256=row['raw_record_sha256'],bucket_start=row['ts_event']))
    if values is None:
        raise ValueError('NO_OBSERVED_BUCKETS_NOT_ZERO_RETURN')
    expected=(end-start)//width
    missing=[iso_ns(start+i*width) for i in range(expected) if start+i*width not in times]
    result=dict(**{name:str(value) for name,value in values.items()},volume=volume)
    return dict(ohlcv=result,actual_buckets=len(references),expected_buckets=expected,
        missing_buckets=missing,bucket_coverage='COMPLETE_OBSERVED_BUCKETS' if not missing and volume is not None else 'MISSING_BUCKETS_OR_VOLUME_UNKNOWN',
        source_object_hashes=source_hashes,records=references)


def reconstruct_equity_session(hourly_sources, *, minute_source, definition_source,
    statistics_source, window, instrument_id, publisher_id, reference_evidence,
    settlement_as_of, guard=None):
    """ES/MES single-session inspection; always returns research_qualified=False.

    At most 48 hourly and 60 minute records per instrument are retained. Minute
    records independently check the last hour; they are never added a second
    time to the hourly session volume. Settlement is a separate causal result.
    """
    if guard:
        guard.check({'stage':'reconstruct_real_equity_session','contract_id':window.contract_id})
    root='MES' if re.fullmatch(r'MES[FGHJKMNQUVXZ][0-9]{1,4}',window.contract_id) else 'ES' if re.fullmatch(r'ES[FGHJKMNQUVXZ][0-9]{1,4}',window.contract_id) else None
    if root is None:
        raise ValueError('ONLY_ES_MES_RECONSTRUCTION_SUPPORTED')
    start,end=window.validate()
    definition=definition_at(definition_source,contract_id=window.contract_id,
        instrument_id=instrument_id,publisher_id=publisher_id,as_of=window.opens_at,guard=guard)
    if definition.get('is_mock') or definition['expiration_ns']<end:
        raise ValueError('REAL_ACTIVE_DEFINITION_REQUIRED')
    multiplier='5' if root=='MES' else '50'
    if (definition.get('min_price_increment') is None or definition.get('unit_of_measure_qty') is None or
        definition['currency']!='USD' or definition['exchange']!='XCME' or
        definition['unit_of_measure']!='IPNT' or Decimal(definition['min_price_increment'])!=Decimal('0.25') or
        Decimal(definition['unit_of_measure_qty'])!=Decimal(multiplier)):
        raise ValueError('ACTUAL_VENDOR_EQUITY_UNITS_MISMATCH')
    hourly_sources=tuple(hourly_sources)
    for source in (*hourly_sources,minute_source,definition_source,statistics_source):
        if window.contract_id not in source.validate()['request']['symbols'].split(','):
            raise ValueError('CONTRACT_MISSING_FROM_EXPLICIT_REQUEST')
    daily=_aggregate(hourly_sources,instrument_id=instrument_id,publisher_id=publisher_id,
        start=start,end=end,schema='ohlcv-1h',max_rows=48,guard=guard)
    final_hour=_aggregate(hourly_sources,instrument_id=instrument_id,publisher_id=publisher_id,
        start=end-3600*NANO,end=end,schema='ohlcv-1h',max_rows=1,guard=guard)
    minute=_aggregate((minute_source,),instrument_id=instrument_id,publisher_id=publisher_id,
        start=end-3600*NANO,end=end,schema='ohlcv-1m',max_rows=60,guard=guard)
    comparisons={key:Decimal(minute['ohlcv'][key])==Decimal(final_hour['ohlcv'][key]) for key in ('open','high','low','close')}
    comparisons['volume']=minute['ohlcv']['volume']==final_hour['ohlcv']['volume']
    comparison=('MATCH' if all(comparisons.values()) and minute['bucket_coverage']=='COMPLETE_OBSERVED_BUCKETS'
                and final_hour['bucket_coverage']=='COMPLETE_OBSERVED_BUCKETS' else 'MISMATCH_OR_INCOMPLETE_REQUIRES_REVIEW')
    settlement=settlement_at(statistics_source,instrument_id=instrument_id,publisher_id=publisher_id,
        session=window.session,as_of=settlement_as_of,reference_evidence=reference_evidence,guard=guard)
    evidence=dict(calendar_sha256=window.calendar_evidence.sha256,
        definition_source_sha256=definition['source_sha256'],definition_record_sha256=definition['raw_record_sha256'],
        hourly_records=daily['records'],minute_records=minute['records'],settlement=settlement)
    return dict(status='REAL_SESSION_PRICE_RECONSTRUCTION_RESEARCH_BLOCKED',research_qualified=False,
        real_futures_backtest_run=False,contract_id=window.contract_id,instrument_id=instrument_id,
        publisher_id=publisher_id,session=str(window.session),opens_at=iso_ns(start),closes_at=iso_ns(end),
        available_at=None,availability_status='UNKNOWN_HISTORICAL_OHLCV_PUBLICATION',
        quote_unit='SP500_index_points',usd_multiplier_per_quote_unit=multiplier,tick_size='0.25',
        ohlcv=daily['ohlcv'],coverage={key:daily[key] for key in ('actual_buckets','expected_buckets','missing_buckets','bucket_coverage')},
        last_hour_check=dict(status=comparison,fields_equal=comparisons,hourly=final_hour,minute=minute),
        settlement=settlement,settlement_reference_at=None,
        settlement_reference_status='TRADING_REFERENCE_DATE_IS_NOT_INTRADAY_PRICING_REFERENCE_TIME',
        derivation_sha256=canonical_hash(evidence),source_evidence=evidence,
        blockers=['HISTORICAL_OHLCV_PUBLICATION_UNKNOWN','SETTLEMENT_PRICING_REFERENCE_TIME_REQUIRED',
                  'FULL_SOURCE_CALENDAR_STATUS_AND_BOUNDARY_INTEGRATION_REVIEW_PENDING'])
