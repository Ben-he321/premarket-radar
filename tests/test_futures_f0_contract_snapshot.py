"""Unit-comparison fixtures; no generated market data enters research storage."""
import csv
from pathlib import Path

import pytest

from src.futures_f0.config import ROOT
from src.futures_f0.contract_snapshot import compare_definition, EXCHANGES, UNITS


def fixture(root):
    with (ROOT / 'docs/futures_f0/contract_registry.csv').open(encoding='utf-8-sig', newline='') as f:
        reg = next(r for r in csv.DictReader(f) if r['root'] == root)
    row = dict(raw_symbol=root + 'K5', parse_status='PARSED_VENDOR_RECORD',
        identity_status='OUTRIGHT_FUTURE_CANDIDATE', exchange=EXCHANGES[reg['exchange']],
        currency='USD', unit_of_measure=UNITS[reg['contract_unit']],
        unit_of_measure_qty=reg['contract_size'], min_price_increment=reg['tick_in_quote_units'],
        instrument_id=1, publisher_id=1, ts_recv='2025-03-03T00:00:00Z',
        activation='2025-01-10T22:30:00Z', expiration='2025-05-14T17:01:00Z',
        display_factor='0.01', min_price_increment_amount='1',
        raw_record_sha256='a' * 64, source_sha256='b' * 64, source_line=2)
    return row, reg


@pytest.mark.parametrize('root,multiplier,tick', [('ZC', '50', '12.5'), ('MZC', '5', '2.5')])
def test_corn_cents_not_hundred_times_pnl(root, multiplier, tick):
    row, reg = fixture(root)
    value = compare_definition(row, reg)
    assert value['unit_comparison'] == 'MATCH'
    assert value['usd_multiplier_per_quote_unit'] == multiplier
    assert value['tick_value_usd'] == tick
    assert value['research_qualified'] is False
    assert value['min_price_increment_amount_used_as_tick_value'] is False


def test_registry_dollar_cents_mismatch_is_not_accepted():
    row, reg = fixture('ZC')
    reg['usd_multiplier_per_quote_unit'] = '5000'
    value = compare_definition(row, reg)
    assert value['unit_comparison'] == 'MISMATCH'
    assert value['checks']['quantity_to_usd_multiplier'] is False


@pytest.mark.parametrize('field,new_value', [('min_price_increment', '0.05'),
                                           ('unit_of_measure_qty', '500'), ('currency', 'EUR')])
def test_actual_field_mismatch_remains_visible(field, new_value):
    row, reg = fixture('MES')
    row[field] = new_value
    assert compare_definition(row, reg)['unit_comparison'] == 'MISMATCH'


def test_activation_is_never_promoted_to_first_trade_or_execution_permission():
    row, reg = fixture('1OZ')
    value = compare_definition(row, reg)
    assert value['vendor_activation'] == '2025-01-10T22:30:00Z'
    assert value['root_first_trade_date'] == '2025-01-13'
    assert value['verified_specific_contract_first_trade'] == 'UNKNOWN'
    assert value['display_factor_applied_to_ohlcv'] is False
    assert value['live_execution_eligible'] is False
    assert value['broker_boundary_status'] == 'UNKNOWN_NO_BROKER_BOUNDARY_EVIDENCE'
    assert 'HISTORICAL_MARGIN' in value['remaining_unknown']
