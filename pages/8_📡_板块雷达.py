"""板块雷达页面。"""

from pathlib import Path
import sys

import pandas as pd
import streamlit as st


# Streamlit Cloud 有时会以 pages/ 作为脚本上下文，显式加入项目根目录可保证 src 包可导入。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.data.finnhub_client import get_finnhub_api_key
from src.scoring.sector_radar import build_sector_radar
from src.ui.theme import inject_global_styles


st.set_page_config(page_title="板块雷达", page_icon="📡", layout="centered")
inject_global_styles()

st.title("板块雷达")
st.caption("M2：先找强势板块，再找板块龙头，最后找板块内跟风候选。")
st.info("富途的热度榜这里暂时没有，用「涨幅 + 相对成交量 RVOL」作为热度近似替代。")


@st.cache_data(ttl=300, show_spinner=False)
def load_sector_radar(api_key: str | None) -> dict[str, object]:
    """缓存板块雷达数据，减少 Finnhub 免费额度消耗。"""

    return build_sector_radar(api_key)


def render_table(df: pd.DataFrame, columns: list[str]) -> None:
    """用 HTML 表格控制列宽和颜色，避免手机端横向溢出。"""

    if df.empty:
        st.warning("暂无可展示数据。休市、周末或数据源延迟时可能出现这种情况。")
        return

    rows = []
    for _, item in df[columns].iterrows():
        cells = []
        for column in columns:
            value = item[column]
            style = ""
            if column == "涨跌幅%":
                color = "#22C55E" if float(value) >= 0 else "#EF4444"
                style = f" style='color:{color}; font-weight:700;'"
                value = f"{float(value):+.2f}%"
            elif column in {"RVOL", "热度分"}:
                value = f"{float(value):.2f}"
            elif column == "成交量":
                numeric_value = float(value)
                if numeric_value >= 1_000_000:
                    value = f"{numeric_value / 1_000_000:.1f}M"
                elif numeric_value >= 1_000:
                    value = f"{numeric_value / 1_000:.1f}K"
                else:
                    value = f"{numeric_value:.0f}"
            cells.append(f"<td{style}>{value}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")

    header = "".join(f"<th>{column}</th>" for column in columns)
    st.markdown(
        """
        <style>
        .sector-table {
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
            font-size: 0.9rem;
        }
        .sector-table th, .sector-table td {
            border-bottom: 1px solid rgba(229, 231, 235, 0.16);
            padding: 0.5rem 0.25rem;
            text-align: right;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .sector-table th:first-child, .sector-table td:first-child {
            text-align: left;
        }
        .sector-table th {
            color: #CBD5E1;
            font-weight: 600;
        }
        </style>
        """
        f"""
        <table class="sector-table">
            <thead><tr>{header}</tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
        """,
        unsafe_allow_html=True,
    )


api_key = get_finnhub_api_key(st.secrets)
if not api_key:
    st.warning("未检测到 Finnhub API Key，将尝试用 yfinance 补充数据；如需更稳定，请在 Streamlit Secrets 配置 FINNHUB_API_KEY。")

with st.spinner("正在扫描板块强弱和成分股..."):
    result = load_sector_radar(api_key)

status = result["status"]
if status.ok:
    st.success(f"✅ {status.message}")
else:
    st.error(f"❌ {status.message}")

sectors_df = result["sectors"]
leaders_df = result["leaders"]
followers_df = result["followers"]

st.subheader("① 今日强势板块榜")
render_table(sectors_df.head(8), ["板块", "代表ETF", "涨跌幅%", "RVOL", "热度分"])

st.subheader("② 各强板块龙头")
render_table(leaders_df, ["板块", "代码", "涨跌幅%", "成交量", "RVOL"])

st.subheader("③ 板块内跟风候选")
render_table(followers_df, ["板块", "代码", "涨跌幅%", "RVOL"])

st.warning("说明：免费数据可能延迟；休市或周末时，当日涨跌幅和成交量可能不是实时交易日数据。")
