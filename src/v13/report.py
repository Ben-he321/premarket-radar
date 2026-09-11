"""Read-only health sampling and explicit report-only verification archive."""
import json
import sqlite3
import subprocess
import zipfile
from collections import Counter
from datetime import datetime,timezone
import numpy as np
import pandas as pd
from .runtime import *

def forward_health():
    old=v11root();status=read(old/'experimental_paper_status.json',{});pid=int(status.get('pid') or 0)
    command=f'Get-CimInstance Win32_Process -Filter "ProcessId = {pid}" | Select-Object ProcessId,ParentProcessId,CommandLine,@{{Name="StartedAt";Expression={{$_.CreationDate.ToUniversalTime().ToString("o")}}}} | ConvertTo-Json -Compress'
    query=subprocess.run(['powershell','-NoProfile','-Command',command],capture_output=True,text=True,timeout=30)
    proc=json.loads(query.stdout) if query.returncode==0 and query.stdout.strip() else {}
    heartbeat=status.get('heartbeat');age=(datetime.now(timezone.utc)-datetime.fromisoformat(heartbeat)).total_seconds() if heartbeat else None
    with sqlite3.connect((old/'experimental_paper/ledger.sqlite').as_uri()+'?mode=ro',uri=True) as conn:
        payload=conn.execute('select payload from state where id=1').fetchone()[0];ledger=json.loads(payload)
        events=[json.loads(x[0]) for x in conn.execute('select payload from events')]
        intents=[json.loads(x[0]) for x in conn.execute('select payload from intents')]
    progress=[]
    for path in sorted((old/'experimental_paper').glob('decision-progress-*.json')):
        x=read(path);symbols=x.get('symbols',{})
        progress.append({'date':x.get('date'),'status_counts':dict(Counter(v.get('status','UNKNOWN') for v in symbols.values())),
                         'symbols':symbols,'file_sha256':sha(path)})
    x={'checked_at':utc(),'process':proc,'heartbeat':heartbeat,'heartbeat_age_seconds':age,
       'fresh':bool(proc) and age is not None and 0<=age<90,'strategy':'FIXED_LEGACY_UNCHANGED_NO_V13_SIGNALS',
       'cash':ledger['cash'],'positions':ledger['positions'],'unsettled':ledger['unsettled'],
       'intents':len(intents),'buys':sum(e.get('type')=='BUY' for e in events),'sells':sum(e.get('type')=='SELL' for e in events),
       'settlements':sum(e.get('type')=='CASH_SETTLEMENT' for e in events),'logical_ledger_sha256':hashlib.sha256(payload.encode()).hexdigest(),
       'orders':status.get('orders'),'next_decision':status.get('next_decision'),'service_status':status.get('status'),'error':status.get('error'),
       'data':read(old/'forward_input_status.json',{}),'decision_progress':progress,
       'claim':'READ_ONLY_CURRENT_SAMPLE; intents are not fills; engineering fixtures are not natural trades'}
    write(root()/'forward_health.json',x);return x

def validate():
    r=root();p=protocol();accounts=read(r/'account_summaries.json');prior=read(r/'prior_result_hashes.json')
    changed=[name for name,h in prior['files'].items() if not Path(name).exists() or sha(name)!=h]
    findings=[];trade_count=0;equity_count=0;reconciliations=[]
    for a in accounts:
        t=pd.read_csv(r/f'accounts/{a["id"]}-trades.csv');c=pd.read_csv(r/f'accounts/{a["id"]}-equity.csv')
        trade_count+=len(t);equity_count+=len(c)
        if c.date.duplicated().any() or c.date.max()>CUTOFF or c.cash.min()<-.01:findings.append(a['id']+':INVALID_EQUITY_DATES_OR_CASH')
        if len(t):
            if t.exit_date.max()>CUTOFF or (t.signal_date>=t.entry_date).any():findings.append(a['id']+':LOOKAHEAD_OR_HOLDOUT')
            if t.symbol.isin(['DXYZ','SPY','QQQ','SOXX']).any():findings.append(a['id']+':EXCLUDED_SECURITY_TRADED')
            expected=(t.price_net_pnl+t.dividend_entitlement).map(lambda v:__import__('src.v11.kernel',fromlist=['rounded']).rounded(v))
            if not np.allclose(expected,t.total_net_pnl,atol=.000001):findings.append(a['id']+':TRADE_PNL_MISMATCH')
            if not np.allclose(t.return_net,t.total_net_pnl/t.entry_cost,atol=1e-10):findings.append(a['id']+':RETURN_DENOMINATOR')
        component=float(t.price_net_pnl.sum()+t.dividend_entitlement.sum()) if len(t) else 0.
        final_open=sum(pos['qty']*pos['last']-pos['cost'] for pos in a['open_position_details'].values())
        residual=a['net_pnl_usd']-component-final_open
        reconciliations.append({'account_id':a['id'],'net_pnl_usd':a['net_pnl_usd'],'closed_price_plus_unrounded_dividend':component,
                                'open_position_unrealized':final_open,'cash_rounding_and_open_dividend_residual':residual,
                                'all_positions_closed':a['open_positions']==0})
        if abs(c.equity.iloc[-1]-a['ending_equity'])>.00001:findings.append(a['id']+':SUMMARY_EQUITY_MISMATCH')
        for suffix in ['equity','trades']:
            if sha(r/f'accounts/{a["id"]}-{suffix}.csv')!=a['output_hashes'][suffix]:findings.append(a['id']+':CHECKPOINT_HASH_MISMATCH')
    f=pd.read_csv(r/'per_stock_conditions.csv');d=pd.read_csv(r/'descriptive_factors_990.csv')
    assert len(f)==1782 and f.symbol.nunique()==66 and len(d)==990 and len(accounts)==486
    assert len([x for x in accounts if x['kind']=='ACTIVITY_RANDOM'])==450
    save_csv('report_reconciliation.csv',reconciliations)
    result={'checked_at':utc(),'accounts':len(accounts),'trades':trade_count,'equity_rows':equity_count,'condition_cells':len(f),
            'descriptive_cells':len(d),'candidate_count':f.symbol.nunique(),'changed_prior_files':changed,'prior_files':len(prior['files']),
            'errors':findings,'max_closed_account_cash_rounding_residual_usd':max((abs(x['cash_rounding_and_open_dividend_residual']) for x in reconciliations if x['all_positions_closed']),default=0.),
            'status':'PASS' if not findings and not changed else 'FAIL'}
    write(r/'result_checks.json',result);return result

def build():
    r=root();p=protocol();checks=validate();health=forward_health()
    primary=read(r/'primary_nine_tests.json');accounts=read(r/'account_summaries.json');coverage=read(r/'data_coverage.json')
    calref=read(r/'calibration_receipt.json');cal=read(calref['report']);caldir=Path(calref['temporary_directory'])
    original=read(v11root().parent/'watchlist-v1_2-momentum/MOMENTUM_PROTOCOL.json')
    lead=[x for x in primary if x['positive_increment_exploratory']]
    base=[x for x in accounts if x['kind']=='CONDITION' and x['cost_case']=='base'];profitable=[x for x in base if x['net_pnl_usd']>0]
    text=['# V1.3 独立条件与公平对照：实际结果','',
          f'已完成 66 个候选的 9 个条件/周期、1782 个逐股条件分组、990 个原五因子描述单元和全部 486 个共享资金回放（36 个条件/无条件成本情景 + 450 个随机活动对照）。基准条件账户 {len(profitable)}/9 个净盈利；经预定方法与数据质量守门后，正向增量探索线索 {len(lead)}/9 个。',
          f'证据标签：**{LABEL}**。这是受到 V1.2 启发、截至 2026-03-10 的历史探索，绝不是新盲测。没有重刷旧保留期或接入原实验账户。',
          '', '## 固定主问题与账户结果','',
          '| 条件 | H | 基准净盈亏 USD | 50 随机均值 USD | 随机 5%–95% USD | 相对 U 盈亏差 | 每日增量均值 | 95% 块区间 | BY q | 推断状态 |',
          '|---|---:|---:|---:|---:|---:|---:|---|---:|---|']
    for x in primary:
        ci=f'{x["ci_low"]:.6f} 至 {x["ci_high"]:.6f}' if x['ci_low'] is not None else '样本不足'
        text.append(f'| {x["condition"]} | {x["horizon"]} | {x["net_pnl_usd"]:.2f} | {x["random_mean_pnl_usd"]:.2f} | {x["random_pnl_p05"]:.2f}–{x["random_pnl_p95"]:.2f} | {x["delta_pnl_vs_U"]:.2f} | {x["mean"]:.6f} | {ci} | {x["q_by"]:.4f} | {x["inference_status"]} |')
    text+=['','M20=20 日收益>0；RS20=个股20日收益减SPY>0；REV5=5日收益<0，是短期反转假设。三者都不要求 FIXED_LEGACY 买点。',
      '主检验采用基准条件账户每日权益变化/5500，减对应全部50个随机账户的均值，双侧 BY 5%，10000 次共同日期块（60日）重采样。只有正向差且通过守门和校正才标探索线索。没有把负向显著、少亏或少花佣金叫作 Alpha。',
      '随机账户基于每个股票/条件/训练窗口的固定接受概率。接受概率相同不能确保最终交易数或曝险匹配；activity_matching_errors.csv 列全部误差，不能据此宣称完全隔离了仓位和交易次数。种子分位数是随机化离散程度，不是新盲测置信区间。',
      '', '## 统计分辨率和方法识别能力','',
      '原 V1.2 的500次、1980项BY、0项通过完整保留。最小 p=1/501；BY 是 step-up，稀疏单项效果难过第一排名阈值，但足够多的小p仍可从较高排名通过。calibration/p_resolution_all_ranks.csv 对照全部排名。新9项检验的1/10001小于第一排名门槛，因此有限重采样不再阻止孤立强效应达到该门槛。',
      f'实际合成方法校准：零效应名义拒绝率 {cal["null_nominal_rejection"]:.2%}、95%区间覆盖 {cal["null_95_ci_coverage"]:.2%}；预冻结守门通过={cal["calibration_guard_passed"]}。100个独立面板含66证券共同市场、时间相关、20日重叠和10%缺失，弱/中/强固定网格及稀疏/密集结果全部附上。',
      '合成试验保存在独立临时测试目录，包内 calibration 明确是方法证据，不进入真实策略表现。该校准不证明非平稳、现金受限账户的一般有效性；经验秩 Rank IC 未在此校准，全部只作描述。',
      '', '## 标签、成本、活动和集中度分别评价','',
      'per_stock_conditions.csv：66候选 × 3条件 × 3周期 × 条件/非条件/同一可用日期全集。单股条件与非条件属于不同日期子集；与SPY是同事件日期严格配对。各项附有效数、非重叠标签数、日期块和描述区间。',
      'shared_conditions.csv 先在同一日期等权汇总可用股票，再只比较条件、非条件均可用的共同日期；DXYZ不进共享股票结果。这里的标签收益不含账户止损，不等于账户收益。',
      'descriptive_factors_990.csv 保留1/5/20/60日及relative20全部15组合状态，Rank IC不用于晋级。原V1.2含区间的描述另原样附上供参照，不冒称本轮新增独立检验。',
      'account_summary.csv、cost_exposure_differences.csv 附全部9条件、3无条件的10bp/25bp/双佣金，全部随机种子只用基准成本。整数股、5500共享资金、0.5%风险、20%单股上限、4持仓、5%止损、无固定止盈保持一致。',
      '资金占用同时给收盘和开盘完成入场后、盘中退出前的时点占用。日线无法证明完整日内路径或时间加权占用，也不能保证代理开盘/止损价的真实成交。',
      'concentration.csv 分解股票、退出年份和最高5笔盈利交易，供核查是否集中。正净额仍需结合回撤、不确定性及集中度，不能单独推广。',
      '', '## 缺失、未知与保留边界','',
      '开始资格只读取信号日已知数据和训练概率，不用未来缺价或行动删除入场。缺次日开盘的意图记录 NO_EXECUTABLE_BAR，不延后补买；统计标签另要求整个路径完整。缺价持仓保留并标记陈旧估值。',
      '遭遇未知复杂公司行动时保留原价代理轨迹并完整标为 UNVERIFIED_COMPLEX_ACTION；不是可靠可执行业绩。条件或任一对应随机账户有未核实复杂行动、缺价持仓或末日未平仓，该主检验降为描述并令用于BY的p=1。原始计算p另保留，绝不隐藏。',
      '当前证券身份、现存股票池和 all 调整快照不是历史时点资料，仍有选择/幸存者和数据版本偏差。DXYZ独立CEF，参考ETF不交易。',
      '缺少合格评价数据的证券：'+(', '.join(x['symbol']+':'+x['status'] for x in coverage if x['status']!='AVAILABLE') or '无')+'。全部仍保留在66候选表中，完整原因见 data_coverage.json、incomplete_and_limits.json。',
      '', '## 新鲜前向状态、工程证据与恢复','',
      f'只读采样 {health["checked_at"]}：fresh={health["fresh"]}，现金 {health["cash"]:.2f}，意图 {health["intents"]}，买 {health["buys"]}、卖 {health["sells"]}、结算 {health["settlements"]}。这不是凌晨旧快照。逐证券数据失败、无信号和永久阻断见 forward_health.json。',
      f'旧成果 {checks["prior_files"]} 个哈希，变化 {len(checks["changed_prior_files"])}；逐笔与权益导出检查 {checks["status"]}，{checks["trades"]}笔、{checks["equity_rows"]}条账户日权益。实际工程测试输出见 tests.log；完整计算起止、输入版本、源提交在协议及 timeline/COMPUTATION_RECEIPT。',
      f'页面 http://localhost:8513 → 独立条件对照。结果目录 {r}。同一虚拟环境 python -m src.v13 run 仅续传，完成后不会重算；python -m src.v13 report 只刷新报告和只读状态。八小时上限不可静默延长。',
      '研究完成后不留新策略后台账户；原 FIXED_LEGACY 服务按既有期限独立运行。付费API调用0、新服务0、新行情下载0、券商订单0。',
      '', '做了什么：完成有限9组条件实验和全部公平对照，并保留旧成果。',
      f'发现什么：基准条件净盈利{len(profitable)}组，方法与数据守门后正向增量探索线索{len(lead)}组，详细差异和限制如上。',
      '还没证明什么：没有证明新盲测可复现优势、完全相同曝险下的Alpha或真实可成交收益。',
      '下一步：有线索也须另立独立前向协议，本轮不自动晋级、不继续追加搜索。']
    (r/'V1_3_RESULTS.md').write_text('\n'.join(text),encoding='utf-8')
    names=['V1_3_RESULTS.md','CONTROLLED_PROTOCOL.json','RUN_BUDGET.json','timeline.json','COMPUTATION_RECEIPT.json',
           'per_stock_conditions.csv','descriptive_factors_990.csv','shared_conditions.csv','shared_condition_daily.csv','frozen_training_probabilities.csv',
           'statistical_label_exclusions.csv','primary_nine_tests.csv','primary_nine_tests.json','account_summary.csv','account_summaries.json',
           'activity_matching_errors.csv','cost_exposure_differences.csv','concentration.csv','incomplete_and_limits.json','data_coverage.json',
           'prior_result_hashes.json','result_checks.json','report_reconciliation.csv','forward_health.json','calibration_receipt.json',
           'TASK_STATE.json','tests.log','engineering_preflight.log','research.stdout.log','research.stderr.log','complex_action_calendar.json']
    names+=[x.relative_to(r).as_posix() for x in (r/'accounts').glob('*.csv')]
    names+=[x.name for x in r.glob('research.resume*.log')]
    for name in ['error_history.json','implementation_notes.json','source_equivalence.json','ui_checks.json']:
        if (r/name).exists():names.append(name)
    paths={n:(r/n).read_bytes() for n in names}
    for name,h in calref['artifact_hashes'].items():
        file=caldir/name
        if sha(file)!=h:raise ValueError('SYNTHETIC_CALIBRATION_ARTIFACT_CHANGED')
        paths['calibration/'+name]=file.read_bytes()
    old=v11root().parent/'watchlist-v1_2-momentum'
    for name in ['momentum_per_symbol.csv','momentum_shared_summary.csv','V1_2_RESULTS.md']:
        paths['existing_v1_2/'+name]=(old/name).read_bytes()
    manifest={'created_at':utc(),'experiment_id':p['experiment_id'],'protocol_hash':p['protocol_hash'],'data_version':p['data_version'],
              'frozen_source_commit':p['source_commit'],'current_source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=CODE,text=True).strip(),
              'files':{n:hashlib.sha256(b).hexdigest() for n,b in paths.items()},'checks':checks,
              'excluded':'Credentials, raw/all market bar library, conditional event caches, full databases, private account ledger',
              'calibration_is_not_strategy_performance':True}
    write(r/'experiment_manifest.json',manifest);paths['experiment_manifest.json']=(r/'experiment_manifest.json').read_bytes()
    archive=r/'verification_v1_3_controlled_factor_bundle.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for name,b in paths.items():z.writestr(name,b)
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        assert all(hashlib.sha256(z.read(n)).hexdigest()==h for n,h in manifest['files'].items())
    return {'archive':str(archive),'bytes':archive.stat().st_size,'sha256':sha(archive),'files':len(paths),'checks':checks['status'],'forward_fresh':health['fresh']}
