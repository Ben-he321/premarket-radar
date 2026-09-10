"""Summarize saved evidence without selecting new parameters or rereading holdout."""
from collections import Counter
import pandas as pd
from .runtime import root,read,write,utc
from .engine import metrics
from .research import slice_events


TRADE_COLUMNS = ['symbol', 'signal_date', 'entry_date', 'exit_date', 'qty',
                 'entry', 'exit', 'net_pnl', 'return_net', 'reason', 'strategy']


def read_trades(path):
    """Absent files are explicitly reported, never claimed to prove zero fills."""
    if not path.exists():
        return pd.DataFrame(columns=TRADE_COLUMNS), 'MISSING_TRADE_FILE'
    trades = pd.read_parquet(path)
    missing = set(TRADE_COLUMNS) - set(trades.columns)
    if missing and len(trades):
        raise ValueError('TRADE_SCHEMA_MISSING:' + ','.join(sorted(missing)))
    return trades.reindex(columns=TRADE_COLUMNS), 'EMPTY_TRADE_FILE' if trades.empty else 'PRESENT'


def percent_or_na(value):
    return 'N/A' if value is None or pd.isna(value) else f'{value:.2%}'


def produce():
    base=root();out=base/'research';u=read(base/'universe.json');cov=read(base/'coverage.json',[])
    reports=[];strategy=[];selection=[]
    frozen=read(out/'frozen_pipeline.json',{})
    for rec in u['records']:
        s=rec['symbol'];cs=[c for c in cov if c['symbol']==s];raw=next((c for c in cs if c['adjustment']=='raw'),{})
        quarantine=[]
        for path in (base/'quarantine'/s).rglob('*.json'):
            q=read(path)
            quarantine.extend([{'code':i['code'],'dates':i['dates'],'adjustment':path.parent.name} for i in q.get('quality',{}).get('issues',[]) if i['severity']=='ERROR'])
        reports.append({**rec,'status':raw.get('status',rec['status']),'coverage':cs,'quarantine':quarantine,
                        'actions_access':read(base/'actions'/(s+'.json'),{}).get('status','UNKNOWN'),
                        'actions_completeness':'UNKNOWN','halt_identity_for_missing_sessions':'UNKNOWN'})
        path=out/(s+'-development-events.parquet')
        e=pd.read_parquet(path) if path.exists() else pd.DataFrame()
        for trial in read(out/'all_trials.json',[]):
            if trial['symbol']!=s:continue
            x=e[e.config==trial['config']] if len(e) else e
            row={'symbol':s,'config':trial['config'],'category':rec['type'],**metrics(x),'evidence':'DEVELOPMENT_ONLY'}
            if len(x):row.update(holding_mean=float(x.holding.mean()),mfe_mean=float(x.mfe.mean()),mae_mean=float(x.mae.mean()),stress25_expectancy=float(x.stress25.mean()),stress50_expectancy=float(x.stress50.mean()))
            strategy.append(row)
        for window in frozen.get('selections',[]):
            if window['symbol']!=s:continue
            key=window.get('per_symbol');x=e[e.config==key] if key and len(e) else pd.DataFrame()
            details={'symbol':s,'stage':window['stage'],'evaluation_start':window['evaluation_start'],'selected':key,
                     'reason':'VALIDATION_POSITIVE_AFTER_TRAIN_SHORTLIST' if key else 'CASH_INSUFFICIENT_HISTORY_OR_NO_VALIDATED_CANDIDATE',
                     'training':metrics(slice_events(x,*window['training'])) if window.get('training') else metrics(pd.DataFrame()),
                     'validation':metrics(slice_events(x,*window['validation'])) if window.get('validation') else metrics(pd.DataFrame())}
            if window['stage']=='OUTER':details['outer_diagnostic']=metrics(slice_events(x,window['evaluation_start'],window['evaluation_end']))
            selection.append(details)
    write(base/'candidate_results.json',reports);write(out/'per_stock_strategy_table.json',strategy);write(out/'selection_evidence.json',selection)
    pd.DataFrame([{k:r.get(k) for k in ('symbol','name','type','status','exchange','research_start','listing_date','security_id','actions_access','actions_completeness')} for r in reports]).to_csv(base/'candidate_results.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(strategy).to_csv(out/'per_stock_strategy_table.csv',index=False,encoding='utf-8-sig')
    portfolios=read(out/'portfolio.json',[]);promotion=[]
    account_metrics=[]
    for p in portfolios:
        stem=p['structure']+'-'+p['cost_case']
        curve=pd.read_parquet(out/(stem+'-equity.parquet'))
        trade_path=out/(stem+'-trades.parquet');t,trade_status=read_trades(trade_path)
        invested=curve.equity-curve.cash-curve.unsettled-curve.dividend_receivable
        turnover=float((t.qty*(t.entry+t.exit)).sum()/curve.equity.mean()) if len(t) else 0
        account_metrics.append({'structure':p['structure'],'cost_case':p['cost_case'],'start':curve.date.iloc[0],'end':curve.date.iloc[-1],
                                'starting_equity':5500,'ending_equity':p['ending_equity'],'net_return':p['ending_equity']/5500-1,
                                'max_drawdown':p['max_drawdown'],'average_capital_utilization':float((invested/curve.equity).mean()),
                                'completed_trade_notional_turnover':turnover,'closed_trades':len(t),'open_positions_at_end':int(curve.positions.iloc[-1]),
                                'basis':'RAW_ACTIONS_DIAGNOSTIC','annualization':'NOT_USED','trade_file_status':trade_status})
    write(out/'account_metrics.json',account_metrics)
    for name in ('SHARED','PER_SYMBOL','HYBRID_REGIME'):
        b=next(r for r in portfolios if r['structure']==name and r['cost_case']=='base')
        stress=next(r for r in portfolios if r['structure']==name and r['cost_case']=='stress25')
        t,trade_status=read_trades(out/(name+'-base-trades.parquet'))
        cutoff=read(base/'preregistration.json')['holdout_start'];t=t[t.exit_date<cutoff]
        positive=t.net_pnl.clip(lower=0).sum();largest=float(t.net_pnl.clip(lower=0).max()/positive) if positive else None
        windows=[]
        for w in { (x['evaluation_start'],x['evaluation_end']) for x in frozen['selections'] if x['stage']=='OUTER'}:
            subset=t[(t.exit_date>=w[0])&(t.exit_date<=w[1])]
            windows.append(float(subset.net_pnl.sum()))
        positive_windows=sum(max(0,x) for x in windows)
        promotion.append({'structure':name,'outer_windows':len(windows),'outer_trades':b['outer']['count'],
                          'net_expectancy':b['outer']['expectancy'],'stress25_expectancy':stress['outer']['expectancy'],
                          'largest_trade_positive_profit_share':largest,
                          'largest_window_positive_profit_share':max(windows)/positive_windows if positive_windows else None,
                          'trade_file_status':trade_status,
                          'qualified':False,'blockers':(['MISSING_TRADE_FILE'] if trade_status=='MISSING_TRADE_FILE' else []) +
                          (['NO_COMPLETED_OUTER_TRADES'] if t.empty else []) +
                          (['NEGATIVE_BASE_OR_STRESS_EXPECTANCY'] if (b['outer']['expectancy'] or 0)<=0 or (stress['outer']['expectancy'] or 0)<0 else []),
                          'engineering_blockers':['ACTIONS_COMPLETENESS_UNKNOWN','DAILY_OPEN_EXECUTION_SEMANTICS_UNVERIFIED']})
    write(out/'promotion.json',promotion)
    write(out/'improvements.json',{'limit':3,'used':0,'reason':'Kept preregistered simple rules; no performance-driven search after holdout','champion':None,'challengers':12})
    counts=dict(Counter(r['status'] for r in reports))
    lines=['# 全池真实运行结果',f'生成时间：{utc()}','',f'候选66；状态分布：{counts}。参考3只不进入订单。',
           '真实历史 SIP 已取得；最新 SIP 未获权限，Basic 历史下载继续。',
           '所有策略/账户结果为研究诊断，未通过晋级；主前向账户现金观察。',
           '','|代码|类型|最终处理状态|原始行数|实际起止|UNKNOWN / 质量问题|','|---|---|---|---:|---|---|']
    for r in reports:
        c=next((c for c in r['coverage'] if c['adjustment']=='raw'),{})
        issues=', '.join(i['code']+':'+str(i['count']) for i in c.get('quality',{}).get('issues',[])) or '无已检出数值异常'
        lines.append(f"|{r['symbol']}|{r['type']}|{r['status']}|{c.get('rows',0)}|{c.get('first')} → {c.get('last')}|{issues}; 公司行动完整性 UNKNOWN|")
    lines+=['','## 账户诊断（统一初始 $5500）','|结构|期末权益|最大回撤|外层净期望/笔|最终保留区间净期望/笔|','|---|---:|---:|---:|---:|']
    for p in portfolios:
        if p['cost_case']=='base':lines.append(f"|{p['structure']}|${p['ending_equity']:.2f}|{percent_or_na(p['max_drawdown'])}|{percent_or_na(p['outer']['expectancy'])}|{percent_or_na(p['holdout']['expectancy'])}|")
    lines+=['','数据逐日缺口和隔离记录见 candidate_results.json；逐股策略见 research/per_stock_strategy_table.csv。',
            '训练/验证/外层选择记录见 research/selection_evidence.json；一次最终保留检验见 research/holdout_receipt.json。',
            '费用是研究假设；股息付款日、部分公司行动及扩展时段可成交口径待核实。',
            '完整搜索空间的正式 DSR 无法确证；固定13配置日期矩阵的 DSR/PBO 仅作局部诊断，不能当作策略验收。',
            '没有真实时间虚拟成交，不以历史回测填入前向账本。']
    (base/'RESULTS.md').write_text('\n'.join(lines),encoding='utf-8')
    return counts
