"""Read-only B1.1 evidence viewer. It cannot start research or alter any ledger."""
from pathlib import Path
import json
import pandas as pd
import streamlit as st

ROOT=Path.home()/'BenAITradingData/ben-b1-1-replay-20260914'
st.set_page_config(page_title='Ben B1.1 回放证据',layout='wide')
st.set_option('client.showSidebarNavigation',False)
st.title('Ben B1.1：真实历史回放证据')
st.caption('独立工程研究 · 5500 美元假设账户 · 不连接券商 · 不改动 M20/U')
st.info('历史报价驱动的模型成交不等于真实成交。单个工程样本不能拼成全池收益。')
if st.button('刷新只读结果'):st.rerun()
state=ROOT/'TASK_STATE.json'
if state.exists():
    value=json.loads(state.read_text(encoding='utf-8-sig'))
    st.write('任务阶段：',value.get('stage','未知'))
    st.write('最后更新：',value.get('updated_at','未知'))
    st.write('研究进程：','正在运行' if value.get('persistent_research_worker_running') else '当前没有研究进程')
    st.caption('此页面在线只代表只读看板在线。')
report=ROOT/'BEN_B1_1_RESULTS.md'
if report.exists():st.markdown(report.read_text(encoding='utf-8'))
else:st.write('正式报告尚未生成；正在保留中间证据。')
for filename,label in [('ACTUAL_ACCOUNT_SUMMARY.csv','逐样本实际账户'),('coverage_funnel.csv','固定样本逐级结果'),('UNIVERSE_COVERAGE.csv','完整66候选覆盖')]:
    path=ROOT/filename
    if path.exists():
        with st.expander(label):st.dataframe(pd.read_csv(path),hide_index=True,use_container_width=True)
bundle=ROOT/'verification_ben_b1_1_replay_bundle.zip'
if bundle.exists():
    st.download_button('下载本轮脱敏验收包',bundle.read_bytes(),file_name=bundle.name,mime='application/zip')
st.caption(f'工作目录：{Path(__file__).resolve().parent}')
st.caption(f'独立数据与恢复目录：{ROOT}')
