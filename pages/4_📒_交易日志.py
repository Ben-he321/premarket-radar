"""交易日志页面占位。"""

from pathlib import Path
import sys

import streamlit as st


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ui.theme import inject_global_styles


st.set_page_config(page_title="交易日志", page_icon="📒", layout="centered")
inject_global_styles()

st.title("交易日志")
st.info("将显示：历史推荐、实际结果、模拟盘战绩")
st.warning("🚧 建设中（M1+）")
