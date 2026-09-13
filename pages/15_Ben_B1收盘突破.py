"""Read-only Ben B1 research dashboard, independent from all forward services."""
from pathlib import Path
import json
import os
import pandas as pd
import streamlit as st

ROOT=Path(os.environ.get('BEN_B1_OUTPUT_DIR', str(Path.home()/'BenAITradingData/ben-b1-research-20260913')))
st.set_page_config(page_title='Ben B1收盘突破',page_icon='📋',layout='wide')
st.title('Ben B1收盘突破')
st.caption('半仓主版本 · 财报避让 · 满仓与融资对照 · 独立历史研究')
summary_path=ROOT/'research/run_summary.json'
if not summary_path.exists():
    st.warning('研究结果尚未生成，不能将缺文件显示为零交易。')
    st.stop()
summary=json.loads(summary_path.read_text(encoding='utf-8'))
st.warning('DATA_GATED_PARTIAL_EXECUTION：已完成真实数据核验与有限窗口计算；完整交易绩效尚未通过数据资格。')
cols=st.columns(4)
for col,(name,value) in zip(cols,[('保留候选',66),('固定实验路径',15),('已核验案例',summary['cases_calculated']),('可发布收益路径',0)]):
    col.metric(name,value)
st.write('原 M20/U 服务与账本独立保留。本页没有启动前向账户或下单功能。')
tabs=st.tabs(['结论与规则','案例核验','股票与数据覆盖','财报窗口','账户与成本','证据下载'])

def table(path,columns=None):
    p=ROOT/path
    if not p.exists():
        st.info(f'尚无此项输出：{path}')
        return
    f=pd.read_csv(p)
    st.dataframe(f if columns is None else f[[c for c in columns if c in f]],use_container_width=True,hide_index=True)

with tabs[0]:
    st.subheader('目前无法判断是否有盈利优势')
    st.write(f"定向财报公告窗口共 {summary['announcement_window_symbol_sessions']} 个证券交易日，其中禁做前 {summary['preblackout_symbol_sessions']} 日，新突破信号 {summary['preblackout_fresh_signals']} 个。样本不足，不能解释为低风险或策略优秀。")
    st.write('本金均为 5,500 美元。P50 每笔目标净权益 50%，最多两只；P100 一个槽位；P200 一个槽位、最高两倍总头寸，负债和利息单独计算。')
    st.write('财报前三个交易日及公告日禁做，并提前退出。收盘后第 5 分钟决策，第 15 分钟取消；买价含执行摩擦不得超过常规收盘价 1%。')
    st.write('融资假设：初始保证金 50%、维持 30%，8% 年利率 ACT/360；固定压力为 12% 利率或 50% 维持要求。未验证真实券商权限。')
    report=ROOT/'BEN_B1_RESULTS.md'
    if report.exists():
        with st.expander('完整中文报告'):st.markdown(report.read_text(encoding='utf-8'))
with tabs[1]:
    st.write('采用核对过收盘竞价的正式收盘价；最后一分钟价格只作对照。')
    table('CASE_RECONCILIATION.csv',['symbol','date','official_close','last_rth_minute_close','close_limit_1pct','valid_same_security_sessions','fresh_cross_and_above_short_group','indicative_net_rr','reasons'])
    st.caption('表中 RR 为结构诊断；缺少实际 ask 时以收盘价作为计算参考，不能当成真实成交。')
with tabs[2]:
    st.subheader('66 个候选与独立范围政策')
    table('UNIVERSE_POLICY.csv',['symbol','name','type','scope_policy','business','business_source','historical_scope'])
    st.subheader('行情、报价、财报和融资资料')
    table('DATA_CAPABILITY.csv')
with tabs[3]:
    table('research/earnings_window_decisions.csv',['symbol','date','earnings_level','earnings_status','earnings_new_entry_allowed','first_entry_signal_on_vendor_daily','source_publication_date','source_published_at','conservative_available_at_bound','strict_missing'])
    st.caption('只覆盖已找到事前计划的三个事件；旧网络接收时间未知。已结束季度不能替代下一次财报日历。')
with tabs[4]:
    st.write('下面 15 条路径只完成资格检查。空收益值表示未评估；没有生成一条零回撤曲线。')
    table('account_summary.csv',['account_id','initial_equity','commission','friction_bps','status','end_equity','net_profit','max_drawdown','orders_created'])
    st.write('本轮新增购买与付费模型调用为 0。既有共享订阅和研发实际费用未知；150 美元/月只是待评估上限，未实际扣款。SPY/QQQ 比较等待可比的合格资金部署区间。')
with tabs[5]:
    st.code(str(ROOT),language=None)
    for name,label,mime in [('BEN_B1_RESULTS.md','下载中文报告','text/markdown'),('verification_ben_b1_research_bundle.zip','下载完整脱敏验收包','application/zip')]:
        p=ROOT/name
        if p.exists():st.download_button(label,p.read_bytes(),file_name=name,mime=mime)
    st.caption('验收包不含凭证、原始行情库或数据库。原始研究缓存保存在独立持久目录，重启后仍在。')
