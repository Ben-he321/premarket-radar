"""Read-only momentum entry. No computation or paper orders on page refresh."""
import pandas as pd
import streamlit as st
from .runtime import root,read

def render():
    st.subheader('V1.2 独立动量研究')
    st.info('已使用历史数据上的探索性研究；新因子未加入原实验账户。')
    r=root();p=r/'momentum_filter_accounts.csv'
    if not p.exists():st.json(read(r/'TASK_STATE.json',{'stage':'尚未运行'}));return
    a=pd.read_csv(p);st.dataframe(a[['version','cost_case','net_pnl_usd','return','max_drawdown','completed_trades','capital_utilization','total_cost_usd']],hide_index=True)
    f=pd.read_csv(r/'momentum_per_symbol.csv')
    symbol=st.selectbox('查看候选的全部 15 项组合',sorted(f.symbol.unique()),key='v12_symbol')
    st.dataframe(f[f.symbol==symbol],hide_index=True)
    st.caption('INSUFFICIENT_EVIDENCE 表示样本不足；描述统计不等于独立有效优势。')
    result=r/'V1_2_RESULTS.md'
    if result.exists():
        with st.expander('本轮完整中文结论'):st.markdown(result.read_text(encoding='utf-8'))
    z=r/'verification_v1_2_momentum_bundle.zip'
    if z.exists():st.download_button('下载 V1.2 动量验收包',z.read_bytes(),file_name=z.name,mime='application/zip')
