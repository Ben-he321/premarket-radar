"""影子组合页面：全自动虚拟盘框架。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

import pandas as pd
import streamlit as st


# 保证 Streamlit Cloud 和本地运行时都能稳定导入 src 包。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.supabase_client import fetch_rows, get_supabase_client, insert_row
from src.ui.theme import inject_global_styles, metric_card, render_safe_line_chart, signed_color


INITIAL_CAPITAL = 4500.0


st.set_page_config(page_title="影子组合", page_icon="🧪", layout="centered")
inject_global_styles()


def format_eur(value: float | int | None) -> str:
    """格式化欧元金额。"""

    if value is None:
        return "暂无"
    sign = "+" if float(value) > 0 else ""
    return f"{sign}€{float(value):,.2f}"


def to_float(value: object, default: float = 0.0) -> float:
    """安全转 float。"""

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build_equity_curve(account_rows: list[dict[str, object]]) -> pd.DataFrame:
    """从账户快照生成收益曲线。"""

    if not account_rows:
        return pd.DataFrame()

    rows = []
    for item in account_rows:
        account_date = item.get("account_date")
        total_equity = item.get("total_equity")
        if account_date is None or total_equity is None:
            continue
        rows.append({"日期": pd.to_datetime(account_date), "总权益": to_float(total_equity)})

    if not rows:
        return pd.DataFrame()
    chart_df = pd.DataFrame(rows).dropna().sort_values("日期").set_index("日期")
    return chart_df


def render_rows_table(rows: list[dict[str, object]], columns: list[str], empty_text: str) -> None:
    """移动端友好的简洁表格。"""

    if not rows:
        st.info(empty_text)
        return
    display_df = pd.DataFrame(rows)
    safe_columns = [column for column in columns if column in display_df.columns]
    st.dataframe(display_df[safe_columns], use_container_width=True, hide_index=True)


client, status = get_supabase_client(st.secrets)

st.markdown("<div class='pmr-kicker'>SHADOW PORTFOLIO</div>", unsafe_allow_html=True)
st.title("影子组合")
st.caption("全自动虚拟盘框架：先搭账户、持仓、成交和收益曲线，后续接入自动交易引擎。")

if status.ok:
    st.success(f"✅ {status.message}")
else:
    st.warning(f"⚠️ {status.message}")
    st.info("请先在 Supabase SQL Editor 执行仓库里的 supabase_schema.sql，然后在 Streamlit Secrets 配置 SUPABASE_URL 和 SUPABASE_KEY。")

account_rows, account_status = fetch_rows(client, "shadow_account", order_by="account_date", desc=True, limit=120)
positions, positions_status = fetch_rows(client, "shadow_positions", order_by="entry_date", desc=True)
trades, trades_status = fetch_rows(client, "shadow_trades", order_by="trade_date", desc=True, limit=100)

latest_account = account_rows[0] if account_rows else {}
cash = to_float(latest_account.get("cash"), INITIAL_CAPITAL)
position_value = sum(to_float(item.get("entry_price")) * to_float(item.get("quantity")) for item in positions)
total_equity = cash + position_value
total_pnl = total_equity - INITIAL_CAPITAL

st.markdown(
    "<div class='pmr-grid'>"
    + metric_card("初始资金", format_eur(INITIAL_CAPITAL), "影子组合默认本金", "EUR")
    + metric_card("当前现金", format_eur(cash), "来自 shadow_account 最新快照", "Supabase")
    + metric_card("持仓市值", format_eur(position_value), "当前按买入价简化估算", "MVP 估值")
    + "</div>",
    unsafe_allow_html=True,
)
st.markdown(
    "<div class='pmr-grid'>"
    + metric_card("总权益", format_eur(total_equity), "现金 + 持仓估算市值", "实时估值 M2+")
    + metric_card("总盈亏", format_eur(total_pnl), "相对 €4500 初始资金", "盈亏为虚拟盘")
    + metric_card("当前持仓数", str(len(positions)), "shadow_positions 当前记录", "自动引擎占位")
    + "</div>",
    unsafe_allow_html=True,
)

if total_pnl != 0:
    st.markdown(
        f"<div class='pmr-muted'>总盈亏颜色参考：<span style='color:{signed_color(total_pnl)}; font-weight:700;'>{format_eur(total_pnl)}</span></div>",
        unsafe_allow_html=True,
    )

st.subheader("收益曲线")
render_safe_line_chart(build_equity_curve(account_rows), "暂无账户快照，初始化影子账户后会显示收益曲线。")

with st.expander("初始化 / 写入测试"):
    st.write("这一步只搭读写框架，不执行真实自动交易。")
    if st.button("初始化影子账户 €4500", disabled=client is None):
        insert_status = insert_row(
            client,
            "shadow_account",
            {
                "account_date": date.today().isoformat(),
                "cash": INITIAL_CAPITAL,
                "market_value": 0,
                "note": "初始化影子账户",
            },
        )
        if insert_status.ok:
            st.success("影子账户已初始化。")
            st.rerun()
        else:
            st.error(insert_status.message)

    with st.form("shadow_trade_form"):
        st.markdown("**记录一笔手动虚拟成交**")
        col_left, col_right = st.columns(2)
        with col_left:
            ticker = st.text_input("ticker", placeholder="例如：NVDA")
            side = st.selectbox("方向", ["买", "卖", "做多", "做空"])
            price = st.number_input("价格", min_value=0.0, step=0.01, format="%.2f")
        with col_right:
            quantity = st.number_input("数量", min_value=0.0, step=1.0)
            trade_date = st.date_input("日期", value=date.today())
            strategy_tag = st.text_input("策略标签", placeholder="例如：踩5穿10 / 龙头 / 跟风")
        reason = st.text_area("理由", placeholder="为什么记录这笔影子交易？")
        submitted = st.form_submit_button("保存虚拟成交", disabled=client is None)

    if submitted:
        if not ticker.strip() or price <= 0 or quantity <= 0:
            st.error("ticker、价格和数量都必须填写，并且价格/数量要大于 0。")
        else:
            insert_status = insert_row(
                client,
                "shadow_trades",
                {
                    "ticker": ticker.strip().upper(),
                    "side": side,
                    "price": float(price),
                    "quantity": float(quantity),
                    "trade_date": trade_date.isoformat(),
                    "pnl": 0,
                    "reason": reason.strip(),
                    "strategy_tag": strategy_tag.strip(),
                },
            )
            if insert_status.ok:
                st.success("虚拟成交已保存。")
                st.rerun()
            else:
                st.error(insert_status.message)

st.subheader("当前持仓")
if not positions_status.ok:
    st.warning(positions_status.message)
render_rows_table(
    positions,
    ["ticker", "entry_price", "quantity", "entry_date", "stop_loss", "strategy_tag", "note"],
    "暂无当前持仓。后续自动交易引擎会把模拟买入写入这里。",
)

st.subheader("历史成交")
if not trades_status.ok:
    st.warning(trades_status.message)
render_rows_table(
    trades,
    ["trade_date", "ticker", "side", "price", "quantity", "pnl", "strategy_tag", "reason"],
    "暂无历史成交。可先用上方表单记录一笔手动虚拟成交。",
)

st.subheader("自动交易引擎")
st.info("🚧 建设中：明天接入真实候选股、risk filter、买卖规则后，这里会自动写入影子组合。当前版本只提供数据库读写框架。")
