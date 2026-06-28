"""策略回测页面。"""

from pathlib import Path
import sys

import pandas as pd
import streamlit as st


# Streamlit Cloud 有时会以 pages/ 作为脚本上下文，显式加入项目根目录可保证 src 包可导入。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.backtesting.ma_volume_strategy import run_strategy_backtest
from src.config import BACKTEST_STOCK_UNIVERSE
from src.ui.theme import inject_global_styles, render_safe_line_chart


st.set_page_config(page_title="策略回测", page_icon="🧪", layout="centered")
inject_global_styles()

st.title("策略回测")
st.caption("插队任务：回测「踩5日线穿10日线放量」信号，对比纪律A和纪律B。")
st.warning("结果仅供参考。历史回测不代表未来收益；市值筛选使用 yfinance 当前 market cap 近似值。")


def format_percent(value: float) -> str:
    """把小数收益率展示成百分比。"""

    return f"{value * 100:.2f}%"


def display_metrics_table(metrics_df: pd.DataFrame) -> None:
    """展示主要回测指标，列数保持克制，适配手机。"""

    if metrics_df.empty:
        st.info("暂无回测结果。")
        return

    display_df = metrics_df.copy()
    for column in ["胜率", "平均盈利%", "平均亏损%", "期望值", "最大回撤"]:
        display_df[column] = display_df[column].map(format_percent)
    display_df["盈亏比"] = display_df["盈亏比"].map(lambda value: f"{value:.2f}")
    st.dataframe(display_df, use_container_width=True, hide_index=True)


def prepare_equity_chart(equity_curve_df: pd.DataFrame) -> pd.DataFrame:
    """把收益曲线整理成安全图表函数可渲染的数值型宽表。"""

    required_columns = {"交易序号", "纪律", "累计收益"}
    if equity_curve_df.empty or not required_columns.issubset(equity_curve_df.columns):
        return pd.DataFrame()

    clean_df = equity_curve_df[list(required_columns)].copy()
    clean_df["交易序号"] = pd.to_numeric(clean_df["交易序号"], errors="coerce")
    clean_df["累计收益"] = pd.to_numeric(clean_df["累计收益"], errors="coerce")
    clean_df = clean_df.dropna(subset=["交易序号", "纪律", "累计收益"])
    if clean_df.empty:
        return pd.DataFrame()

    clean_df["交易序号"] = clean_df["交易序号"].astype(int)
    chart_df = clean_df.pivot_table(index="交易序号", columns="纪律", values="累计收益", aggfunc="last").sort_index()
    chart_df = chart_df.apply(pd.to_numeric, errors="coerce")
    chart_df = chart_df.replace([float("inf"), float("-inf")], pd.NA).dropna(axis=1, how="all")
    chart_df.columns.name = None
    return chart_df


def build_plain_summary(metrics_df: pd.DataFrame) -> str:
    """生成大白话总结。"""

    if metrics_df.empty:
        return "这次没有得到有效交易，可能是股票池太小、周期太短，或者数据源暂时不可用。"

    best = metrics_df.sort_values("期望值", ascending=False).iloc[0]
    profitable = best["期望值"] > 0
    verdict = "整体看有正期望" if profitable else "整体看暂时没有正期望"
    return (
        f"{verdict}。当前表现最好的是「{best['纪律']}」，每笔平均收益约 {format_percent(float(best['期望值']))}，"
        f"胜率约 {format_percent(float(best['胜率']))}。A 的优势是少追高，结果通常更稳；"
        "B 的优势是吃强势延续，但追高和移动止损会放大波动。如果最大回撤偏大，说明这套打法对连续亏损比较敏感，需要再加市场环境过滤。"
    )


@st.cache_data(ttl=3600, show_spinner=False)
def cached_backtest(tickers: tuple[str, ...], period: str) -> dict[str, object]:
    """缓存回测结果，避免页面每次刷新都重新下载数据。"""

    return run_strategy_backtest(list(tickers), period=period)


period = st.selectbox("回测周期", options=["1y", "2y", "5y"], index=1, help="周期越长越慢，但样本更充分。")
universe_size = st.selectbox(
    "股票池规模",
    options=[80, 150, 300, len(BACKTEST_STOCK_UNIVERSE)],
    format_func=lambda value: "全量股票池" if value == len(BACKTEST_STOCK_UNIVERSE) else f"前 {value} 只（较快）",
    index=1,
    help="全量更接近目标股票池，但免费数据源可能较慢。",
)
selected_tickers = tuple(BACKTEST_STOCK_UNIVERSE[:universe_size])
st.info(f"本次将尝试回测 {len(selected_tickers)} 只股票；单只股票数据缺失或异常会自动跳过。")

if st.button("开始回测", type="primary"):
    progress_bar = st.progress(0)
    status_text = st.empty()
    with st.spinner("正在回测，请稍等..."):
        progress_bar.progress(0.1)
        status_text.write("正在读取缓存或下载历史日 K 数据...")
        result = cached_backtest(selected_tickers, period)
        progress_bar.progress(0.8)
        status_text.write("正在整理指标和收益曲线...")
    progress_bar.progress(1.0)
    status_text.write("回测完成。")
    st.session_state["backtest_result"] = result
else:
    result = st.session_state.get("backtest_result")

if result:
    metrics_df = result["metrics_df"]
    group_metrics_df = result["group_metrics_df"]
    equity_curve_df = result["equity_curve_df"]
    market_cap_df = result["market_cap_df"]
    skipped = result["skipped"]

    st.subheader("核心指标")
    display_metrics_table(metrics_df)

    st.subheader("累计收益曲线")
    chart_df = prepare_equity_chart(equity_curve_df)
    render_safe_line_chart(chart_df, "暂无可绘制的收益曲线，可能是有效交易太少或数据源暂时返回异常。")

    st.subheader("按市值分组对比")
    if not group_metrics_df.empty:
        group_display = group_metrics_df.copy()
        for column in ["胜率", "期望值", "最大回撤"]:
            group_display[column] = group_display[column].map(format_percent)
        st.dataframe(group_display, use_container_width=True, hide_index=True)
    else:
        st.info("市值分组样本不足，暂时无法对比。")

    st.subheader("大白话总结")
    st.write(build_plain_summary(metrics_df))

    with st.expander("数据质量与筛选详情"):
        included_count = int((market_cap_df["是否纳入"] == "是").sum()) if not market_cap_df.empty else 0
        st.write(f"通过市值近似筛选：{included_count} 只。")
        st.write(f"历史数据缺失或异常跳过：{len(skipped)} 只。")
        if skipped:
            st.write("跳过 ticker：", ", ".join(skipped[:80]))
else:
    st.info("点击「开始回测」后，会在这里显示策略表现。")
