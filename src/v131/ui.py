def render():
    import streamlit as st
    import pandas as pd
    from datetime import datetime,timezone
    from .runtime import root,read
    r=root();st.subheader('V1.3.1 证据修复与冻结前向研究')
    st.caption(f'本地持久目录：{r}')
    st.warning('两个 5500 美元比较账本，不能相加为用户本金。均未证明有效优势，不接真实账户。')
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
    if report.exists():st.markdown(report.read_text(encoding='utf-8'))
    z=r/'verification_v1_3_1_evidence_forward_bundle.zip'
    if z.exists():st.download_button('下载 V1.3.1 验收包',z.read_bytes(),file_name=z.name,mime='application/zip')
