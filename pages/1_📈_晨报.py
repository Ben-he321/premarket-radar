"""晨报页面：M1 阶段展示 Finnhub gap scanner。"""

import streamlit as st

from src.config import DEFAULT_STOCK_UNIVERSE
from src.data.finnhub_client import QuoteRow, fetch_gap_scanner, get_finnhub_api_key


st.set_page_config(page_title="晨报", page_icon="📈", layout="centered")

st.title("晨报")
st.caption("M1：真实盘前/最新行情 gap scanner，数据来自 Finnhub。")


@st.cache_data(ttl=300, show_spinner=False)
def load_gap_scanner(api_key: str | None) -> tuple[object, list[QuoteRow]]:
    """缓存 Finnhub 查询结果，减少免费额度消耗。"""

    return fetch_gap_scanner(DEFAULT_STOCK_UNIVERSE, api_key)


def format_volume(volume: int) -> str:
    """把成交量格式化为手机上更容易阅读的短文本。"""

    if volume >= 1_000_000:
        return f"{volume / 1_000_000:.1f}M"
    if volume >= 1_000:
        return f"{volume / 1_000:.1f}K"
    return str(volume)


def render_rank_table(title: str, rows: list[QuoteRow]) -> None:
    """用简洁 HTML 表格控制列宽，避免手机端撑出屏幕。"""

    st.subheader(title)

    if not rows:
        st.info("暂无可展示数据。")
        return

    table_rows = []
    for row in rows:
        color = "#22C55E" if row.gap_percent >= 0 else "#EF4444"
        table_rows.append(
            "<tr>"
            f"<td>{row.ticker}</td>"
            f"<td>${row.price:.2f}</td>"
            f"<td style='color:{color}; font-weight:700;'>{row.gap_percent:+.2f}%</td>"
            f"<td>{format_volume(row.volume)}</td>"
            "</tr>"
        )

    st.markdown(
        """
        <style>
        .gap-table {
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
            font-size: 0.92rem;
        }
        .gap-table th, .gap-table td {
            border-bottom: 1px solid rgba(229, 231, 235, 0.18);
            padding: 0.55rem 0.35rem;
            text-align: right;
            white-space: nowrap;
        }
        .gap-table th:first-child, .gap-table td:first-child {
            text-align: left;
        }
        .gap-table th {
            color: #CBD5E1;
            font-weight: 600;
        }
        </style>
        """
        f"""
        <table class="gap-table">
            <thead>
                <tr>
                    <th>代码</th>
                    <th>现价</th>
                    <th>涨跌幅%</th>
                    <th>成交量</th>
                </tr>
            </thead>
            <tbody>
                {''.join(table_rows)}
            </tbody>
        </table>
        """,
        unsafe_allow_html=True,
    )


api_key = get_finnhub_api_key(st.secrets)

with st.spinner("正在读取 Finnhub 行情..."):
    status, scanner_rows = load_gap_scanner(api_key)

st.subheader("数据连接状态")
if status.ok:
    st.success("✅ Finnhub 连接正常")
else:
    st.error(f"❌ {status.message}")

st.info("将显示：大盘红绿灯、热门板块榜、候选股（含 float/gap%/弹性分/逻辑/失效位）")

if scanner_rows:
    gainers = sorted(scanner_rows, key=lambda item: item.gap_percent, reverse=True)[:10]
    losers = sorted(scanner_rows, key=lambda item: item.gap_percent)[:10]

    render_rank_table("盘前涨幅榜 Top 10", gainers)
    render_rank_table("跌幅榜 Top 10", losers)
else:
    st.warning("配置 Finnhub API Key 后，这里会显示真实的盘前涨跌幅榜。")

st.warning("🚧 其他晨报模块建设中（M2+）")
