"""盘前雷达首页：今日作战简报。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys

import pandas as pd
import streamlit as st


# 保证 Streamlit Cloud 和本地运行时都能稳定导入 src 包。
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.data.finnhub_client import get_finnhub_api_key
from src.scoring.sector_radar import build_sector_radar
from src.ui.theme import CYAN, GREEN, RED, inject_global_styles, metric_card, signed_color


st.set_page_config(
    page_title="盘前雷达 Pre-Market Radar",
    page_icon="📡",
    layout="centered",
)
inject_global_styles()


@st.cache_data(ttl=300, show_spinner=False)
def load_home_radar(api_key: str | None) -> dict[str, object]:
    """复用板块雷达数据，首页只取摘要，避免重复请求。"""

    return build_sector_radar(api_key, top_sector_count=4)


def safe_dataframe(value: object) -> pd.DataFrame:
    """把未知结果安全转为 DataFrame。"""

    return value if isinstance(value, pd.DataFrame) else pd.DataFrame()


def fmt_pct(value: float | int | None) -> str:
    """格式化百分比，空值显示占位。"""

    if value is None:
        return "暂无"
    return f"{float(value):+.2f}%"


def market_temperature(sectors_df: pd.DataFrame) -> tuple[str, str, str, str]:
    """用强势板块表现粗略生成 risk-on/risk-off 温度。"""

    if sectors_df.empty or "涨跌幅%" not in sectors_df.columns:
        return "数据等待", "休市或数据源延迟时，今日市场温度会暂时显示为等待。", CYAN, "NEUTRAL"

    top_change = float(sectors_df.iloc[0]["涨跌幅%"])
    avg_top = float(pd.to_numeric(sectors_df.head(4)["涨跌幅%"], errors="coerce").mean())
    if top_change > 0.8 and avg_top > 0:
        return "Risk-On", "强势板块扩散良好，适合优先观察龙头与补涨票。", GREEN, "ON"
    if top_change < -0.5 and avg_top < 0:
        return "Risk-Off", "板块动能偏弱，盘前更适合降低追高欲望，等待确认。", RED, "OFF"
    return "中性观察", "板块分歧仍在，先看成交量是否继续向强板块集中。", CYAN, "WATCH"


def render_sector_table(sectors_df: pd.DataFrame) -> None:
    """渲染首页强势板块简表。"""

    if sectors_df.empty:
        st.info("暂无可展示的板块数据。休市、周末或数据源延迟时属于正常情况。")
        return

    rows = []
    for _, row in sectors_df.head(6).iterrows():
        change = float(row["涨跌幅%"])
        rows.append(
            "<tr>"
            f"<td>{row['板块']}</td>"
            f"<td>{row['代表ETF']}</td>"
            f"<td style='color:{signed_color(change)}; font-weight:700;'>{fmt_pct(change)}</td>"
            f"<td>{float(row['RVOL']):.2f}</td>"
            f"<td><span class='pmr-accent'>{float(row['热度分']):.2f}</span></td>"
            "</tr>"
        )

    st.markdown(
        """
        <table class="pmr-table">
            <thead>
                <tr>
                    <th>板块</th>
                    <th>ETF</th>
                    <th>涨跌幅</th>
                    <th>RVOL</th>
                    <th>热度</th>
                </tr>
            </thead>
            <tbody>
        """
        + "".join(rows)
        + """
            </tbody>
        </table>
        """,
        unsafe_allow_html=True,
    )


api_key = get_finnhub_api_key(st.secrets)
with st.spinner("正在生成今日作战简报..."):
    radar = load_home_radar(api_key)

sectors_df = safe_dataframe(radar.get("sectors"))
leaders_df = safe_dataframe(radar.get("leaders"))
followers_df = safe_dataframe(radar.get("followers"))

temperature, summary, signal_color, signal_code = market_temperature(sectors_df)
top_sector = sectors_df.iloc[0] if not sectors_df.empty else None
top_leader = leaders_df.iloc[0] if not leaders_df.empty else None
top_follower = followers_df.iloc[0] if not followers_df.empty else None

st.markdown("<div class='pmr-kicker'>PRE-MARKET RADAR</div>", unsafe_allow_html=True)
st.markdown("<div class='pmr-title'>今日作战简报</div>", unsafe_allow_html=True)
st.caption(f"更新时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} · 数据延迟可接受，结果仅作盘前观察")

st.markdown(
    f"""
    <div class="pmr-topline">
        <div>
            <div class="pmr-kicker">今日市场温度</div>
            <div class="pmr-title">{temperature}</div>
            <div class="pmr-muted">{summary}</div>
        </div>
        <div class="pmr-signal">
            <span class="pmr-dot" style="background:{signal_color};"></span>
            {signal_code}
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

strong_sector_main = str(top_sector["板块"]) if top_sector is not None else "暂无数据"
strong_sector_detail = (
    f"{top_sector['代表ETF']} {fmt_pct(float(top_sector['涨跌幅%']))} · RVOL {float(top_sector['RVOL']):.2f}"
    if top_sector is not None
    else "休市或数据源延迟时会显示占位。"
)

leader_main = str(top_leader["代码"]) if top_leader is not None else "暂无数据"
leader_detail = (
    f"{top_leader['板块']} · {fmt_pct(float(top_leader['涨跌幅%']))} · RVOL {float(top_leader['RVOL']):.2f}"
    if top_leader is not None
    else "等待板块龙头数据更新。"
)

focus_main = str(top_follower["代码"]) if top_follower is not None else "暂无数据"
focus_detail = (
    f"{top_follower['板块']} · {fmt_pct(float(top_follower['涨跌幅%']))} · RVOL {float(top_follower['RVOL']):.2f}"
    if top_follower is not None
    else "跟风候选需要成交量确认。"
)

st.markdown(
    "<div class='pmr-grid'>"
    + metric_card("最强板块", strong_sector_main, strong_sector_detail, "强度由 ETF 涨幅 + RVOL 近似计算")
    + metric_card("龙头异动", leader_main, leader_detail, "优先观察成交额与持续性")
    + metric_card("今日重点关注", focus_main, focus_detail, "补涨逻辑只作观察，不构成建议")
    + "</div>",
    unsafe_allow_html=True,
)

st.markdown(
    "<div class='pmr-section'><h3>今日强势板块简表</h3><span class='pmr-muted'>热度 = 涨幅 + RVOL 近似</span></div>",
    unsafe_allow_html=True,
)
render_sector_table(sectors_df)

if not api_key:
    st.warning("未检测到 Finnhub API Key，首页已尝试使用 yfinance 补充数据；如需更稳定，请在 Streamlit Secrets 配置 FINNHUB_API_KEY。")
