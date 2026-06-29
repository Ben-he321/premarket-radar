"""Finnhub 数据客户端：读取密钥、连接自检、抓取 gap scanner 所需行情。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Iterable

import requests


FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
REQUEST_TIMEOUT_SECONDS = 8


@dataclass(frozen=True)
class ConnectionStatus:
    """Finnhub 连接状态，页面只展示状态和原因，不展示 API key。"""

    ok: bool
    message: str


@dataclass(frozen=True)
class QuoteRow:
    """晨报表格需要展示的单只股票行情。"""

    ticker: str
    price: float
    gap_percent: float
    volume: int


def _read_dotenv_key(key_name: str) -> str | None:
    """从本地 .env 文件读取指定 key，不依赖额外包，方便新手部署。"""

    env_path = Path(".env")
    if not env_path.exists():
        return None

    for line in env_path.read_text(encoding="utf-8").splitlines():
        clean_line = line.strip()
        if not clean_line or clean_line.startswith("#") or "=" not in clean_line:
            continue

        name, value = clean_line.split("=", 1)
        if name.strip() == key_name:
            return value.strip().strip('"').strip("'") or None

    return None


def get_finnhub_api_key(secrets: object | None = None) -> str | None:
    """按 Streamlit Secrets -> 环境变量 -> 本地 .env 的顺序读取 Finnhub API key。"""

    if secrets is not None:
        try:
            secret_value = secrets["FINNHUB_API_KEY"]  # type: ignore[index]
            if secret_value:
                return str(secret_value).strip()
        except Exception:
            # 没有配置 secrets.toml 或 key 不存在时，继续尝试本地环境变量。
            pass

    env_value = os.getenv("FINNHUB_API_KEY")
    if env_value:
        return env_value.strip()

    return _read_dotenv_key("FINNHUB_API_KEY")


def _get_json(endpoint: str, api_key: str, params: dict[str, object] | None = None) -> dict:
    """调用 Finnhub 并返回 JSON；出错时抛异常，由上层转为中文提示。"""

    query_params = dict(params or {})
    query_params["token"] = api_key

    response = requests.get(
        f"{FINNHUB_BASE_URL}/{endpoint}",
        params=query_params,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()

    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(str(data["error"]))

    if not isinstance(data, dict):
        raise RuntimeError("Finnhub 返回格式异常")

    return data


def check_connection(api_key: str | None) -> ConnectionStatus:
    """使用轻量 quote 接口检查 Finnhub key 是否可用。"""

    if not api_key:
        return ConnectionStatus(
            ok=False,
            message="未检测到 Finnhub API Key，请在 Streamlit Secrets 配置 FINNHUB_API_KEY",
        )

    try:
        data = _get_json("quote", api_key, {"symbol": "AAPL"})
        if "c" not in data:
            return ConnectionStatus(ok=False, message="Finnhub 返回数据缺少行情字段")
        return ConnectionStatus(ok=True, message="Finnhub 连接正常")
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "未知"
        return ConnectionStatus(ok=False, message=f"Finnhub 连接失败：HTTP {status_code}")
    except Exception as exc:
        return ConnectionStatus(ok=False, message=f"Finnhub 连接失败：{exc}")


def _fetch_latest_volume(ticker: str, api_key: str) -> int:
    """读取最近日线成交量；失败时返回 0，避免影响整页。"""

    now = datetime.now(timezone.utc)
    params = {
        "symbol": ticker,
        "resolution": "D",
        "from": int((now - timedelta(days=10)).timestamp()),
        "to": int(now.timestamp()),
    }

    data = _get_json("stock/candle", api_key, params)
    volumes = data.get("v") or []
    if not volumes:
        return 0

    return int(volumes[-1] or 0)


def fetch_quote_row(ticker: str, api_key: str) -> QuoteRow | None:
    """抓取单只股票行情并计算相对前收盘 gap%，失败返回 None。"""

    try:
        quote = _get_json("quote", api_key, {"symbol": ticker})
        current_price = float(quote.get("c") or 0)
        previous_close = float(quote.get("pc") or 0)

        if current_price <= 0 or previous_close <= 0:
            return None

        gap_percent = (current_price - previous_close) / previous_close * 100
        volume = _fetch_latest_volume(ticker, api_key)

        return QuoteRow(
            ticker=ticker,
            price=current_price,
            gap_percent=gap_percent,
            volume=volume,
        )
    except Exception:
        # 单只 ticker 失败时跳过，不能让整个晨报页面崩溃。
        return None


def fetch_gap_scanner(tickers: Iterable[str], api_key: str | None) -> tuple[ConnectionStatus, list[QuoteRow]]:
    """批量抓取股票池行情，返回连接状态和可展示的数据行。"""

    status = check_connection(api_key)
    if not status.ok or not api_key:
        return status, []

    rows: list[QuoteRow] = []
    for ticker in tickers:
        row = fetch_quote_row(ticker, api_key)
        if row is not None:
            rows.append(row)

    if not rows:
        return ConnectionStatus(ok=False, message="Finnhub 连接成功，但暂时没有可展示的行情数据"), []

    return status, rows
