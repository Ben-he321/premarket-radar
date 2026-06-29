"""踩5日线穿10日线放量策略回测。"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from src.data.safe_yfinance import safe_download, safe_fast_info


MARKET_CAP_MIN = 1_000_000_000
SLIPPAGE_RATE = 0.003
STOP_LOSS_RATE = 0.05
TOUCH_MA5_TOLERANCE = 0.02
VOLUME_MULTIPLIER = 1.5


def normalize_ticker(ticker: str) -> str:
    """统一 ticker 格式，yfinance 对 BRK-B 这类写法更友好。"""

    return ticker.strip().upper().replace(".", "-")


def get_market_cap(ticker: str) -> int | None:
    """读取当前市值；失败返回 None，回测时自动跳过。"""

    try:
        info = safe_fast_info(ticker)
        market_cap = info.get("market_cap")
        return int(market_cap) if market_cap else None
    except Exception:
        return None


def filter_by_market_cap(tickers: list[str], min_market_cap: int = MARKET_CAP_MIN) -> tuple[list[str], pd.DataFrame]:
    """用当前 market cap 近似过滤股票池，坏数据会跳过。"""

    kept: list[str] = []
    rows: list[dict[str, object]] = []
    for ticker in tickers:
        clean_ticker = normalize_ticker(ticker)
        market_cap = get_market_cap(clean_ticker)
        passed = market_cap is not None and market_cap >= min_market_cap
        if passed:
            kept.append(clean_ticker)
        rows.append({"ticker": clean_ticker, "market_cap": market_cap, "是否纳入": "是" if passed else "否"})
    return kept, pd.DataFrame(rows)


def download_price_data(tickers: list[str], period: str = "2y") -> dict[str, pd.DataFrame]:
    """批量下载日 K 数据，单只坏数据会被跳过。"""

    if not tickers:
        return {}

    data: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        try:
            frame = safe_download(ticker, period=period, interval="1d")
            frame = frame.rename(columns=str.title)
            required = ["Open", "High", "Low", "Close", "Volume"]
            if frame.empty or not all(column in frame.columns for column in required):
                continue
            frame = frame[required].dropna()
            frame = frame[frame["Volume"] > 0]
            if len(frame) >= 30:
                data[ticker] = frame
        except Exception:
            continue
    return data


def _detect_signals(frame: pd.DataFrame) -> pd.Series:
    """识别入场信号：踩5日线、站上10日线、收涨、放量。"""

    ma5 = frame["Close"].rolling(5).mean()
    ma10 = frame["Close"].rolling(10).mean()
    avg_volume5 = frame["Volume"].shift(1).rolling(5).mean()
    touch_ma5 = ((frame["Low"] - ma5).abs() / ma5) <= TOUCH_MA5_TOLERANCE
    above_ma10 = frame["Close"] >= ma10
    close_up = frame["Close"] > frame["Close"].shift(1)
    volume_breakout = frame["Volume"] > avg_volume5 * VOLUME_MULTIPLIER
    return touch_ma5 & above_ma10 & close_up & volume_breakout


def _apply_slippage(entry_price: float, exit_price: float) -> float:
    """买卖各扣 0.3% 滑点后的收益率。"""

    buy_cost = entry_price * (1 + SLIPPAGE_RATE)
    sell_proceeds = exit_price * (1 - SLIPPAGE_RATE)
    return (sell_proceeds - buy_cost) / buy_cost


def _market_group(market_cap: int | None) -> str:
    """按市值粗分大盘/中盘/小盘。"""

    if market_cap is None:
        return "未知"
    if market_cap >= 10_000_000_000:
        return "大盘"
    if market_cap >= 2_000_000_000:
        return "中盘"
    return "小盘"


def _simulate_a(frame: pd.DataFrame, ticker: str, signal_index: int, hold_days: int, market_cap: int | None) -> dict[str, object] | None:
    """纪律A：高开超过3%放弃，开盘买，固定持有天数或止损。"""

    entry_index = signal_index + 1
    exit_index = entry_index + hold_days - 1
    if exit_index >= len(frame):
        return None

    signal_close = float(frame.iloc[signal_index]["Close"])
    entry_open = float(frame.iloc[entry_index]["Open"])
    if (entry_open - signal_close) / signal_close > 0.03:
        return None

    stop_price = entry_open * (1 - STOP_LOSS_RATE)
    exit_price = float(frame.iloc[exit_index]["Close"])
    exit_reason = f"持有{hold_days}天到期"
    for day_index in range(entry_index, exit_index + 1):
        if float(frame.iloc[day_index]["Low"]) <= stop_price:
            exit_price = stop_price
            exit_reason = "触发止损"
            break

    return {"ticker": ticker, "纪律": f"纪律A 持有{hold_days}天", "收益率": _apply_slippage(entry_open, exit_price), "退出原因": exit_reason, "市值分组": _market_group(market_cap)}


def _simulate_b(frame: pd.DataFrame, ticker: str, signal_index: int, market_cap: int | None) -> dict[str, object] | None:
    """纪律B：次日红盘才买，允许追高开，最多3天，用移动止损。"""

    entry_index = signal_index + 1
    exit_index = entry_index + 2
    if exit_index >= len(frame):
        return None

    signal_close = float(frame.iloc[signal_index]["Close"])
    entry_open = float(frame.iloc[entry_index]["Open"])
    if entry_open <= signal_close:
        return None

    stop_price = entry_open * (1 - STOP_LOSS_RATE)
    exit_price = float(frame.iloc[exit_index]["Close"])
    exit_reason = "第3天收盘退出"
    for day_index in range(entry_index, exit_index + 1):
        row = frame.iloc[day_index]
        if float(row["Low"]) <= stop_price:
            exit_price = stop_price
            exit_reason = "触发移动止损"
            break
        close_price = float(row["Close"])
        if close_price > entry_open:
            stop_price = max(stop_price, close_price * (1 - STOP_LOSS_RATE))

    return {"ticker": ticker, "纪律": "纪律B 移动止损", "收益率": _apply_slippage(entry_open, exit_price), "退出原因": exit_reason, "市值分组": _market_group(market_cap)}


def run_strategy_backtest(tickers: list[str], period: str = "2y", progress_callback: Callable[[float, str], None] | None = None) -> dict[str, object]:
    """运行完整回测，返回交易明细、指标、收益曲线和跳过信息。"""

    clean_tickers = [normalize_ticker(ticker) for ticker in tickers]
    if progress_callback:
        progress_callback(0.1, "正在筛选市值...")
    market_tickers, market_cap_df = filter_by_market_cap(clean_tickers)

    if progress_callback:
        progress_callback(0.35, "正在下载历史日 K 数据...")
    price_data = download_price_data(market_tickers, period=period)
    market_cap_map = {row["ticker"]: row["market_cap"] for row in market_cap_df.to_dict("records") if row.get("market_cap") is not None}

    trades: list[dict[str, object]] = []
    skipped: list[str] = []
    total = max(len(market_tickers), 1)
    for index, ticker in enumerate(market_tickers, start=1):
        frame = price_data.get(ticker)
        if frame is None or frame.empty:
            skipped.append(ticker)
            continue
        signals = _detect_signals(frame)
        for signal_index in np.flatnonzero(signals.to_numpy()):
            for hold_days in (1, 2, 3):
                trade_a = _simulate_a(frame, ticker, int(signal_index), hold_days, market_cap_map.get(ticker))
                if trade_a:
                    trades.append(trade_a)
            trade_b = _simulate_b(frame, ticker, int(signal_index), market_cap_map.get(ticker))
            if trade_b:
                trades.append(trade_b)
        if progress_callback:
            progress_callback(0.35 + index / total * 0.6, f"正在回测：{ticker}")

    trades_df = pd.DataFrame(trades)
    return {
        "market_cap_df": market_cap_df,
        "trades_df": trades_df,
        "metrics_df": summarize_metrics(trades_df),
        "group_metrics_df": summarize_group_metrics(trades_df),
        "equity_curve_df": build_equity_curves(trades_df),
        "skipped": skipped,
    }


def _max_drawdown(returns: pd.Series) -> float:
    """用逐笔收益曲线估算最大回撤。"""

    if returns.empty:
        return 0.0
    equity = (1 + returns).cumprod()
    drawdown = equity / equity.cummax() - 1
    return float(drawdown.min())


def summarize_metrics(trades_df: pd.DataFrame) -> pd.DataFrame:
    """按交易纪律汇总胜率、盈亏比、期望值等指标。"""

    if trades_df.empty:
        return pd.DataFrame()
    rows: list[dict[str, object]] = []
    for discipline, group in trades_df.groupby("纪律", sort=False):
        returns = group["收益率"].astype(float)
        wins = returns[returns > 0]
        losses = returns[returns <= 0]
        avg_win = float(wins.mean()) if not wins.empty else 0.0
        avg_loss = float(losses.mean()) if not losses.empty else 0.0
        rows.append({"纪律": discipline, "触发信号总次数": int(len(group)), "胜率": float((returns > 0).mean()), "平均盈利%": avg_win, "平均亏损%": avg_loss, "盈亏比": avg_win / abs(avg_loss) if avg_loss < 0 else 0.0, "期望值": float(returns.mean()), "最大回撤": _max_drawdown(returns)})
    return pd.DataFrame(rows)


def summarize_group_metrics(trades_df: pd.DataFrame) -> pd.DataFrame:
    """按市值分组对比策略表现。"""

    if trades_df.empty:
        return pd.DataFrame()
    rows: list[dict[str, object]] = []
    for (discipline, market_group), group in trades_df.groupby(["纪律", "市值分组"], sort=False):
        returns = group["收益率"].astype(float)
        rows.append({"纪律": discipline, "市值分组": market_group, "交易数": int(len(group)), "胜率": float((returns > 0).mean()), "期望值": float(returns.mean()), "最大回撤": _max_drawdown(returns)})
    return pd.DataFrame(rows)


def build_equity_curves(trades_df: pd.DataFrame) -> pd.DataFrame:
    """构造简单累计收益曲线，按交易出现顺序复利。"""

    if trades_df.empty:
        return pd.DataFrame()
    curves: list[pd.DataFrame] = []
    for discipline, group in trades_df.groupby("纪律", sort=False):
        curves.append(pd.DataFrame({"交易序号": range(1, len(group) + 1), "累计收益": (1 + group["收益率"].astype(float)).cumprod() - 1, "纪律": discipline}))
    return pd.concat(curves, ignore_index=True)
