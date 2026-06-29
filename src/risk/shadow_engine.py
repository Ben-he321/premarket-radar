"""影子组合自动引擎：简化版虚拟买卖规则。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd

from src.scoring.sector_radar import build_sector_radar, fetch_ticker_snapshot
from src.supabase_client import SupabaseRestClient, SupabaseStatus, delete_rows, insert_row


INITIAL_CAPITAL = 4500.0
RISK_PER_TRADE = 0.01
STOP_LOSS_RATE = 0.05
MAX_HOLD_DAYS = 3
MAX_AUTO_BUYS_PER_RUN = 2


@dataclass
class ShadowEngineResult:
    """页面展示用的自动引擎结果。"""

    ok: bool
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    buys: int = 0
    sells: int = 0


def _to_float(value: object, default: float = 0.0) -> float:
    """安全转 float。"""

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_date(value: object) -> date | None:
    """安全转 date。"""

    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except (TypeError, ValueError):
        return None


def _latest_cash(account_rows: list[dict[str, object]]) -> float:
    """读取最新账户现金；没有快照时使用初始资金。"""

    if not account_rows:
        return INITIAL_CAPITAL
    return _to_float(account_rows[0].get("cash"), INITIAL_CAPITAL)


def _already_bought_today(trades: list[dict[str, object]], ticker: str, today: str) -> bool:
    """避免每次刷新重复买入同一只 ticker。"""

    for trade in trades:
        if str(trade.get("ticker", "")).upper() != ticker:
            continue
        if str(trade.get("trade_date")) != today:
            continue
        if str(trade.get("side")) == "买" and "自动引擎" in str(trade.get("reason", "")):
            return True
    return False


def _candidate_tickers(radar: dict[str, object]) -> list[tuple[str, str]]:
    """从板块雷达结果里提取候选 ticker 和策略标签。"""

    candidates: list[tuple[str, str]] = []
    for key, label in (("leaders", "板块龙头"), ("followers", "板块跟风")):
        frame = radar.get(key)
        if not isinstance(frame, pd.DataFrame) or frame.empty or "代码" not in frame.columns:
            continue
        for _, row in frame.head(6).iterrows():
            ticker = str(row.get("代码", "")).strip().upper()
            sector = str(row.get("板块", "")).strip()
            if ticker:
                candidates.append((ticker, f"{label}:{sector}"))

    # 去重并保持原始强弱顺序。
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for ticker, label in candidates:
        if ticker not in seen:
            unique.append((ticker, label))
            seen.add(ticker)
    return unique


def _insert_account_snapshot(client: SupabaseRestClient, cash: float, market_value: float, note: str) -> SupabaseStatus:
    """写入账户快照，用于收益曲线。"""

    return insert_row(
        client,
        "shadow_account",
        {
            "account_date": date.today().isoformat(),
            "cash": round(cash, 2),
            "market_value": round(market_value, 2),
            "note": note,
        },
    )


def run_shadow_engine(
    client: SupabaseRestClient | None,
    api_key: str | None,
    account_rows: list[dict[str, object]],
    positions: list[dict[str, object]],
    trades: list[dict[str, object]],
) -> ShadowEngineResult:
    """运行简化版影子组合自动买卖；任何失败都返回中文消息，不抛异常。"""

    result = ShadowEngineResult(ok=True)
    if client is None:
        return ShadowEngineResult(ok=False, errors=["Supabase 未连接，自动引擎暂停。"])

    today = date.today()
    today_text = today.isoformat()
    cash = _latest_cash(account_rows)
    held_tickers = {str(item.get("ticker", "")).upper() for item in positions}
    sold_position_ids: set[str] = set()
    bought_market_value = 0.0

    # 先处理卖出：-5% 止损或持有 3 天到期。
    for position in positions:
        ticker = str(position.get("ticker", "")).strip().upper()
        position_id = position.get("id")
        if not ticker or position_id is None:
            continue

        entry_price = _to_float(position.get("entry_price"))
        quantity = _to_float(position.get("quantity"))
        stop_loss = _to_float(position.get("stop_loss"), entry_price * (1 - STOP_LOSS_RATE))
        entry_date = _to_date(position.get("entry_date"))
        hold_days = (today - entry_date).days if entry_date else 0
        snapshot = fetch_ticker_snapshot(api_key, ticker)
        current_price = _to_float(snapshot.get("price")) if snapshot else 0.0
        if current_price <= 0 or entry_price <= 0 or quantity <= 0:
            continue

        should_sell = current_price <= stop_loss or hold_days >= MAX_HOLD_DAYS
        if not should_sell:
            continue

        reason = "自动引擎：触发 -5% 止损" if current_price <= stop_loss else "自动引擎：持有 3 天到期"
        pnl = (current_price - entry_price) * quantity
        trade_status = insert_row(
            client,
            "shadow_trades",
            {
                "ticker": ticker,
                "side": "卖",
                "price": round(current_price, 4),
                "quantity": quantity,
                "trade_date": today_text,
                "pnl": round(pnl, 2),
                "reason": reason,
                "strategy_tag": str(position.get("strategy_tag") or "自动引擎"),
            },
        )
        if not trade_status.ok:
            result.errors.append(trade_status.message)
            continue

        delete_status = delete_rows(client, "shadow_positions", {"id": str(position_id)})
        if not delete_status.ok:
            result.errors.append(delete_status.message)
            continue

        cash += current_price * quantity
        sold_position_ids.add(str(position_id))
        held_tickers.discard(ticker)
        result.sells += 1
        result.messages.append(f"{ticker} 已虚拟卖出：{reason}，盈亏 €{pnl:,.2f}")

    # 再处理买入：从板块雷达挑选强势龙头/跟风候选。
    try:
        radar = build_sector_radar(api_key, top_sector_count=3)
        candidates = _candidate_tickers(radar)
    except Exception as exc:
        candidates = []
        result.errors.append(f"读取板块雷达失败，自动买入暂停：{exc}")

    total_equity = max(INITIAL_CAPITAL, cash + sum(_to_float(p.get("entry_price")) * _to_float(p.get("quantity")) for p in positions))
    risk_budget = total_equity * RISK_PER_TRADE

    for ticker, strategy_tag in candidates:
        if result.buys >= MAX_AUTO_BUYS_PER_RUN:
            break
        if ticker in held_tickers or _already_bought_today(trades, ticker, today_text):
            continue

        snapshot = fetch_ticker_snapshot(api_key, ticker)
        current_price = _to_float(snapshot.get("price")) if snapshot else 0.0
        if current_price <= 0:
            continue

        stop_loss = current_price * (1 - STOP_LOSS_RATE)
        risk_per_share = max(current_price - stop_loss, 0.01)
        quantity = int(risk_budget / risk_per_share)
        quantity = min(quantity, int(cash / current_price))
        if quantity <= 0:
            result.messages.append("现金不足，自动买入暂停。")
            break

        position_status = insert_row(
            client,
            "shadow_positions",
            {
                "ticker": ticker,
                "entry_price": round(current_price, 4),
                "quantity": quantity,
                "entry_date": today_text,
                "stop_loss": round(stop_loss, 4),
                "strategy_tag": strategy_tag,
                "note": "自动引擎虚拟买入",
            },
        )
        if not position_status.ok:
            result.errors.append(position_status.message)
            continue

        trade_status = insert_row(
            client,
            "shadow_trades",
            {
                "ticker": ticker,
                "side": "买",
                "price": round(current_price, 4),
                "quantity": quantity,
                "trade_date": today_text,
                "pnl": 0,
                "reason": "自动引擎：来自板块雷达强势候选",
                "strategy_tag": strategy_tag,
            },
        )
        if not trade_status.ok:
            result.errors.append(trade_status.message)
            continue

        cash -= current_price * quantity
        bought_market_value += current_price * quantity
        held_tickers.add(ticker)
        result.buys += 1
        result.messages.append(f"{ticker} 已虚拟买入：{quantity} 股，价格 ${current_price:,.2f}，止损 ${stop_loss:,.2f}")

    active_positions = [item for item in positions if str(item.get("id")) not in sold_position_ids]
    market_value = sum(_to_float(item.get("entry_price")) * _to_float(item.get("quantity")) for item in active_positions)
    market_value += bought_market_value

    if result.buys or result.sells:
        account_status = _insert_account_snapshot(client, cash, market_value, "自动引擎运行后账户快照")
        if not account_status.ok:
            result.errors.append(account_status.message)

    if not result.messages and not result.errors:
        result.messages.append("自动引擎已检查：暂无需要买入或卖出的信号。")

    result.ok = not result.errors
    return result
