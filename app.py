"""盘前雷达 Pre-Market Radar 的 Streamlit 入口页面。"""

from datetime import datetime
import platform

import streamlit as st


# 页面基础设置：使用 centered 布局，方便电脑和手机浏览器直接打开。
st.set_page_config(
    page_title="盘前雷达 Pre-Market Radar",
    page_icon="📡",
    layout="centered",
)


st.title("盘前雷达 Pre-Market Radar")
st.caption("美股盘前分析 App 骨架（M0 阶段）")

st.info(
    "当前版本只搭建项目结构和占位页面，不连接真实数据源、不调用 API、不读取任何密钥。"
)

st.subheader("系统状态")

# 这些信息用于部署后自检，证明 Python 与 Streamlit 环境正常运行。
st.success("✅ 骨架已部署")
st.write(f"当前日期时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
st.write(f"Python 版本：{platform.python_version()}")
st.write(f"Streamlit 版本：{st.__version__}")

st.divider()

st.write(
    "这里将逐步扩展为盘前首页仪表盘，未来显示大盘状态、热门板块、候选股和风险提示。"
)
st.warning("请使用左侧导航打开 6 个占位页面，查看后续模块规划。")
