"""Isolated frozen SPY/QQQ passive accounts; never writes strategy ledgers.

The pure accounting function accepts explicit inputs. CLI execution is separate
and obtains only missing benchmark opening minutes in its own cache directory.
No all-adjusted price is used for an order or cash-dividend account.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import math
from pathlib import Path

import pandas as pd
import pandas_market_calendars as mcal

from src.ben_b1.ledger import money
from .runtime import ROOT, START, END, read, write, sha, utc

VERSION = "B12_PASSIVE_INTEGER_DIVIDEND_V1"
CACHE = Path.home() / "BenAITradingData" / "watchlist-research-v1"
IDENTITIES = {"SPY": "cf2e42f7a7757218", "QQQ": "9f5a75ca5ef78c26"}
SOURCES = ["https://docs.alpaca.markets/us/docs/market-data-faq",
           "https://alpaca.markets/learn/stock-minute-bars",
           "https://docs.alpaca.markets/us/reference/stockbars"]
ASSUMPTIONS = {
    "initial_capital_usd": 5500, "commission_per_executed_order_usd": 1,
    "buy_execution_friction_fraction": .001, "integer_shares": True,
    "initial_buy": "First NYSE session RTH opening minute open; model execution, not actual fill",
    "dividend_entitlement": "Shares held before ex-date; cash rounded to cents HALF_UP per distribution",
    "dividend_cash": "Actual payable calendar date; next strictly later NYSE session open reinvests available cash once",
    "reinvestment": "Whole shares; zero quantity means no order and no commission; no repeated daily attempts",
    "cash_interest": 0, "terminal_sale": False,
    "terminal_mark": "Alpaca raw consolidated daily close, same basis as strategy; primary-exchange official auction independently UNKNOWN",
    "receivables": "Entitled but unpaid distributions included in end equity, separately from cash",
    "all_adjusted_reference": "Fractional no-fee vendor total-return reference only; never combined with cash distributions",
    "all_adjusted_entry": "Raw first RTH minute open times same-session all-close/raw-close adjustment factor",
    "fund_management_fee": "Already reflected in fund NAV; no additional deduction",
    "taxes_and_withholding": "Excluded, not an after-tax account",
    "fixed_project_fees": "Not imposed on passive benchmarks",
    "historical_network_received_at": "UNKNOWN",
    "opening_minute_available_at": "After minute end; using its open is a retrospective execution model, not proven live order availability",
    "comparison_limit": "Coverage-limited strategy replay versus complete benchmark is not independent evidence of superiority",
}


def exchange_schedule(start=START, end=END):
    return mcal.get_calendar("NYSE").schedule(start_date=start, end_date=end)


def positive(value):
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def simulate_hold(symbol, sessions, raw_close, all_close, rth_open, dividends,
                  capital=5500., commission=1., friction=.001):
    """Deterministic whole-share accounting, with no IO or synthesized inputs.

    sessions is the explicit NYSE session-date list. Prices map date to a raw
    price, except all_close which is ONLY used for a separate reference series.
    Missing executable price gates the affected order; no later backfill.
    """
    if not sessions or sessions != sorted(set(sessions)):
        raise ValueError("SESSIONS_MUST_BE_UNIQUE_SORTED")
    if capital <= 0 or commission < 0 or not 0 <= friction < 1:
        raise ValueError("INVALID_FROZEN_FINANCE_INPUT")
    first, last = sessions[0], sessions[-1]
    actions, seen = [], {}
    for value in dividends:
        if value.get("symbol") != symbol:
            continue
        ex = str(value.get("ex_date", ""))[:10]
        if not first <= ex <= last:
            continue  # Payment during ownership alone does not confer entitlement.
        pay = str(value.get("payable_date", ""))[:10]
        try:
            date.fromisoformat(ex); date.fromisoformat(pay)
            rate = float(value["rate"])
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("DIVIDEND_DATE_OR_RATE_UNKNOWN") from exc
        if not positive(rate) or pay < ex or ex not in sessions:
            raise ValueError("INVALID_DIVIDEND_INPUT")
        identity = value.get("id") or f"{symbol}:{ex}:{pay}:{rate}"
        signature = (ex, pay, rate)
        if identity in seen:
            if seen[identity] != signature:
                raise ValueError("CONFLICTING_DIVIDEND_ID")
            continue
        seen[identity] = signature
        actions.append({"id": identity, "ex_date": ex, "payable_date": pay,
                        "rate": rate, "entitled_shares": 0, "amount_usd": 0.,
                        "status": "NOT_YET_EX", "reinvestment_date": next((d for d in sessions if d > pay), None)})
    cash, shares = money(capital), 0
    fees, friction_cost, cost_basis = 0., 0., 0.
    fills, cashflows, daily, issues, order_decisions = [], [], [], [], []
    initial_ok = positive(rth_open.get(first))
    adjusted_entry = (rth_open[first] * all_close[first] / raw_close[first]
                      if initial_ok and positive(all_close.get(first)) and positive(raw_close.get(first)) else None)
    if not initial_ok:
        issues.append({"date": first, "reason": "INITIAL_RTH_OPEN_UNKNOWN_NO_LATER_BACKFILL"})
    peak = capital
    cursor = date.fromisoformat(first)
    while cursor <= date.fromisoformat(last):
        day = cursor.isoformat()
        for action in actions:
            if action["ex_date"] == day:
                action["entitled_shares"] = shares
                action["amount_usd"] = money(shares * action["rate"])
                action["status"] = "RECEIVABLE" if shares else "NOT_ENTITLED"
            if action["payable_date"] == day and action["status"] == "RECEIVABLE":
                cash = money(cash + action["amount_usd"])
                action["status"] = "PAID"
                cashflows.append({"date": day, "type": "DIVIDEND_PAID", "action_id": action["id"], "cash_usd": action["amount_usd"]})
        if day in sessions:
            reinvest = [a for a in actions if a["reinvestment_date"] == day and a["status"] == "PAID"]
            reason = "INITIAL_BUY" if day == first else "DIVIDEND_REINVEST" if reinvest else None
            if reason and initial_ok:
                base = rth_open.get(day)
                decision = {"date": day, "reason": reason, "cash_before_usd": cash,
                            "historical_network_received_at": "UNKNOWN", "execution_evidence": "RTH_MINUTE_OPEN_MODEL"}
                if not positive(base):
                    decision["status"] = "OPEN_UNKNOWN_NO_ORDER_NO_BACKFILL"
                    issues.append({"date": day, "reason": decision["status"]})
                else:
                    execution = base * (1 + friction)
                    qty = max(0, math.floor((cash - commission) / execution + 1e-12))
                    while qty and money(qty * execution + commission) > cash:
                        qty -= 1
                    if qty:
                        debit = money(qty * execution + commission)
                        order_friction = qty * (execution - base)
                        cash = money(cash - debit); shares += qty
                        cost_basis = money(cost_basis + debit)
                        fees = money(fees + commission); friction_cost += order_friction
                        fill = {**decision, "side": "BUY", "shares": qty, "raw_rth_open": base,
                                "model_execution_price": execution, "commission_usd": commission,
                                "friction_usd": order_friction, "cash_debit_usd": debit,
                                "cash_after_usd": cash, "status": "MODEL_EXECUTED"}
                        fills.append(fill); decision.update(status="MODEL_EXECUTED", shares=qty)
                    else:
                        decision.update(status="NO_AFFORDABLE_WHOLE_SHARE_NO_COMMISSION", shares=0)
                order_decisions.append(decision)
            receivable = money(sum(a["amount_usd"] for a in actions if a["status"] == "RECEIVABLE"))
            close = raw_close.get(day)
            valid_mark = positive(close)
            if not valid_mark:
                issues.append({"date": day, "reason": "RAW_CLOSE_UNKNOWN"})
            equity = money(cash + shares * close + receivable) if valid_mark and initial_ok else None
            if equity is not None:
                peak = max(peak, equity)
            reference = (capital * all_close[day] / adjusted_entry
                         if adjusted_entry and positive(all_close.get(day)) else None)
            daily.append({"trade_date": day, "symbol": symbol, "shares": shares, "cash_usd": cash,
                          "dividend_receivable_usd": receivable, "raw_close": close if valid_mark else None,
                          "market_value_usd": money(shares * close) if valid_mark else None,
                          "equity_usd": equity, "cost_basis_usd": cost_basis,
                          "drawdown": equity / peak - 1 if equity is not None else None,
                          "all_adjusted_no_fee_reference_equity_usd": reference,
                          "benchmark_execution_basis": "HISTORICAL_MODEL_NOT_ACTUAL_FILL"})
        if cash < 0:
            raise AssertionError("NEGATIVE_CASH_FORBIDDEN")
        cursor += timedelta(days=1)
    terminal = daily[-1]
    end_equity = terminal["equity_usd"]
    reference_complete = all(d["all_adjusted_no_fee_reference_equity_usd"] is not None for d in daily)
    summary = {"symbol": symbol, "version": VERSION, "start": first, "end": last,
               "status": "NOT_RUN_DATA_GATED" if not initial_ok else "PARTIAL_INPUTS" if issues else "COMPUTED_REAL_INPUT_MODEL_EXECUTION",
               "initial_capital_usd": capital, "end_equity_usd": end_equity,
               "end_cash_usd": cash, "end_shares": shares, "end_market_value_usd": terminal["market_value_usd"],
               "end_dividend_receivable_usd": terminal["dividend_receivable_usd"],
               "net_profit_usd": money(end_equity-capital) if end_equity is not None else None,
               "cumulative_return": end_equity/capital-1 if end_equity is not None else None,
               "maximum_drawdown": min(d["drawdown"] for d in daily) if all(d["drawdown"] is not None for d in daily) else None,
               "commissions_usd": fees, "friction_usd": friction_cost,
               "dividend_cash_paid_usd": money(sum(x["cash_usd"] for x in cashflows)),
               "buy_order_count": len(fills), "sell_order_count": 0, "terminal_sell_cost_included": False,
               "reference_status": "COMPLETE_VENDOR_ALL_REFERENCE" if reference_complete else "ALL_REFERENCE_INPUT_UNKNOWN",
               "all_adjusted_reference_end_usd": terminal["all_adjusted_no_fee_reference_equity_usd"],
               "all_adjusted_reference_return": terminal["all_adjusted_no_fee_reference_equity_usd"]/capital-1 if reference_complete else None,
               "issues": issues, "assumptions": ASSUMPTIONS}
    return {"summary": summary, "daily": daily, "fills": fills, "dividends": actions,
            "cashflows": cashflows, "order_decisions": order_decisions}


def cached_daily(symbol, adjustment, start=START, end=END, cache=CACHE):
    manifest_path = cache / "datasets" / symbol / IDENTITIES[symbol] / adjustment / "manifest.json"
    manifest = read(manifest_path)
    if manifest["symbol"] != symbol or manifest["adjustment"] != adjustment:
        raise ValueError("BENCHMARK_MANIFEST_IDENTITY_MISMATCH")
    inputs = [{"path": str(manifest_path), "sha256": sha(manifest_path), "read_only": True}]
    frames = []
    for chunk in manifest["chunks"].values():
        if chunk["end"] < start or chunk["start"] > end:
            continue
        path = cache / chunk["path"]
        if chunk.get("status") != "OK" or chunk.get("feed") != "sip" or sha(path) != chunk["sha256"]:
            raise ValueError("BENCHMARK_CACHE_NOT_VERIFIED")
        frame = pd.read_parquet(path)
        if "symbol" in frame and not frame.symbol.eq(symbol).all():
            raise ValueError("BENCHMARK_CACHE_SYMBOL_MISMATCH")
        frame["trade_date"] = frame.trade_date.astype(str).str[:10]
        frames.append(frame[frame.trade_date.between(start, end)])
        inputs.append({"path": str(path), "sha256": chunk["sha256"], "read_only": True,
                       "adjustment": adjustment, "feed": "sip", "retrieved_at": chunk.get("retrieved_at", "UNKNOWN"),
                       "historical_network_received_at": "UNKNOWN"})
    if not frames:
        raise ValueError("BENCHMARK_DAILY_CACHE_MISSING")
    result = pd.concat(frames, ignore_index=True).sort_values("trade_date")
    if result.trade_date.duplicated().any():
        raise ValueError("BENCHMARK_DUPLICATE_DAILY_ROWS")
    return result, inputs


def exact_rth_open(frame, timestamp):
    """No substitution of a later minute, pre-market trade or daily.open."""
    if frame.empty or "timestamp" not in frame:
        return None
    rows = frame[pd.to_datetime(frame.timestamp, utc=True).eq(pd.Timestamp(timestamp))]
    if len(rows) != 1:
        return None
    value = float(rows.iloc[0].open)
    return value if positive(value) else None


def run(output=ROOT / "benchmarks" / "frozen_v1"):
    """Explicit real-input execution only; immutable completed output is reused."""
    output = Path(output)
    if (output / "COMPLETE.json").exists():
        complete = read(output / "COMPLETE.json")
        for item in complete["outputs"]:
            if sha(output / item["path"]) != item["sha256"]:
                raise ValueError("BENCHMARK_COMPLETED_OUTPUT_CHANGED")
        return read(output / "SUMMARY.json")
    protocol = read(ROOT / "B12_PROTOCOL.json")
    if (protocol["start"], protocol["end"], protocol["capital_each"], protocol["commission_usd"],
        protocol["base_execution_friction_fraction"]) != (START, END, 5500, 1, .001):
        raise ValueError("BENCHMARK_PROTOCOL_CHANGED")
    spec = {"version": VERSION, "protocol_sha256": sha(ROOT / "B12_PROTOCOL.json"),
            "symbols": ["SPY", "QQQ"], "start": START, "end": END, "assumptions": ASSUMPTIONS,
            "source_documentation": SOURCES}
    if (output / "RUN_SPEC.json").exists() and read(output / "RUN_SPEC.json") != spec:
        raise ValueError("BENCHMARK_OUTPUT_VERSION_CONFLICT")
    write(output / "RUN_SPEC.json", spec)
    schedule = exchange_schedule(); sessions = schedule.index.strftime("%Y-%m-%d").tolist()
    actions_path = ROOT / "data" / "corporate_actions.json"
    actions = read(actions_path)
    if actions.get("status") not in {"ACCESS_OK", "OK", "COMPLETE"}:
        raise ValueError("BENCHMARK_CORPORATE_ACTION_SOURCE_INCOMPLETE")
    from .data import B12Market
    market = None
    summaries, inputs, opening_rows = [], [], []
    inputs.append({"path": str(actions_path), "sha256": sha(actions_path), "read_only": True,
                   "historical_available_at": "UNKNOWN", "coverage_basis": "VENDOR_STANDARD_NOT_INDEPENDENTLY_EXHAUSTIVE"})
    for symbol in spec["symbols"]:
        try:
            raw, refs = cached_daily(symbol, "raw"); inputs.extend(refs)
            adjusted, refs = cached_daily(symbol, "all"); inputs.extend(refs)
            dividends = [a for a in actions["corporate_actions"].get("cash_dividends", []) if a.get("symbol") == symbol]
            for kind, values in actions["corporate_actions"].items():
                if kind == "cash_dividends":
                    continue
                for value in values:
                    if symbol in [value.get(k) for k in ("symbol", "old_symbol", "new_symbol", "initiating_symbol", "target_symbol")]:
                        effective = value.get("ex_date") or value.get("effective_date")
                        if not effective or START <= str(effective)[:10] <= END:
                            raise ValueError("NON_CASH_ACTION_REQUIRES_SEPARATE_REVIEW")
            required = {START}
            for action in dividends:
                if START <= str(action.get("ex_date", ""))[:10] <= END:
                    pay = action.get("payable_date")
                    if not pay:
                        raise ValueError("DIVIDEND_PAYABLE_DATE_UNKNOWN")
                    later = next((d for d in sessions if d > str(pay)[:10]), None)
                    if later:
                        required.add(later)
            opens = {}
            for day in sorted(required):
                if market is None:
                    market = B12Market(ROOT / "data" / "benchmark_inputs")
                opening = schedule.loc[day].market_open
                frame, receipt = market.acquire(symbol, opening, opening + pd.Timedelta(seconds=59, microseconds=999999),
                                                 adjustment="raw", timeframe="1Min", scope="B12_BENCHMARK_RTH_OPEN")
                opens[day] = exact_rth_open(frame, opening)
                dest = output / "inputs" / f"{symbol}_{day}_opening_minute.parquet"
                dest.parent.mkdir(parents=True, exist_ok=True); frame.to_parquet(dest, index=False)
                inputs.append({"path": str(dest), "sha256": sha(dest), "receipt": receipt,
                               "purpose": "FIRST_RTH_MINUTE_OPEN_MODEL", "historical_network_received_at": "UNKNOWN"})
                daily_row = raw[raw.trade_date.eq(day)]
                opening_rows.append({"symbol": symbol, "trade_date": day, "timestamp_utc": opening.isoformat(),
                                     "rth_open": opens[day], "raw_daily_open_diagnostic_only": float(daily_row.iloc[0].open) if len(daily_row) == 1 else None,
                                     "basis": "RTH_MINUTE_OPEN_MODEL_NOT_OFFICIAL_AUCTION_PROOF"})
            result = simulate_hold(symbol, sessions, raw.set_index("trade_date").close.to_dict(),
                                   adjusted.set_index("trade_date").close.to_dict(), opens, dividends)
            write(output / symbol / "ACCOUNT.json", result)
            for field in ("daily", "fills", "dividends", "cashflows", "order_decisions"):
                pd.DataFrame(result[field]).to_csv(output / symbol / f"{field}.csv", index=False, encoding="utf-8-sig")
            summaries.append(result["summary"])
        except Exception as exc:
            # All caught errors are local data/validation errors, never secrets or
            # provider response bodies. Do not prevent the other reference.
            summaries.append({"symbol": symbol, "status": "NOT_RUN_DATA_GATED", "error_type": type(exc).__name__,
                              "reason": str(exc) if isinstance(exc, ValueError) else "SEE_LOCAL_INPUT_CAPABILITY_OR_EXCEPTION_TYPE",
                              "initial_capital_usd": 5500, "end_equity_usd": None})
        write(output / "SUMMARY.json", summaries)
        write(output / "INPUT_MANIFEST.json", inputs)
        write(output / "RTH_OPEN_VERIFICATION.json", opening_rows)
        write(output / "RUN_STATE.json", {"version": VERSION, "updated_at": utc(), "completed_symbols": [x["symbol"] for x in summaries],
                                          "state": "RUNNING" if len(summaries) < 2 else "FINISHED", "paid_api_calls": 0})
    write(output / "MARKET_STATE_REDACTED.json", {"requests": len(market.state.get("requests", [])) if market else 0,
                                                  "cache_directory": str(ROOT / "data" / "benchmark_inputs"),
                                                  "historical_network_received_at": "UNKNOWN"})
    notes = ["# B1.2 SPY/QQQ 冻结被动基准", "", "每个账户初始 5500 美元，2026-01-02 至 2026-03-31。现金零息；期末继续持仓，未扣虚拟卖出费用。",
             "", "开盘成交为真实 09:30 分钟 open 加 0.1% 摩擦的模型执行，不能称为实际成交或已核验官方开盘竞价。日线 open 仅留作核对。",
             "日线价格按 Alpaca 成交条件聚合，与本轮策略采用同一供应商合并收盘口径；并非独立核验交易所官方收盘竞价。日线成交量可能含延长时段，不用于本基准执行。",
             "", "按除息前持股数计提应收，实际付款日入现金，下一交易日开盘尝试一次整数股再投资。单独 all 复权线是无费用分数股总回报参考；现金分红账户只用 raw 价格，禁止双算分红。",
             "基金管理费已在净值内，不再扣；税费与预扣税未计入。供应商公司行动覆盖未独立穷尽验证，历史网络接收时间 UNKNOWN。",
             "", "策略覆盖不足时，指数成绩仅是同本金同日期的参照，不能据此宣称完整公平实验或独立有效性证明。", ""]
    for item in summaries:
        notes.append(f"- {item['symbol']}: {item['status']}；期末权益 {item.get('end_equity_usd')}；现金 {item.get('end_cash_usd')}；股数 {item.get('end_shares')}；应收 {item.get('end_dividend_receivable_usd')}；净盈亏 {item.get('net_profit_usd')}。")
    notes += ["", "来源口径："] + [f"- {url}" for url in SOURCES]
    (output / "BENCHMARKS.md").write_text("\n".join(notes), encoding="utf-8")
    outputs = [{"path": str(p.relative_to(output)), "sha256": sha(p)} for p in sorted(output.rglob("*")) if p.is_file() and p.name != "COMPLETE.json"]
    write(output / "COMPLETE.json", {"finished_at": utc(), "version": VERSION, "outputs": outputs})
    return summaries


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="Execute frozen real-input reference accounts explicitly")
    args = parser.parse_args()
    if args.run:
        print(run())
    else:
        parser.print_help()
