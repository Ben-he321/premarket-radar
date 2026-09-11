def render():
    import streamlit as st
    import pandas as pd
    from datetime import datetime,timezone
    from .runtime import root,read
    r=root();st.subheader('V1.3.1 证据修复与冻结前向研究')
    st.caption(f'本地持久目录：{r}')
    st.warning('两个 5500 美元比较账本，不能相加为用户本金。均未证明有效优势，不接真实账户。')
    observation=read(r/'forward_observation/latest.json',{})
    if observation:
        st.subheader('冻结前向只读汇总')
        st.caption(f'汇总截至 {observation["checked_at"]}；当前阶段不调整策略。')
        st.write('下次纽约决策：',observation['next_decision']['new_york'])
        st.write('对应马德里时间：',observation['next_decision']['madrid'])
        rows=[]
        for name,x in observation['books'].items():
            if x.get('status')=='LEDGER_MISSING':continue
            rows.append({'账本':name,'现金':x['cash'],'预留':x['reserved_cash'],'持仓市值':x['holding_value'],
                         '成本':x['cost_total'],'净收益':x['net_pnl'],'已平仓交易数':x['completed_trades'],
                         '采样最大回撤':f'{x["observed_max_drawdown"]:.2%}'})
        st.dataframe(pd.DataFrame(rows),hide_index=True)
        delta=observation['momentum_minus_control']
        st.write(f'动量账户减对照：净收益 {delta.get("net_pnl","UNKNOWN")} USD；成本 {delta.get("cost_total","UNKNOWN")} USD。')
        st.caption('回撤基于已保存快照，未采样时段未知；持仓使用账本已有价格。成本已计入净收益。旧缓存未记录的原始行情接收时间为 UNKNOWN。')
        with st.expander('决策归档及首个自然周期证据'):
            st.json(observation['cycle_evidence'])
            st.caption(f'只读报告与日报/周报目录：{r / "forward_observation"}')
    status=read(r/'forward_status.json',{})
    if status:
        fresh=(datetime.now(timezone.utc)-datetime.fromisoformat(status['heartbeat'])).total_seconds()<90
        if not fresh:st.warning('心跳已过期；后台在线状态待验证。')
        st.write('后台状态：',status.get('service'),status.get('reason'))
        st.write('下次计划：',status.get('next_decision'))
        for name,x in status['books'].items():
            st.write(name)
            st.dataframe(pd.DataFrame([{k:x[k] for k in ['cash','reserved_cash','available_cash','equity','immutable_intents','buy_fills','sell_fills','settlement_events']}]),hide_index=True)
        if not any(x['buy_fills'] for x in status['books'].values()):st.info('尚无自然前向成交。等待计划时段；历史重述和 mock 测试不计入前向业绩。')
        with st.expander('后台详情与停止入口'):st.json(status)
    else:st.info('新前向账本尚未启动；查看工程门槛结果。')
    st.json(read(r/'forward_engineering_gate.json',{}))
    for file in ['corpora_action_identity_review.csv','missing_quote_review.csv','primary_nine_tests.csv']:
        if (r/file).exists():
            with st.expander(file):st.dataframe(pd.read_csv(r/file),hide_index=True)
    report=r/'V1_3_1_RESULTS.md'
    if report.exists():
        with st.expander('已审阅的 V1.3.1 历史验收报告'):st.markdown(report.read_text(encoding='utf-8'))
    z=r/'verification_v1_3_1_evidence_forward_bundle.zip'
    if z.exists():st.download_button('下载 V1.3.1 验收包',z.read_bytes(),file_name=z.name,mime='application/zip')
