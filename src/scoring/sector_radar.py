"""自上而下板块雷达：强势板块、龙头、跟风候选。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import requests

from src.config import SECTOR_RADAR_CONFIG
from src.data.finnhub_client import FINNHUB_BASE_URL, REQUEST_TIMEOUT_SECONDS
from src.data.safe_yfinance import safe_download


@dataclass(frozen=True)
class RadarStatus:
    """页面展示用状态，不包含任何 API key 内容。"""

    ok: bool
    message: str


def _finnhub_get(endpoint: str, api_key: str, params: dict[str, Any]) -> dict[str, Any]:
    """轻量 Finnhub GET 封装，异常交给上层转成友好提示。"""

    query = dict(params)
    query["token"] = api_key
    response = requests.get(f"{FINNHUB_BASE_URL}/{endpoint}", params=query, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Finnhub 返回格式异常")
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data


def _to_float(value: Any) -> float:
    """兼容 pandas 标量和单元素 Series，避免 yfinance 返回结构变化导致警告。"""

    if isinstance(value, pd.Series):
        return float(value.iloc[0])
    return float(value)


def _fetch_quote(api_key: str | None, ticker: str) -> dict[str, float] | None:
    """优先用 Finnhub quote；失败时用 yfinance 最近日线补充。"""

    if api_key:
        try:
            data = _finnhub_get("quote", api_key, {"symbol": ticker})
            current = float(data.get("c") or 0)
            previous_close = float(data.get("pc") or 0)
            if current > 0 and previous_close > 0:
                return {"price": current, "previous_close": previous_close}
        except Exception:
            pass

    try:
        hist = safe_download(ticker, period="5d", interval="1d")
        if len(hist) >= 2:
            return {"price": _to_float(hist["Close"].iloc[-1]), "previous_close": _to_float(hist["Close"].iloc[-2])}
    except Exception:
        return None
    return None


def _fetch_volume_stats(api_key: str | None, ticker: str) -> dict[str, float] | None:
    """读取今日成交量和 20 日均量；失败返回 None。"""

    if api_key:
        try:
            now = datetime.now(timezone.utc)
            data = _finnhub_get("stock/candle", api_key, {"symbol": ticker, "resolution": "D", "from": int((now - timedelta(days=45)).timestamp()), "to": int(now.timestamp())})
            volumes = [float(value) for value in data.get("v", []) if value is not None]
            if len(volumes) >= 21:
                today_volume = volumes[-1]
                avg_volume20 = sum(volumes[-21:-1]) / 20
                if avg_volume20 > 0:
                    return {"volume": today_volume, "avg_volume20": avg_volume20}
        except Exception:
            pass

    try:
        hist = safe_download(ticker, period="2mo", interval="1d")
        if len(hist) >= 21:
            volumes = hist["Volume"].dropna()
            today_volume = _to_float(volumes.iloc[-1])
            avg_volume20 = _to_float(volumes.iloc[-21:-1].mean())
            if avg_volume20 > 0:
                return {"volume": today_volume, "avg_volume20": avg_volume20}
    except Exception:
        return None
    return None


def _fetch_yfinance_snapshot(ticker: str) -> dict[str, float] | None:
    """用一次 yfinance 日线下载同时计算价格、涨跌幅、成交量和 RVOL。"""

    try:
        hist = safe_download(ticker, period="2mo", interval="1d")
        if len(hist) < 21:
            return None
        close = pd.to_numeric(hist["Close"], errors="coerce").dropna()
        volume_series = pd.to_numeric(hist["Volume"], errors="coerce").dropna()
        if len(close) < 2 or len(volume_series) < 21:
            return None
        price = _to_float(close.iloc[-1])
        previous_close = _to_float(close.iloc[-2])
        volume = _to_float(volume_series.iloc[-1])
        avg_volume20 = _to_float(volume_series.iloc[-21:-1].mean())
        if price <= 0 or previous_close <= 0 or avg_volume20 <= 0:
            return None
        change_pct = (price - previous_close) / previous_close * 100
        rvol = volume / avg_volume20
        return {"price": price, "change_pct": change_pct, "volume": volume, "rvol": rvol, "dollar_volume": price * volume}
    except Exception:
        return None


def _fetch_ticker_snapshot(api_key: str | None, ticker: str) -> dict[str, float] | None:
    """整合价格、涨跌幅、成交量、RVOL 和成交额。"""

    if api_key:
        quote = _fetch_quote(api_key, ticker)
        volume_stats = _fetch_volume_stats(api_key, ticker)
        if quote and volume_stats:
            previous_close = quote["previous_close"]
            change_pct = (quote["price"] - previous_close) / previous_close * 100
            volume = volume_stats["volume"]
            rvol = volume / volume_stats["avg_volume20"]
            return {"price": quote["price"], "change_pct": change_pct, "volume": volume, "rvol": rvol, "dollar_volume": quote["price"] * volume}

    return _fetch_yfinance_snapshot(ticker)


def fetch_ticker_snapshot(api_key: str | None, ticker: str) -> dict[str, float] | None:
    """公开给影子组合引擎使用的单票快照；失败返回 None，不影响页面加载。"""

    try:
        return _fetch_ticker_snapshot(api_key, ticker)
    except Exception:
        return None


def _format_volume(value: float) -> str:
    """把成交量压缩成移动端友好的短文本。"""

    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"


def build_sector_radar(api_key: str | None, top_sector_count: int = 5) -> dict[str, object]:
    """生成板块强弱榜、龙头和跟风候选。"""

    sector_rows: list[dict[str, object]] = []
    leader_rows: list[dict[str, object]] = []
    follower_rows: list[dict[str, object]] = []

    for sector_name, sector_config in SECTOR_RADAR_CONFIG.items():
        etf_snapshot = _fetch_ticker_snapshot(api_key, sector_config["etf"])
        if not etf_snapshot:
            continue
        rvol = float(etf_snapshot["rvol"])
        change_pct = float(etf_snapshot["change_pct"])
        heat_score = change_pct + min(max(rvol - 1, -1), 4) * 2
        sector_rows.append({"板块": sector_name, "代表ETF": sector_config["etf"], "涨跌幅%": change_pct, "RVOL": rvol, "热度分": heat_score})

    if not sector_rows:
        return {"status": RadarStatus(False, "暂时没有可用的板块数据。周末、休市或免费数据源延迟时可能出现这种情况。"), "sectors": pd.DataFrame(), "leaders": pd.DataFrame(), "followers": pd.DataFrame()}

    sectors_df = pd.DataFrame(sector_rows).sort_values("热度分", ascending=False).reset_index(drop=True)
    strong_sectors = sectors_df.head(top_sector_count)

    for _, sector in strong_sectors.iterrows():
        sector_name = str(sector["板块"])
        stock_rows: list[dict[str, object]] = []
        for ticker in SECTOR_RADAR_CONFIG[sector_name]["tickers"]:
            snapshot = _fetch_ticker_snapshot(api_key, ticker)
            if not snapshot:
                continue
            stock_rows.append({"板块": sector_name, "代码": ticker, "涨跌幅%": float(snapshot["change_pct"]), "成交量": _format_volume(float(snapshot["volume"])), "RVOL": float(snapshot["rvol"]), "成交额": float(snapshot["dollar_volume"])})
        if not stock_rows:
            continue

        stock_df = pd.DataFrame(stock_rows)
        stock_df["龙头分"] = stock_df["涨跌幅%"] + stock_df["RVOL"].clip(upper=5) * 1.5
        leaders = stock_df.sort_values(["龙头分", "成交额"], ascending=False).head(3)
        leader_rows.extend(leaders[["板块", "代码", "涨跌幅%", "成交量", "RVOL"]].to_dict("records"))

        leader_change = float(leaders["涨跌幅%"].max()) if not leaders.empty else 0.0
        followers = stock_df[(stock_df["涨跌幅%"] >= -1.5) & (stock_df["涨跌幅%"] <= max(leader_change - 1.0, 3.0)) & (stock_df["RVOL"] >= 1.2)].sort_values(["RVOL", "涨跌幅%"], ascending=False).head(4)
        follower_rows.extend(followers[["板块", "代码", "涨跌幅%", "RVOL"]].to_dict("records"))

    return {"status": RadarStatus(True, "板块雷达数据已更新"), "sectors": sectors_df, "leaders": pd.DataFrame(leader_rows), "followers": pd.DataFrame(follower_rows)}
