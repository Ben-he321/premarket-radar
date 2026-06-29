"""yfinance 安全封装：给单次数据请求加超时，避免一只票卡住整页。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any, Callable, TypeVar

import pandas as pd
import yfinance as yf


YFINANCE_TIMEOUT_SECONDS = 8
T = TypeVar("T")


def _run_with_timeout(func: Callable[[], T], default: T, timeout: int = YFINANCE_TIMEOUT_SECONDS) -> T:
    """在线程中执行阻塞调用；超时或异常时返回默认值。"""

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(func)
    try:
        return future.result(timeout=timeout)
    except (TimeoutError, Exception):
        future.cancel()
        return default
    finally:
        # wait=False 可以让页面先返回，避免 yfinance 底层网络卡死拖住主线程。
        executor.shutdown(wait=False, cancel_futures=True)


def safe_download(ticker: str, *, period: str, interval: str = "1d", timeout: int = YFINANCE_TIMEOUT_SECONDS) -> pd.DataFrame:
    """安全下载单个 ticker 的历史行情；失败返回空 DataFrame。"""

    def _download() -> pd.DataFrame:
        return yf.download(
            ticker,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False,
            threads=False,
            timeout=timeout,
        )

    result = _run_with_timeout(_download, pd.DataFrame(), timeout=timeout)
    return result if isinstance(result, pd.DataFrame) else pd.DataFrame()


def safe_history(ticker: str, *, period: str, interval: str = "1d", timeout: int = YFINANCE_TIMEOUT_SECONDS) -> pd.DataFrame:
    """安全读取 Ticker.history；失败返回空 DataFrame。"""

    def _history() -> pd.DataFrame:
        return yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False, timeout=timeout)

    result = _run_with_timeout(_history, pd.DataFrame(), timeout=timeout)
    return result if isinstance(result, pd.DataFrame) else pd.DataFrame()


def safe_info(ticker: str, *, timeout: int = YFINANCE_TIMEOUT_SECONDS) -> dict[str, Any]:
    """安全读取 yfinance get_info；失败返回空字典。"""

    def _info() -> dict[str, Any]:
        info = yf.Ticker(ticker).get_info()
        return info if isinstance(info, dict) else {}

    result = _run_with_timeout(_info, {}, timeout=timeout)
    return result if isinstance(result, dict) else {}


def safe_fast_info(ticker: str, *, timeout: int = YFINANCE_TIMEOUT_SECONDS) -> dict[str, Any]:
    """安全读取 yfinance fast_info；失败返回空字典。"""

    def _fast_info() -> dict[str, Any]:
        info = yf.Ticker(ticker).fast_info
        if isinstance(info, dict):
            return dict(info)
        return {
            "market_cap": getattr(info, "market_cap", None),
            "last_price": getattr(info, "last_price", None),
            "previous_close": getattr(info, "previous_close", None),
        }

    result = _run_with_timeout(_fast_info, {}, timeout=timeout)
    return result if isinstance(result, dict) else {}
