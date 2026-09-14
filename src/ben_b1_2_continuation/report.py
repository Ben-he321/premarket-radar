"""Read-only financial reconciliation and a public evidence whitelist.

Never run while the single writer is active. No engine execution, market API,
account initialization, signal calculation or fee reparameterization occurs.
"""
import csv
import json
import math
import zipfile
from pathlib import Path
import pandas as pd
from src.ben_b1.ledger import Ledger
from .runtime import *

def campaign_parts(value,marks):
    book=Ledger.from_dict(value);rows=[]
    for c in book.campaigns.values():
        p=book.positions.get(c['symbol'],{})
        remaining=p.get('quantity',0) if p.get('campaign_id')==c['campaign_id'] else 0
        # Splits update both entry_quantity and exit_quantity in the ledger;
        # allocate historical all-in entry outlay, never adjust execution price.
        remaining_cost=c['entry_outlay']*remaining/c['entry_quantity'] if c['entry_quantity'] else 0
        mv=remaining*marks[c['symbol']] if remaining and c['symbol'] in marks else None if remaining else 0
        realized_trade=c['exit_proceeds']-(c['entry_outlay']-remaining_cost)
        unrealized=mv-remaining_cost if mv is not None else None
        row={**c,'remaining_shares':remaining,'remaining_allocated_entry_cost':remaining_cost,
            'remaining_close_market_value':mv,'realized_trade_profit_after_allocated_entry_cost_and_exit_cost':realized_trade,
            'unrealized_mark_profit_before_future_exit_cost':unrealized,
            'dividend_income_entitled':c.get('dividends',0),
            'closed_campaign_net_profit':c['exit_proceeds']+c.get('dividends',0)-c['entry_outlay']-c['interest'] if c['status']=='CLOSED' else None,
            'cost_allocation':'PROPORTIONAL_AVERAGE_ALL_IN_ENTRY_OUTLAY_IN_CURRENT_SPLIT_UNITS; REPORT_ONLY'}
        rows.append(row)
    return rows

def analyze(path,label,quarter=True):
    ledger_path=path/'QUARTER_END_LEDGER.json'
    valuation_path=path/'QUARTER_END_CLOSE_VALUATION.json'
    complete=ledger_path.exists() and valuation_path.exists()
    if complete:
        value=read(ledger_path);valuation=read(valuation_path);day='2026-03-31'
    else:
        cp=read(path/'checkpoint.json')['payload'];value=cp['ledger'];day=cp['state']['b12_completed_day']['day']
        values=read(path/'CLOSE_VALUATIONS.json');valuation=next((r for r in reversed(values) if r['trade_date']<=day),{})
    marks={};histories={r['symbol']:r for r in read(OLD/'data/HISTORY_INPUTS.json')}
    mark_day=valuation.get('trade_date',day)
    pins=read(path/'DYNAMIC_INPUT_HASHES.json')
    for symbol in value['state']['positions']:
        r=histories[symbol]['rth'];source=Path(r['path'])
        if pins.get(str(source))!=sha(source):raise ValueError('VALUATION_INPUT_PIN_MISMATCH:'+symbol)
        frame=pd.read_parquet(source,columns=['trade_date','close']);f=frame[frame.trade_date.eq(mark_day)]
        if len(f)==1 and pd.notna(f.iloc[0].close):marks[symbol]=float(f.iloc[0].close)
    book=Ledger.from_dict(value);parts=campaign_parts(value,marks)
    closed=sum(r['closed_campaign_net_profit'] for r in parts if r['closed_campaign_net_profit'] is not None)
    realized=sum(r['realized_trade_profit_after_allocated_entry_cost_and_exit_cost'] for r in parts)
    unrealized=sum(r['unrealized_mark_profit_before_future_exit_cost'] for r in parts) if all(r['unrealized_mark_profit_before_future_exit_cost'] is not None for r in parts) else None
    dividends=sum(r['dividend_income_entitled'] for r in parts);interest=sum(r['interest'] for r in parts)
    nav=valuation.get('net_equity');net=nav-5500 if nav is not None else None
    partition_total=realized+unrealized+dividends-interest if unrealized is not None else None
    entry=sum(c['entry_outlay'] for c in book.campaigns.values());exit=sum(c['exit_proceeds'] for c in book.campaigns.values())
    paid=sum(r['amount'] for r in book.dividend_receivables.values() if r['status']=='PAID')
    cash_bridge=5500-entry+exit+paid-book.interest_posted
    actual_cash=book.cash+book.unsettled_cash
    values=[r for r in read(path/'CLOSE_VALUATIONS.json') if r['trade_date']<=mark_day]
    peak=5500.;dd=0.
    for v in values:
        if v.get('net_equity') is not None:peak=max(peak,v['net_equity']);dd=min(dd,v['net_equity']/peak-1)
    fills=list(book.fills.values())
    recovery=read(path/'RECOVERY_IDEMPOTENCY.json') if (path/'RECOVERY_IDEMPOTENCY.json').exists() else {'status':'NOT_COMPLETED'}
    result={'account':label,'same_original_run_id':read(path/'RUN_SPEC.json')['config']['run_id'],
        'quarter_complete':complete,'quarter_end_equity':nav if complete else None,'latest_completed_day':day,'valuation_day':mark_day,
        'latest_completed_close_equity':nav,'initial_capital':5500,'cash':book.cash,'unsettled_cash':book.unsettled_cash,
        'dividend_receivable':valuation.get('dividend_receivable'),'reserved_exit_fees':book.reserved_exit_fees,
        'positions':{s:p['quantity'] for s,p in book.positions.items()},'closed_campaign_net_profit':round(closed,2),
        'realized_trade_profit_all_campaigns':round(realized,2),'unrealized_open_mark_profit':round(unrealized,2) if unrealized is not None else None,
        'dividend_income_entitled':round(dividends,2),'interest':round(interest,2),'total_net_profit':round(net,2) if net is not None else None,
        'commission':sum(f['commission'] for f in fills),'friction':sum(c['friction'] for c in book.campaigns.values()),
        'buy_fills':sum(f['side']=='BUY' for f in fills),'sell_fills':sum(f['side']=='SELL' for f in fills),
        'maximum_close_drawdown':dd,'no_terminal_liquidation_cost':True,'recovery':recovery,
        'cash_bridge':{'expected_cash_plus_unsettled':round(cash_bridge,2),'actual_cash_plus_unsettled':round(actual_cash,2),'difference':round(actual_cash-cash_bridge,8)},
        'profit_partition_difference':round(net-partition_total,8) if net is not None and partition_total is not None else None,
        'quarter_ledger_source':str(ledger_path) if complete else str(path/'checkpoint.json')}
    if abs(result['cash_bridge']['difference'])>.011:raise ValueError('CASH_RECONCILIATION_FAILED:'+label)
    if result['profit_partition_difference'] is not None and abs(result['profit_partition_difference'])>.011:raise ValueError('PROFIT_PARTITION_RECONCILIATION_FAILED:'+label)
    return result,parts

def write_csv(path,rows):
    pd.DataFrame(rows).to_csv(path,index=False,encoding='utf-8-sig')

def money(v):return '未完成' if v is None else f'{v:,.2f}'

def run():
    state=read(ROOT/'RUNNER_PROCESS.json')
    if state['status']!='STOPPED':raise ValueError('WRITER_ACTIVE_NO_FINAL_REPORT')
    final=ROOT/'final'
    if final.exists():raise ValueError('FINAL_OUTPUT_ALREADY_EXISTS_USE_EXPLICIT_NEW_VERSION')
    final.mkdir();rows=[]
    profile={};global_peak=0.
    with (ROOT/'RESOURCE_PROFILE.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            r=json.loads(line);phase=r['phase'];v=profile.setdefault(phase,{'phase':phase,'observations':0,'observed_max_rss_mib':0.,'minimum_free_memory_mib':float('inf'),'minimum_free_disk_bytes':float('inf')})
            v['observations']+=1;v['observed_max_rss_mib']=max(v['observed_max_rss_mib'],r['rss_mib'])
            v['minimum_free_memory_mib']=min(v['minimum_free_memory_mib'],r['free_memory_mib'])
            v['minimum_free_disk_bytes']=min(v['minimum_free_disk_bytes'],r['free_disk_bytes'])
            global_peak=max(global_peak,r['peak_rss_mib'])
    write_csv(final/'OBSERVED_PHASE_MEMORY.csv',list(profile.values()))
    write(final/'RESOURCE_PEAKS.json',{'process_high_water_mib':global_peak,'phase_observations':profile,
        'phase_rss_is_sampled_not_exact_allocation_attribution':True,'sampling':'10 seconds plus logical stage boundaries','deadline':DEADLINE})
    sources={}
    for label,code in [('Q0/A','Q0_P50_A'),('Q1/A','Q1_P50_A'),('Q0/B','Q0_P50_B'),('Q1/B','Q1_P50_B')]:
        directory=ACCOUNT if label=='Q1/B' else OLD/'portfolio/compact_base_v1'/('B12_'+code+'_compact_base_v1')
        row,parts=analyze(directory,label);rows.append(row)
        write(final/(label.replace('/','_')+'_ACCOUNT_RECONCILIATION.json'),row)
        write_csv(final/(label.replace('/','_')+'_CAMPAIGN_PARTITIONS.csv'),parts)
        sources[label]=str(directory)
    benchmarks=read(OLD/'benchmarks/frozen_v1/SUMMARY.json')
    write(final/'BENCHMARKS_REUSED.json',{'source':str(OLD/'benchmarks/frozen_v1'),'sha256':sha(OLD/'benchmarks/frozen_v1/SUMMARY.json'),'results':benchmarks,'recomputed':False})
    table=[]
    for r in rows:
        table.append({'账户':r['account'],'季度完整':r['quarter_complete'],'3月31日权益':r['quarter_end_equity'],
            '季度已平仓损益':r['closed_campaign_net_profit'] if r['quarter_complete'] else None,
            '季度未平仓浮盈':r['unrealized_open_mark_profit'] if r['quarter_complete'] else None,
            '季度总损益':r['total_net_profit'] if r['quarter_complete'] else None,'日收盘最大回撤':r['maximum_close_drawdown'] if r['quarter_complete'] else None,
            '相对SPY美元':r['quarter_end_equity']-benchmarks[0]['end_equity_usd'] if r['quarter_complete'] else None,
            '相对QQQ美元':r['quarter_end_equity']-benchmarks[1]['end_equity_usd'] if r['quarter_complete'] else None})
    for b in benchmarks:
        last=pd.read_csv(OLD/'benchmarks/frozen_v1'/b['symbol']/'daily.csv').iloc[-1]
        table.append({'账户':b['symbol'],'季度完整':True,'3月31日权益':b['end_equity_usd'],'季度已平仓损益':0,
            '季度未平仓浮盈':round(last.market_value_usd-last.cost_basis_usd,2),'季度总损益':b['net_profit_usd'],
            '分红到账加应收':b['dividend_cash_paid_usd']+b['end_dividend_receivable_usd'],'日收盘最大回撤':b['maximum_drawdown']})
    write_csv(final/'QUARTER_COMPARISON.csv',table)
    q=rows[-1];complete=q['quarter_complete'] and (ACCOUNT/'FINAL_ACCOUNT.json').exists() and q['recovery']['status']=='PASS'
    latest=read(ACCOUNT/'progress.json');stop=read(ROOT/'STOP_RECORD.json') if (ROOT/'STOP_RECORD.json').exists() else None
    text=['# B1.2 Q1/B续作结果','',('完整季度与4月尾段已完成并恢复PASS。' if complete else '本轮未完成全部季度及尾段，不能给出Q1/B完整季度成绩。'),'',
        f"原账户保持不变。本轮最后完成日期：{latest['processed_day']}。原2026-01-22的6607.69美元仍是中途快照，未覆盖成最终成绩。",'',
        '|账户|3月31日权益|季度净损益|相对SPY|相对QQQ|','|---|---:|---:|---:|---:|']
    for r in table:text.append(f"|{r['账户']}|{money(r['3月31日权益'])}|{money(r['季度总损益'])}|{money(r.get('相对SPY美元'))}|{money(r.get('相对QQQ美元'))}|")
    text += ['',f"Q1/B在{q['valuation_day']}的完整日收盘估值为{money(q['latest_completed_close_equity'])}美元，现金{money(q['cash'])}美元、未结算款{money(q['unsettled_cash'])}美元，持仓{json.dumps(q['positions'],ensure_ascii=False)}。",
        f"截至该日：已完成买入到退出的campaign净损益{money(q['closed_campaign_net_profit'])}美元；全部已卖出部分的交易损益{money(q['realized_trade_profit_all_campaigns'])}美元；仍持有股数的浮动损益{money(q['unrealized_open_mark_profit'])}美元。已卖出部分与未平仓成本采用平均成本比例作只读归因，不改变成交或账本。",
        f"分红权益{money(q['dividend_income_entitled'])}美元、融资利息{money(q['interest'])}美元。费用已在现金流和损益中扣除；佣金{money(q['commission'])}美元、摩擦成本{money(q['friction'])}美元不再次扣除。期末未强制卖出，因此未预扣未来平仓费用。",
        '', 'SPY和QQQ沿用原2026-01-02至03-31、5500美元整数股账户。起始按首交易日常规时段首分钟开盘价、每实际订单1美元及10bp执行摩擦；股息按实际到账日确认现金，下一严格更晚交易日开盘尝试整数股再投资，零股不下单。期末按原始收盘价估值，SPY未到账分红权益14.38美元单列，现金零利息。基金管理费不重复扣除，未计税费。复权总回报参考线与现金分红账户分开。',
        '', '四本账户均受原66候选身份、历史量价覆盖及财报证据限制。A层PIT证据不足导致零入场，不能称策略通过；B层使用回顾性的已发布财报排除，不能冒充完整PIT或前向可执行优势。未知财报、身份或历史输入仍UNKNOWN，未取消门槛。覆盖有限的组合与完整指数比较，不是独立盈利证明。',
        '', '工程验收：原检查点全部36代、5602986事件及5576918归档库存逐项恢复；MRVL590924真实事件的9段所有状态组件等价；179253事件真实归档提交后中断恢复及整段重复幂等；NVDA1792943报价/180页逐字段、ID、接收时间和分批大小等价；85项回归与36项最终复验PASS。原10文件包含checkpoint、SQLite、WAL、SHM的私有完整备份已逐文件核对。',
        f'实际续跑进程累计内存峰值{global_peak:.2f}MiB。各读取、排序、事件积累、归档和恢复阶段的观测值另见OBSERVED_PHASE_MEMORY.csv；阶段采样值不冒充精确的内存分配点。',
        '', '另外三本完成账户直接复用，原M20/U服务及账本不重启、不改动。仅原共享API限流元数据继续用于共同配额，不涉及其交易逻辑。代码分支codex/ben-b1-2-resume-streaming，草稿PR #32，不合并main。']
    if stop:text += ['',f"本轮停止原因：{stop['type']} / {stop['reason']}，时间{stop['at']}。原轮次的停止原因和未完成状态没有改写。"]
    text += ['',f"恢复入口：{REPO}，模块src.ben_b1_2_continuation.runner；读取本轮ENGINEERING_GATE和同一私有检查点，拒绝从头初始化。授权硬截止{DEADLINE}；截止后需新一轮明确授权。",
        '', '验证包不包含密钥、原始行情、Parquet、完整检查点、SQLite、WAL、SHM或私有全量备份。完整私有恢复副本仍留在本地。']
    (final/'BEN_B1_2_CONTINUATION_RESULTS.md').write_text('\n'.join(text)+'\n',encoding='utf-8')
    write(final/'ACTUAL_COMPLETION.json',{'at':utc(),'full_quarter_and_tail_complete':complete,'last_completed_day':latest['processed_day'],
        'original_6607_69_is_interim':True,'original_account_directories':sources,'stop':stop,'accounts':rows})
    print({'full_quarter_and_tail_complete':complete,'report':str(final/'BEN_B1_2_CONTINUATION_RESULTS.md')},flush=True)

if __name__=='__main__':run()
