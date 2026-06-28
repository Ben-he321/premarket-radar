"""交易日志页面：手动记录交易并持久化到本地 JSON 文件。"""

from __future__ import annotations

from datetime import date, datetime
import html
import json
from pathlib import Path
import sys
from uuid import uuid4

import streamlit as st


# Streamlit Cloud 和本地运行时的工作目录可能不同，显式加入项目根目录。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ui.theme import inject_global_styles, signed_color


DATA_DIR = PROJECT_ROOT / "data"
TRADE_LOG_PATH = DATA_DIR / "trade_journal.json"


st.set_page_config(page_title="交易日志", page_icon="📒", layout="centered")
inject_global_styles()


def ensure_data_file() -> None:
    """确保本地数据目录存在；真实交易记录文件不提交到仓库。"""

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not TRADE_LOG_PATH.exists():
        TRADE_LOG_PATH.write_text("[]", encoding="utf-8")


def load_trades() -> list[dict[str, object]]:
    """读取交易记录；文件损坏时给出提示并返回空列表。"""

    try:
        ensure_data_file()
        data = json.loads(TRADE_LOG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        st.warning("交易日志文件格式异常，已暂时按空记录处理。")
    except json.JSONDecodeError:
        st.error("交易日志 JSON 文件解析失败，请备份后检查 data/trade_journal.json。")
    except OSError as exc:
        st.error(f"读取交易日志失败：{exc}")
    return []


def save_trades(trades: list[dict[str, object]]) -> bool:
    """保存交易记录，返回是否成功。"""

    try:
        ensure_data_file()
        TRADE_LOG_PATH.write_text(json.dumps(trades, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except OSError as exc:
        st.error(f"保存交易日志失败：{exc}")
        return False


def calculate_pnl(direction: str, buy_price: float, sell_price: float, quantity: float) -> tuple[float, float]:
    """按方向计算盈亏金额和百分比。买/做多按多头，卖/做空按空头。"""

    if direction in {"卖", "做空"}:
        pnl_amount = (buy_price - sell_price) * quantity
        base_price = buy_price
    else:
        pnl_amount = (sell_price - buy_price) * quantity
        base_price = buy_price

    cost_basis = base_price * quantity
    pnl_percent = pnl_amount / cost_basis * 100 if cost_basis else 0.0
    return pnl_amount, pnl_percent


def format_money(value: float) -> str:
    """格式化美元金额。"""

    sign = "+" if value > 0 else ""
    return f"{sign}${value:,.2f}"


def format_percent(value: float) -> str:
    """格式化百分比。"""

    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def build_summary(trades: list[dict[str, object]]) -> dict[str, float]:
    """汇总交易统计指标。"""

    total = len(trades)
    pnl_values = [float(item.get("pnl_amount", 0.0)) for item in trades]
    wins = sum(1 for value in pnl_values if value > 0)
    total_pnl = sum(pnl_values)
    return {
        "total": float(total),
        "win_rate": wins / total * 100 if total else 0.0,
        "total_pnl": total_pnl,
        "avg_pnl": total_pnl / total if total else 0.0,
    }


def sorted_trades(trades: list[dict[str, object]]) -> list[dict[str, object]]:
    """按日期倒序展示，日期相同时按创建时间倒序。"""

    return sorted(
        trades,
        key=lambda item: (str(item.get("trade_date", "")), str(item.get("created_at", ""))),
        reverse=True,
    )


def render_summary_cards(trades: list[dict[str, object]]) -> None:
    """渲染顶部统计摘要卡片。"""

    summary = build_summary(trades)
    total_pnl = summary["total_pnl"]
    avg_pnl = summary["avg_pnl"]
    pnl_color = signed_color(total_pnl)
    avg_color = signed_color(avg_pnl)
    st.markdown(
        "<div class='pmr-grid'>"
        + (
            "<div class='pmr-card'>"
            "<div class='pmr-card-label'>总交易笔数</div>"
            f"<div class='pmr-card-main pmr-accent'>{int(summary['total'])}</div>"
            "<div class='pmr-muted'>已记录的手动交易</div>"
            "<div class='pmr-muted'><span class='pmr-accent'>本地 JSON 保存</span></div>"
            "</div>"
        )
        + (
            "<div class='pmr-card'>"
            "<div class='pmr-card-label'>胜率</div>"
            f"<div class='pmr-card-main pmr-accent'>{summary['win_rate']:.1f}%</div>"
            "<div class='pmr-muted'>盈利交易占比</div>"
            "<div class='pmr-muted'><span class='pmr-accent'>盈利 > 0 计为胜</span></div>"
            "</div>"
        )
        + (
            "<div class='pmr-card'>"
            "<div class='pmr-card-label'>总盈亏 / 平均盈亏</div>"
            f"<div class='pmr-card-main' style='color:{pnl_color};'>{format_money(total_pnl)}</div>"
            f"<div class='pmr-muted'>平均每笔 <span style='color:{avg_color}; font-weight:700;'>{format_money(avg_pnl)}</span></div>"
            "<div class='pmr-muted'><span class='pmr-accent'>关键数字仅作复盘参考</span></div>"
            "</div>"
        )
        + "</div>",
        unsafe_allow_html=True,
    )


def render_trade_table(trades: list[dict[str, object]]) -> None:
    """用紧凑 HTML 表格展示历史记录，手机端自动省略过长文本。"""

    if not trades:
        st.info("还没有交易记录。先在上方录入第一笔交易，刷新页面后也会保留。")
        return

    rows = []
    for item in sorted_trades(trades):
        pnl_amount = float(item.get("pnl_amount", 0.0))
        pnl_percent = float(item.get("pnl_percent", 0.0))
        color = signed_color(pnl_amount)
        reason = html.escape(str(item.get("reason", "")))
        notes = html.escape(str(item.get("notes", "")))
        tags = html.escape(str(item.get("tags", "")))
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('trade_date', '')))}</td>"
            f"<td><strong>{html.escape(str(item.get('ticker', '')))}</strong></td>"
            f"<td>{html.escape(str(item.get('direction', '')))}</td>"
            f"<td>{float(item.get('buy_price', 0.0)):.2f}</td>"
            f"<td>{float(item.get('sell_price', 0.0)):.2f}</td>"
            f"<td>{float(item.get('quantity', 0.0)):g}</td>"
            f"<td style='color:{color}; font-weight:700;'>{format_money(pnl_amount)}</td>"
            f"<td style='color:{color}; font-weight:700;'>{format_percent(pnl_percent)}</td>"
            f"<td>{tags}</td>"
            f"<td title='{reason}'>{reason}</td>"
            f"<td title='{notes}'>{notes}</td>"
            "</tr>"
        )

    st.markdown(
        """
        <style>
        .trade-table-wrap {
            width: 100%;
            overflow-x: auto;
            border: 1px solid var(--pmr-border);
            border-radius: 8px;
            background: rgba(10, 16, 23, 0.86);
        }
        .trade-table {
            width: 100%;
            border-collapse: collapse;
            min-width: 780px;
            font-size: 0.88rem;
        }
        .trade-table th, .trade-table td {
            border-bottom: 1px solid rgba(148, 163, 184, 0.12);
            padding: 0.58rem 0.5rem;
            text-align: right;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 11rem;
        }
        .trade-table th:first-child, .trade-table td:first-child,
        .trade-table th:nth-child(2), .trade-table td:nth-child(2),
        .trade-table th:nth-child(9), .trade-table td:nth-child(9),
        .trade-table th:nth-child(10), .trade-table td:nth-child(10),
        .trade-table th:nth-child(11), .trade-table td:nth-child(11) {
            text-align: left;
        }
        .trade-table th {
            color: var(--pmr-dim);
            font-weight: 650;
            background: rgba(15, 23, 32, 0.92);
        }
        @media (max-width: 760px) {
            .trade-table {
                min-width: 720px;
                font-size: 0.8rem;
            }
        }
        </style>
        """
        + """
        <div class="trade-table-wrap">
            <table class="trade-table">
                <thead>
                    <tr>
                        <th>日期</th>
                        <th>ticker</th>
                        <th>方向</th>
                        <th>买入价</th>
                        <th>卖出价</th>
                        <th>数量</th>
                        <th>盈亏</th>
                        <th>盈亏%</th>
                        <th>标签</th>
                        <th>交易理由</th>
                        <th>备注</th>
                    </tr>
                </thead>
                <tbody>
        """
        + "".join(rows)
        + """
                </tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


def trade_label(item: dict[str, object]) -> str:
    """生成删除下拉框的显示文本。"""

    pnl = format_money(float(item.get("pnl_amount", 0.0)))
    return f"{item.get('trade_date', '')} · {item.get('ticker', '')} · {item.get('direction', '')} · {pnl}"


trades = load_trades()

st.markdown("<div class='pmr-kicker'>TRADE JOURNAL</div>", unsafe_allow_html=True)
st.title("交易日志")
st.caption("手动记录每笔交易，自动计算盈亏，用于复盘自己的交易纪律。")
st.info(f"数据保存在本地文件：data/trade_journal.json。该文件已加入 .gitignore，不会提交到仓库。")

render_summary_cards(trades)

st.subheader("新增交易记录")
with st.form("trade_form", clear_on_submit=True):
    col_left, col_right = st.columns(2)
    with col_left:
        ticker = st.text_input("股票代码（ticker）", placeholder="例如：NVDA")
        direction = st.selectbox("方向", options=["买", "卖", "做多", "做空"], index=0)
        buy_price = st.number_input("买入价", min_value=0.0, value=0.0, step=0.01, format="%.2f")
        sell_price = st.number_input("卖出价", min_value=0.0, value=0.0, step=0.01, format="%.2f")
    with col_right:
        quantity = st.number_input("数量", min_value=0.0, value=0.0, step=1.0)
        trade_date = st.date_input("日期", value=date.today())
        tags = st.text_input("标签", placeholder='例如：踩5穿10, 龙头, 跟风')
        reason = st.text_area("交易理由", placeholder="为什么进场？信号、板块、风险点是什么？")

    notes = st.text_area("备注", placeholder="执行问题、情绪状态、后续复盘都可以写在这里。")
    submitted = st.form_submit_button("保存交易", type="primary")

if submitted:
    clean_ticker = ticker.strip().upper()
    if not clean_ticker:
        st.error("请填写股票代码。")
    elif buy_price <= 0 or sell_price <= 0 or quantity <= 0:
        st.error("买入价、卖出价和数量都必须大于 0。")
    else:
        pnl_amount, pnl_percent = calculate_pnl(direction, buy_price, sell_price, quantity)
        new_trade = {
            "id": str(uuid4()),
            "ticker": clean_ticker,
            "direction": direction,
            "buy_price": float(buy_price),
            "sell_price": float(sell_price),
            "quantity": float(quantity),
            "trade_date": trade_date.isoformat(),
            "reason": reason.strip(),
            "tags": tags.strip(),
            "notes": notes.strip(),
            "pnl_amount": pnl_amount,
            "pnl_percent": pnl_percent,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        if save_trades(trades + [new_trade]):
            st.success(f"已保存 {clean_ticker}，本笔盈亏 {format_money(pnl_amount)}（{format_percent(pnl_percent)}）。")
            st.rerun()

st.subheader("历史记录")
render_trade_table(trades)

if trades:
    st.subheader("删除单条记录")
    options = {trade_label(item): str(item.get("id", "")) for item in sorted_trades(trades)}
    selected_label = st.selectbox("选择要删除的记录", options=list(options.keys()))
    confirm_delete = st.checkbox("我确认删除这条记录")
    if st.button("删除选中记录", disabled=not confirm_delete):
        delete_id = options[selected_label]
        remaining = [item for item in trades if str(item.get("id", "")) != delete_id]
        if len(remaining) == len(trades):
            st.error("没有找到要删除的记录。")
        elif save_trades(remaining):
            st.success("已删除选中记录。")
            st.rerun()
