"""设置页面占位。"""

from pathlib import Path
import sys

import streamlit as st


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ui.theme import inject_global_styles


st.set_page_config(page_title="设置", page_icon="⚙️", layout="centered")
inject_global_styles()

st.title("设置")
st.info("将配置：自选股、关注板块、风险%、告警偏好")
st.warning("🚧 建设中（M1+）")
