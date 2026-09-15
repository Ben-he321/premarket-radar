"""Isolated, cash-funded SPY/QQQ benchmark. No network or strategy imports.

Real inputs are existing source-pinned audit caches. Artificial fixtures belong
only in tests. This equity benchmark is not the futures accounting kernel.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path

CENT = Decimal("0.01")
COMMISSION = Decimal("1")
FRICTION = Decimal("0.001")
START = "2022-01-01"
END = "2025-12-31"
INITIAL = Decimal("11500")


def money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def digest_file(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False), encoding="utf-8")


def write_csv(path, records):
    if not records:
        return
    fields = list(dict.fromkeys(key for row in records for key in row))
    with Path(path).open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)


def purchase_amounts(price, quantity, commission=COMMISSION, friction=FRICTION):
    notional = money(Decimal(str(price)) * quantity)
    impact = money(Decimal(str(price)) * quantity * friction)
    return notional, impact, money(commission)


def affordable_quantity(cash, price, commission=COMMISSION, friction=FRICTION):
    price = Decimal(str(price))
    if price <= 0 or cash < commission:
        return 0
    # Rounding two components can save at most one cent. Binary search remains
    # bounded even for an artificial tiny-price fixture and finds the true max.
    low = 0
    high = max(1, int(((cash - commission + CENT) / (price * (1 + friction)))
                      .to_integral_value(rounding=ROUND_FLOOR)) + 1)
    while low + 1 < high:
        middle = (low + high) // 2
        if sum(purchase_amounts(price, middle, commission, friction)) <= cash:
            low = middle
        else:
            high = middle
    return low


def simulate(symbol, bars, distributions, initial=INITIAL, splits=()):
    """Consume complete ascending sessions; pay at close, reinvest later.

    Decimal cash is rounded only when an actual ledger amount is booked.
    An unpaid ex-date entitlement is an asset, never spendable cash.
    """
    if not bars:
        raise ValueError("NO_REAL_PRICE_ROWS")
    days = [str(row["date"]) for row in bars]
    if days != sorted(set(days)):
        raise ValueError("DUPLICATE_OR_UNSORTED_SESSIONS")
    events = defaultdict(list)
    seen = set()
    for row in distributions:
        ex_day = row["ex_date"]
        if not days[0] <= ex_day <= days[-1]:
            continue
        pay_day = row.get("payable_date")
        if not pay_day:
            raise ValueError("UNKNOWN_DIVIDEND_PAY_DATE")
        date.fromisoformat(pay_day)
        if pay_day < ex_day or ex_day in seen or ex_day not in days:
            raise ValueError("INVALID_OR_DUPLICATE_DISTRIBUTION")
        if Decimal(str(row["rate"])) < 0:
            raise ValueError("NEGATIVE_DIVIDEND")
        seen.add(ex_day)
        events[ex_day].append(row)
    split_events = defaultdict(list)
    for row in splits:
        split_events[row["date"]].append(row)
    cash, quantity = money(initial), 0
    pending, fills, rights, curve, split_log = [], [], [], [], []
    paid_total, fee_total, impact_total = Decimal("0"), Decimal("0"), Decimal("0")
    buy_next_open = True
    peak = money(initial)
    for day_index, row in enumerate(bars):
        day = days[day_index]
        open_price, close = Decimal(str(row["open"])), Decimal(str(row["close"]))
        if not open_price.is_finite() or not close.is_finite() or min(open_price, close) <= 0:
            raise ValueError("INVALID_PRICE")
        for split in split_events[day]:
            ratio = Decimal(str(split["ratio"]))
            new_quantity = Decimal(quantity) * ratio
            if ratio <= 0 or new_quantity != new_quantity.to_integral_value():
                raise ValueError("UNRESOLVED_FRACTIONAL_SPLIT_ENTITLEMENT")
            split_log.append(dict(symbol=symbol, date=day, before=quantity,
                                  after=int(new_quantity), ratio=float(ratio)))
            quantity = int(new_quantity)
        # Entitlement is before ex-date open, hence no entitlement for a new buy.
        for event in events[day]:
            amount = money(Decimal(quantity) * Decimal(str(event["rate"])))
            item = dict(symbol=symbol, ex_date=day, payable_date=event["payable_date"],
                        entitled_shares=quantity, rate=float(event["rate"]),
                        amount=float(amount), received_session=None,
                        source_url=event.get("source_url", "UNKNOWN"),
                        verification=event.get("verification", "UNKNOWN"))
            rights.append(item)
            pending.append((amount, item))
        if buy_next_open:
            q = affordable_quantity(cash, open_price)
            if q:
                gross, impact, fee = purchase_amounts(open_price, q)
                cash -= gross + impact + fee
                quantity += q
                fee_total += fee
                impact_total += impact
                fills.append(dict(symbol=symbol, date=day, phase="OPEN", side="BUY",
                                  reason="INITIAL" if day_index == 0 else "PAID_DIVIDEND_REINVESTMENT",
                                  quantity=q, raw_price=float(open_price),
                                  gross=float(gross), friction_usd=float(impact),
                                  commission_usd=float(fee), cash_after=float(cash)))
            buy_next_open = False
        # Non-session payment dates become available only after this close.
        still_pending = []
        for amount, item in pending:
            if item["payable_date"] <= day:
                cash += amount
                paid_total += amount
                item["received_session"] = day
                if amount:
                    buy_next_open = True
            else:
                still_pending.append((amount, item))
        pending = still_pending
        receivable = sum((item[0] for item in pending), Decimal("0"))
        market_value = money(close * quantity)
        equity = cash + market_value + receivable
        peak = max(peak, equity)
        if cash < 0:
            raise AssertionError("NEGATIVE_CASH")
        exit_gross, exit_impact, exit_fee = (purchase_amounts(close, quantity)
                                            if quantity else (Decimal("0"),) * 3)
        curve.append(dict(symbol=symbol, date=day, cash=float(cash), shares=quantity,
                          raw_close=float(close), market_value=float(market_value),
                          dividend_receivable=float(receivable),
                          paid_dividends_cumulative=float(paid_total),
                          equity=float(equity), commission_cumulative=float(fee_total),
                          friction_cumulative=float(impact_total),
                          drawdown=float(equity / peak - 1),
                          hypothetical_exit_cost=float(exit_impact + exit_fee),
                          equity_if_terminal_sale=float(equity - exit_impact - exit_fee)))
    return dict(symbol=symbol, initial=float(initial), curve=curve, fills=fills,
                distributions=rights, splits=split_log,
                reconcile=reconcile(initial, curve, fills, rights, split_log))


def reconcile(initial, curve, fills, rights, split_log):
    """Independent amounts reconstruction, not simulation state reuse."""
    cash, quantity = money(initial), 0
    by_fill, by_paid, by_ex, by_split = (defaultdict(list) for _ in range(4))
    for item in fills:
        by_fill[item["date"]].append(item)
    for item in rights:
        by_ex[item["ex_date"]].append(item)
        if item["received_session"]:
            by_paid[item["received_session"]].append(item)
    for item in split_log:
        by_split[item["date"]].append(item)
    receivable = Decimal("0")
    for row in curve:
        day = row["date"]
        for event in by_split[day]:
            quantity = int(quantity * Decimal(str(event["ratio"])))
        for event in by_ex[day]:
            if quantity != event["entitled_shares"]:
                raise AssertionError("ENTITLEMENT_SHARE_MISMATCH")
            receivable += money(event["amount"])
        for fill in by_fill[day]:
            gross = money(Decimal(str(fill["raw_price"])) * fill["quantity"])
            impact = money(Decimal(str(fill["raw_price"])) * fill["quantity"] * FRICTION)
            if (gross != money(fill["gross"]) or impact != money(fill["friction_usd"])
                    or money(fill["commission_usd"]) != money(COMMISSION)):
                raise AssertionError("FILL_COST_FORMULA_MISMATCH")
            cash -= sum(money(fill[key]) for key in ("gross", "friction_usd", "commission_usd"))
            quantity += fill["quantity"]
        for event in by_paid[day]:
            cash += money(event["amount"])
            receivable -= money(event["amount"])
        equity = cash + money(Decimal(str(row["raw_close"])) * quantity) + receivable
        if (cash != money(row["cash"]) or quantity != row["shares"] or
                receivable != money(row["dividend_receivable"]) or
                equity != money(row["equity"])):
            raise AssertionError("INDEPENDENT_RECONCILIATION_FAILED_" + day)
    return dict(status="PASS", sessions=len(curve), every_session_checked=True,
                method="Independent cash + raw-close integer shares + unpaid entitlements",
                cash=float(cash), shares=quantity, dividend_receivable=float(receivable),
                equity=float(equity), negative_cash_sessions=0)


def performance(account, start=START, end=END, label="FULL"):
    selected = [row for row in account["curve"] if start <= row["date"] <= end]
    previous = [row for row in account["curve"] if row["date"] < start]
    if not selected:
        raise ValueError("NO_PERIOD_ROWS")
    beginning = previous[-1]["equity"] if previous else account["initial"]
    ending = selected[-1]["equity"]
    peak, minimum_dd, maximum_dd_usd = beginning, 0.0, 0.0
    for row in selected:
        peak = max(peak, row["equity"])
        minimum_dd = min(minimum_dd, row["equity"] / peak - 1)
        maximum_dd_usd = max(maximum_dd_usd, peak - row["equity"])
    years = ((date.fromisoformat(end) - date.fromisoformat(start)).days + 1) / 365.2425
    selected_fills = [row for row in account["fills"] if start <= row["date"] <= end]
    return dict(symbol=account["symbol"], period=label, nominal_start=start,
                nominal_end=end, first_session=selected[0]["date"], last_session=selected[-1]["date"],
                starting_equity=beginning, ending_equity=ending,
                net_profit=round(ending - beginning, 2), cumulative_return=ending / beginning - 1,
                annualized_return=(ending / beginning) ** (1 / years) - 1,
                max_drawdown=minimum_dd, max_drawdown_usd=round(maximum_dd_usd, 2),
                ending_cash=selected[-1]["cash"], ending_shares=selected[-1]["shares"],
                unpaid_dividends=selected[-1]["dividend_receivable"],
                ending_equity_after_hypothetical_sale=selected[-1]["equity_if_terminal_sale"],
                terminal_sale_cost_in_main_equity=False,
                actual_buy_orders=len(selected_fills), actual_sell_orders=0,
                transaction_cost=sum(row["friction_usd"] + row["commission_usd"] for row in selected_fills),
                fixed_project_fee=0, cash_interest=0, status="EXECUTED_REAL_CACHE_RESTRICTED")


def _real_input(symbol, source_base):
    """Verify only two small instrument partitions, never stock strategy data."""
    import pandas as pd
    import pandas_market_calendars as mcal

    prior = source_base / "economics-audit-20260912"
    supplement = source_base / "economics-supplement-spy-150-20260912"
    action_path = source_base / "watchlist-research-v1" / "actions" / f"{symbol}.json"
    if symbol == "SPY":
        run = supplement / "runs/002"
        quotes_path, distributions_path = run / "spy_quote_evidence.csv", run / "spy_distributions.json"
        quality_path = run / "spy_quality.json"
        objects = read_json(quality_path)["objects"]
        issuer_path = supplement / "sources/spy_issuer_rows.json"
        issuer = read_json(issuer_path)
        xlsx = supplement / "sources/spdr-etf-historical-distributions.xlsx"
        if digest_file(xlsx) != issuer["xlsx_sha256"]:
            raise ValueError("SPY_ISSUER_SOURCE_HASH_MISMATCH")
        auxiliary = [issuer_path, xlsx]
    else:
        run = prior / "runs/001"
        quotes_path, distributions_path = run / "qqq_quote_evidence.csv", run / "qqq_unique_distributions.json"
        quality_path = run / "source_manifest.json"
        objects = [row for row in read_json(quality_path)["objects"]
                   if row["symbol"] == symbol and row["adjustment"] == "raw"]
        auxiliary = [prior / "qqq_official_overrides.json"]
    input_files = [quotes_path, distributions_path, quality_path, action_path, *auxiliary]
    inventory = [dict(path=str(path), sha256=digest_file(path), bytes=path.stat().st_size)
                 for path in input_files]
    quotes = pd.read_csv(quotes_path)
    quotes = quotes[(quotes.date >= START) & (quotes.date <= END)].copy()
    if quotes.date.duplicated().any():
        raise ValueError("DUPLICATE_PRICE_DATES")
    quotes = quotes.sort_values("date").reset_index(drop=True)
    days = [str(item) for item in mcal.get_calendar("NYSE").valid_days(START, END).date]
    if quotes.date.tolist() != days:
        raise ValueError("MISSING_OR_NONSESSION_PRICES")
    numeric = quotes[["open", "high", "low", "close", "volume"]]
    if (numeric.isna().any().any() or not numeric.map(math.isfinite).all().all()
            or not (numeric[["open", "high", "low", "close"]] > 0).all().all()
            or not (quotes.volume >= 0).all()
            or not (quotes.high >= quotes[["open", "low", "close"]].max(axis=1)).all()
            or not (quotes.low <= quotes[["open", "high", "close"]].min(axis=1)).all()):
        raise ValueError("INVALID_OHLCV")
    partition_frames = []
    for item in objects:
        if item.get("start", START) > END or item.get("end", END) < START:
            continue
        path = Path(item["path"])
        if not path.is_absolute():
            path = source_base / "watchlist-research-v1" / path
        if digest_file(path) != item["sha256"]:
            raise ValueError("RAW_PARTITION_HASH_MISMATCH")
        frame = pd.read_parquet(path, filters=[("trade_date", ">=", START), ("trade_date", "<=", END)])
        if not frame.empty:
            partition_frames.append(frame)
        inventory.append(dict(path=str(path), sha256=item["sha256"], bytes=path.stat().st_size,
                              retrieved_at=item.get("retrieved_at", "UNKNOWN")))
    if not partition_frames:
        raise ValueError("NO_PINNED_RAW_PARTITIONS")
    actual = pd.concat(partition_frames, ignore_index=True).sort_values("trade_date")
    if actual.trade_date.duplicated().any() or actual.trade_date.tolist() != days:
        raise ValueError("PINNED_PARTITION_COVERAGE_MISMATCH")
    for column in ("open", "high", "low", "close", "volume"):
        if not ((actual[column].to_numpy() - quotes[column].to_numpy()).__abs__() < 1e-8).all():
            raise ValueError("AUDIT_EXTRACT_DIFFERS_FROM_PINNED_RAW_" + column)
    corporate = read_json(action_path)
    if corporate["status"] != "ACCESS_OK" or corporate["start"] > START or corporate["end"] < END:
        raise ValueError("CORPORATE_ACTION_SOURCE_UNAVAILABLE")
    # A new event type needs explicit handling, never silently passes as no split.
    for kind, records in corporate["data"].items():
        if kind != "cash_dividends" and records:
            raise ValueError("CORPORATE_ACTION_REQUIRES_REVIEW_" + kind)
    distributions = [row for row in read_json(distributions_path)
                     if START <= row["ex_date"] <= END]
    if not distributions:
        raise ValueError("NO_DISTRIBUTION_EVIDENCE")
    quality = dict(symbol=symbol, sessions=len(days), first_session=days[0], last_session=days[-1],
                   missing_sessions=0, duplicate_sessions=0, invalid_ohlcv=0,
                   distributions=len(distributions), raw_partition_extract_equality="PASS",
                   provider_reported_splits=0, independent_split_absence_verification="UNKNOWN",
                   dividend_evidence_levels=sorted({row.get("verification", "UNKNOWN") for row in distributions}),
                   source="Existing Alpaca SIP raw cache; existing audited distribution overlays",
                   restrictions=["Daily OHLC proxy fills, not independently verified auction executions",
                                 "Corporate-action completeness remains provider-only except cited issuer distributions",
                                 "No investor-specific tax, withholding, FX or cash interest"])
    return quotes.to_dict("records"), distributions, inventory, quality


def run(output, source_base):
    output = Path(output).resolve()
    source_base = Path(source_base).resolve()
    protected = (source_base / "economics-audit-20260912", source_base / "economics-supplement-spy-150-20260912",
                 source_base / "watchlist-research-v1", source_base / "watchlist-v1_3_1-evidence-forward")
    if any(output == item or output.is_relative_to(item) for item in protected):
        raise ValueError("OUTPUT_MUST_BE_ISOLATED")
    output.mkdir(parents=True, exist_ok=True)
    protocol = dict(frozen_at=datetime.now(timezone.utc).isoformat(), start=START, end=END,
                    initial_usd=float(INITIAL), strategy="Buy maximum cash-affordable integer shares once, reinvest only after dividends pay",
                    entry="First NYSE session raw open", terminal="Last NYSE session raw close; no main-account sale",
                    commission_per_order_usd=float(COMMISSION), one_way_friction=float(FRICTION),
                    pay_cash="Pay-date close, next NYSE close if holiday", reinvest="Next session open after pay credit",
                    capital_gain_and_dividend_tax="NOT_INCLUDED", cash_interest=0, project_fixed_fee=0,
                    evaluation="2024-2025 continuous path, no reset", new_market_downloads=0,
                    stock_strategy_run=False, forward_ledger_access=False)
    protocol_path = output / "BENCHMARK_PROTOCOL.json"
    if protocol_path.exists():
        previous = read_json(protocol_path)
        if {k: v for k, v in previous.items() if k != "frozen_at"} != {k: v for k, v in protocol.items() if k != "frozen_at"}:
            raise ValueError("BENCHMARK_PROTOCOL_ALREADY_FROZEN_DIFFERENTLY")
    else:
        write_json(protocol_path, protocol)
    started = datetime.now(timezone.utc)
    comparisons, annuals, inventory, checks, quality = [], [], [], [], []
    for symbol in ("SPY", "QQQ"):
        try:
            bars, distributions, inputs, qualification = _real_input(symbol, source_base)
            inventory.extend(inputs)
            account = simulate(symbol, bars, distributions)
            for key in ("curve", "fills", "distributions"):
                write_csv(output / f"{symbol}_{key}.csv", account[key])
            quality.append(qualification)
            checks.append(dict(symbol=symbol, **account["reconcile"]))
            for label, start, end in (("FULL", START, END), ("DEVELOPMENT", START, "2023-12-31"),
                                      ("EVALUATION", "2024-01-01", END)):
                comparisons.append(performance(account, start, end, label))
            for year in range(2022, 2026):
                annuals.append(performance(account, f"{year}-01-01", f"{year}-12-31", str(year)))
        except Exception as exc:
            checks.append(dict(symbol=symbol, status="BLOCKED", reason=str(exc), exception_type=type(exc).__name__))
            comparisons.append(dict(symbol=symbol, period="FULL", status="DATA_BLOCKED_NOT_EXECUTED", reason=str(exc)))
    preserved = all(digest_file(row["path"]) == row["sha256"] for row in inventory)
    if not preserved:
        raise AssertionError("SOURCE_CHANGED_DURING_BENCHMARK")
    write_csv(output / "benchmark_comparison.csv", comparisons)
    write_csv(output / "annual_performance.csv", annuals)
    write_json(output / "source_manifest.json", dict(source_files=inventory, sources_unchanged=preserved,
                                                    protocol_sha256=digest_file(protocol_path),
                                                    benchmark_code_sha256=digest_file(__file__),
                                                    methodology_sha256=digest_file(Path(__file__).parents[2] / "docs/futures_f0/BENCHMARK_METHOD.md")))
    write_json(output / "data_quality.json", quality)
    result = dict(started_at=started.isoformat(), completed_at=datetime.now(timezone.utc).isoformat(),
                  status="EXECUTED_REAL_CACHE_RESTRICTED" if len(quality) == 2 else "PARTIAL_OR_BLOCKED",
                  results=checks, source_files_unchanged=preserved, network_calls=0,
                  main_equity_excludes_hypothetical_terminal_sale_cost=True,
                  futures_backtest_evidence=False,
                  old_audit_results_modified=False, paid_services_used=False)
    write_json(output / "verification.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-base", required=True, type=Path)
    arguments = parser.parse_args()
    run(arguments.output, arguments.source_base)
