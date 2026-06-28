"""个股查询页面占位。"""

from pathlib import Path
import sys

import streamlit as st


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ui.theme import inject_global_styles


st.set_page_config(page_title="个股查询", page_icon="🔍", layout="centered")
inject_global_styles()

st.title("个股查询")

# 当前输入框只用于占位展示，暂不连接真实数据源。
ticker = st.text_input("输入股票代码（ticker）", placeholder="例如：AAPL")

if ticker:
    st.write(f"已输入：{ticker.upper()}")

st.info("将显示：基本面/新闻/Gamma/期权情绪/方向倾向")
st.warning("🚧 建设中（M1+）")
