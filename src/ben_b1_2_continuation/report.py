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
import pandas_market_calendars as mcal
from src.ben_b1.ledger import Ledger
from src.ben_b1_2.compact import stream_hash
from .runtime import *
from .report_support import coverage_summary,verified_benchmarks

START='2026-01-02'
END='2026-03-31'
TAIL_END='2026-04-30'
NY='America/New_York'

def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)

def valuation_issues(value,day):
    issues=[]
    if value.get('trade_date')!=day or value.get('asof_day')!=day:issues.append('VALUATION_DATE_MISMATCH')
    if value.get('valuation_status')!='CURRENT_MARKS' or value.get('missing_current_marks'):issues.append('CURRENT_MARKS_NOT_COMPLETE')
    if not finite(value.get('net_equity')) or value.get('net_equity',-1)<0:issues.append('EQUITY_NOT_FINITE_NONNEGATIVE')
    schedule=mcal.get_calendar('NYSE').schedule(day,day)
    if len(schedule)!=1:issues.append('VALUATION_NOT_A_SESSION')
    else:
        try:
            cutoff=pd.Timestamp(value['account_state_cutoff'])
            if cutoff.tzinfo is None or str(cutoff.tz_convert(NY).date())!=day or cutoff<schedule.iloc[0].market_close:issues.append('ACCOUNT_CUTOFF_NOT_SAME_COMPLETED_SESSION')
            if pd.Timestamp(value['price_cutoff'])!=schedule.iloc[0].market_close:issues.append('CLOSE_PRICE_CUTOFF_MISMATCH')
        except (KeyError,ValueError,TypeError):issues.append('CUTOFF_UNKNOWN')
    return issues

def calendar_audit(values,end):
    selected=sorted((r for r in values if START<=r.get('trade_date','')<=end),key=lambda r:r['trade_date'])
    expected=[str(d.date()) for d in mcal.get_calendar('NYSE').schedule(START,end).index]
    issues=[]
    if [r['trade_date'] for r in selected]!=expected:issues.append('SESSION_CALENDAR_MISSING_OR_DUPLICATE')
    bad=[{'day':r['trade_date'],'issues':valuation_issues(r,r['trade_date'])} for r in selected]
    bad=[r for r in bad if r['issues']]
    if bad:issues.append('INVALID_DAILY_VALUATIONS')
    peak=5500.;dd=0.
    if not issues:
        for r in selected:peak=max(peak,r['net_equity']);dd=min(dd,r['net_equity']/peak-1)
    return {'status':'PASS' if not issues else 'UNKNOWN_INCOMPLETE','expected_sessions':len(expected),
            'actual_rows':len(selected),'issues':issues,'invalid_days':bad,'maximum_close_drawdown':dd if not issues else None}

def verified_checkpoint(path):
    source=path/'checkpoint.json';before=source.stat();wrapper=read(source);cp=wrapper['payload']
    if wrapper.get('sha256')!=stream_hash(cp):raise ValueError('READ_ONLY_CHECKPOINT_WRAPPER_HASH_FAILED:'+str(source))
    after=source.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('CHECKPOINT_CHANGED_DURING_READ_ONLY_REPORT')
    return cp,{'source':str(source),'wrapper_sha256':wrapper['sha256'],'hash_verified':True,'engine_restored':False,'events_executed':0}

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
    path=Path(path)
    ledger_path=path/'QUARTER_END_LEDGER.json'
    valuation_path=path/'QUARTER_END_CLOSE_VALUATION.json'
    values=read(path/'CLOSE_VALUATIONS.json');checkpoint_proof=None;state_cutoff=None;issues=[]
    quarter_files=ledger_path.exists() and valuation_path.exists()
    if quarter and quarter_files:
        value=read(ledger_path);valuation=read(valuation_path);day=END
        if valuation not in values:issues.append('QUARTER_VALUATION_NOT_IDENTICAL_TO_DAILY_ROW')
    else:
        cp,checkpoint_proof=verified_checkpoint(path);value=cp['ledger'];day=cp['state']['b12_completed_day']['day'];state_cutoff=cp['state']['at']
        exact=[r for r in values if r['trade_date']==day and r.get('account_state_cutoff')==state_cutoff]
        valuation=exact[0] if len(exact)==1 else {}
        if len(exact)!=1:issues.append('NO_UNIQUE_SAME_DAY_CHECKPOINT_CLOSE_VALUATION')
        del cp
    issues+=valuation_issues(valuation,day)
    if value['state'].get('asof_day')!=day:issues.append('LEDGER_DAY_MISMATCH')
    marks={};histories={r['symbol']:r for r in read(OLD/'data/HISTORY_INPUTS.json')}
    mark_day=day
    pins=read(path/'DYNAMIC_INPUT_HASHES.json')
    for symbol in value['state']['positions']:
        r=histories[symbol]['rth'];source=Path(r['path'])
        if pins.get(str(source))!=sha(source):raise ValueError('VALUATION_INPUT_PIN_MISMATCH:'+symbol)
        frame=pd.read_parquet(source,columns=['trade_date','close']);f=frame[frame.trade_date.eq(mark_day)]
        if len(f)==1 and finite(float(f.iloc[0].close)) and float(f.iloc[0].close)>0:marks[symbol]=float(f.iloc[0].close)
        else:issues.append('NO_VALID_CURRENT_CLOSE:'+symbol)
    book=Ledger.from_dict(value);parts=campaign_parts(value,marks)
    actual_snapshot=book.snapshot(marks)
    for field in ('net_equity','cash','unsettled_cash','dividend_receivable','market_value','debt','reserved_exit_fees','interest_total'):
        a=actual_snapshot.get(field);b=valuation.get(field)
        if not finite(a) or not finite(b) or abs(a-b)>.011:issues.append('LEDGER_VALUATION_MISMATCH:'+field)
    audit=calendar_audit(values,day)
    complete=quarter and quarter_files and day==END and not issues and audit['status']=='PASS'
    closed=sum(r['closed_campaign_net_profit'] for r in parts if r['closed_campaign_net_profit'] is not None)
    realized=sum(r['realized_trade_profit_after_allocated_entry_cost_and_exit_cost'] for r in parts)
    unrealized=sum(r['unrealized_mark_profit_before_future_exit_cost'] for r in parts) if all(r['unrealized_mark_profit_before_future_exit_cost'] is not None for r in parts) else None
    dividends=sum(r['dividend_income_entitled'] for r in parts);interest=sum(r['interest'] for r in parts)
    nav=valuation.get('net_equity') if not issues else None;net=nav-5500 if nav is not None else None
    partition_total=realized+unrealized+dividends-interest if unrealized is not None else None
    entry=sum(c['entry_outlay'] for c in book.campaigns.values());exit=sum(c['exit_proceeds'] for c in book.campaigns.values())
    paid=sum(r['amount'] for r in book.dividend_receivables.values() if r['status']=='PAID')
    cash_bridge=5500-entry+exit+paid-book.interest_posted
    actual_cash=book.cash+book.unsettled_cash
    fills=list(book.fills.values())
    recovery=read(path/'RECOVERY_IDEMPOTENCY.json') if (path/'RECOVERY_IDEMPOTENCY.json').exists() else {'status':'NOT_COMPLETED'}
    result={'account':label,'same_original_run_id':read(path/'RUN_SPEC.json')['config']['run_id'],
        'period':'QUARTER' if quarter and quarter_files else 'LATEST_DURABLE_CHECKPOINT',
        'quarter_complete':complete,'quarter_end_equity':nav if complete else None,'latest_completed_day':day,'valuation_day':mark_day,
        'valuation_issues':issues,'calendar_audit':audit,'checkpoint_read_only_verification':checkpoint_proof,
        'latest_completed_close_equity':nav,'initial_capital':5500,'cash':book.cash,'unsettled_cash':book.unsettled_cash,
        'dividend_receivable':valuation.get('dividend_receivable'),'reserved_exit_fees':book.reserved_exit_fees,
        'positions':{s:p['quantity'] for s,p in book.positions.items()},'closed_campaign_net_profit':round(closed,2),
        'realized_trade_profit_all_campaigns':round(realized,2),'unrealized_open_mark_profit':round(unrealized,2) if unrealized is not None else None,
        'dividend_income_entitled':round(dividends,2),'interest':round(interest,2),'total_net_profit':round(net,2) if net is not None else None,
        'commission':sum(f['commission'] for f in fills),'friction':sum(c['friction'] for c in book.campaigns.values()),
        'buy_fills':sum(f['side']=='BUY' for f in fills),'sell_fills':sum(f['side']=='SELL' for f in fills),
        'maximum_close_drawdown':audit['maximum_close_drawdown'],'no_terminal_liquidation_cost':True,'recovery':recovery,
        'cash_bridge':{'expected_cash_plus_unsettled':round(cash_bridge,2),'actual_cash_plus_unsettled':round(actual_cash,2),'difference':round(actual_cash-cash_bridge,8)},
        'profit_partition_difference':round(net-partition_total,8) if net is not None and partition_total is not None else None,
        'ledger_source':str(ledger_path) if quarter and quarter_files else str(path/'checkpoint.json')}
    if abs(result['cash_bridge']['difference'])>.011:raise ValueError('CASH_RECONCILIATION_FAILED:'+label)
    if result['profit_partition_difference'] is not None and abs(result['profit_partition_difference'])>.011:raise ValueError('PROFIT_PARTITION_RECONCILIATION_FAILED:'+label)
    return result,parts

def write_csv(path,rows):
    pd.DataFrame(rows).to_csv(path,index=False,encoding='utf-8-sig')

def money(v):return '未完成' if v is None else f'{v:,.2f}'

def recovery_matches_checkpoint(recovery,proof):
    digest=(proof or {}).get('wrapper_sha256')
    return bool(digest and (proof or {}).get('hash_verified') and recovery.get('status')=='PASS'
                and recovery.get('synthetic') is False and recovery.get('before')==recovery.get('after')==digest)

def migration_restore_matches_expected(restored,expected,expected_sha256):
    return bool(expected.get('status')=='PASS_ONLY_PATH_RELOCATED_PENDING_FULL_RESTORE'
        and expected.get('only_archive_path_changed') is True and expected.get('events_executed')==0
        and expected.get('same_start')==STARTED_AT and expected.get('same_deadline')==DEADLINE
        and expected.get('expected_payload_hashes') and expected.get('expected_state_hashes')
        and restored.get('status')=='PASS' and restored.get('expected_proof_sha256')==expected_sha256
        and restored.get('payload_hashes')==expected['expected_payload_hashes']
        and restored.get('state_hashes')==expected['expected_state_hashes']
        and restored.get('all_archive_hashes_verified_by_actual_restore') is True
        and restored.get('cash_positions_costs_stop_legs_orders_reserves_settlements_consumed_quotes_preserved') is True
        and restored.get('events_executed_before_verification')==0
        and restored.get('same_run_id')==RUN_ID and restored.get('same_deadline')==DEADLINE
        and restored.get('completed_day')==expected.get('completed_day')
        and restored.get('cash')==expected.get('cash'))

def engineering_evidence():
    gate=read(active_gate_path());checked=[]
    for name,expected in gate['files'].items():
        p=Path(name);checked.append({'path':name,'hash_matches':p.is_file() and sha(p)==expected})
    result={'gate_path':str(active_gate_path()),'gate_status':gate['status'],'gate_at':gate['at'],'files':checked,
        'regression_cases':gate.get('tests_passed'),'repeated_final_guard_cases':gate.get('final_guard_cases_passed')}
    for key,name in [('restore','REAL_PREFIX_RESTORE.json'),('dense','DENSE_REAL_EQUIVALENCE.json'),
                     ('crash','REAL_DENSE_CRASH_RECOVERY.json'),('normalization','REAL_DENSE_INPUT_EQUIVALENCE_v2.json')]:
        result[key]=read(ROOT/'engineering'/name)
    result['status']='PASS' if gate['status']=='PASS' and all(r['hash_matches'] for r in checked) and all(result[k].get('status','').startswith('PASS') for k in ('restore','dense','crash','normalization')) else 'FAIL'
    if (ROOT/'engineering/STORAGE_MIGRATION_EXPECTED.json').exists():
        p=ROOT/'engineering/STORAGE_MIGRATION_RESTORE.json'
        result['storage_migration_restore']=read(p) if p.exists() else {'status':'NOT_VERIFIED'}
        expected_path=ROOT/'engineering/STORAGE_MIGRATION_EXPECTED.json'
        result['storage_migration_proof_binding']=migration_restore_matches_expected(result['storage_migration_restore'],read(expected_path),sha(expected_path))
        if not result['storage_migration_proof_binding']:result['status']='FAIL'
        for key,name in [('storage_dense','STORAGE_DENSE_REAL_EQUIVALENCE.json'),('storage_crash','STORAGE_REAL_CRASH_RECOVERY.json')]:
            p=ROOT/'engineering'/name;result[key]=read(p) if p.exists() else {'status':'NOT_VERIFIED'}
            if result[key]['status']!='PASS':result['status']='FAIL'
    return result

def run():
    state=read(ROOT/'RUNNER_PROCESS.json')
    if state['status']!='STOPPED':raise ValueError('WRITER_ACTIVE_NO_FINAL_REPORT')
    final=ROOT/'final'
    if final.exists():raise ValueError('FINAL_OUTPUT_ALREADY_EXISTS_USE_EXPLICIT_NEW_VERSION')
    final.mkdir();rows=[];tails=[]
    evidence=engineering_evidence();write(final/'ENGINEERING_EVIDENCE_RECHECK.json',evidence)
    profile={};global_peak=0.
    with (ROOT/'RESOURCE_PROFILE.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            r=json.loads(line);phase=r['phase'];v=profile.setdefault(phase,{'phase':phase,'observations':0,'observed_max_rss_mib':0.,'minimum_free_memory_mib':float('inf'),'minimum_free_disk_bytes':float('inf')})
            v['observations']+=1;v['observed_max_rss_mib']=max(v['observed_max_rss_mib'],r['rss_mib'])
            v['minimum_free_memory_mib']=min(v['minimum_free_memory_mib'],r['free_memory_mib'])
            v['minimum_free_disk_bytes']=min(v['minimum_free_disk_bytes'],r['free_disk_bytes'])
            global_peak=max(global_peak,r['peak_rss_mib'])
    error_samples=list(ROOT.glob('STOP_RECORD*.json'))+list((ROOT/'attempts').rglob('STOP_RECORD*.json'))+list(ROOT.glob('PLANNED_STORAGE_STOP.json'))
    for path in error_samples:
        recorded=read(path).get('process_memory') or {}
        global_peak=max(global_peak,recorded.get('peak_rss_mib',0))
    write_csv(final/'OBSERVED_PHASE_MEMORY.csv',list(profile.values()))
    write(final/'RESOURCE_PEAKS.json',{'maximum_recorded_process_high_water_mib':global_peak,'phase_observations':profile,
        'phase_rss_is_sampled_not_exact_allocation_attribution':True,'sampling':'10 seconds plus logical stage boundaries','deadline':DEADLINE})
    sources={}
    for label,code in [('Q0/A','Q0_P50_A'),('Q1/A','Q1_P50_A'),('Q0/B','Q0_P50_B'),('Q1/B','Q1_P50_B')]:
        directory=ACCOUNT if label=='Q1/B' else OLD/'portfolio/compact_base_v1'/('B12_'+code+'_compact_base_v1')
        row,parts=analyze(directory,label);rows.append(row)
        write(final/(label.replace('/','_')+'_ACCOUNT_RECONCILIATION.json'),row)
        write_csv(final/(label.replace('/','_')+'_CAMPAIGN_PARTITIONS.csv'),parts)
        tail,tail_parts=analyze(directory,label,quarter=False)
        final_summary=read(directory/'FINAL_ACCOUNT.json') if (directory/'FINAL_ACCOUNT.json').exists() else {}
        saved_progress=read(directory/'progress.json')
        coverage=coverage_summary(read(directory/'QUOTE_COVERAGE.json'),saved_progress['processed_day'])
        write(final/(label.replace('/','_')+'_QUOTE_COVERAGE_SUMMARY.json'),coverage)
        write_csv(final/(label.replace('/','_')+'_QUOTE_COVERAGE_SUMMARY.csv'),coverage['groups'])
        tail['tail_period_processed']=bool(tail['latest_completed_day']==TAIL_END and saved_progress.get('processed_day')==TAIL_END
            and tail['checkpoint_read_only_verification']['hash_verified'])
        tail['final_export_attested']=bool(final_summary.get('actually_executed') and final_summary.get('processed_through')==TAIL_END and final_summary.get('error_count')==0)
        tail['tail_valuation_and_calendar_verified']=not tail['valuation_issues'] and tail['calendar_audit']['status']=='PASS'
        tail['tail_restore_verified']=recovery_matches_checkpoint(tail['recovery'],tail['checkpoint_read_only_verification'])
        tail['tail_complete']=tail['tail_period_processed'] and tail['final_export_attested'] and tail['tail_valuation_and_calendar_verified'] and tail['tail_restore_verified']
        tail['all_positions_closed_and_settled']=not tail['positions'] and tail['unsettled_cash']==0
        tail['change_after_quarter_end']={
            'equity':round(tail['latest_completed_close_equity']-row['quarter_end_equity'],2) if row['quarter_complete'] and tail['latest_completed_close_equity'] is not None else None,
            'buy_fills':tail['buy_fills']-row['buy_fills'] if row['quarter_complete'] else None,
            'sell_fills':tail['sell_fills']-row['sell_fills'] if row['quarter_complete'] else None,
            'commission':tail['commission']-row['commission'] if row['quarter_complete'] else None}
        if tail['change_after_quarter_end']['buy_fills'] not in (None,0):raise ValueError('FORBIDDEN_NEW_BUY_IN_EXIT_ONLY_TAIL:'+label)
        tails.append(tail)
        write(final/(label.replace('/','_')+'_TAIL_RECONCILIATION.json'),tail)
        write_csv(final/(label.replace('/','_')+'_TAIL_CAMPAIGN_PARTITIONS.csv'),tail_parts)
        sources[label]=str(directory)
    benchmark_map,benchmark_proof=verified_benchmarks(OLD/'benchmarks/frozen_v1',START,END)
    benchmarks=[benchmark_map[s] for s in ('SPY','QQQ')]
    write(final/'BENCHMARK_READ_ONLY_VERIFICATION.json',benchmark_proof)
    write(final/'BENCHMARKS_REUSED.json',{'source':str(OLD/'benchmarks/frozen_v1'),'sha256':sha(OLD/'benchmarks/frozen_v1/SUMMARY.json'),'results':benchmarks,'recomputed':False})
    table=[]
    for r in rows:
        table.append({'账户':r['account'],'季度完整':r['quarter_complete'],'3月31日权益':r['quarter_end_equity'],
            '季度已平仓损益':r['closed_campaign_net_profit'] if r['quarter_complete'] else None,
            '季度未平仓浮盈':r['unrealized_open_mark_profit'] if r['quarter_complete'] else None,
            '季度总损益':r['total_net_profit'] if r['quarter_complete'] else None,'日收盘最大回撤':r['maximum_close_drawdown'] if r['quarter_complete'] else None,
            '相对SPY美元':r['quarter_end_equity']-benchmark_map['SPY']['end_equity_usd'] if r['quarter_complete'] else None,
            '相对QQQ美元':r['quarter_end_equity']-benchmark_map['QQQ']['end_equity_usd'] if r['quarter_complete'] else None})
    for b in benchmarks:
        last=pd.read_csv(OLD/'benchmarks/frozen_v1'/b['symbol']/'daily.csv').iloc[-1]
        table.append({'账户':b['symbol'],'季度完整':True,'3月31日权益':b['end_equity_usd'],'季度已平仓损益':0,
            '季度未平仓浮盈':round(last.market_value_usd-last.cost_basis_usd,2),'季度总损益':b['net_profit_usd'],
            '分红到账加应收':b['dividend_cash_paid_usd']+b['end_dividend_receivable_usd'],'日收盘最大回撤':b['maximum_drawdown']})
    write_csv(final/'QUARTER_COMPARISON.csv',table)
    write_csv(final/'TAIL_COMPARISON.csv',[{'account':r['account'],'processed_day':r['latest_completed_day'],'tail_complete':r['tail_complete'],
        'end_equity':r['latest_completed_close_equity'],'cash':r['cash'],'unsettled':r['unsettled_cash'],'positions':json.dumps(r['positions']),
        'closed_campaign_profit':r['closed_campaign_net_profit'],'unrealized_open_profit':r['unrealized_open_mark_profit'],
        'quarter_to_tail_equity_change':r['change_after_quarter_end']['equity'],'new_buys_in_tail':r['change_after_quarter_end']['buy_fills'],
        'new_sells_in_tail':r['change_after_quarter_end']['sell_fills'],'recovery':r['recovery'].get('status')} for r in tails])
    q=rows[-1];tail=tails[-1];complete=q['quarter_complete'] and tail['tail_complete'] and evidence['status']=='PASS'
    latest=read(ACCOUNT/'progress.json');last_error=read(ROOT/'STOP_RECORD.json') if (ROOT/'STOP_RECORD.json').exists() else None
    stop=last_error if last_error and last_error['at']>=state.get('started_at','') else None
    outcome='完整季度与4月尾段已完成并恢复PASS。' if complete else ('Q1/B完整季度已核对；4月尾段或最终工程验收尚未全部完成，二者分别列示。' if q['quarter_complete'] else '本轮未完成可验收的完整季度，不能给出Q1/B完整季度成绩。')
    text=['# B1.2 Q1/B续作结果','',outcome,'',
        f"原账户保持不变。本轮最后完成日期：{latest['processed_day']}。原2026-01-22的6607.69美元仍是中途快照，未覆盖成最终成绩。",'',
        '|账户|3月31日权益|季度净损益|相对SPY|相对QQQ|','|---|---:|---:|---:|---:|']
    for r in table:text.append(f"|{r['账户']}|{money(r['3月31日权益'])}|{money(r['季度总损益'])}|{money(r.get('相对SPY美元'))}|{money(r.get('相对QQQ美元'))}|")
    text += ['',f"Q1/B在{q['valuation_day']}的完整日收盘估值为{money(q['latest_completed_close_equity'])}美元，现金{money(q['cash'])}美元、未结算款{money(q['unsettled_cash'])}美元，持仓{json.dumps(q['positions'],ensure_ascii=False)}。",
        f"截至该日：已完成买入到退出的campaign净损益{money(q['closed_campaign_net_profit'])}美元；全部已卖出部分的交易损益{money(q['realized_trade_profit_all_campaigns'])}美元；仍持有股数的浮动损益{money(q['unrealized_open_mark_profit'])}美元。已卖出部分与未平仓成本采用平均成本比例作只读归因，不改变成交或账本。",
        f"分红权益{money(q['dividend_income_entitled'])}美元、融资利息{money(q['interest'])}美元。费用已在现金流和损益中扣除；佣金{money(q['commission'])}美元、摩擦成本{money(q['friction'])}美元不再次扣除。期末未强制卖出，因此未预扣未来平仓费用。",
        '', 'SPY和QQQ沿用原2026-01-02至03-31、5500美元整数股账户。起始按首交易日常规时段首分钟开盘价、每实际订单1美元及10bp执行摩擦；股息按实际到账日确认现金，下一严格更晚交易日开盘尝试整数股再投资，零股不下单。期末按原始收盘价估值，SPY未到账分红权益14.38美元单列，现金零利息。基金管理费不重复扣除，未计税费。复权总回报参考线与现金分红账户分开。',
        '', '四本账户均受原66候选身份、历史量价覆盖及财报证据限制。A层PIT证据不足导致零入场，不能称策略通过；B层使用回顾性的已发布财报排除，不能冒充完整PIT或前向可执行优势。未知财报、身份或历史输入仍UNKNOWN，未取消门槛。覆盖有限的组合与完整指数比较，不是独立盈利证明。',
        '逐本QUOTE_COVERAGE_SUMMARY文件按已保存日期、证券/日期/用途去重，分别保留成功请求、完整空响应、未请求和未知/失败；报价条数不是独立覆盖样本，complete仅证明请求分页完成。未正式保存日期的记录另列，不计为已完成。基准61个交易日、5500美元本金及原完成清单哈希均重新只读核对。',
        '', f"4月尾段／最新已保存账本：截至{tail['latest_completed_day']}，权益{money(tail['latest_completed_close_equity'])}美元、现金{money(tail['cash'])}美元、未结算款{money(tail['unsettled_cash'])}美元、持仓{json.dumps(tail['positions'],ensure_ascii=False)}。尾段完成={tail['tail_complete']}，最终恢复={tail['recovery'].get('status')}，全部持仓已退出且结算={tail['all_positions_closed_and_settled']}。季度末之后的权益变化{money(tail['change_after_quarter_end']['equity'])}美元，新增买入成交{tail['change_after_quarter_end']['buy_fills']}笔、卖出成交{tail['change_after_quarter_end']['sell_fills']}笔；未完成字段保留未知。",
        f"工程证据复核={evidence['status']}；旧检查点恢复={evidence['restore']['status']}；{evidence['dense']['real_events']}条真实事件、{evidence['dense']['block_count']}段状态等价={evidence['dense']['status']}；真实中断与重复幂等={evidence['crash']['status']}；真实报价分批输入等价={evidence['normalization']['status']}。{evidence['regression_cases']}项回归及{evidence['repeated_final_guard_cases']}项最终重复复验读取实际冻结证据，重复复验不另算独立用例。私有完整备份与逐文件核对记录另附。",
        f'心跳及错误记录中的进程内存峰值最高为{global_peak:.2f}MiB。各读取、排序、事件积累、归档和恢复阶段的观测值另见OBSERVED_PHASE_MEMORY.csv；阶段采样值不冒充精确的内存分配点。',
        '', '另外三本完成账户直接复用，原M20/U服务及账本不重启、不改动。仅原共享API限流元数据继续用于共同配额，不涉及其交易逻辑。代码分支codex/ben-b1-2-resume-streaming，草稿PR #32，不合并main。']
    if stop:text += ['',f"本轮停止原因：{stop['type']} / {stop['reason']}，时间{stop['at']}。原轮次的停止原因和未完成状态没有改写。"]
    elif last_error:text += ['',f"本轮早先尝试在{last_error['at']}发生{last_error['type']}，其错误、源码、状态和恢复证据保存在attempts目录；该错误不是当前尝试的停止状态。"]
    if 'storage_migration_restore' in evidence:
        text += ['',f"存储迁移：本轮C盘完整静止副本244个文件、13324049727字节保留，D盘只重定位检查点归档路径。第二次尝试在18:50:19 UTC按已记录的30分钟等待边界计划停机，Jan23没有提交，不改写为资源故障。新版本55项回归、590924真实事件逐块等价及179253事件中断重放均PASS；D盘实际全量迁移恢复={evidence['storage_migration_restore']['status']}。它们与前述早期工程案例有重叠，不相加冒充独立用例数。"]
    text += ['',f"恢复入口：{REPO/'scripts/run_b12_continuation.py'}，使用既有ai-m1虚拟环境。该入口仅为进程设置D盘TEMP/TMP，再读取本轮ENGINEERING_GATE和同一私有检查点，拒绝从头初始化。授权硬截止{DEADLINE}；截止后需新一轮明确授权。",
        '', '验证包不包含密钥、原始行情、Parquet、完整检查点、SQLite、WAL、SHM或私有全量备份。完整私有恢复副本仍留在本地。']
    (final/'BEN_B1_2_CONTINUATION_RESULTS.md').write_text('\n'.join(text)+'\n',encoding='utf-8')
    write(final/'ACTUAL_COMPLETION.json',{'at':utc(),'full_quarter_and_tail_complete':complete,'quarter_complete':q['quarter_complete'],'tail_complete':tail['tail_complete'],
        'last_completed_day':latest['processed_day'],'original_6607_69_is_interim':True,'original_account_directories':sources,'stop':stop,'accounts':rows,'tails':tails})
    print({'full_quarter_and_tail_complete':complete,'report':str(final/'BEN_B1_2_CONTINUATION_RESULTS.md')},flush=True)

if __name__=='__main__':run()
