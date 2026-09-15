"""Fixed before loading research prices. No strategy search interface."""
from pathlib import Path
import json
from .runtime import canonical_hash, digest, write_json, utcnow

MARKETS = {
    'GOLD': {'class': 'PRECIOUS_METALS', 'signal': 'GC', 'execution': ['MGC', '1OZ']},
    'COPPER': {'class': 'INDUSTRIAL_METALS', 'signal': 'HG', 'execution': ['MHG']},
    'OIL': {'class': 'ENERGY', 'signal': 'CL', 'execution': ['MCL']},
    'CORN': {'class': 'AGRICULTURE', 'signal': 'ZC', 'execution': ['MZC']},
    'EURUSD': {'class': 'FX', 'signal': '6E', 'execution': ['M6E']},
    'SP500': {'class': 'EQUITY_INDEX', 'signal': 'ES', 'execution': ['MES']},
}

PROTOCOL = {
    'version': 'F0-PILOT-20260915',
    'source': 'User task Ben_F0_11500��Ԫ_��Ʒ���ڻ������븡ӯ�Ӳ�_С������_20260915.txt',
    'capital_usd': 11500, 'warmup': ['2021-01-01', '2021-12-31'],
    'descriptive': ['2022-01-01', '2023-12-31'], 'evaluation': ['2024-01-01', '2025-12-31'],
    'markets': MARKETS, 'versions': ['F0', 'F1'],
    'costs': [
        {'name': 'BASE_2T', 'slippage_ticks_per_side': 2, 'commission_multiple': 1},
        {'name': 'STRESS_4T', 'slippage_ticks_per_side': 4, 'commission_multiple': 1},
        {'name': 'DOUBLE_COMMISSION_4T', 'slippage_ticks_per_side': 4, 'commission_multiple': 2}],
    'missing_commission_assumption_usd_per_contract_side': 2,
    'matrix_max_accounts_per_margin_evidence_layer': 6,
    'margin': {'historical': 'REQUIRES_DATED_VERIFIED_TABLE',
               'assumed_base_initial_fraction': 0.10, 'assumed_stress_initial_fraction': 0.20,
               'assumed_maintenance_fraction_of_initial': 0.75,
               'assumed_and_historical_tables_separate': True},
    'entry': {'atr': 'WILDER', 'atr_period': 20, 'channel': 55, 'exclude_current': True,
              'condition': 'strict close outside prior channel', 'initial_stop_atr': 2,
              'fill': 'next full exchange session open proxy plus adverse slippage',
              'gap_crossed_stop': 'SKIP', 'adjust_stop_to_fit_size': False},
    'exit': {'channel': 20, 'exclude_current': True, 'signal_exit': 'next session open',
             'trail': 'highest/lowest completed holding close +/-2 current ATR, never loosen',
             'stop_gap': 'adverse open plus slippage', 'intraday_stop': 'daily stop proxy; not quote validation',
             'exit_before_adds': True, 'same_session_reverse': False},
    'risk': {'initial_campaign_fraction_including_base_roundtrip': 0.01,
             'initial_layer_margin_usd': 1000, 'add_layer_margin_usd': 1000,
             'campaign_margin_usd': 2500, 'max_markets': 4,
             'portfolio_margin_fraction': 0.35, 'max_absolute_notional_equity': 10,
             'portfolio_mark_to_stop_giveback_fraction': 0.05,
             'campaign_giveback_fraction': 0.03, 'gold_copper_giveback_fraction': 0.03,
             'negative_risk_offsets': False, 'fractional_contracts': False,
             'breach_reduction_priority': ['margin urgency descending', 'latest layer first', 'market code'],
             'maintenance_breach': 'record MARGIN_BREACH; reduce only when tradable'},
    'liquidity': {'lookback_complete_sessions': 20, 'median_volume_max_participation': 0.001,
                  'need_positive_previous_volume': True, 'missing_volume': 'UNKNOWN_NOT_ZERO',
                  'selection': 'smallest eligible verified notional; root lexical tie; one root per market',
                  'note': 'Engineering convention fixed before prices; no tuning to outcomes'},
    'pyramiding': {'F0_adds': 0, 'F1_adds': 2, 'max_layers': 3,
                  'max_add_quantity': 'q0', 'D0': 'first actual entry to original stop distance',
                  'trigger_multiples': [1, 2], 'stop_prerequisite_R': [0, 1],
                  'net_unrealized_positive': True, 'recheck_next_open': True,
                  'one_tier_per_completed_session': True,
                  'consume_tier_at_first_next_open_check_even_if_rejected': True},
    'allocation': ['existing reductions/exits', 'approved adds',
                   'new entries by prior breakout strength/ATR descending, market code tie'],
    'roll': {'deadline_sessions_before_earliest_boundary': 5,
             'boundaries': ['first_notice', 'last_trade', 'broker_forced_liquidation_if_applicable'],
             'early_volume_rule': 'next valid expiry volume > current for two prior completed sessions',
             'execute': 'next session old raw exit/new raw entry, both sides costs',
             'signal_link': 'causal additive known overlap only',
             'transform': ['stop', 'first_entry_anchor', 'trailing_extreme', 'channel history'],
             'roll_gap_is_pnl': False, 'missing_boundary': 'BLOCK_NEW_ENTRY'},
    'data': {'exchange_sessions': True, 'utc_natural_day_main_layer': False,
             'specific_raw_contract_execution': True, 'vendor_definition_and_exchange_specs': True,
             'settlement_distinct_from_close': True, 'publication_time_required': True,
             'download_received_time_not_historical_publication': True,
             'unknown_historical_receipt': 'UNKNOWN', 'pre_listing_synthetic_data': False,
             'missing_halt_limit_unknown_are_distinct': True},
    'accounting': {'shared_capital_per_account': True, 'futures_variation_margin': True,
                   'margin_is_not_expense_or_extra_asset': True, 'stock_T1': False,
                   'notional_financing_interest': False, 'cash_interest': 0,
                   'external_monthly_costs_usd': [0, 30, 150], 'inject_cash': False},
    'benchmarks': {'symbols': ['SPY', 'QQQ'], 'capital': 11500,
                   'entry': '2022-01-03 raw open', 'terminal': '2025-12-31 raw close',
                   'dividends': 'pay date close cash; next tradable session open integer reinvestment',
                   'order_fee_usd': 1, 'slippage_bps_per_side': 10,
                   'terminal_sale_in_primary_nav': False, 'terminal_sale_separate': True},
    'research_gate': {'min_markets_traded': 4, 'min_risk_classes': 3, 'min_months': 12,
                      'min_independent_campaigns': 60, 'insufficient': 'INSUFFICIENT_NOT_PASS_OR_FAIL',
                      'evaluation_net_profit_positive': True, 'profit_factor_min': 1.2,
                      'max_drawdown_fraction': 0.20, 'stress_cost_profit_positive': True,
                      'unexplained_margin_breaches': 0, 'max_one_market_positive_profit_share': 0.50,
                      'F1_help': 'profit>F0 and profit/absolute dollar drawdown>=F0; unusable if F0 loss or nonpositive denominator',
                      'uncertainty': 'common-date blocks; no IID trade bootstrap/p-value certificate',
                      'winner_sensitivity': 'disclose only; never delete real trades'},
    'operations': {'start_utc': '2026-09-15T10:47:39Z', 'deadline_utc': '2026-09-15T14:47:39Z',
                   'rss_limit_gib': 1.5, 'minimum_system_available_gib': 2,
                   'download_cash_budget_usd': 0, 'paid_models': False,
                   'live_broker': False, 'forward_account': False,
                   'B12_resume': False, 'stock_services_unchanged': True},
}


def freeze(output, attachment):
    value = dict(PROTOCOL, attachment_sha256=digest(attachment))
    path = Path(output) / 'FROZEN_PROTOCOL.json'
    if path.exists():
        old = json.loads(path.read_text(encoding='utf-8-sig'))
        value['frozen_at'] = old['frozen_at']
    else:
        value['frozen_at'] = utcnow().isoformat()
    value['protocol_sha256'] = canonical_hash(value)
    write_json(path, value, immutable=True)
    return value
