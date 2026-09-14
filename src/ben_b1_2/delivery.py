"""Read existing frozen B1.2 results, reconcile and package a strict whitelist.

This module does not fetch data or execute any strategy. Incomplete work stays
explicitly incomplete; it never initializes a missing account as a result.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import tomllib
import zipfile
import pandas as pd
from .runtime import ROOT, REPO, OLD_REPO, B11, B1, START, END, TAIL_END, read, write, sha, utc
from .sample_diagnostics import build as sample_comparison

ZIP_NAME='verification_ben_b1_2_window_portfolio_bundle.zip'

def existing(path,default=None):
    return read(path) if Path(path).is_file() else default

def numeric(x):
    return None if x is None or (isinstance(x,float) and pd.isna(x)) else float(x)

def usd(x):return '未知／未完成' if numeric(x) is None else f'{float(x):,.2f}'

def pct(x):return '未知／未完成' if numeric(x) is None else f'{float(x)*100:.2f}%'

def audit_account(path):
    path=Path(path);final=existing(path/'FINAL_ACCOUNT.json',{})
    q=existing(path/'QUARTER_END_CLOSE_VALUATION.json',{})
    quarter=existing(path/'QUARTER_END_ACCOUNT.json',{})
    progress=existing(path/'progress.json',{})
    values=existing(path/'CLOSE_VALUATIONS.json',[])
    selected=[r for r in values if START<=r['trade_date']<=END]
    equity=[numeric(r.get('net_equity')) for r in selected]
    peak=5500;dd=[]
    for e in equity:
        if e is None:continue
        peak=max(peak,e);dd.append(e/peak-1)
    campaigns=quarter.get('campaigns',{}).get('campaigns',[])
    fills_path=path/'fills.csv'
    fills_status='MISSING'
    try:
        fills=pd.read_csv(fills_path) if fills_path.is_file() else pd.DataFrame()
        if fills_path.is_file():fills_status='READ_OK'
    except pd.errors.EmptyDataError:
        fills=pd.DataFrame();fills_status='EMPTY_FILE_NO_SCHEMA'
    if len(fills):
        field='at' if 'at' in fills else 'fill_effective_at'
        fills=fills[pd.to_datetime(fills[field],utc=True).dt.tz_convert('America/New_York').dt.strftime('%Y-%m-%d')<=END]
    symbols=sorted(fills.symbol.unique()) if len(fills) else []
    end=numeric(q.get('net_equity')) if not q.get('missing_current_marks') and q.get('valuation_status')=='CURRENT_MARKS' else None
    latest=final.get('latest_equity') or {}
    tail_known=not latest.get('missing_current_marks') and latest.get('valuation_status')=='CURRENT_MARKS'
    recovery=existing(path/'RECOVERY_IDEMPOTENCY.json',{}).get('status','NOT_VERIFIED')
    completed=bool(final.get('actually_executed') and final.get('processed_through')==TAIL_END and final.get('error_count')==0 and fills_status=='READ_OK' and recovery=='PASS')
    row={'account':path.name,'actually_completed':completed,'status':final.get('status','NOT_COMPLETED'),
         'coverage_status':final.get('coverage_status','COVERAGE_LIMITED'),'processed_through':final.get('processed_through',progress.get('processed_day')),
         'initial_capital':5500,'quarter_end_equity':end,'quarter_profit':end-5500 if end is not None else None,
         'quarter_return':end/5500-1 if end is not None else None,'quarter_cash':q.get('cash'),'quarter_reserved_exit_fees':q.get('reserved_exit_fees'),
         'quarter_unsettled_cash':q.get('unsettled_cash'),'quarter_dividend_receivable':q.get('dividend_receivable'),
         'quarter_market_value':q.get('market_value'),'quarter_debt':q.get('debt'),'quarter_interest':q.get('interest_total'),
         'quarter_close_rows':len(selected),'quarter_missing_mark_days':sum(e is None for e in equity),
         'quarter_max_drawdown':min(dd) if len(equity)==61 and all(e is not None for e in equity) else None,
         'buy_intents':quarter.get('buy_intents'),'buy_fills':quarter.get('buy_fills'),'sell_fills':quarter.get('sell_fills'),
         'traded_symbols':symbols,'fills_table_status':fills_status,
         'commission_paid_through_quarter':sum(c.get('commission',0) for c in campaigns) if quarter else None,
         'friction_paid_through_quarter':sum(c.get('friction',0) for c in campaigns) if quarter else None,
         'tail_cash':latest.get('cash'),'tail_equity':latest.get('net_equity') if tail_known else None,
         'tail_positions':sorted(final.get('open_positions',{})),'tail_pending_exits':final.get('pending_exit_count'),
         'error_count':final.get('error_count'),'recovery':recovery,
         'one_shared_initial_5500':final.get('portfolio_not_stitched',False),'path':str(path)}
    return row

def preservation_check():
    before=read(ROOT/'PRESERVATION_BEFORE.json');changed=[]
    for p,h in before['files'].items():
        actual=sha(p) if Path(p).is_file() else None
        if actual!=h:changed.append({'path':p,'expected':h,'actual':actual})
    result={'at':utc(),'checked_files':len(before['files']),'changed':changed,'status':'PASS' if not changed else 'CHANGED_REVIEW_REQUIRED',
            'live_ledger_policy':'Old live services may evolve naturally; no rollback or reset performed.'}
    write(ROOT/'PRESERVATION_CHECK.json',result);return result

def report(sample_version,portfolio_version):
    compare=sample_comparison(sample_version)
    root=ROOT/'portfolio'/portfolio_version
    paths=sorted(p for p in root.glob('B12_*') if p.is_dir()) if root.exists() else []
    accounts=[audit_account(p) for p in paths]
    errors=[{'path':str(p),'error':read(p)} for p in root.glob('*_ERROR.json')] if root.exists() else []
    benchmarks=existing(ROOT/'benchmarks/frozen_v1/SUMMARY.json',[])
    for account in accounts:
        for benchmark in benchmarks:
            left=numeric(account.get('quarter_end_equity'));right=numeric(benchmark.get('end_equity_usd'))
            account['relative_'+benchmark['symbol']+'_dollars']=left-right if left is not None and right is not None else None
    capability=read(ROOT/'INPUT_CAPABILITY.json');quality=read(ROOT/'quality/QUALITY_SUMMARY.json')
    q0=read(ROOT/'Q0_REUSE_VERIFICATION.json');preserve=preservation_check()
    equality=existing(ROOT/'engineering/MRVL_REAL_ECONOMIC_EQUIVALENCE.json',{}).get('status','NOT_VERIFIED')
    write(ROOT/'ACCOUNT_COMPARISON.json',accounts)
    pd.DataFrame([{k:json.dumps(v) if isinstance(v,(list,dict)) else v for k,v in r.items()} for r in accounts]).to_csv(ROOT/'ACCOUNT_COMPARISON.csv',index=False,encoding='utf-8-sig')
    reconciliation={'at':utc(),'fixed_original20_complete':compare['all40_completed'],
      'fixed_original20_recovery_pass':compare.get('all40_recovery_pass',False),
      'expected_common_accounts':4,'observed_accounts':len(accounts),'common_accounts_complete':len(accounts)==4 and all(r['actually_completed'] for r in accounts),
      'common_account_errors':errors,'all_account_recovery_pass':len(accounts)==4 and all(r['recovery']=='PASS' for r in accounts),
      'common_capital_not_stitched':len(accounts)==4 and all(r['one_shared_initial_5500'] for r in accounts),
      'all66_retained':capability['all66_retained'],'preservation_status':preserve['status'],
      'synthetic_research_results':False,'parameter_search':False,'full_pool_performance_certified':False}
    write(ROOT/'REPORT_RECONCILIATION.json',reconciliation)
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip()
    branch=subprocess.check_output(['git','branch','--show-current'],cwd=REPO,text=True).strip()
    lines=['# Ben B1.2 实际执行结果','',f'报告生成：{utc()}。仓库 `Ben-he321/premarket-radar`，分支 `{branch}`，当前提交 `{commit}`。',
      f'实际工作目录：`{REPO}`。实际数据及证据目录：`{ROOT}`。','',
      '本轮是报价时窗工程对照和有界的共同资金历史诊断。A/B 分开；所有共同账户保留 COVERAGE_LIMITED，不构成完整全池绩效、独立盈利证明或前向启用许可。','',
      '## 原20样本的报价时窗对照','',
      f'Q0 复用 B1.1 final_v4，不重跑旧结果。Q1 版本 `{sample_version}`，40项 A/B 样本完成状态：{compare["all40_completed"]}。',
      f'40项恢复验证全部通过：{compare.get("all40_recovery_pass", "NOT_VERIFIED")}；缺失或失败的恢复证据不能由完成字段代替。',
      f'上次12项固定点报价阻断中：{compare["blockers_window_has_quotes"]}项窗口内有报价，{compare["blockers_window_has_no_quotes"]}项整个窗口没有报价；实际通过全部冻结条件并产生模型成交 {compare["blockers_actual_Q1_model_entries"]}项；窗口有报价但仍未通过全部条件 {compare["blockers_completed_with_quotes_but_no_entry"]}项；尚未完成 {compare["blockers_not_completed"]}项。',
      '“后来有报价”不是“当时能成交”。逐项首报价、失败原因、首次全过条件时间、意图时间及成交量见 ORIGINAL12_FIXED_POINT_BLOCKERS.csv；BE/RIVN 不得提前到16:05发单。',
      'Q1 是 POST_REVIEW_EXECUTION_HYPOTHESIS；独立工程样本各自5500美元，未拼成组合曲线。','',
      '## 共同资金P50','',f'新入场区间固定 {START} 至 {END}，61个纽约交易日。每本只有5500美元、整数股、目标50%净权益、最多两仓。至 {TAIL_END} 的尾段只退出和结算，尚存持仓、应收和未结算款不会假平仓。',
      '主表采用同日供应商收盘字段在账本副本上估值，与SPY/QQQ同口径；执行中的当时买卖价、现金路径及意图均不改动。期末持仓未假设卖出，不重复扣卖出费用。','',
      '| 账户 | 实际处理至 | 季末权益 | 季度净盈亏 | 季度收益 | 最大回撤 | 买/卖成交笔数 | 实际参与股票 |',
      '|---|---|---:|---:|---:|---:|---|---|']
    for r in accounts:
        lines.append(f'| {r["account"]} | {r["processed_through"] or "未完成"} | {usd(r["quarter_end_equity"])} | {usd(r["quarter_profit"])} | {pct(r["quarter_return"])} | {pct(r["quarter_max_drawdown"])} | {r["buy_fills"]}/{r["sell_fills"]} | {", ".join(r["traded_symbols"]) or "无已核实成交"} |')
    if not accounts:lines.append('| 四本共同账户 | NOT_RUN | 未完成 | 未完成 | 未完成 | 未完成 | 未完成 | 不以初始化5500充当回放 |')
    lines+=['','以上收益是该固定季度的累计收益，没有把三个月年化为稳定盈利结论。缺失任何期末价格或未完整走完季度时，权益/回撤保持未知。四本结果不能混合为一条策略曲线。','',
       '| 账户 | 季末现金 | 退出费预留 | 持仓市值 | 未结算 | 分红应收 | 已付佣金 | 模型摩擦 | 尾段现金 | 尾段剩余持仓 |',
       '|---|---:|---:|---:|---:|---:|---:|---:|---:|---|']
    for r in accounts:lines.append(f'| {r["account"]} | {usd(r["quarter_cash"])} | {usd(r["quarter_reserved_exit_fees"])} | {usd(r["quarter_market_value"])} | {usd(r["quarter_unsettled_cash"])} | {usd(r["quarter_dividend_receivable"])} | {usd(r["commission_paid_through_quarter"])} | {usd(r["friction_paid_through_quarter"])} | {usd(r["tail_cash"])} | {", ".join(r["tail_positions"]) or "无／详见完成状态"} |')
    lines+=['','## 同期SPY/QQQ参照','',
      '同为5500美元，首个交易日RTH开盘模型买入，整数股、每实际订单1美元佣金及0.1%摩擦，现金零息。实际付款后下一交易日尝试一次整数股再投资；期末未付款分红列应收。raw账户与all复权总回报参考分开，基金净值已含管理费，不重复扣除；未计税费。','',
      '| 基准 | 状态 | 期末权益 | 净盈亏 | 期末现金 | 期末股数 | 分红应收 |','|---|---|---:|---:|---:|---:|---:|']
    for b in benchmarks:lines.append(f'| {b["symbol"]} | {b["status"]} | {usd(b.get("end_equity_usd"))} | {usd(b.get("net_profit_usd"))} | {usd(b.get("end_cash_usd"))} | {b.get("end_shares")} | {usd(b.get("end_dividend_receivable_usd"))} |')
    if not benchmarks:lines.append('| SPY/QQQ | NOT_RUN／未生成 | 未知 | 未知 | 未知 | 未知 | 未知 |')
    lines+=['','## 数据覆盖与验证边界','',
      f'66候选全部保留，原60 KEEP、5排除、1身份/范围UNKNOWN不变。60只已完成有界输入请求；57只一季度有价格，逐日价格资格记录为{capability["price_qualified_symbol_sessions"]}个证券日，不等于全部通过财报和报价门槛。',
      'QNT/SKHY在请求区间为空；XE只有尾段少量观察价格；INFQ及其他短历史按当时预热是否足够处理。首条可见价格不是已核实上市日期。SPCX尾段改名/缺价不自动补造；ECHO季度查询使用历史SATS身份。',
      '季度B层24只具有有界普通财报链，A层仅六份独立计划证据；其余未知继续禁止入场。WULF4/14初步财务公告已作为B层实际边界。MRNA初步公告链、BMNR等仍未知，普通季度链也未证明穷尽非定期财务公告。',
      'Finnhub精确配置入口仍缺 FINNHUB_API_KEY，接口权限未验证。准确位置为现有 premarket-radar-ai-m1/.streamlit/secrets.toml 顶层；本轮不要求重新填写已有Alpaca密钥，没有购买服务。',
      f'真实输入质量检查覆盖{quality["checked_objects"]}个对象、{quality["rows"]:,}行：重复{quality["duplicates"]}、无效OHLC {quality["invalid_ohlc"]}、缺失成交量{quality["missing_volume"]}。空响应、分钟无成交、停牌与接口失败不互相替代。',
      '排序成交量仅在原已核对竞价量的日期可用；未知不填零，价差完全并列又缺二级排序量时按原内核保留未知。NOW预热期5:1拆股在12/18生效边界换算旧历史单位，未来公司行动不提前作用。',
      '旧历史网络接收时间为UNKNOWN；当前抓取时间和计划保守可得边界单独保存。历史收盘后1分钟定稿只是模型假设，Basic当前实时可得性未验证；历史L1不是排队或真实成交证明。','',
      '## 工程检查、保留和恢复','',
      f'初版完整L1记录触及本机16GB内存边界，因此保留初版并增加纯存储归档与流式导出。MRVL优化前后真实事件等价检查状态：{equality}。准确事件ID/摘要、重复处理和异常中断恢复的测试与失败修复记录见engineering；未获PASS时不得声称真实经济状态等价。',
      f'四本账户完成：{reconciliation["common_accounts_complete"]}；四本恢复校验全通过：{reconciliation["all_account_recovery_pass"]}。旧文件检查：{preserve["status"]}（{preserve["checked_files"]}项）。',
      '运行状态以TASK_STATE.json、samples/WORKER_STATE.json、账户progress.json及PID实际存在为准。恢复使用对应版本checkpoint和同目录replay_archive.sqlite，并核对动态输入哈希；先检查有没有同名进程，不能启动第二份相同账户。完整本地数据库不在验收ZIP内。','',
      '## 结论与下一步','',
      '本轮交付应区分：有真实模型成交的工程回放；全部条件不满足而零成交；数据未知导致无法评价；以及因错误/预算未完成的账户。具体状态和错误不以空表或初始化现金代替。',
      ('固定20样本的报价时窗对照已全部完成。' if compare['all40_completed'] else '固定20样本的报价时窗对照尚未全部完成，不对未完成样本下结论。')+'更多交易不自动意味着更好。共同资金结果仍受财报、范围版本、收盘及竞价量、历史行情可得性等限制，因此不能确认策略具有独立优势，也不能仅凭当前覆盖受限样本断言没有优势。',
      '下一步最少是补齐可靠的历史计划版本、已知初步财务公告链、缺失证券日期及相应交易时点输入；Finnhub如需使用先由Ben配置现有凭证并验证权限，而非购买服务。当前Basic实时执行条件仍不满足验证要求。P100、P200、其余消融和Ben前向均保留待后续，本轮不扩参或自动晋级。']
    if errors:lines+=['','实际运行错误（完整恢复点保留）：']+[f'- {e["error"].get("account")}: {e["error"].get("reason")}' for e in errors]
    (ROOT/'BEN_B1_2_RESULTS.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return reconciliation

def package(sample_version,portfolio_version):
    selected={}
    def add(path,name=None):
        p=Path(path)
        if p.is_file():selected[name or str(p.relative_to(ROOT)).replace('\\','/')]=p
    root_names={'BEN_B1_2_RESULTS.md','ACCOUNT_COMPARISON.json','ACCOUNT_COMPARISON.csv','REPORT_RECONCILIATION.json',
        'B12_PROTOCOL.json','TASK_STATE.json','STAGE_LOG.jsonl','PRESERVATION_BEFORE.json','PRESERVATION_CHECK.json',
        'Q0_REUSE_VERIFICATION.json','INPUT_CAPABILITY.json','INPUT_CAPABILITY_EARNINGS.json','ALL66_INPUT_COVERAGE.csv',
        'ALL66_DAILY_INPUT_QUALIFICATION.csv','ENVIRONMENT_CHECK.json','DELIVERY_CODE.json','SOURCE_MANIFEST.json','RESUME.md','RULE_DIFFERENCES.md','RESOURCE_BUDGET.md'}
    for name in root_names:add(ROOT/name)
    engineering_names={'ENGINE_INTEGRATION.json','ENGINE_INTEGRATION.md','B12_ENGINE_INTEGRATION.json','BENCHMARK_IMPLEMENTATION_CHECK.json',
        'CHECKOUT_LINE_ENDING_RESTORATION.json','Q0_REUSE_FIRST_CHECK_LINE_ENDINGS.json','MRVL_INPUT_PRESERVATION_BEFORE_COMPACT.json',
        'MRVL_PRE_COMPACT_ECONOMIC_BASELINE.json','MRVL_COMPACT_EQUIVALENCE.json','MRVL_COMPACT_EQUIVALENCE.md','MRVL_REAL_ECONOMIC_EQUIVALENCE.json',
        'CORRECTION_REASON.json','ALL66_DAILY_INPUT_QUALIFICATION.csv','ALL66_INPUT_COVERAGE.csv','INPUT_CAPABILITY.json',
        'compare_mrvl_compact.py','verify_q0_reuse.py','DELIVERY_VALIDATION.json','FINAL_ENGINEERING_SUMMARY.json'}
    for stem in ['all_b1_b12','all_b1_b12_compact','b11_regression','compact_initial','compact_expanded','compact_final',
        'data_initial','portfolio_initial','q1_initial','q1_expanded','samples_initial','pytest_benchmarks','pytest_earnings','pytest_delivery','final_regression',
        'shared_cash_mark_boundary_initial','shared_cash_boundary_final','final_recovery_boundary_initial','final_recovery_boundary_final','day_progress_boundary_final',
        'final_regression_before_delivery_fixture_review']:
        engineering_names.update({stem+'.log',stem+'.xml'})
    zones={'engineering':engineering_names,
       'quality':{'HISTORICAL_QUERY_MAPPING.csv','INPUT_HASHES.json','MARKET_OBJECT_QUALITY.csv','NOW_SPLIT_UNIT_REVIEW.json','QUALITY_SUMMARY.json'},
       'earnings':{'BUILD_PROGRESS_V2.json','BUILD_PROGRESS.json','COVERAGE_EXPORT_V2.json','coverage_export.log','CREDENTIAL_RECHECK_REDACTED.json',
                  'EARNINGS_ALL66_COVERAGE_V2.csv','EARNINGS_DAILY_COVERAGE_V2.csv','EARNINGS_EVIDENCE_V2.md',
                  'PAIR_SEGMENTS_B12_V1.json','PAIR_SEGMENTS_B12_V2.json','PRIMARY_EARNINGS_B12_V1.json','PRIMARY_EARNINGS_B12_V2.json'}}
    for folder,names in zones.items():
        for p in (ROOT/folder).rglob('*'):
            if p.name in names and not any(x in p.parts for x in ['private_source_pages','source_pages','private_http_cache']):add(p)
    run_names={'summary.json','FINAL_ACCOUNT.json','QUARTER_END_ACCOUNT.json','QUARTER_END_LEDGER.json','QUARTER_END_CLOSE_VALUATION.json',
       'CLOSE_VALUATIONS.json','continuous_close_equity.csv','RUN_SPEC.json','RUN_SUMMARY.json','INPUT_HASHES.json','DYNAMIC_INPUT_HASHES.json',
       'QUOTE_COVERAGE.json','COMMON_CLOCK.json','progress.json','RECOVERY_IDEMPOTENCY.json','ARCHIVE_MANIFEST.json','WINDOW_QUOTE_COVERAGE.json',
       'HOLDING_QUOTE_REQUESTS.json','orders.csv','fills.csv','campaigns.csv','daily_equity.csv','coverage_funnel.csv','event_trace.csv',
       'quote_inventory.csv','account_events.csv','data_gaps.csv','q1_quote_evaluations.csv','ENGINE_ERRORS.json','runner.stdout.log','runner.stderr.log',
       'Q0_Q1_ORIGINAL20_COMPARISON.csv','ORIGINAL12_FIXED_POINT_BLOCKERS.csv','COMPARISON_SUMMARY.json',
       'Q0_A_ERROR.json','Q1_A_ERROR.json','Q0_B_ERROR.json','Q1_B_ERROR.json'}
    for prefix in [ROOT/'samples'/sample_version,ROOT/'portfolio'/portfolio_version]:
        for p in prefix.rglob('*'):
            if p.name in run_names:add(p)
    benchmark_names={'SUMMARY.json','ACCOUNT.json','daily.csv','fills.csv','dividends.csv','cashflows.csv','order_decisions.csv',
        'RUN_SPEC.json','INPUT_MANIFEST.json','RTH_OPEN_VERIFICATION.json','RUN_STATE.json','MARKET_STATE_REDACTED.json','BENCHMARKS.md','COMPLETE.json'}
    for p in (ROOT/'benchmarks/frozen_v1').rglob('*'):
        if p.name in benchmark_names and 'inputs' not in p.relative_to(ROOT/'benchmarks/frozen_v1').parts:add(p)
    for p in (ROOT/'samples/initial_v1').rglob('*'):
        if p.name in {'PRESERVED_FOR_RESOURCE_OPT.json','summary.json','RECOVERY_IDEMPOTENCY.json','progress.json','RUN_SPEC.json'}:add(p)
    for p in (ROOT/'samples/compact_equivalence_v1').rglob('*'):
        if p.name in {'summary.json','RECOVERY_IDEMPOTENCY.json','ARCHIVE_MANIFEST.json','RUN_SPEC.json'}:add(p)
    for name in ['ACQUISITION_SUMMARY.json','ALL66_SCOPE_VERSION.csv','HISTORY_INPUTS.json','HISTORY_REQUEST_FREEZE.json','WORKER_STATE.json']:
        add(ROOT/'data'/name)
    for p in (REPO/'src/ben_b1_2').glob('*.py'):add(p,'source/src/ben_b1_2/'+p.name)
    for p in (REPO/'tests').glob('test_ben_b1_2*.py'):add(p,'source/tests/'+p.name)
    for p in (REPO/'docs/ben_b1_2').glob('*'):
        if p.suffix in {'.md','.txt','.json'}:add(p,'source/docs/ben_b1_2/'+p.name)
    for name in ['replay.py','ledger.py','rules.py','events.py','b11_research.py','b11_data.py','data_probe.py','b11_runtime.py']:
        add(REPO/'src/ben_b1'/name,'source/src/ben_b1/'+name)
    add(REPO/'requirements.txt','source/requirements.txt')
    for p in (B11/'replay/final_v4').rglob('*'):
        if p.name in {'summary.json','coverage_funnel.csv','orders.csv','fills.csv','campaigns.csv','RUN_SPEC.json','RECOVERY_IDEMPOTENCY.json'}:
            add(p,'existing_q0/'+str(p.relative_to(B11/'replay/final_v4')).replace('\\','/'))
    config=OLD_REPO/'.streamlit/secrets.toml';secret_values=[]
    if config.exists():
        values=tomllib.loads(config.read_text(encoding='utf-8-sig'))
        secret_values=[str(values[k]).encode() for k in ['ALPACA_API_KEY','ALPACA_SECRET_KEY','FINNHUB_API_KEY'] if values.get(k) and len(str(values[k]))>=12]
    manifest=[]
    for name,p in sorted(selected.items()):
        if any(x in name.lower() for x in ['secrets.toml','.env','private_source_pages','.sqlite','.parquet','checkpoint.json']):raise ValueError('BUNDLE_FORBIDDEN_PATH:'+name)
        content=p.read_bytes()
        if any(v in content for v in secret_values):raise ValueError('BUNDLE_SECRET_VALUE_DETECTED_IN:'+name)
        manifest.append({'name':name,'bytes':len(content),'sha256':sha(p)})
    write(ROOT/'BUNDLE_MANIFEST.json',{'at':utc(),'whitelist_files':manifest,'excluded':['credentials','raw market library','full database','full checkpoint','personal files']})
    dest=ROOT/ZIP_NAME
    with zipfile.ZipFile(dest,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name,p in sorted(selected.items()):z.write(p,name)
        z.write(ROOT/'BUNDLE_MANIFEST.json','BUNDLE_MANIFEST.json')
    with zipfile.ZipFile(dest) as z:
        bad=z.testzip()
        if bad:raise ValueError('ZIP_CORRUPT_ENTRY:'+bad)
        names=z.namelist()
        if len(names)!=len(set(names)):raise ValueError('ZIP_DUPLICATE_NAME')
        if 'BEN_B1_2_RESULTS.md' not in names:raise ValueError('ZIP_REPORT_MISSING')
    result={'at':utc(),'zip':str(dest),'bytes':dest.stat().st_size,'sha256':sha(dest),'entries':len(names),'testzip':'PASS',
            'actual_credential_scan':'PASS_NO_VALUES_FOUND','raw_library_or_full_db_included':False}
    write(ROOT/'DELIVERY.json',result)
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--sample-version',required=True);p.add_argument('--portfolio-version',required=True);p.add_argument('--package',action='store_true')
    a=p.parse_args();print(json.dumps(report(a.sample_version,a.portfolio_version)))
    if a.package:print(json.dumps(package(a.sample_version,a.portfolio_version)))
