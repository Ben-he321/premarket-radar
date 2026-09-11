"""Chinese interpretation, fresh read-only forward health and report-only archive."""
from datetime import datetime,timezone
from pathlib import Path
import hashlib
import json
import sqlite3
import subprocess
import zipfile
import pandas as pd
from .runtime import root,v11root,read,write,utc,freeze,tags,LABEL

def forward_health():
    old=v11root();status=read(old/'experimental_paper_status.json',{});pid=status.get('pid')
    command=f'Get-CimInstance Win32_Process -Filter "ProcessId = {int(pid or 0)}" | Select-Object ProcessId,CommandLine,@{{Name="StartedAt";Expression={{$_.CreationDate.ToUniversalTime().ToString("o")}}}} | ConvertTo-Json -Compress'
    result=subprocess.run(['powershell','-NoProfile','-Command',command],capture_output=True,text=True,timeout=30)
    proc=json.loads(result.stdout) if result.returncode==0 and result.stdout.strip() else {}
    live=bool(proc.get('ProcessId'))
    heartbeat=status.get('heartbeat');age=(datetime.now(timezone.utc)-datetime.fromisoformat(heartbeat)).total_seconds() if heartbeat else None
    db=old/'experimental_paper/ledger.sqlite'
    with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as c:
        payload=c.execute('select payload from state where id=1').fetchone()[0];ledger=json.loads(payload)
        events=[json.loads(x[0]) for x in c.execute('select payload from events')]
        intents=[json.loads(x[0]) for x in c.execute('select payload from intents')]
    before=read(root()/'ledger_before_migration.json',{})
    health={'checked_at':utc(),'live_process':live,'pid':pid,'command':proc.get('CommandLine'),
            'process_started_at':proc.get('StartedAt'),
            'heartbeat':heartbeat,'heartbeat_age_seconds':age,'fresh':live and age is not None and age<90,
            'cash':ledger['cash'],'equity_reported':status.get('equity'),'positions':ledger['positions'],
            'intents':len(intents),'buys':sum(x.get('type')=='BUY' for x in events),'sells':sum(x.get('type')=='SELL' for x in events),
            'settlements':sum(x.get('type')=='CASH_SETTLEMENT' for x in events),'unsettled':ledger['unsettled'],
            'data_cutoff':read(old/'forward_input_status.json',{}).get('cutoff'),'next_decision':status.get('next_decision'),
            'expires_at':status.get('expires_at'),'lock':str(old/'experimental_paper/service.lock'),
            'status':status.get('status'),'strategy':'FIXED_LEGACY_UNCHANGED_NO_MOMENTUM_FILTER',
            'logical_ledger_same_as_before_fix':hashlib.sha256(payload.encode()).hexdigest()==before.get('state_sha256'),
            'original_intents_preserved':len(intents)>=before.get('intents',0),'error':status.get('error'),
            'decision_progress_files':[p.name for p in (old/'experimental_paper').glob('decision-progress-*.json')]}
    write(root()/'forward_health.json',health);return health

def build():
    r=root();p=freeze();health=forward_health()
    f=pd.read_csv(r/'momentum_per_symbol.csv');accounts=pd.read_csv(r/'momentum_filter_accounts.csv');coverage=read(r/'data_coverage.json')
    assert len(f)==990 and f.symbol.nunique()==66 and len(accounts)==12
    before=read(r/'prior_result_hashes.json');changed=[name for name,h in before['files'].items() if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=h]
    write(r/'prior_results_preserved.json',{'checked_at':utc(),'files':len(before['files']),'changed':changed})
    insufficient=f[f.status=='INSUFFICIENT_EVIDENCE'];passed=f[f.fdr_evidence==True]
    rows=['# V1.2 独立动量研究：实际结果','',
      f'实验：{p["experiment_id"]}。协议冻结于 {p["frozen_at"]}，哈希 `{p["protocol_hash"]}`。',
      f'证据等级：**{LABEL}**。开发数据最晚 2026-03-10；没有计算旧保留期绩效、重新选择或晋级策略。',
      '', '本轮结论', '',
      f'已实际完成全部 66 个候选 × 5 个因子 × 3 个持有标签，共 **{len(f)} 项组合**；其中 {len(f)-len(insufficient)} 项具备本协议要求的描述统计/块区间样本，{len(insufficient)} 项证据不足。',
      f'对单股时序 IC 与强弱档差合计 1980 项检验，采用 BY 方法控制依赖下的多重比较。**{len(passed)} 项组合通过 5% FDR 阈值**。未通过不等于证明无效；500 次重采样的最小 p 值约 0.002，叠加 1980 项比较后，发现稀疏小效应的能力有限。',
      '', '因子描述线索（中位数跨证券与持有期，仅用于解释，不是新的选股标准）',
      '', '| 因子 | 时序 Rank IC 中位数 | 强档减弱档收益中位数 |', '|---|---:|---:|']
    for factor,g in f.groupby('factor'):
        rows.append(f'| {factor} | {g.time_series_rank_ic.median():.4f} | {g.strong_minus_weak.median():.3%} |')
    rows+=['', '短期 1/5 日的负 IC 可作为反转描述线索；没有事后翻转信号或增加反向交易版本。20/60 日和相对 SPY 分别报告。任何单个显眼股票或未调整显著值都不作为已证实优势。',
      '', '统一账户成本后对照（各自 5500 美元，全部合格股票竞争同一资金）', '',
      '| 版本 | 成本 | 净盈亏 USD | 收益 | 完成交易 | 平均资金使用率 | 总佣金与摩擦 USD |', '|---|---|---:|---:|---:|---:|---:|']
    for _,x in accounts.iterrows():rows.append(f'| {x.version} | {x.cost_case} | {x.net_pnl_usd:.2f} | {x["return"]:.2%} | {int(x.completed_trades)} | {x.capital_utilization:.2%} | {x.total_cost_usd:.2f} |')
    rows+=['', 'R0 为原 FIXED_LEGACY；R1/R2/R3 仅分别加入 return_20>0、return_60>0、relative20>0。没有第五个版本，没有更改风险、仓位、费用、止损或持有期。',
      '四个版本在基准、25bp 压力和双佣金情景下均为亏损。过滤器的亏损减少同时伴随更少交易、更低资金使用和更少费用，不能直接叫作额外 Alpha，也不能据此认定动量整体无效。相对 R0 的盈亏、交易数、曝险和回撤差异逐项保存在账户表。',
      '', '计算与时间口径', '',
      '所有证券使用同一 504 个交易日训练、126 日后续窗口，训练与后续之间隔离 20 日。仅训练段估计三档阈值；任何标签必须在所属窗口内成熟。最后不完整窗口也列出，样本不足不补造。',
      '标签从信号日下一交易日开盘到第 H 日收盘，不是当日开盘买入。单股时序 Rank IC 比较同一股票不同日期；不是同日全池横截面 IC。',
      '均值区间采用 20 日历交易日块、500 次固定种子重采样；同一日历窗口的所有证券共用日期块抽样。IC 区间在固定经验秩上作日期块重采样。重叠标签不当作独立交易，另报非重叠事件数与日期块数。',
      '共享表先在同日、同档内等权汇总证券，日期整体重采样；其 ALL 对照是可用日期/档均值的平均，不能替代个股同日无条件对照，也不提供横截面 IC。DXYZ 只保留在单独 CEF 研究中，不进入普通股票汇总或账户。',
      '统计标签使用一致的 all 调整价格。当前调整快照不是历史时点版本，存在现存股票池选择/幸存者偏差。原始/all 日期不一致或同日 OHLC 调整比例偏差超过预冻结阈值的记录被隔离。股息和拆股由原价账户显式处理，不重复加到 all 标签里。',
      '账户使用 V1.1 同一内核和原价数据。日线价格是回放代理，不保证实际可成交。无法退出的缺价持仓保留并标记；收盘时点无持仓不意味着日内无资金占用。资金使用率采用收盘市值/权益，因此低估日内占用，另给完成交易和费用帮助解释。',
      '交易导出包含 price_net_pnl、dividend_entitlement、total_net_pnl、entry_cost、return_net。价格净额扣佣金/摩擦；总净额加股息应收；收益率分母含入场费用。现金结算、尚未支付股息和未平仓市值是账户层项目。',
      '', '证据不足和数据状态', '',
      '下列证券至少有一项组合不足，详细原因、每项有效事件数和区间均保留，不缩减股票池：'+', '.join(sorted(insufficient.symbol.unique()))+'。',
      '没有开发期数据：'+(', '.join(x['symbol'] for x in coverage if not x['rows']) or '无')+'。参考数据缺失时相对标签明确缺失，不填零。',
      '', '两项运行边界与原实验账户', '',
      'A：已有持仓的公司行动 BLOCK 只跳过该证券；隔离双持仓测试确认另一证券仍能止损退出。B：按证券保存无信号/有信号完成、待重试、永久阻断、重试耗尽及错过窗口；最多三次尝试，间隔至少一分钟，成功证券不重复，晚到数据不补写意图。',
      f'新鲜检查 {health["checked_at"]}：PID {health["pid"]}，心跳 {health["heartbeat"]}，fresh={health["fresh"]}；现金 {health["cash"]:.2f}，意图 {health["intents"]}，买 {health["buys"]}、卖 {health["sells"]}、结算 {health["settlements"]}。行情截止日 {health["data_cutoff"]}。',
      f'逻辑账本与修复前一致：{health["logical_ledger_same_as_before_fix"]}；保持原账户期限和 FIXED_LEGACY，不接收本研究的 R1–R3。旧结果 {len(before["files"])} 个文件哈希核对，变动 {len(changed)}。',
      '', '实际执行与限制', '',
      'tests.log 和 boundary_regression_tests.json 是真实执行的工程测试记录，使用隔离 fixture；研究表来自真实缓存行情。0 付费模型调用、0 新订阅；没有重新下载全历史。',
      '本轮历史研究完成，不启动动量前向账户。仍需协议冻结之后的新前向证据、独立数据版本与更充分样本才能判断可复现优势。持续优化、自动冠军和价值/质量/增长/情绪因子均未实现。',
      '', '入口与恢复', '',
      '页面：http://localhost:8513 → 动量研究。独立输出目录：'+str(r)+'。',
      '恢复命令：在项目目录使用 `.venv\\Scripts\\python.exe -m src.v12 run`；已完成缓存按协议复用，单实例锁防止重复。超过冻结的八小时上限会停止，不自动延长或修改协议。',
      '研究参数改变必须另立实验，不得编辑当前协议或删除结果后以相同实验名重跑。']
    (r/'V1_2_RESULTS.md').write_text('\n'.join(rows),encoding='utf-8')
    usage=['# 因子使用与检验边界','', '五个因子的公式、回看周期、输入价格基准见 MOMENTUM_PROTOCOL.json；来源是本地 src/watchlist/features.py 的既有因果计算。',
      '', '| 因子 | 本轮独立研究 | 本轮成本回放 | 原前向实验账户 |','|---|---|---|---|',
      '| return_1 | 5/10/20日标签的时序 IC、训练三档及后续验证 | 无新增过滤器；R0 原规则已有收盘高于前收盘的条件 | 仅原条件，未加新因子 |',
      '| return_5 | 同上 | 未加入 | 未加入 |','| return_20 | 同上 | R1 固定正值过滤 | 未加入 |',
      '| return_60 | 同上 | R2 固定正值过滤 | 未加入 |','| relative20 | 同上，相对SPY | R3 固定正值过滤 | 未加入 |',
      '', '源码调用链：v12.data.frame → 既有 feature_frame；v12.research → 训练分位、后续固定档及日期块统计；v12.accounts → 既有 signal 与 v11.kernel.replay。',
      '检验执行不等于有效性通过。本轮没有通过预冻结多重比较的组合，四个成本对照也没有正净收益；不把描述性正 IC 称为已验证因子。',
      '因果与账本工程测试覆盖下一日开盘、标签未成熟、缺失行情、参考缺失、块重采样固定性、价格/股息对账、边界 A/B 和原 V1.1 回归。日志见 tests.log。',
      '三个 QuantSkills 本轮未安装或运行完整上游算法：factor-factory 仍只是既有目录/结构参考，factor-orthogonalize 未执行完整正交化，backtest-overfit 的旧 DSR/PBO 证据属于 V1，本轮没有冒称它生成了动量结论。本轮是五个明确公式的本地检验。',
      '价值、质量、增长、情绪及自动月度改进：NOT_IMPLEMENTED。']
    (r/'factor_usage_and_tests.md').write_text('\n'.join(usage),encoding='utf-8')
    files=['V1_2_RESULTS.md','MOMENTUM_PROTOCOL.json','momentum_per_symbol.csv','momentum_per_symbol.json','momentum_window_results.csv',
           'momentum_shared_summary.csv','momentum_filter_accounts.csv','momentum_filter_accounts.json','factor_usage_and_tests.md',
           'forward_health.json','boundary_regression_tests.json','tests.log','data_coverage.json','account_data_issues.json',
           'prior_results_preserved.json','ledger_before_migration.json','implementation_notes.json','result_checks.json','TASK_STATE.json',
           'COMPUTATION_RECEIPT.json','research.stdout.log']
    if (r/'source_equivalence.json').exists():files.append('source_equivalence.json')
    files += [x.relative_to(r).as_posix() for x in (r/'accounts').glob('*-trades.csv')]
    paths={name:(r/name).read_bytes() for name in files}
    code=Path(__file__).resolve().parents[2]
    receipt=read(r/'COMPUTATION_RECEIPT.json')
    manifest={'created_at':utc(),'metadata':tags(),'frozen_at':p['frozen_at'],'research_completion':receipt,
              'current_source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=code,text=True).strip(),
              'frozen_source_commit':p['source_commit'],'protocol_hash':p['protocol_hash'],'data_version':p['data_version'],
              'comparisons':len(f),'filter_accounts':len(accounts),'errors':read(r/'last_error.json'),
              'prior_results_changed':changed,'files':{n:hashlib.sha256(b).hexdigest() for n,b in paths.items()},
              'omitted':'credentials, full raw/all bars, conditional event cache, full databases, private ledger backup',
              'research_elapsed_seconds':(datetime.fromisoformat(receipt['at'])-datetime.fromisoformat(p['frozen_at'])).total_seconds(),
              'elapsed_seconds_since_freeze':(datetime.now(timezone.utc)-datetime.fromisoformat(p['frozen_at'])).total_seconds()}
    write(r/'experiment_manifest.json',manifest);paths['experiment_manifest.json']=(r/'experiment_manifest.json').read_bytes()
    with zipfile.ZipFile(r/'verification_v1_2_momentum_bundle.zip','w',zipfile.ZIP_DEFLATED) as z:
        for name,payload in paths.items():z.writestr(name,payload)
    return {'files':len(paths),'changed_prior_files':changed,'forward_fresh':health['fresh']}
