"""Observed contract definitions compared with the frozen exchange registry.

Matching tick/quantity fields is only one qualification layer. This module never
creates engine ContractSpec objects or certifies historical trading eligibility.
"""
from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from .data import RAW_SYMBOL
from .runtime import digest, write_json
from .vendor import timestamp_ns


EXCHANGES = {'CME': 'XCME', 'CBOT': 'XCBT', 'COMEX': 'XCEC', 'NYMEX': 'XNYM'}
UNITS = {'troy_ounce': 'TRYOZ', 'pound': 'LBS', 'barrel': 'BBL',
         'bushel': 'BU', 'EUR': 'EUR', 'index_point': 'IPNT'}


def compare_definition(record, registry_row):
    """Compare exact units, keeping listing, margin and calendar unknowns apart."""
    symbol = record.get('raw_symbol', '')
    match = RAW_SYMBOL.fullmatch(symbol)
    if not match or match.group(1) != registry_row['root']:
        raise ValueError('CONTRACT_ROOT_REGISTRY_MISMATCH')
    if record.get('parse_status') != 'PARSED_VENDOR_RECORD':
        raise ValueError('PARSED_DEFINITION_REQUIRED')
    row = registry_row
    checks = {
        'outright_identity': record['identity_status'] == 'OUTRIGHT_FUTURE_CANDIDATE',
        'exchange': record['exchange'] == EXCHANGES[row['exchange']],
        'currency': record['currency'] == row['currency'],
        'quantity_unit': record['unit_of_measure'] == UNITS[row['contract_unit']],
        'quantity': (record['unit_of_measure_qty'] is not None and
                     Decimal(record['unit_of_measure_qty']) == Decimal(row['contract_size'])),
        'tick': (record['min_price_increment'] is not None and
                 Decimal(record['min_price_increment']) == Decimal(row['tick_in_quote_units'])),
        'registry_tick_value': Decimal(row['usd_multiplier_per_quote_unit']) *
                              Decimal(row['tick_in_quote_units']) == Decimal(row['tick_value_usd']),
    }
    # Corn is cents per bushel, not USD/bushel; quantity alone is NOT multiplier.
    unit_usd = Decimal('0.01') if row['quote_unit'] == 'US_cents_per_bushel' else Decimal(1)
    checks['quantity_to_usd_multiplier'] = (Decimal(row['contract_size']) * unit_usd ==
                                          Decimal(row['usd_multiplier_per_quote_unit']))
    unknowns = ['DATED_SESSION_CALENDAR', 'NOTICE_AND_SAFE_EXIT_BOUNDARY',
                'HISTORICAL_MARGIN', 'HISTORICAL_COMMISSION', 'HISTORICAL_OHLCV_PUBLICATION']
    return dict(
        contract_id=symbol, root=row['root'], market=row['market'],
        vendor='Databento GLBX.MDP3', vendor_instrument_id=record['instrument_id'],
        publisher_id=record['publisher_id'], vendor_exchange=record['exchange'],
        exchange=row['exchange'], currency=record['currency'],
        observed_definition_at=record['ts_recv'], vendor_activation=record['activation'],
        activation_is_verified_first_trade=False,
        verified_specific_contract_first_trade='UNKNOWN',
        root_first_trade_date=row['root_first_trade_date'],
        vendor_expiration=record['expiration'],
        expiry_boundary_status='OBSERVED_VENDOR_FIELD_NOT_COMPLETE_NOTICE_CALENDAR',
        quote_unit=row['quote_unit'], unit_of_measure=record['unit_of_measure'],
        vendor_quantity=record['unit_of_measure_qty'],
        vendor_min_price_increment=record['min_price_increment'],
        usd_multiplier_per_quote_unit=row['usd_multiplier_per_quote_unit'],
        tick_value_usd=row['tick_value_usd'],
        vendor_display_factor=record['display_factor'], display_factor_applied_to_ohlcv=False,
        vendor_min_price_increment_amount=record['min_price_increment_amount'],
        min_price_increment_amount_used_as_tick_value=False,
        checks=checks, unit_comparison='MATCH' if all(checks.values()) else 'MISMATCH',
        remaining_unknown=unknowns, research_qualified=False,
        delivery_type=row['delivery_type'],
        broker_boundary_status='UNKNOWN_NO_BROKER_BOUNDARY_EVIDENCE',
        broker_scope='NOT_CONNECTED_RESEARCH_ONLY', live_execution_eligible=False,
        spec_source_url=row['spec_source_url'], launch_source_url=row['launch_source_url'],
        raw_record_sha256=record['raw_record_sha256'], source_sha256=record['source_sha256'],
        source_line=record['source_line'])


def snapshot_contracts(source, registry_path, as_of, output, *, guard=None):
    """One bounded historical definition snapshot; never backdate later updates."""
    receipt = source.validate()
    if receipt['request']['schema'] != 'definition':
        raise ValueError('DEFINITION_SOURCE_REQUIRED')
    if receipt.get('mock') or receipt.get('is_mock'):
        raise ValueError('MOCK_SNAPSHOT_NOT_RESEARCH_DATA')
    cutoff = timestamp_ns(as_of)
    with Path(registry_path).open(encoding='utf-8-sig', newline='') as stream:
        registry = {row['root']: row for row in csv.DictReader(stream)}
    versions = {}
    for row in source.records(guard=guard):
        if row['parse_status'] != 'PARSED_VENDOR_RECORD':
            raise ValueError('QUARANTINED_DEFINITION_REQUIRES_REVIEW')
        if row['ts_recv_ns'] > cutoff:
            continue
        key = row['raw_symbol']
        if key not in versions and len(versions) >= 18:
            raise ValueError('CONTRACT_SNAPSHOT_SIZE_BOUNDARY')
        versions[key] = row
    rows = []
    for symbol in receipt['request']['symbols'].split(','):
        row = versions.get(symbol)
        if row is None:
            rows.append(dict(contract_id=symbol, unit_comparison='UNKNOWN_NO_PRIOR_DEFINITION',
                             research_qualified=False))
            continue
        if row['security_update_action'] not in ('A', 'M'):
            rows.append(dict(contract_id=symbol, unit_comparison='UNKNOWN_INACTIVE_DEFINITION',
                             research_qualified=False))
            continue
        rows.append(compare_definition(row, registry[RAW_SYMBOL.fullmatch(symbol).group(1)]))
    value = dict(as_of=as_of, source_receipt_sha256=digest(source.receipt_path),
                 source_sha256=receipt['sha256'], registry_sha256=digest(registry_path),
                 matched_contracts=sum(r['unit_comparison'] == 'MATCH' for r in rows),
                 requested_contracts=len(rows), research_qualified_contracts=0, contracts=rows)
    write_json(output, value, immutable=True)
    return value
