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


def _timestamp_to_date(value: Any) -> str:
    """把 Finnhub/yfinance 的时间戳统一转成交易日文本。"""

    try:
        timestamp = int(value)
        if timestamp > 0:
            return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        pass
    return ""


def _index_to_trade_date(value: Any) -> str:
    """从 yfinance 日线索引安全提取最后一根 K 线日期。"""

    try:
        return pd.Timestamp(value).date().isoformat()
    except (TypeError, ValueError):
        return ""


def _fetch_quote(api_key: str | None, ticker: str) -> dict[str, object] | None:
    """优先用 Finnhub quote；失败时用 yfinance 最近日线补充，并明确标注来源。"""

    if api_key:
        try:
            data = _finnhub_get("quote", api_key, {"symbol": ticker})
            current = float(data.get("c") or 0)
            previous_close = float(data.get("pc") or 0)
            if current > 0 and previous_close > 0:
                return {
                    "price": current,
                    "previous_close": previous_close,
                    "trade_date": _timestamp_to_date(data.get("t")),
                    "data_source": "finnhub",
                }
        except Exception:
            pass

    try:
        hist = safe_download(ticker, period="5d", interval="1d")
        if len(hist) >= 2:
            return {
                "price": _to_float(hist["Close"].iloc[-1]),
                "previous_close": _to_float(hist["Close"].iloc[-2]),
                "trade_date": _index_to_trade_date(hist.index[-1]),
                "data_source": "yfinance",
            }
    except Exception:
        return None
    return None


def _fetch_volume_stats(api_key: str | None, ticker: str) -> dict[str, object] | None:
    """读取最近日线成交量和 20 日均量；失败返回 None，并明确标注来源。"""

    if api_key:
        try:
            now = datetime.now(timezone.utc)
            data = _finnhub_get("stock/candle", api_key, {"symbol": ticker, "resolution": "D", "from": int((now - timedelta(days=45)).timestamp()), "to": int(now.timestamp())})
            volumes = [float(value) for value in data.get("v", []) if value is not None]
            timestamps = data.get("t", [])
            if len(volumes) >= 21:
                today_volume = volumes[-1]
                avg_volume20 = sum(volumes[-21:-1]) / 20
                if avg_volume20 > 0:
                    trade_date = _timestamp_to_date(timestamps[-1]) if timestamps else ""
                    return {
                        "volume": today_volume,
                        "avg_volume20": avg_volume20,
                        "trade_date": trade_date,
                        "data_source": "finnhub",
                    }
        except Exception:
            pass

    try:
        hist = safe_download(ticker, period="2mo", interval="1d")
        if len(hist) >= 21:
            volumes = hist["Volume"].dropna()
            today_volume = _to_float(volumes.iloc[-1])
            avg_volume20 = _to_float(volumes.iloc[-21:-1].mean())
            if avg_volume20 > 0:
                return {
                    "volume": today_volume,
                    "avg_volume20": avg_volume20,
                    "trade_date": _index_to_trade_date(volumes.index[-1]),
                    "data_source": "yfinance",
                }
    except Exception:
        return None
    return None


def _fetch_yfinance_snapshot(ticker: str) -> dict[str, object] | None:
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
        return {
            "price": price,
            "change_pct": change_pct,
            "volume": volume,
            "rvol": rvol,
            "dollar_volume": price * volume,
            "data_source": "yfinance",
            "trade_date": _index_to_trade_date(hist.index[-1]),
            "session": "prev_close",
        }
    except Exception:
        return None


def _fetch_ticker_snapshot(api_key: str | None, ticker: str) -> dict[str, object] | None:
    """整合价格、涨跌幅、成交量、RVOL 和成交额，并标明这是隔夜收盘口径。"""

    if api_key:
        quote = _fetch_quote(api_key, ticker)
        volume_stats = _fetch_volume_stats(api_key, ticker)
        if quote and volume_stats:
            price = float(quote["price"])
            previous_close = float(quote["previous_close"])
            volume = float(volume_stats["volume"])
            avg_volume20 = float(volume_stats["avg_volume20"])
            change_pct = (price - previous_close) / previous_close * 100
            rvol = volume / avg_volume20
            data_source = "finnhub" if quote.get("data_source") == "finnhub" and volume_stats.get("data_source") == "finnhub" else "yfinance"
            return {
                "price": price,
                "change_pct": change_pct,
                "volume": volume,
                "rvol": rvol,
                "dollar_volume": price * volume,
                "data_source": data_source,
                "trade_date": str(quote.get("trade_date") or volume_stats.get("trade_date") or ""),
                "session": "prev_close",
            }

    return _fetch_yfinance_snapshot(ticker)


def fetch_ticker_snapshot(api_key: str | None, ticker: str) -> dict[str, object] | None:
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


def calculate_sector_heat_score(change_pct: float, rvol: float) -> float:
    """板块热度分公式；前向雷达和历史回填必须共用这一处。"""

    return float(change_pct) + min(max(float(rvol) - 1, -1), 4) * 2


def calculate_leader_score(change_pct: float, rvol: float) -> float:
    """板块龙头分公式；保持和 build_sector_radar 的排序口径一致。"""

    return float(change_pct) + min(float(rvol), 5) * 1.5


def _radar_metadata(rows: list[dict[str, object]]) -> dict[str, str]:
    """汇总本次雷达结果的来源标签，页面用于诚实提示。"""

    sources = sorted({str(row.get("data_source") or "") for row in rows if row.get("data_source")})
    dates = sorted({str(row.get("trade_date") or "") for row in rows if row.get("trade_date")}, reverse=True)
    return {
        "data_source": sources[0] if len(sources) == 1 else "混合" if sources else "未知",
        "trade_date": dates[0] if dates else "未知",
        "session": "prev_close",
    }


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
        heat_score = calculate_sector_heat_score(change_pct, rvol)
        sector_rows.append(
            {
                "板块": sector_name,
                "代表ETF": sector_config["etf"],
                "涨跌幅%": change_pct,
                "RVOL": rvol,
                "热度分": heat_score,
                "data_source": str(etf_snapshot.get("data_source") or "未知"),
                "trade_date": str(etf_snapshot.get("trade_date") or ""),
                "session": "prev_close",
            }
        )

    if not sector_rows:
        return {
            "status": RadarStatus(False, "暂时没有可用的板块数据。周末、休市或数据源延迟时可能出现这种情况。"),
            "metadata": {"data_source": "未知", "trade_date": "未知", "session": "prev_close"},
            "sectors": pd.DataFrame(),
            "leaders": pd.DataFrame(),
            "followers": pd.DataFrame(),
        }

    sectors_df = pd.DataFrame(sector_rows).sort_values("热度分", ascending=False).reset_index(drop=True)
    strong_sectors = sectors_df.head(top_sector_count)

    for _, sector in strong_sectors.iterrows():
        sector_name = str(sector["板块"])
        stock_rows: list[dict[str, object]] = []
        for ticker in SECTOR_RADAR_CONFIG[sector_name]["tickers"]:
            snapshot = _fetch_ticker_snapshot(api_key, ticker)
            if not snapshot:
                continue
            stock_rows.append(
                {
                    "板块": sector_name,
                    "代码": ticker,
                    "涨跌幅%": float(snapshot["change_pct"]),
                    "成交量": float(snapshot["volume"]),
                    "成交量文本": _format_volume(float(snapshot["volume"])),
                    "RVOL": float(snapshot["rvol"]),
                    "成交额": float(snapshot["dollar_volume"]),
                    "data_source": str(snapshot.get("data_source") or "未知"),
                    "trade_date": str(snapshot.get("trade_date") or ""),
                    "session": "prev_close",
                }
            )
        if not stock_rows:
            continue

        stock_df = pd.DataFrame(stock_rows)
        stock_df["龙头分"] = stock_df.apply(lambda row: calculate_leader_score(row["涨跌幅%"], row["RVOL"]), axis=1)
        leaders = stock_df.sort_values(["龙头分", "成交额"], ascending=False).head(3)
        leader_rows.extend(leaders[["板块", "代码", "涨跌幅%", "成交量", "RVOL", "data_source", "trade_date", "session"]].to_dict("records"))

        leader_change = float(leaders["涨跌幅%"].max()) if not leaders.empty else 0.0
        followers = stock_df[(stock_df["涨跌幅%"] >= -1.5) & (stock_df["涨跌幅%"] <= max(leader_change - 1.0, 3.0)) & (stock_df["RVOL"] >= 1.2)].sort_values(["RVOL", "涨跌幅%"], ascending=False).head(4)
        follower_rows.extend(followers[["板块", "代码", "涨跌幅%", "RVOL", "data_source", "trade_date", "session"]].to_dict("records"))

    return {
        "status": RadarStatus(True, "板块雷达数据已更新（隔夜收盘口径）"),
        "metadata": _radar_metadata(sector_rows + leader_rows),
        "sectors": sectors_df,
        "leaders": pd.DataFrame(leader_rows),
        "followers": pd.DataFrame(follower_rows),
    }
