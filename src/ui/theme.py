"""统一视觉系统：高端金融终端风格的 CSS 与小组件。"""

from __future__ import annotations

import html

import streamlit as st


CYAN = "#22D3EE"
GREEN = "#7DD3A7"
RED = "#FCA5A5"
BG = "#0B1118"
PANEL = "#111A24"
PANEL_SOFT = "#0F1720"
BORDER = "rgba(148, 163, 184, 0.18)"
TEXT = "#E5E7EB"
TEXT_MUTED = "#94A3B8"
TEXT_DIM = "#64748B"


def inject_global_styles() -> None:
    """注入全局 CSS，让所有页面拥有统一的深色金融终端质感。"""

    st.markdown(
        f"""
        <style>
        :root {{
            --pmr-bg: {BG};
            --pmr-panel: {PANEL};
            --pmr-panel-soft: {PANEL_SOFT};
            --pmr-border: {BORDER};
            --pmr-text: {TEXT};
            --pmr-muted: {TEXT_MUTED};
            --pmr-dim: {TEXT_DIM};
            --pmr-cyan: {CYAN};
            --pmr-green: {GREEN};
            --pmr-red: {RED};
        }}

        .stApp {{
            background:
                radial-gradient(circle at top left, rgba(34, 211, 238, 0.045), transparent 30rem),
                linear-gradient(180deg, #0B1118 0%, #090E14 100%);
            color: var(--pmr-text);
        }}

        .block-container {{
            padding-top: 2.2rem;
            padding-bottom: 3rem;
            max-width: 980px;
        }}

        h1, h2, h3 {{
            letter-spacing: 0;
            color: var(--pmr-text);
        }}

        p, li, .stMarkdown, .stCaption {{
            color: var(--pmr-muted);
        }}

        [data-testid="stSidebar"] {{
            background: #081019;
            border-right: 1px solid var(--pmr-border);
        }}

        div[data-testid="stAlert"] {{
            border-radius: 8px;
            border: 1px solid var(--pmr-border);
            background: rgba(17, 26, 36, 0.72);
        }}

        .pmr-topline {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            padding: 1rem 1.1rem;
            border: 1px solid var(--pmr-border);
            background: linear-gradient(180deg, rgba(17, 26, 36, 0.96), rgba(12, 18, 26, 0.96));
            border-radius: 8px;
            margin: 0.9rem 0 1.1rem;
        }}

        .pmr-kicker {{
            color: var(--pmr-dim);
            font-size: 0.78rem;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            margin-bottom: 0.2rem;
        }}

        .pmr-title {{
            color: var(--pmr-text);
            font-size: 1.25rem;
            font-weight: 700;
            line-height: 1.25;
        }}

        .pmr-muted {{
            color: var(--pmr-muted);
            font-size: 0.92rem;
            line-height: 1.55;
        }}

        .pmr-signal {{
            display: inline-flex;
            align-items: center;
            gap: 0.45rem;
            white-space: nowrap;
            border: 1px solid var(--pmr-border);
            background: rgba(15, 23, 32, 0.88);
            border-radius: 999px;
            padding: 0.45rem 0.7rem;
            color: var(--pmr-text);
            font-weight: 650;
        }}

        .pmr-dot {{
            width: 0.68rem;
            height: 0.68rem;
            border-radius: 2px;
            display: inline-block;
            background: var(--pmr-cyan);
        }}

        .pmr-grid {{
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 0.85rem;
            margin: 1rem 0 1.2rem;
        }}

        .pmr-card {{
            border: 1px solid var(--pmr-border);
            background: linear-gradient(180deg, rgba(17, 26, 36, 0.98), rgba(10, 16, 23, 0.98));
            border-radius: 8px;
            padding: 1rem;
            min-height: 8.8rem;
        }}

        .pmr-card-label {{
            color: var(--pmr-dim);
            font-size: 0.78rem;
            margin-bottom: 0.55rem;
        }}

        .pmr-card-main {{
            color: var(--pmr-text);
            font-size: 1.55rem;
            line-height: 1.15;
            font-weight: 760;
            margin-bottom: 0.4rem;
            overflow-wrap: anywhere;
        }}

        .pmr-accent {{
            color: var(--pmr-cyan);
        }}

        .pmr-section {{
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            gap: 0.8rem;
            margin: 1.2rem 0 0.55rem;
        }}

        .pmr-section h3 {{
            margin: 0;
            font-size: 1rem;
        }}

        .pmr-table {{
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
            border: 1px solid var(--pmr-border);
            background: rgba(10, 16, 23, 0.86);
            border-radius: 8px;
            overflow: hidden;
            font-size: 0.9rem;
        }}

        .pmr-table th, .pmr-table td {{
            border-bottom: 1px solid rgba(148, 163, 184, 0.12);
            padding: 0.62rem 0.55rem;
            text-align: right;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}

        .pmr-table th:first-child, .pmr-table td:first-child {{
            text-align: left;
        }}

        .pmr-table th {{
            color: var(--pmr-dim);
            font-weight: 650;
            background: rgba(15, 23, 32, 0.92);
        }}

        @media (max-width: 760px) {{
            .pmr-topline {{
                align-items: flex-start;
                flex-direction: column;
            }}
            .pmr-grid {{
                grid-template-columns: 1fr;
            }}
            .pmr-card {{
                min-height: auto;
            }}
            .pmr-table th, .pmr-table td {{
                padding: 0.52rem 0.35rem;
                font-size: 0.82rem;
            }}
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def metric_card(label: str, main: str, detail: str, accent: str | None = None) -> str:
    """生成统一核心卡片 HTML。"""

    safe_label = html.escape(label)
    safe_main = html.escape(main)
    safe_detail = html.escape(detail)
    accent_html = f"<div class='pmr-muted'><span class='pmr-accent'>{html.escape(accent)}</span></div>" if accent else ""
    return (
        "<div class='pmr-card'>"
        f"<div class='pmr-card-label'>{safe_label}</div>"
        f"<div class='pmr-card-main'>{safe_main}</div>"
        f"<div class='pmr-muted'>{safe_detail}</div>"
        f"{accent_html}"
        "</div>"
    )


def signed_color(value: float) -> str:
    """按涨跌返回柔和红绿。"""

    return GREEN if value >= 0 else RED
