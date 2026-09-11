"""Read-only V1.1 evidence UI. Page refresh never starts research or paper orders."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import streamlit as st
import pandas as pd
from src.v11.runtime import root,source,read

st.set_page_config(page_title='V1.1 统一计算与实验账户',layout='wide')
st.title('V1.1 统一计算与实验账户')
st.caption('66 个候选完整保留 · 实验候选未证明正期望 · 不接实盘')
r=root()
st.caption(f'本轮本地持久目录：{r}')
if st.button('刷新状态'):st.rerun()
tabs=st.tabs(['V1 旧结果','V1.1 工程重述','实验纸面账户','主现金观察账户','因子覆盖与验收','动量研究'])
with tabs[0]:
    st.info('既有已观察结果；本页面不重跑旧研究。')
    p=read(source()/'research'/'portfolio.json')
    if p:st.dataframe(pd.DataFrame(p)[['structure','cost_case','ending_equity','fees','skipped']],hide_index=True)
    else:st.warning('旧报告文件缺失，不能解读为零交易。')
with tabs[1]:
    st.warning('这是已观察数据上的工程重述，不是新样本外成绩或策略晋级。')
    p=r/'mechanical_restatement.csv'
    if p.exists():st.dataframe(pd.read_csv(p),hide_index=True)
    else:st.info('机械复算尚未完成。')
    st.json(read(r/'candidate_restatement_summary.json',{}))
    st.json(read(r/'sip_checks.json',{}))
with tabs[2]:
    p=read(r/'experimental_paper_status.json')
    if p:
        from datetime import datetime,timezone
        heartbeat=p.get('heartbeat')
        if not heartbeat or (datetime.now(timezone.utc)-datetime.fromisoformat(heartbeat)).total_seconds()>90:
            st.warning('后台心跳已过期或尚未出现，当前在线状态待验证。')
        a,b,c,d=st.columns(4);a.metric('现金 USD',f'{p["cash"]:.2f}');b.metric('真实时间意图',p['immutable_intents'])
        c.metric('延迟行情虚拟买入',p['buy_fills']);d.metric('延迟行情虚拟卖出',p['sell_fills'])
        if not p['buy_fills']:st.info('WAITING_FOR_SIGNAL：尚无自然前向成交；工程测试不代表真实前向验收通过。')
        st.json(p)
    else:st.info('实验调度器状态尚未验证。')
with tabs[3]:
    st.info('原主观察账户独立保留，不使用工程重述收益填入此账户。')
    st.json(read(source()/'paper'/'status.json',{'status':'STATUS_FILE_MISSING'}))
with tabs[4]:
    p=r/'FACTOR_COVERAGE.md'
    if p.exists():st.markdown(p.read_text(encoding='utf-8'))
    z=r/'verification_v1_1_bundle.zip'
    if z.exists():st.download_button('下载 V1.1 脱敏验收包',z.read_bytes(),file_name=z.name,mime='application/zip')
    st.caption('页面只读取结果；不会自动运行策略或创建订单。')
with tabs[5]:
    from src.v12.ui import render
    render()
