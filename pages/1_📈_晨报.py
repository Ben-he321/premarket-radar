"""晨报页面占位。"""

import streamlit as st


st.set_page_config(page_title="晨报", page_icon="📈", layout="centered")

st.title("晨报")
st.info("将显示：大盘红绿灯、热门板块榜、候选股（含 float/gap%/弹性分/逻辑/失效位）")
st.warning("🚧 建设中（M1+）")
