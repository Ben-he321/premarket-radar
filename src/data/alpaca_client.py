"""Read-only official alpaca-py adapter. SIP is mandatory; no trading client."""
from datetime import date, datetime, timedelta, timezone
import time
from urllib.parse import urlparse

from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
import pandas as pd
import requests

from .alpaca_calendar import request_bounds
from .alpaca_config import DataConfig, PROBE_SYMBOLS

COLUMNS = ["symbol", "timestamp_original", "timestamp", "trade_date", "open", "high",
           "low", "close", "volume", "trade_count", "vwap"]


class DataAccessError(Exception):
    """Safe user-facing error; never include HTTP body, headers, or credentials."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def error_code(exc):
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return "AUTH_OR_PERMISSION_DENIED"
    # Alpaca also uses HTTP 422 for SIP subscription denial.
    if status == 422 and "subscription" in str(exc).lower():
        return "SIP_PERMISSION_DENIED"
    if status == 429:
        return "RATE_LIMITED"
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return "NETWORK_ERROR"
    return "REQUEST_FAILED"


class PacedStockClient(StockHistoricalDataClient):
    """Pace and retry each SDK HTTP page, with bounded connect/read timeouts.

    _one_request is the only private SDK hook (pinned and tested against 0.44.0).
    The SDK's own pagination is retained, without a total-result limit.
    """
    def __init__(self, config: DataConfig):
        super().__init__(config.api_key, config.secret_key, raw_data=True)
        self._retry = 0
        self.interval = 60 / config.requests_per_minute
        self.last_request = 0.0

    def _one_request(self, method, url, opts, retry):
        if method != "GET" or urlparse(url).netloc != "data.alpaca.markets":
            raise DataAccessError("READ_ONLY_ENDPOINT_REQUIRED")
        for attempt in range(4):
            time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                return super()._one_request(method, url, {**opts, "timeout": (10, 45)}, 0)
            except (APIError, requests.RequestException) as exc:
                status = getattr(exc, "status_code", None)
                transient = status == 429 or (status is not None and status >= 500)
                transient |= isinstance(exc, (requests.Timeout, requests.ConnectionError))
                if not transient or attempt == 3:
                    raise DataAccessError(error_code(exc)) from None
                time.sleep(min(2 ** attempt, 8))


def normalize_bars(payload: dict, symbols: tuple[str, ...]) -> pd.DataFrame:
    rows = []
    if not isinstance(payload, dict) or set(payload) - set(symbols):
        raise DataAccessError("UNEXPECTED_RESPONSE")
    for symbol, bars in payload.items():
        if not isinstance(bars, list):
            raise DataAccessError("UNEXPECTED_RESPONSE")
        for bar in bars:
            original = bar.get("t")
            stamp = pd.Timestamp(original)
            if pd.isna(stamp) or stamp.tzinfo is None:
                raise DataAccessError("INVALID_TIMESTAMP")
            rows.append({"symbol": symbol, "timestamp_original": str(original),
                         "timestamp": stamp.tz_convert("UTC"),
                         "trade_date": stamp.tz_convert("America/New_York").date().isoformat(),
                         **{name: bar.get(key) for name, key in
                            [("open", "o"), ("high", "h"), ("low", "l"), ("close", "c"),
                             ("volume", "v"), ("trade_count", "n"), ("vwap", "vw")]}})
    frame = pd.DataFrame(rows, columns=COLUMNS)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    for name in ("open", "high", "low", "close", "volume", "trade_count", "vwap"):
        frame[name] = pd.to_numeric(frame[name], errors="coerce").astype(float)
    return frame


class AlpacaMarketData:
    def __init__(self, config: DataConfig, sdk=None):
        if not config.has_credentials and sdk is None:
            raise DataAccessError("MISSING_CREDENTIALS")
        self.sdk = sdk if sdk is not None else PacedStockClient(config)
        self.asof = None

    def bars(self, symbols: tuple[str, ...], start: date, end: date, adjustment: str):
        begin, finish = request_bounds(start, end)
        request = StockBarsRequest(symbol_or_symbols=list(symbols), start=begin, end=finish,
                                   timeframe=TimeFrame.Day, feed=DataFeed.SIP,
                                   adjustment=Adjustment(adjustment), asof=str(self.asof or end))
        try:
            frame = normalize_bars(self.sdk.get_stock_bars(request), symbols)
        except DataAccessError:
            raise
        except (APIError, requests.RequestException) as exc:
            raise DataAccessError(error_code(exc)) from None
        except (TypeError, ValueError, KeyError):
            raise DataAccessError("UNEXPECTED_RESPONSE") from None
        if not frame.empty and not frame.trade_date.between(start.isoformat(), end.isoformat()).all():
            raise DataAccessError("OUT_OF_RANGE_RESPONSE")
        return frame

    def probe(self, kind: str, end: date):
        checked = datetime.now(timezone.utc).isoformat()
        self.asof = end
        try:
            if kind == "historical":
                frame = self.bars(PROBE_SYMBOLS, end - timedelta(days=10), end, "raw")
                stamps = {s: frame.loc[frame.symbol == s, "timestamp"].max().isoformat()
                          for s in PROBE_SYMBOLS if s in set(frame.symbol)}
            elif kind == "latest":
                result = self.sdk.get_stock_latest_trade(
                    StockLatestTradeRequest(symbol_or_symbols=list(PROBE_SYMBOLS), feed=DataFeed.SIP))
                stamps = {s: pd.Timestamp(result[s]["t"]).isoformat()
                          for s in PROBE_SYMBOLS if s in result and result[s].get("t")}
            else:
                raise ValueError("unknown probe")
            status = "ACCESS_OK" if set(stamps) == set(PROBE_SYMBOLS) else "INCOMPLETE_RESPONSE"
            return {"check": kind, "feed": "sip", "status": status,
                    "checked_at": checked, "last_timestamps": stamps}
        except DataAccessError as exc:
            code = exc.code
        except (APIError, requests.RequestException) as exc:
            code = error_code(exc)
        except (ValueError, TypeError, KeyError):
            code = "UNEXPECTED_RESPONSE"
        return {"check": kind, "feed": "sip", "status": code, "checked_at": checked}
