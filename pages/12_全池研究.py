"""Read-only local control room. Opening a page never starts a worker."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
import streamlit as st
from src.watchlist.runtime import root,read

st.set_page_config(page_title='Ben AI Trading · 全池研究',layout='wide')
st.title('Ben AI Trading · 全池研究')
st.caption('Alpaca Basic · 历史 SIP · 本地研究与现金观察 · 无券商下单')
base=root();research=base/'research'
st.info(f'数据实际保存在：{base}。本地磁盘持久保存；电脑休眠或关机时后台不会执行。')
tabs=st.tabs(['任务总览','全量自选池','数据质量','个股研究','策略比较','历史组合','前向虚拟盘'])
with tabs[0]:
    status=read(base/'status.json',{});summary=read(research/'summary.json',{})
    st.write('工作目录：',str(Path(__file__).resolve().parents[1]))
    st.write('开发分支：codex/watchlist-research-v1；继承 PR #23，未合并 main')
    st.write('研究批次：', '已完成；没有策略晋级' if summary.get('status')=='COMPLETE' else status.get('stage','尚未开始'))
    st.write('候选 / 研究：',summary.get('candidate_symbols',66),' / ',summary.get('research_symbols','进行中'))
    st.write('候选配置：12；旧固定基线：1；最后126日仅评估一次。')
    with st.expander('查看状态与研究限制'):
        st.json(status);st.json(summary)
    checks=read(base/'sip_checks.json',{})
    st.write('历史 SIP：',checks.get('historical',{}).get('status','待验证'))
    st.write('最新 SIP：',checks.get('latest',{}).get('status','待验证'))
    st.caption('NOT_REQUIRED_BASIC 表示最新 SIP 权限未开通；已获历史权限可继续下载。无 IEX/Futu/合成数据回退。')
    st.write('付费模型调用：0；费用：0 美元 / 25 美元上限')
    st.write('恢复入口：工作目录中的 RUNBOOK.md 与 scripts/启动全池研究.ps1')
with tabs[1]:
    universe=read(base/'universe.json',{})
    coverage=read(base/'coverage.json',[])
    records=universe.get('records',[])
    rows=[]
    for r in records:
        c=next((x for x in coverage if x['symbol']==r['symbol'] and x['adjustment']=='raw'),{})
        rows.append({**{k:r.get(k) for k in ('symbol','name','type','exchange','research_start','listing_date','security_id','historical_mapping')},
                     'status':c.get('status',r['status']),'rows':c.get('rows',0),'first':c.get('first'),'last':c.get('last')})
    st.write(f'{len(rows)} 个候选；SPY / QQQ / SOXX 仅作参考。DXYZ 单独作为 CEF 诊断，不进入普通股票共享选择或组合。')
    st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)
    st.download_button('导出全部候选处理表',pd.DataFrame(rows).to_csv(index=False).encode('utf-8-sig'),'candidate-status.csv')
with tabs[2]:
    flat=[{k:v for k,v in x.items() if k!='quality'}|{'quality_issues':len(x.get('quality',{}).get('issues',[]))} for x in coverage]
    st.dataframe(pd.DataFrame(flat),use_container_width=True,hide_index=True)
    st.warning('UNKNOWN 缺口可能来自停牌、历史覆盖不足或其他原因；首条观测不是已核实上市日。隔离目录保留原始异常响应。')
    symbol=st.selectbox('查看证券质量', [r['symbol'] for r in records] or ['暂无数据'])
    for c in coverage:
        if c['symbol']==symbol:st.json(c)
    st.write('增量更新',read(base/'incremental_verification.json',{}))
    st.write('备份与恢复',read(base/'backup_verification.json',{}))
with tabs[3]:
    st.dataframe(pd.DataFrame(read(research/'symbols.json',[])),use_container_width=True,hide_index=True)
    trials=read(research/'all_trials.json',[])
    chosen=st.selectbox('个股策略', [r['symbol'] for r in records] or ['暂无数据'],key='strategy_symbol')
    st.dataframe(pd.DataFrame([{k:v for k,v in t.items() if k not in ('development','parameters')}|t['development'] for t in trials if t['symbol']==chosen]),use_container_width=True)
    st.caption('上述为开发区间 all 复权标准金额事件诊断；不代表可执行账户收益。短历史标的没有借用最终保留区间训练。')
with tabs[4]:
    st.dataframe(pd.DataFrame(read(research/'promotion.json',[])),use_container_width=True)
    with st.expander('研究预注册'):st.json(read(base/'preregistration.json',{}))
    with st.expander('统计量与适用限制'):st.json(read(research/'statistics.json',{}))
    with st.expander('全部滚动选择'):st.json(read(research/'frozen_pipeline.json',{}))
with tabs[5]:
    st.warning('5500 美元单账户回放均标记研究诊断：公司行动完整性、股息付款日和日线成交时点未完全核实，不据此启动交易。')
    portfolio=read(research/'portfolio.json',[])
    st.dataframe(pd.DataFrame([{k:v for k,v in row.items() if not isinstance(v,dict)} for row in portfolio]),use_container_width=True)
    name=st.selectbox('账户曲线',['SHARED','PER_SYMBOL','HYBRID_REGIME','FIXED_LEGACY'])
    curve=research/(name+'-base-equity.parquet')
    if curve.exists():
        f=pd.read_parquet(curve);st.line_chart(f.set_index('date')[['equity']]);st.dataframe(f.tail(20),use_container_width=True)
    st.write('同区间参考',read(research/'benchmarks.json',[]))
with tabs[6]:
    st.warning('暂无合格策略。主虚拟账户保持 5500 美元现金；只累计实际启动后的真实行情观察，无历史回填成交。')
    st.json(read(base/'paper/status.json',{'status':'尚未启动'}))
    st.caption('此页只读取结果。停止与恢复方法见 RUNBOOK.md；刷新页面不会启动下载、研究或虚拟交易。')
