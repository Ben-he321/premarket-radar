"""复盘报告页面：每日虚拟盘复盘框架。"""

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

from src.supabase_client import fetch_rows, get_supabase_client, upsert_row
from src.ui.theme import inject_global_styles, metric_card, signed_color


st.set_page_config(page_title="复盘报告", page_icon="📝", layout="centered")
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


def find_report(reports: list[dict[str, object]], report_date: date) -> dict[str, object]:
    """按日期寻找已有复盘。"""

    target = report_date.isoformat()
    for item in reports:
        if str(item.get("report_date")) == target:
            return item
    return {}


client, status = get_supabase_client(st.secrets)

st.markdown("<div class='pmr-kicker'>DAILY REVIEW</div>", unsafe_allow_html=True)
st.title("复盘报告")
st.caption("每日复盘框架：记录当日盈亏、操作、亏损原因、卖飞问题和总结。")

if status.ok:
    st.success(f"✅ {status.message}")
else:
    st.warning(f"⚠️ {status.message}")
    st.info("请先在 Supabase SQL Editor 执行 supabase_schema.sql，并在 Streamlit Secrets 配置 SUPABASE_URL 和 SUPABASE_KEY。")

reports, report_status = fetch_rows(client, "daily_report", order_by="report_date", desc=True, limit=60)
if not report_status.ok:
    st.warning(report_status.message)

selected_date = st.date_input("选择复盘日期", value=date.today())
current_report = find_report(reports, selected_date)
daily_pnl = to_float(current_report.get("daily_pnl"), 0.0)

st.markdown(
    "<div class='pmr-grid'>"
    + metric_card("复盘日期", selected_date.isoformat(), "按交易日记录", "daily_report")
    + metric_card("当日盈亏", format_eur(daily_pnl), "来自当前报告", "虚拟盘")
    + metric_card("报告状态", "已保存" if current_report else "未保存", "可在下方编辑后保存", "MVP")
    + "</div>",
    unsafe_allow_html=True,
)

if current_report:
    st.markdown(
        f"<div class='pmr-muted'>当日盈亏颜色参考：<span style='color:{signed_color(daily_pnl)}; font-weight:700;'>{format_eur(daily_pnl)}</span></div>",
        unsafe_allow_html=True,
    )

with st.form("daily_report_form"):
    report_pnl = st.number_input("当日盈亏（€）", value=daily_pnl, step=10.0, format="%.2f")
    actions = st.text_area(
        "当日操作记录",
        value=str(current_report.get("actions") or ""),
        placeholder="今天影子组合买了什么、卖了什么、为什么做？",
    )
    loss_analysis = st.text_area(
        "亏损单分析",
        value=str(current_report.get("loss_analysis") or ""),
        placeholder="亏损来自追高、止损太宽、板块误判，还是执行问题？",
    )
    missed_opportunities = st.text_area(
        "卖飞 / 错过机会",
        value=str(current_report.get("missed_opportunities") or ""),
        placeholder="有没有卖飞？有没有该买没买？原因是什么？",
    )
    summary = st.text_area(
        "大白话总结",
        value=str(current_report.get("summary") or ""),
        placeholder="今天最重要的一条教训是什么？明天要改什么？",
    )
    submitted = st.form_submit_button("保存复盘报告", type="primary", disabled=client is None)

if submitted:
    save_status = upsert_row(
        client,
        "daily_report",
        {
            "report_date": selected_date.isoformat(),
            "daily_pnl": float(report_pnl),
            "actions": actions.strip(),
            "loss_analysis": loss_analysis.strip(),
            "missed_opportunities": missed_opportunities.strip(),
            "summary": summary.strip(),
        },
        on_conflict="report_date",
    )
    if save_status.ok:
        st.success("复盘报告已保存。")
        st.rerun()
    else:
        st.error(save_status.message)

st.subheader("历史复盘")
if not reports:
    st.info("暂无复盘报告。保存第一份报告后，这里会显示历史记录。")
else:
    display_df = pd.DataFrame(reports)
    columns = [
        "report_date",
        "daily_pnl",
        "actions",
        "loss_analysis",
        "missed_opportunities",
        "summary",
        "updated_at",
    ]
    safe_columns = [column for column in columns if column in display_df.columns]
    st.dataframe(display_df[safe_columns], use_container_width=True, hide_index=True)

st.subheader("AI 复盘引擎")
st.info("🚧 建设中：后续会结合 shadow_trades、持仓表现和 AI，自动生成亏损分析、卖飞分析和明日改进清单。")
