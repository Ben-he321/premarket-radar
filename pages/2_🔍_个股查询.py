"""个股查询页面：输入 ticker 查看基础、基本面与技术位信息。"""

from __future__ import annotations

import html
from pathlib import Path
import sys

import pandas as pd
import requests
import streamlit as st


# Streamlit Cloud 和本地运行时的工作目录可能不同，显式加入项目根目录。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.data.finnhub_client import FINNHUB_BASE_URL, REQUEST_TIMEOUT_SECONDS, get_finnhub_api_key
from src.data.safe_yfinance import safe_history, safe_info
from src.ui.theme import inject_global_styles, render_safe_line_chart, signed_color


st.set_page_config(page_title="个股查询", page_icon="🔍", layout="centered")
inject_global_styles()


def safe_float(value: object) -> float | None:
    """把接口返回值安全转成 float，失败时返回 None。"""

    try:
        if value is None or value == "":
            return None
        result = float(value)
        return result if pd.notna(result) else None
    except (TypeError, ValueError):
        return None


def format_money(value: float | None, prefix: str = "$") -> str:
    """格式化价格或金额。"""

    if value is None:
        return "暂无"
    return f"{prefix}{value:,.2f}"


def format_large_number(value: float | None) -> str:
    """把市值、股本、成交量格式化为更易读的单位。"""

    if value is None:
        return "暂无"
    abs_value = abs(value)
    if abs_value >= 1_000_000_000_000:
        return f"{value / 1_000_000_000_000:.2f}T"
    if abs_value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if abs_value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if abs_value >= 1_000:
        return f"{value / 1_000:.2f}K"
    return f"{value:,.0f}"


def format_percent(value: float | None) -> str:
    """格式化百分比。"""

    if value is None:
        return "暂无"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def clean_ticker(value: str) -> str:
    """规范化 ticker 输入。"""

    return value.strip().upper().replace(" ", "")


def get_finnhub_json(endpoint: str, api_key: str | None, params: dict[str, object]) -> dict[str, object]:
    """轻量调用 Finnhub；没有 key 或失败时返回空字典。"""

    if not api_key:
        return {}
    try:
        query = dict(params)
        query["token"] = api_key
        response = requests.get(
            f"{FINNHUB_BASE_URL}/{endpoint}",
            params=query,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) and not data.get("error") else {}
    except Exception:
        # 单项数据失败只返回空结果，让页面继续用 yfinance 补充。
        return {}


def load_yfinance_history(ticker: str) -> pd.DataFrame:
    """读取近 3 个月日线，用于技术位和 30 日小图表。"""

    try:
        history = safe_history(ticker, period="3mo", interval="1d")
        if history.empty:
            return pd.DataFrame()
        history = history.reset_index()
        if "Date" in history.columns:
            history["Date"] = pd.to_datetime(history["Date"]).dt.tz_localize(None)
            history = history.set_index("Date")
        return history.dropna(how="all")
    except Exception:
        return pd.DataFrame()


def load_yfinance_info(ticker: str) -> dict[str, object]:
    """读取 yfinance 基本面信息；失败时返回空字典。"""

    try:
        return safe_info(ticker)
    except Exception:
        return {}


def calculate_atr(history: pd.DataFrame, window: int = 14) -> float | None:
    """用日线计算 ATR，历史数据不足时返回 None。"""

    required = {"High", "Low", "Close"}
    if history.empty or not required.issubset(history.columns) or len(history) < window + 1:
        return None

    high = pd.to_numeric(history["High"], errors="coerce")
    low = pd.to_numeric(history["Low"], errors="coerce")
    close = pd.to_numeric(history["Close"], errors="coerce")
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.rolling(window).mean().dropna()
    return safe_float(atr.iloc[-1]) if not atr.empty else None


def latest_history_value(history: pd.DataFrame, column: str) -> float | None:
    """读取历史日线最后一个有效值。"""

    if history.empty or column not in history.columns:
        return None
    series = pd.to_numeric(history[column], errors="coerce").dropna()
    return safe_float(series.iloc[-1]) if not series.empty else None


@st.cache_data(ttl=300, show_spinner=False)
def load_stock_snapshot(ticker: str, api_key: str | None) -> dict[str, object]:
    """缓存个股快照，减少 Finnhub/yfinance 调用次数。"""

    quote = get_finnhub_json("quote", api_key, {"symbol": ticker})
    profile = get_finnhub_json("stock/profile2", api_key, {"symbol": ticker})
    history = load_yfinance_history(ticker)
    yf_info = load_yfinance_info(ticker)

    current_price = safe_float(quote.get("c")) or safe_float(yf_info.get("currentPrice")) or latest_history_value(history, "Close")
    previous_close = safe_float(quote.get("pc")) or safe_float(yf_info.get("previousClose"))
    change_percent = None
    if current_price is not None and previous_close and previous_close > 0:
        change_percent = (current_price - previous_close) / previous_close * 100

    volume = latest_history_value(history, "Volume") or safe_float(yf_info.get("volume"))
    market_cap = safe_float(profile.get("marketCapitalization"))
    if market_cap is not None:
        # Finnhub 的 marketCapitalization 单位通常是百万美元，转为美元。
        market_cap *= 1_000_000
    market_cap = market_cap or safe_float(yf_info.get("marketCap"))

    float_shares = safe_float(yf_info.get("floatShares"))
    shares_outstanding = safe_float(profile.get("shareOutstanding"))
    if shares_outstanding is not None:
        shares_outstanding *= 1_000_000
    float_or_shares = float_shares or shares_outstanding or safe_float(yf_info.get("sharesOutstanding"))

    close = pd.to_numeric(history["Close"], errors="coerce").dropna() if not history.empty and "Close" in history else pd.Series(dtype=float)
    ma5 = safe_float(close.tail(5).mean()) if len(close) >= 5 else None
    ma10 = safe_float(close.tail(10).mean()) if len(close) >= 10 else None
    ma20 = safe_float(close.tail(20).mean()) if len(close) >= 20 else None
    recent_high = safe_float(pd.to_numeric(history["High"], errors="coerce").tail(30).max()) if not history.empty and "High" in history else None
    recent_low = safe_float(pd.to_numeric(history["Low"], errors="coerce").tail(30).min()) if not history.empty and "Low" in history else None
    atr = calculate_atr(history)

    chart_df = pd.DataFrame()
    if not close.empty:
        chart_series = pd.to_numeric(close.tail(30), errors="coerce").dropna().astype(float)
        chart_df = chart_series.to_frame(name="收盘价")
        chart_df.index.name = "日期"

    return {
        "ticker": ticker,
        "quote": quote,
        "profile": profile,
        "yf_info": yf_info,
        "history": history,
        "chart_df": chart_df,
        "company_name": profile.get("name") or yf_info.get("longName") or yf_info.get("shortName") or ticker,
        "current_price": current_price,
        "change_percent": change_percent,
        "volume": volume,
        "market_cap": market_cap,
        "float_or_shares": float_or_shares,
        "industry": profile.get("finnhubIndustry") or yf_info.get("industry") or "暂无",
        "sector": yf_info.get("sector") or "暂无",
        "recent_high": recent_high,
        "recent_low": recent_low,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "atr": atr,
        "has_any_data": bool(quote or profile or yf_info or not history.empty),
    }


def render_card(title: str, rows: list[tuple[str, str, str | None]]) -> None:
    """渲染统一深色卡片，rows 为 标签、数值、颜色。"""

    body = []
    for label, value, color in rows:
        value_style = f" style='color:{color}; font-weight:760;'" if color else ""
        body.append(
            "<div class='stock-row'>"
            f"<span>{html.escape(label)}</span>"
            f"<strong{value_style}>{html.escape(value)}</strong>"
            "</div>"
        )

    st.markdown(
        f"""
        <div class="pmr-card stock-card">
            <div class="pmr-card-label">{html.escape(title)}</div>
            {''.join(body)}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_page_styles() -> None:
    """补充个股查询页的小范围样式。"""

    st.markdown(
        """
        <style>
        .stock-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 0.85rem;
            margin: 1rem 0 1.2rem;
        }
        .stock-card {
            min-height: 12rem;
        }
        .stock-row {
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            gap: 0.75rem;
            border-bottom: 1px solid rgba(148, 163, 184, 0.10);
            padding: 0.48rem 0;
        }
        .stock-row span {
            color: var(--pmr-dim);
            font-size: 0.84rem;
        }
        .stock-row strong {
            color: var(--pmr-text);
            font-size: 0.93rem;
            text-align: right;
            overflow-wrap: anywhere;
        }
        .placeholder-band {
            border: 1px solid var(--pmr-border);
            background: rgba(10, 16, 23, 0.86);
            border-radius: 8px;
            padding: 1rem;
            margin-top: 0.8rem;
        }
        @media (max-width: 760px) {
            .stock-grid {
                grid-template-columns: 1fr;
            }
            .stock-card {
                min-height: auto;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


render_page_styles()

st.markdown("<div class='pmr-kicker'>STOCK LOOKUP</div>", unsafe_allow_html=True)
st.title("个股查询")
st.caption("输入 ticker，查看基础行情、基本面、技术位和近 30 日走势。")

api_key = get_finnhub_api_key(st.secrets)
if not api_key:
    st.info("未检测到 Finnhub API Key，将优先使用 yfinance 补充可用数据。")

with st.form("stock_lookup_form"):
    ticker_input = st.text_input("输入股票代码（ticker）", placeholder="例如：NVDA / AAPL / TSLA")
    submitted = st.form_submit_button("查询", type="primary")

ticker = clean_ticker(ticker_input)

if not ticker:
    st.info("请输入一个美股 ticker 开始查询。示例：NVDA、AAPL、TSLA。")
    st.warning("AI 解读 和 Gamma/期权 模块已预留，M3+ 阶段接入。")
else:
    with st.spinner(f"正在查询 {ticker}..."):
        snapshot = load_stock_snapshot(ticker, api_key)

    if not snapshot["has_any_data"]:
        st.error(f"没有查到 {ticker} 的可用数据。请检查 ticker 是否正确，或稍后再试。")
    else:
        change_percent = snapshot["change_percent"]
        change_color = signed_color(float(change_percent or 0))

        st.markdown(
            f"""
            <div class="pmr-topline">
                <div>
                    <div class="pmr-kicker">{html.escape(ticker)}</div>
                    <div class="pmr-title">{html.escape(str(snapshot['company_name']))}</div>
                    <div class="pmr-muted">数据来自 Finnhub，缺失字段由 yfinance 补充；免费数据可能延迟。</div>
                </div>
                <div class="pmr-signal">
                    <span class="pmr-dot" style="background:{change_color};"></span>
                    {format_percent(change_percent)}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        col1, col2, col3 = st.columns(3)
        with col1:
            render_card(
                "基本信息",
                [
                    ("公司名", str(snapshot["company_name"]), None),
                    ("当前价", format_money(snapshot["current_price"]), "var(--pmr-cyan)"),
                    ("当日涨跌幅", format_percent(change_percent), change_color),
                    ("成交量", format_large_number(snapshot["volume"]), None),
                ],
            )
        with col2:
            render_card(
                "基本面",
                [
                    ("市值", format_large_number(snapshot["market_cap"]), "var(--pmr-cyan)"),
                    ("float/股本", format_large_number(snapshot["float_or_shares"]), None),
                    ("行业", str(snapshot["industry"]), None),
                    ("板块", str(snapshot["sector"]), None),
                ],
            )
        with col3:
            render_card(
                "技术位",
                [
                    ("30日高点", format_money(snapshot["recent_high"]), None),
                    ("30日低点", format_money(snapshot["recent_low"]), None),
                    (
                        "MA5 / MA10 / MA20",
                        f"{format_money(snapshot['ma5'])} / {format_money(snapshot['ma10'])} / {format_money(snapshot['ma20'])}",
                        "var(--pmr-cyan)",
                    ),
                    ("ATR(14)", format_money(snapshot["atr"]), None),
                ],
            )

        st.subheader("近 30 日价格走势")
        chart_df = snapshot["chart_df"]
        render_safe_line_chart(chart_df, "暂无足够历史数据绘制近 30 日走势。")

        st.subheader("预留模块")
        st.markdown(
            """
            <div class="placeholder-band">
                <div class="pmr-title">AI 解读</div>
                <div class="pmr-muted">M3+ 建设中：未来将结合新闻、基本面、技术位和板块语境生成中文交易解读。</div>
            </div>
            <div class="placeholder-band">
                <div class="pmr-title">Gamma/期权</div>
                <div class="pmr-muted">M3+ 建设中：未来将展示 Gamma、call wall、put wall、期权情绪和关键价位。</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
