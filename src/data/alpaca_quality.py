"""Report evidence, never infer an IPO/halt or impute missing market values."""
from datetime import date
import numpy as np
import pandas as pd
from .alpaca_calendar import sessions


def inspect_quality(frame, symbols, start: date, end: date):
    expected = {d.isoformat() for d in sessions(start, end)}
    issues = []

    def add(symbol, code, dates, severity="WARNING"):
        if len(dates):
            issues.append({"symbol": symbol, "code": code, "severity": severity,
                           "count": len(dates), "dates": sorted(set(map(str, dates)))})

    for symbol in symbols:
        data = frame[frame.symbol == symbol].sort_values("trade_date")
        observed = set(data.trade_date)
        missing = expected - observed
        if not observed:
            add(symbol, "UNKNOWN_NO_DATA", sorted(expected))
            continue
        first = min(observed)
        add(symbol, "UNKNOWN_PREHISTORY", [d for d in missing if d < first])
        add(symbol, "UNKNOWN_MISSING_SESSION", [d for d in missing if d >= first])
        add(symbol, "NON_SESSION_BAR", observed - expected, "ERROR")
        add(symbol, "DUPLICATE", data.loc[data.duplicated(["symbol", "trade_date"], keep=False), "trade_date"], "ERROR")
        fields = ["open", "high", "low", "close", "volume", "vwap", "trade_count"]
        add(symbol, "MISSING_VALUE", data.loc[data[fields].isna().any(axis=1), "trade_date"], "ERROR")
        prices = data[["open", "high", "low", "close", "vwap"]]
        invalid = ((prices <= 0) | ~np.isfinite(prices)).any(axis=1)
        add(symbol, "INVALID_PRICE", data.loc[invalid, "trade_date"], "ERROR")
        invalid_ohlc = ((data.low > data.high) | (data.open < data.low) |
                       (data.open > data.high) | (data.close < data.low) | (data.close > data.high))
        add(symbol, "OHLC_RELATION", data.loc[invalid_ohlc, "trade_date"], "ERROR")
        volume = data.volume
        bad_volume = volume.isna() | ~np.isfinite(volume) | (volume < 0)
        add(symbol, "INVALID_VOLUME", data.loc[bad_volume, "trade_date"], "ERROR")
        baseline = volume.shift(1).rolling(20, min_periods=5).median()
        abnormal = (volume == 0) | (volume > baseline * 20) | (volume < baseline / 100)
        add(symbol, "ABNORMAL_VOLUME_REVIEW", data.loc[abnormal, "trade_date"])
    return {"issues": issues, "calendar": "NYSE (pandas-market-calendars)",
            "closed_calendar_days": (end - start).days + 1 - len(expected),
            "halt_verification": "UNKNOWN", "listing_verification": "UNKNOWN",
            "passed_numeric_checks": not any(i["severity"] == "ERROR" for i in issues)}
