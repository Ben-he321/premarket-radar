"""Whitelist reports and selected account evidence, never databases or secrets."""
import json,sqlite3,zipfile,subprocess
from pathlib import Path
import pandas as pd
from .runtime import *
from .forward_protocol import BOOKS,freeze

ROOT_FILES=['V1_3_1_RESULTS.md','corpora_action_identity_review.csv','security_identity_registry.json','complex_action_calendar.json',
 'missing_held_paths.csv','missing_quote_review.csv','missing_quote_receipt.json','original_top10_frozen.csv','original_top10_freeze_receipt.json',
 'important_trade_review.csv','important_trade_price_evidence.csv','fixed_intent_manifest.json','RESTATEMENT_PROTOCOL.json',
 'fixed_intent_account_differences.csv','account_summary.csv','account_summaries.json','primary_nine_tests.csv','primary_nine_tests.json',
 'activity_matching_errors.csv','concentration.csv','cost_exposure_differences.csv','incomplete_and_limits.json','data_coverage.json',
 'restatement_engineering.json','engineering_checks.json','engineering_tests.xml','engineering_tests.log','FORWARD_PROTOCOL.json',
 'forward_engineering_gate.json','forward_status.json','forward_input_probe.json','latest_input_status.json','latest_action_status.json',
 'preservation_check.json','old_ledger_prefix.json','prior_result_hashes.json','takeover.json','RUN_BUDGET.json',
 'implementation_correction_receipt.json','identity_dependency_receipt.json','delivery_source.json','background_installation.json',
 'core_cost_comparison.csv','old_new_primary_comparison.csv','unresolved_items.json','forward_events_snapshot.json','TASK_STATE.json']

def table(frame):
    def cell(x):return f'{x:.8g}' if isinstance(x,float) else str(x).replace('|','/')
    rows=[list(frame.columns),['---']*len(frame.columns),*frame.itertuples(index=False,name=None)]
    return '\n'.join('| '+' | '.join(cell(x) for x in row)+' |' for row in rows)

def run():
    base=root();old={x['id']:x for x in read(prior()/'account_summaries.json')};new={x['id']:x for x in read(base/'account_summaries.json')}
    primary=read(base/'primary_nine_tests.json');oldp=read(prior()/'primary_nine_tests.json');diff=pd.read_csv(base/'fixed_intent_account_differences.csv')
    core=[]
    for name in ['M20','U']:
        for cost in ['base','stress25','double_commission']:
            key=f'{name}-H20-{cost}';a=old[key];b=new[key]
            core.append({'account_id':key,'old_net':a['net_pnl_usd'],'restated_net':b['net_pnl_usd'],'delta_net':b['net_pnl_usd']-a['net_pnl_usd'],
                         **{k:b[k] for k in ['ending_equity','max_drawdown','completed_trades','total_cost_usd','commissions','friction_usd','minimum_cash','minimum_available_cash']}})
    pd.DataFrame(core).to_csv(base/'core_cost_comparison.csv',index=False)
    comparison=[]
    from src.v13.statistics import by_adjust
    oldq=by_adjust([x['p'] for x in oldp])
    for a,b,q in zip(oldp,primary,oldq):
        assert (a['condition'],a['horizon'])==(b['condition'],b['horizon'])
        comparison.append({'condition':b['condition'],'horizon':b['horizon'],'old_raw_p':a['p'],'old_raw_q_by':float(q),'old_guard_p':a['primary_p'],'old_guard_q_by':a['q_by'],
                           'new_raw_p':b['p'],'new_raw_q_by':b['raw_q_by'],'new_guard_p':b['primary_p'],'new_guard_q_by':b['q_by'],'new_pnl':b['net_pnl_usd'],'new_mean_daily_increment_vs_random':b['mean'],'random_accounts':b['random_seed_count']})
    pd.DataFrame(comparison).to_csv(base/'old_new_primary_comparison.csv',index=False)
    forward=read(base/'forward_status.json',{});events={}
    for name in BOOKS:
        path=base/'forward'/name/'ledger.sqlite'
        if path.exists():
            with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as c:
                events[name]={table:[json.loads(x[0]) for x in c.execute(f'SELECT payload FROM {table}')] for table in ['intents','events']}
    write(base/'forward_events_snapshot.json',{'at':utc(),'books':events,'source':'READ_ONLY_CURRENT_SQLITE_REPORT_NOT_DATABASE_EXPORT'})
    keep=preservation();engineering=read(base/'engineering_checks.json',{})
    unresolved={'at':utc(),'no_alpha_claim':True,'historical_data_reused':True,'raw_daily_fills':'Daily open/stop/H-close proxies; intrabar fills/order sequence NOT independently verified',
                'missing_held_accounts':[x['id'] for x in new.values() if x['missing_held_bar_sessions']],
                'complex_action_accounts':[x['id'] for x in new.values() if x['status']!='COMPUTED'],
                'open_at_historical_cutoff':[x['id'] for x in new.values() if x['open_positions']],
                'current_ineligible':[x for x in read(base/'latest_input_status.json')['coverage'] if x['state']!='QUALIFIED'],
                'corporate_actions':'Three requested events resolved, not a complete independent review of every security/action; mismatched IDs without a verified different issuer block',
                'current_exchange_identity':'Current name and listing match; many stable IDs and historical point-in-time identity remain UNKNOWN',
                'forward':'Natural buy-hold-exit-settlement cycle and 60 observation sessions pending; mock fixtures excluded',
                'initial_forward_input_cache':'First probe uses pinned V1 and existing V1.1 cache plus missing daily requests; per-request receipts for that initial reuse are not fully recorded',
                'execution_environment':'Local Windows needs power/network and signed-in user for login task; not cloud service'}
    write(base/'unresolved_items.json',unresolved)
    a=old['M20-H20-base'];b=new['M20-H20-base'];m=next(x for x in primary if x['condition']=='M20' and x['horizon']==20)
    inputs=read(base/'forward_input_probe.json');correction=read(base/'implementation_correction_receipt.json',{})
    lines=['# V1.3.1 证据修复与冻结前向研究',f'生成：{utc()}。66 个候选保留。数据与报告：`{base}`。页面：http://localhost:8513/ ，「证据修复与前向对照」。',
           '\n## 三套独立口径',f'- **原报告** M20/H20：净 {a["net_pnl_usd"]:.2f} USD，期末 {a["ending_equity"]:.2f}，{a["completed_trades"]} 笔。原始 p 约 0.06209、原始 BY q 约 0.52698；数据守门 p=1。原文件全部保留。',
           f'- **固定意图工程重述** M20/H20：净 {b["net_pnl_usd"]:.2f} USD，期末 {b["ending_equity"]:.2f}，{b["completed_trades"]} 笔，最大回撤 {abs(b["max_drawdown"]):.2%}。原始 p={m["p"]:.8f}，原始 BY q={m["raw_q_by"]:.8f}；守门 p={m["primary_p"]:.8f}，守门 BY q={m["q_by"]:.8f}。这不是新的盲测或业绩改善证明。',
           f'- **新自然前向**：状态 `{forward.get("service","NOT_STARTED")}`。实际启动 `{forward.get("actual_started_at","NOT_STARTED")}`；{json.dumps({k:{x:v[x] for x in ["cash","reserved_cash","available_cash","equity","immutable_intents","buy_fills","sell_fills","settlement_events"]} for k,v in forward.get("books",{}).items()},ensure_ascii=False)}。仅启动后真实时间记录计入；两个 5500 美元账本不得相加为用户本金。',
           '\n## 核验与修复',
           '- META：相同 CUSIP 的 FB→META 代码变更。ECHO：2021 年现金并购属于 CUSIP 27875T101 的 Echo Global Logistics；当前 EchoStar/SATS 为 278768106，标记 NOT_APPLICABLE_OTHER_SECURITY，不对目标仓位加现金或注销股份。',
           '- RKLB：法律重组 2025-05-23，市场切换 2025-05-27；773122106→773121108，普通股一比一转换，经济权益连续。不是将所有更名事件一概忽略。原始字段、日期及官方链接见 corpora_action_identity_review.csv。',
           '- 原 26 条缺价路径去重为 CLSK / 2024-11-08 一条证券日期。定向 SIP 查询无合格成交量，SEC 8-K 证明停牌区间。未填价、未提前删除入场、未删除随机账户；主检验仍保留缺价守门。',
           '- 原最大 5 盈利、5 亏损样本已按旧文件冻结；原始/复权日线、信号日期、成交量、费用及相邻拆并股逐项留证。所有 10 笔日线价格费用核对一致；盘中实际成交及止损先后仍仅为代理，不能称真实成交核验。',
           '- 共用 V1.3.1 内核预留持仓退出费、未成交买入与退出预算、负净回收的待结算负债；预留不是资产，也不提前收费。仍按原 T+1 净额结算，买卖费用各一次，股息应收不可支用。',
           f'- {len(new)} 个最终账户使用原 486 意图哈希，9 个检验、50 种子、10000 次及 seed 131729、原成本情景均固定。零透支检查通过；{int((diff.cash_changed_sessions>0).sum())} 个账户现金路径变化。完整股数和后续路径差异见 fixed_intent_account_differences.csv。',
           f'- 首次工程输出因重复取整缺陷作废，保留在 `{correction.get("retained_path")}`；不是统计筛选。修复后增加现金不受限时必须与旧内核同股数的回归，再用同一意图做唯一有效重述；作废结果不参与选优或前向账本。',
           '\n## 成本与统计',table(pd.DataFrame(core)[['account_id','old_net','restated_net','delta_net','total_cost_usd','completed_trades']]),
           '\n'+table(pd.DataFrame(comparison)[['condition','horizon','new_raw_p','new_raw_q_by','new_guard_p','new_guard_q_by']]),
           f'固定主检验正向通过数：{sum(bool(x["positive_increment_exploratory"]) for x in primary)}/9。不得自动晋级；不能把负向显著差异当成盈利优势。',
           '\n## 工程检查与保全',f'测试：{engineering}。旧静态文件检查 {keep["static_files_checked"]} 个，{keep["status"]}；旧意图/事件前缀均保留，原服务自然新增：{keep["natural_counts"]}。旧账本的状态允许随真实时间变化，未将整个活动数据库假称不变。',
           '\n## 前向运行与恢复',f'真实历史输入覆盖 {inputs["coverage"]} 个，当前合格 {inputs["qualified"]} 个；个别资格/身份限制见 unresolved_items.json。API 最新 SIP 权限不是历史分钟代理的前提，不切换 IEX/Futu/yfinance。',
           f'Windows 任务名 `BenAITrading-EvidenceForward-V1_3_1`；管理进程 PID `{forward.get("pid")}`；心跳 `{forward.get("heartbeat")}`；下次决策 `{forward.get("next_decision")}`；预计观察结束 `{freeze()["scheduled_observation_end"]}`。单进程管理两本、共享行情缓存和全机同库 120 次/分钟限速。页面关闭不影响后台；电脑休眠/断网会中断，登录后可恢复。',
           f'停止：在 `{base / "forward/STOP"}` 创建空文件。恢复：仅移除该 STOP 文件后运行仓库 `.venv/Scripts/pythonw.exe -m src.v131 paper-service`，或启动上述 Windows 计划任务。已有意图与账本不删除；不补录错过时段的新意图。',
           '自然买入→持仓→退出→结算尚待发生，60 个交易日观察尚未完成。等待期不构成策略有效证明。运行错误与重试保存在 forward/errors、forward/decisions、forward_status.json。',
           '\n## 未解决限制',json.dumps(unresolved,ensure_ascii=False,indent=2),
           '\n验收包只含白名单报告、选定对照账户、测试、事件摘要和哈希；不含密钥、完整数据库、完整行情库。付费模型调用与购买服务均为 0。']
    (base/'V1_3_1_RESULTS.md').write_text('\n\n'.join(lines),encoding='utf-8')
    state('DELIVERED_REAL_RESTATEMENT_FORWARD_RUNNING' if forward.get('service')=='RUNNING' else 'DELIVERED_WITH_FORWARD_GATE_BLOCK',accounts=486,forward_pid=forward.get('pid'))
    paths=[base/name for name in ROOT_FILES if (base/name).exists()]
    for x in core:
        for suffix in ['equity','trades']:paths.append(base/f"accounts/{x['account_id']}-{suffix}.csv")
    for name in ['forward/activation.json']:
        if (base/name).exists():paths.append(base/name)
    missing=[name for name in ROOT_FILES if not (base/name).exists()]
    manifest={'at':utc(),'files':{p.relative_to(base).as_posix():sha(p) for p in paths},'optional_missing':missing,'excluded':'secrets, full database, raw data store, rejected-run bulk outputs, full 486 trade histories (hash references supplied)'}
    write(base/'bundle_manifest.json',manifest);paths.append(base/'bundle_manifest.json')
    from src.data.alpaca_config import load_config
    cfg=load_config();secret_values=[x.encode() for x in [cfg.api_key,cfg.secret_key] if x]
    for p in paths:
        content=p.read_bytes()
        if any(value in content for value in secret_values):raise ValueError('SECRET_VALUE_FOUND_ABORT_EXPORT')
        if p.suffix.lower() not in ['.json','.csv','.md','.xml','.log']:raise ValueError('NON_WHITELIST_FILE')
    output=base/'verification_v1_3_1_evidence_forward_bundle.zip'
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in paths:z.write(p,p.relative_to(base).as_posix())
    with zipfile.ZipFile(output) as z:
        assert z.testzip() is None
        assert all(__import__('hashlib').sha256(z.read(k)).hexdigest()==h for k,h in manifest['files'].items())
    write(base/'bundle_receipt.json',{'at':utc(),'path':str(output),'size':output.stat().st_size,'sha256':sha(output),'entries':len(paths),'crc_and_all_member_hashes':'PASS','actual_secret_value_scan':'PASS'})
    print(read(base/'bundle_receipt.json'),flush=True)
