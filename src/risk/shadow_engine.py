"""影子组合自动引擎：简化版虚拟买卖规则。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd

from src.risk.market_temperature import MarketTemperature, build_market_temperature
from src.scoring.sector_radar import build_sector_radar, fetch_ticker_snapshot
from src.supabase_client import SupabaseRestClient, SupabaseStatus, delete_rows, insert_row, upsert_row


INITIAL_CAPITAL = 4500.0
RISK_PER_TRADE = 0.01
STOP_LOSS_RATE = 0.05
MAX_HOLD_DAYS = 3
MAX_AUTO_BUYS_PER_RUN = 2
AUTO_ENGINE_MARKER = "[AUTO_ENGINE_DONE]"
BUY_SIDES = {"买", "做多"}
SELL_SIDES = {"卖", "做空"}

__all__ = [
    "ShadowEngineResult",
    "derive_cash_from_trades",
    "derive_market_value_from_positions",
    "run_shadow_engine",
]


@dataclass
class ShadowEngineResult:
    """页面展示用的自动引擎结果。"""

    ok: bool
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    buys: int = 0
    sells: int = 0
    market_label: str = "未知"
    market_signal: str = "UNKNOWN"
    decision_summary: str = "市场状态尚未计算。"
    skip_logs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ShadowEngineDecision:
    """自动引擎的市场总闸决策，集中放规则，方便后续调整。"""

    market_label: str
    market_signal: str
    allow_new_positions: bool
    leaders_only: bool
    strongest_sector_only: bool
    risk_multiplier: float
    max_buys: int
    summary: str


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


def derive_cash_from_trades(trades: list[dict[str, object]], initial_cash: float = INITIAL_CAPITAL) -> float:
    """从成交流水推导现金，避免 shadow_account 快照和真实成交脱节。

    现金只允许通过成交变化：
    cash = 初始资金 - Σ(买入成交额) + Σ(卖出成交额)
    这样只要买卖成交写进 shadow_trades，现金就会自动匹配，不再依赖任何单独写入的快照字段。
    """

    cash = float(initial_cash)
    for trade in trades:
        side = str(trade.get("side") or "").strip()
        price = _to_float(trade.get("price"))
        quantity = _to_float(trade.get("quantity"))
        notional = price * quantity
        if price <= 0 or quantity <= 0:
            continue
        if side in BUY_SIDES:
            cash -= notional
        elif side in SELL_SIDES:
            cash += notional
    return cash


def derive_market_value_from_positions(positions: list[dict[str, object]]) -> float:
    """按当前 MVP 口径从持仓推导持仓市值：entry_price × quantity。"""

    return sum(_to_float(item.get("entry_price")) * _to_float(item.get("quantity")) for item in positions)


def _append_skip_log(result: ShadowEngineResult, ticker: str, reason: str) -> None:
    """记录跳过下单原因；这些日志会写入 daily_report 方便复盘。"""

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log = f"{timestamp} skip {ticker}: {reason}"
    result.skip_logs.append(log)
    result.messages.append(log)


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


def build_shadow_engine_decision(market: MarketTemperature) -> ShadowEngineDecision:
    """根据市场情绪总闸决定影子组合今天怎么开仓。

    规则说明：
    - 进攻/RISK-ON：市场环境支持主动出击，允许龙头和跟风候选，使用标准 1% 风险预算。
    - 中性/NEUTRAL：市场没有明确顺风，只买最强板块龙头，不买跟风候选，风险预算减半。
    - 防守/RISK-OFF：不新开仓，只管理已有持仓的止损和到期卖出。
    - 数据加载中/未知：无法确认大环境时按防守处理，避免在盲区里自动开仓。
    """

    if market.label == "进攻" or market.signal_code == "RISK-ON":
        return ShadowEngineDecision(
            market_label=market.label,
            market_signal=market.signal_code,
            allow_new_positions=True,
            leaders_only=False,
            strongest_sector_only=False,
            risk_multiplier=1.0,
            max_buys=MAX_AUTO_BUYS_PER_RUN,
            summary="市场偏进攻，引擎正常执行：从强势板块里同时观察龙头和跟风候选，按每笔约 1% 账户风险开仓。",
        )

    if market.label == "中性" or market.signal_code == "NEUTRAL":
        return ShadowEngineDecision(
            market_label=market.label,
            market_signal=market.signal_code,
            allow_new_positions=True,
            leaders_only=True,
            strongest_sector_only=True,
            risk_multiplier=0.5,
            max_buys=1,
            summary="市场偏中性，引擎谨慎执行：只买最强板块的龙头，不买跟风候选，仓位按标准风险预算减半。",
        )

    if market.label == "防守" or market.signal_code == "RISK-OFF":
        summary = "今日市场偏防守，引擎暂停新开仓，仅管理existing持仓。"
    else:
        summary = "市场温度数据暂不完整，引擎按防守处理：暂停新开仓，仅管理existing持仓。"

    return ShadowEngineDecision(
        market_label=market.label,
        market_signal=market.signal_code,
        allow_new_positions=False,
        leaders_only=True,
        strongest_sector_only=True,
        risk_multiplier=0.0,
        max_buys=0,
        summary=summary,
    )


def _candidate_tickers(
    radar: dict[str, object],
    *,
    leaders_only: bool = False,
    strongest_sector_only: bool = False,
) -> list[tuple[str, str]]:
    """从板块雷达结果里提取候选 ticker 和策略标签。"""

    candidates: list[tuple[str, str]] = []
    allowed_sector = ""
    sectors = radar.get("sectors")
    if strongest_sector_only and isinstance(sectors, pd.DataFrame) and not sectors.empty and "板块" in sectors.columns:
        allowed_sector = str(sectors.iloc[0].get("板块", "")).strip()

    groups = (("leaders", "板块龙头"),) if leaders_only else (("leaders", "板块龙头"), ("followers", "板块跟风"))
    for key, label in groups:
        frame = radar.get(key)
        if not isinstance(frame, pd.DataFrame) or frame.empty or "代码" not in frame.columns:
            continue
        for _, row in frame.head(6).iterrows():
            ticker = str(row.get("代码", "")).strip().upper()
            sector = str(row.get("板块", "")).strip()
            if allowed_sector and sector != allowed_sector:
                continue
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


def _insert_account_snapshot(client: SupabaseRestClient, trades: list[dict[str, object]], positions: list[dict[str, object]], note: str) -> SupabaseStatus:
    """写入账户快照；现金和市值都从 trades/positions 推导，不手工指定。"""

    return insert_row(
        client,
        "shadow_account",
        {
            "account_date": date.today().isoformat(),
            "cash": round(derive_cash_from_trades(trades), 2),
            "market_value": round(derive_market_value_from_positions(positions), 2),
            "note": note,
        },
    )


def _today_report(daily_reports: list[dict[str, object]], today_text: str) -> dict[str, object]:
    """从 daily_report 里找到今天的报告；没有则返回空字典。"""

    for report in daily_reports:
        if str(report.get("report_date")) == today_text:
            return report
    return {}


def _auto_engine_already_ran(daily_reports: list[dict[str, object]], today_text: str) -> bool:
    """检查今天是否已经自动运行过，避免刷新页面重复下单。"""

    report = _today_report(daily_reports, today_text)
    actions = str(report.get("actions") or "")
    return AUTO_ENGINE_MARKER in actions


def _mark_auto_engine_done(
    client: SupabaseRestClient,
    daily_reports: list[dict[str, object]],
    today_text: str,
    result: ShadowEngineResult,
    decision: ShadowEngineDecision,
) -> SupabaseStatus:
    """把今日自动引擎已执行写进 daily_report，保留已有复盘文本。"""

    report = _today_report(daily_reports, today_text)
    existing_actions = str(report.get("actions") or "").strip()
    skip_text = "；跳过：" + " | ".join(result.skip_logs) if result.skip_logs else ""
    run_summary = (
        f"{AUTO_ENGINE_MARKER} 自动引擎已执行：市场={decision.market_label}({decision.market_signal})；"
        f"决策={decision.summary}；买入 {result.buys} 笔，卖出 {result.sells} 笔{skip_text}。"
    )
    actions = f"{existing_actions}\n{run_summary}".strip() if existing_actions else run_summary

    return upsert_row(
        client,
        "daily_report",
        {
            "report_date": today_text,
            "daily_pnl": _to_float(report.get("daily_pnl"), 0.0),
            "actions": actions,
            "loss_analysis": str(report.get("loss_analysis") or ""),
            "missed_opportunities": str(report.get("missed_opportunities") or ""),
            "summary": str(report.get("summary") or "自动引擎已完成今日检查。"),
        },
        on_conflict="report_date",
    )


def run_shadow_engine(
    client: SupabaseRestClient | None,
    api_key: str | None,
    account_rows: list[dict[str, object]],
    positions: list[dict[str, object]],
    trades: list[dict[str, object]],
    daily_reports: list[dict[str, object]] | None = None,
) -> ShadowEngineResult:
    """运行简化版影子组合自动买卖；任何失败都返回中文消息，不抛异常。"""

    result = ShadowEngineResult(ok=True)
    if client is None:
        return ShadowEngineResult(ok=False, errors=["Supabase 未连接，自动引擎暂停。"])

    today = date.today()
    today_text = today.isoformat()
    daily_reports = daily_reports or []

    try:
        market = build_market_temperature()
        decision = build_shadow_engine_decision(market)
    except Exception as exc:
        result.messages.append(f"市场温度读取失败，引擎按防守处理：{exc}")
        decision = ShadowEngineDecision(
            market_label="未知",
            market_signal="UNKNOWN",
            allow_new_positions=False,
            leaders_only=True,
            strongest_sector_only=True,
            risk_multiplier=0.0,
            max_buys=0,
            summary="市场温度读取失败，引擎按防守处理：暂停新开仓，仅管理existing持仓。",
        )
    result.market_label = decision.market_label
    result.market_signal = decision.market_signal
    result.decision_summary = decision.summary
    result.messages.append(f"市场状态：{decision.market_label}（{decision.market_signal}）。{decision.summary}")
    if _auto_engine_already_ran(daily_reports, today_text):
        result.messages.append("今日自动引擎已执行过，本次刷新不再重复下单。")
        return result

    running_trades = list(trades)
    running_positions = list(positions)
    cash = derive_cash_from_trades(running_trades)
    held_tickers = {str(item.get("ticker", "")).upper() for item in positions}

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
        trade_payload = {
            "ticker": ticker,
            "side": "卖",
            "price": round(current_price, 4),
            "quantity": quantity,
            "trade_date": today_text,
            "pnl": round(pnl, 2),
            "reason": reason,
            "strategy_tag": str(position.get("strategy_tag") or "自动引擎"),
        }
        trade_status = insert_row(
            client,
            "shadow_trades",
            trade_payload,
        )
        if not trade_status.ok:
            result.errors.append(trade_status.message)
            continue

        delete_status = delete_rows(client, "shadow_positions", {"id": str(position_id)})
        if not delete_status.ok:
            result.errors.append(delete_status.message)
            continue

        running_trades.append(trade_payload)
        running_positions = [item for item in running_positions if str(item.get("id")) != str(position_id)]
        cash = derive_cash_from_trades(running_trades)
        held_tickers.discard(ticker)
        result.sells += 1
        result.messages.append(f"{ticker} 已虚拟卖出：{reason}，盈亏 €{pnl:,.2f}")

    # 再处理买入：先服从市场总闸，再决定是否读取板块雷达找候选。
    if not decision.allow_new_positions:
        candidates = []
    else:
        try:
            radar = build_sector_radar(api_key, top_sector_count=3)
            candidates = _candidate_tickers(
                radar,
                leaders_only=decision.leaders_only,
                strongest_sector_only=decision.strongest_sector_only,
            )
        except Exception as exc:
            candidates = []
            result.messages.append(f"读取板块雷达失败，本次不新开仓：{exc}")

    # M2 先用固定初始资金做风险锚点：€4500 × 1% ≈ €45；中性市场再减半。
    risk_budget = INITIAL_CAPITAL * RISK_PER_TRADE * decision.risk_multiplier

    for ticker, strategy_tag in candidates:
        if result.buys >= decision.max_buys:
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
        if quantity <= 0:
            _append_skip_log(result, ticker, "风险预算不足以买入 1 股")
            continue

        planned_cost = current_price * quantity
        if planned_cost > cash:
            _append_skip_log(result, ticker, f"现金不足 需要{planned_cost:.2f} 可用{cash:.2f}")
            continue

        position_payload = {
            "ticker": ticker,
            "entry_price": round(current_price, 4),
            "quantity": quantity,
            "entry_date": today_text,
            "stop_loss": round(stop_loss, 4),
            "strategy_tag": strategy_tag,
            "note": "自动引擎虚拟买入",
        }
        position_status = insert_row(
            client,
            "shadow_positions",
            position_payload,
        )
        if not position_status.ok:
            result.errors.append(position_status.message)
            continue

        trade_payload = {
            "ticker": ticker,
            "side": "买",
            "price": round(current_price, 4),
            "quantity": quantity,
            "trade_date": today_text,
            "pnl": 0,
            "reason": f"自动引擎：{decision.market_label}市场决策，来自板块雷达强势候选",
            "strategy_tag": strategy_tag,
        }
        trade_status = insert_row(
            client,
            "shadow_trades",
            trade_payload,
        )
        if not trade_status.ok:
            result.errors.append(trade_status.message)
            continue

        running_trades.append(trade_payload)
        running_positions.append(position_payload)
        cash = derive_cash_from_trades(running_trades)
        held_tickers.add(ticker)
        result.buys += 1
        result.messages.append(f"{ticker} 已虚拟买入：{quantity} 股，价格 ${current_price:,.2f}，止损 ${stop_loss:,.2f}")

    if result.buys or result.sells:
        account_status = _insert_account_snapshot(client, running_trades, running_positions, "自动引擎运行后派生账户快照")
        if not account_status.ok:
            result.errors.append(account_status.message)

    if not result.messages and not result.errors:
        result.messages.append("自动引擎已检查：暂无需要买入或卖出的信号。")

    if not result.errors:
        mark_status = _mark_auto_engine_done(client, daily_reports, today_text, result, decision)
        if not mark_status.ok:
            result.errors.append(mark_status.message)

    result.ok = not result.errors
    return result
