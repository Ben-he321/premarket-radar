"""Typed inputs for the isolated futures daily-bar research engine.

This module contains no broker client and no synthetic-data fallback. Historical
availability and actual download receipt are intentionally different fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable


@dataclass(frozen=True)
class ContractSpec:
    contract_id: str
    market: str
    root: str
    multiplier: float
    tick_size: float
    listed: date
    last_trade: date
    safe_exit_session: date | None
    exchange: str = "UNKNOWN"
    currency: str = "USD"
    quote_unit: str = "UNKNOWN"
    verified: bool = False
    calendar_verified: bool = False
    vendor_definition_verified: bool = False
    boundary_verified: bool = False
    source: str = "UNKNOWN"
    initial_margin: float | None = None
    maintenance_margin: float | None = None
    margin_asof: datetime | None = None
    margin_valid_until: date | None = None
    commission_per_side: float | None = None
    commission_source: str = "ASSUMED_2_USD_PER_CONTRACT_SIDE"

    @property
    def tick_value(self) -> float:
        return self.multiplier * self.tick_size


@dataclass(frozen=True)
class SessionBar:
    contract_id: str
    session: date
    opens_at: datetime
    closes_at: datetime
    available_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None
    source_hash: str
    received_at: datetime | None = None
    settlement: float | None = None
    settlement_available_at: datetime | None = None
    status: str = "QUALIFIED"
    # Halt/limit/unknown execution must be supplied explicitly by qualification.
    tradable_open: bool = True
    tradable_stop: bool = True
    session_verified: bool = False
    is_mock: bool = False
    # Supplied by the verified exchange calendar, not weekday arithmetic.
    next_session: date | None = None
    settlement_reference_at: datetime | None = None
    # A locally reconstructed capture-time prefix has an internal simulation
    # clock, not an observed publication by the vendor. Keep all clocks distinct.
    availability_basis: str = "SUPPLIER_PUBLICATION"
    input_cutoff: datetime | None = None
    internal_calculated_at: datetime | None = None
    supplier_published_at: datetime | None = None
    temporal_evidence_hash: str | None = None


@dataclass(frozen=True)
class RollInstruction:
    """A decision supported by data available before the execution session.

    ``execution_offset`` is previous known new close minus old close, in raw
    execution quote units. ``signal_offset`` similarly converts the stored
    standard-contract indicator series. Neither is the future opening gap.
    """
    market: str
    old_contract: str
    new_contract: str
    known_at: datetime
    execution_offset: float
    reason: str
    evidence_hash: str
    new_signal_contract: str | None = None
    signal_offset: float = 0.0


@dataclass(frozen=True)
class MarketDay:
    market: str
    signal: SessionBar
    execution: tuple[SessionBar, ...]
    roll: RollInstruction | None = None
    # Verified close-to-close mapping must share the same quote units.
    mapping_verified: bool = False
    mapping_source: str = "UNKNOWN"


@dataclass(frozen=True)
class EngineConfig:
    version: str = "F0"
    initial_equity: float = 11500.0
    slippage_ticks: int = 2
    commission_multiplier: int = 1
    margin_scenario: str = "ASSUMED_MARGIN_10_PERCENT"
    initial_margin_fraction: float = 0.10
    maintenance_fraction_of_initial: float = 0.75
    allow_mock: bool = False
    trading_start: date = date(2022, 1, 1)
    trading_end: date = date(2025, 12, 31)
    evaluation_start: date = date(2024, 1, 1)
    # These are protocol constants, not a search API.
    atr_period: int = 20
    entry_period: int = 55
    exit_period: int = 20
    stop_atr: float = 2.0
    initial_risk_fraction: float = 0.01
    layer_margin_cap: float = 1000.0
    campaign_margin_cap: float = 2500.0
    portfolio_margin_fraction: float = 0.35
    gross_leverage_cap: float = 10.0
    max_markets: int = 4
    total_giveback_fraction: float = 0.05
    campaign_giveback_fraction: float = 0.03
    metals_giveback_fraction: float = 0.03
    liquidity_lookback: int = 20
    participation_fraction: float = 0.001


@dataclass
class Layer:
    quantity: int
    entry: float
    settlement_basis: float
    created_at: datetime
    tier: int
    settled_through: datetime | None = None
    realized_roll_per_contract: float = 0.0
    fees_per_contract: float = 0.0


@dataclass
class Campaign:
    campaign_id: str
    market: str
    contract_id: str
    direction: int
    layers: list[Layer]
    first_entry: float
    initial_stop: float
    stop: float
    d0: float
    q0: int
    highest_close: float
    lowest_close: float
    opened_at: datetime
    mark: float
    mark_at: datetime
    realized_gross: float = 0.0
    fees: float = 0.0
    consumed_tiers: set[int] = field(default_factory=set)
    tier_one_checked_session: date | None = None
    exit_pending: str | None = None
    last_added_at: datetime | None = None
    pending_stop: float | None = None
    pending_stop_known_at: datetime | None = None
    completed_holding_sessions: int = 0

    @property
    def quantity(self) -> int:
        return sum(layer.quantity for layer in self.layers)


@dataclass(frozen=True)
class Intent:
    market: str
    direction: int
    signal_session: date
    known_at: datetime
    signal_close: float
    atr: float
    stop: float
    strength: float
    signal_hash: str
    # Prior-session execution closes establish standard-to-micro basis.
    execution_closes: tuple[tuple[str, float], ...]
    tier: int = 0
    execution_session: date | None = None


@dataclass
class EngineResult:
    status: str = "NOT_RUN"
    trades: list[dict] = field(default_factory=list)
    campaigns: list[dict] = field(default_factory=list)
    rolls: list[dict] = field(default_factory=list)
    daily_equity: list[dict] = field(default_factory=list)
    margin_path: list[dict] = field(default_factory=list)
    skips: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    input_hashes: list[str] = field(default_factory=list)
    processed_sessions: int = 0
    open_positions: list[dict] = field(default_factory=list)
    reconciliation: dict = field(default_factory=dict)
    coverage_status: str = "NOT_RUN"
    coverage_issues: list[dict] = field(default_factory=list)
