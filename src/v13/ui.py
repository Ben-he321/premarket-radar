def render():
    import streamlit as st
    import pandas as pd
    from .runtime import root,read
    r=root();st.subheader('V1.3 独立条件与公平对照')
    st.warning('历史探索复用数据；不接入原前向账户，不代表新的盲测成绩。')
    state=read(r/'TASK_STATE.json',{})
    st.json(state)
    p=r/'V1_3_RESULTS.md'
    if p.exists():st.markdown(p.read_text(encoding='utf-8'))
    else:st.info('固定研究正在执行；这里只读状态，不会启动第二个研究进程。')
    for name,label in [('primary_nine_tests.csv','9项固定主问题'),('account_summary.csv','全部账户与随机种子'),('data_coverage.json','66候选数据覆盖')]:
        path=r/name
        if path.exists():
            with st.expander(label):
                st.dataframe(pd.read_csv(path) if path.suffix=='.csv' else pd.DataFrame(read(path)),hide_index=True)
    z=r/'verification_v1_3_controlled_factor_bundle.zip'
    if z.exists():st.download_button('下载 V1.3 脱敏验收包',z.read_bytes(),file_name=z.name,mime='application/zip')
    st.caption(f'本地持久目录：{r}')
