"""市场情绪总闸：用 VIX、进攻/防守板块和 SPY 均线做三档判断。"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.data.safe_yfinance import safe_download
from src.ui.theme import CYAN, GREEN, RED


OFFENSIVE_TICKERS = ["XLK", "SMH", "IWM"]
DEFENSIVE_TICKERS = ["XLP", "XLU", "XLV"]
MARKET_TICKERS = ["SPY", "QQQ"]
VIX_TICKER = "^VIX"


@dataclass(frozen=True)
class MarketTemperature:
    """首页展示用的市场温度结果。"""

    label: str
    signal_code: str
    color: str
    summary: str
    vix: float | None = None
    offensive_strength: float | None = None
    defensive_strength: float | None = None
    spy_above_ma20: bool | None = None


def _close_series(ticker: str, period: str = "2mo") -> pd.Series:
    """读取单个 ticker 的收盘价，失败返回空序列。"""

    try:
        frame = safe_download(ticker, period=period, interval="1d")
        if frame.empty:
            return pd.Series(dtype=float)

        if isinstance(frame.columns, pd.MultiIndex):
            close_data = pd.DataFrame()
            for level in range(frame.columns.nlevels):
                try:
                    close_data = frame.xs("Close", axis=1, level=level, drop_level=False)
                    if not close_data.empty:
                        break
                except (KeyError, ValueError):
                    continue
            if close_data.empty:
                return pd.Series(dtype=float)
            close_series = close_data.iloc[:, 0]
        elif "Close" in frame.columns:
            close_series = frame["Close"]
        else:
            return pd.Series(dtype=float)

        # yfinance 偶尔会返回单列 DataFrame，这里统一压成一维 Series。
        if isinstance(close_series, pd.DataFrame):
            if close_series.empty:
                return pd.Series(dtype=float)
            close_series = close_series.iloc[:, 0]

        return pd.to_numeric(close_series.squeeze(), errors="coerce").dropna().astype(float)
    except Exception:
        return pd.Series(dtype=float)


def _five_day_return(ticker: str) -> float | None:
    """计算近 5 个交易日收益率。"""

    close = _close_series(ticker, period="1mo")
    if len(close) < 6:
        return None
    previous = float(close.iloc[-6])
    if previous <= 0:
        return None
    return (float(close.iloc[-1]) - previous) / previous * 100


def _average_available(values: list[float | None]) -> float | None:
    """对可用值求平均；全部缺失时返回 None。"""

    clean = [float(value) for value in values if value is not None]
    return sum(clean) / len(clean) if clean else None


def build_market_temperature() -> MarketTemperature:
    """构建市场情绪总闸；数据缺失时友好返回“数据加载中”。"""

    vix_close = _close_series(VIX_TICKER, period="1mo")
    spy_close = _close_series("SPY", period="3mo")

    vix = float(vix_close.iloc[-1]) if not vix_close.empty else None
    offensive_strength = _average_available([_five_day_return(ticker) for ticker in OFFENSIVE_TICKERS])
    defensive_strength = _average_available([_five_day_return(ticker) for ticker in DEFENSIVE_TICKERS])

    spy_above_ma20: bool | None = None
    if len(spy_close) >= 20:
        spy_above_ma20 = float(spy_close.iloc[-1]) >= float(spy_close.tail(20).mean())

    if vix is None or offensive_strength is None or defensive_strength is None or spy_above_ma20 is None:
        return MarketTemperature(
            label="数据加载中",
            signal_code="WAIT",
            color=CYAN,
            summary="市场温度需要 VIX、SPY 和板块 ETF 数据；当前数据源可能延迟，页面会继续加载其他模块。",
            vix=vix,
            offensive_strength=offensive_strength,
            defensive_strength=defensive_strength,
            spy_above_ma20=spy_above_ma20,
        )

    relative_strength = offensive_strength - defensive_strength
    score = 0
    score += 1 if vix < 18 else -1 if vix > 24 else 0
    score += 1 if relative_strength > 0.5 else -1 if relative_strength < -0.5 else 0
    score += 1 if spy_above_ma20 else -1

    if score >= 2:
        return MarketTemperature(
            label="进攻",
            signal_code="RISK-ON",
            color=GREEN,
            summary="VIX 偏低或可控，进攻板块相对防守板块更强，SPY 站在 20 日均线上方，适合优先观察强势板块里的龙头和跟风候选。",
            vix=vix,
            offensive_strength=offensive_strength,
            defensive_strength=defensive_strength,
            spy_above_ma20=spy_above_ma20,
        )

    if score <= -2:
        return MarketTemperature(
            label="防守",
            signal_code="RISK-OFF",
            color=RED,
            summary="VIX 偏高或进攻板块落后，SPY 也偏弱，盘前更适合降低追高欲望，优先等确认和止损纪律。",
            vix=vix,
            offensive_strength=offensive_strength,
            defensive_strength=defensive_strength,
            spy_above_ma20=spy_above_ma20,
        )

    return MarketTemperature(
        label="中性",
        signal_code="NEUTRAL",
        color=CYAN,
        summary="市场信号分歧，既没有明确 risk-on，也没有明显 risk-off。适合轻仓观察成交量是否继续向强势板块集中。",
        vix=vix,
        offensive_strength=offensive_strength,
        defensive_strength=defensive_strength,
        spy_above_ma20=spy_above_ma20,
    )
