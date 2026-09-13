"""Create auditable B1 reports and a strict allowlist bundle, without market APIs."""
from pathlib import Path
from datetime import datetime, timezone
import json
import shutil
import subprocess
import zipfile
import xml.etree.ElementTree as ET
import pandas as pd
from .freeze import ROOT, REPO, OLD, sha, write, now


def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def md(name,text):(ROOT/name).write_text(text.strip()+'\n',encoding='utf-8')


def report():
    run=read(ROOT/'research/run_summary.json'); coarse=read(ROOT/'coarse/summary.json')
    probe=read(ROOT/'data_probe/PROBE_STATE.json'); scope=pd.read_csv(ROOT/'UNIVERSE_POLICY.csv')
    before=read(ROOT/'old_files_before.json')
    changed=[p for p,h in before['files'].items() if not Path(p).exists() or sha(p)!=h]
    old_status=subprocess.check_output(['git','status','--porcelain'],cwd=OLD,text=True)
    live=read(ROOT.parent/'watchlist-v1_3_1-evidence-forward/forward_status.json')
    preservation={'at':now(),'files_checked':len(before['files']),'changed_files':changed,
                  'old_worktree_git_status':old_status,'old_worktree_clean':not old_status.strip(),
                  'old_service_pid':live['pid'],'old_heartbeat':live['heartbeat'],'errors':live['errors'],
                  'ledger_states':live['books'],'old_services_changed_by_B1':False,'ben_forward_started':False}
    write(ROOT/'PRESERVATION_CHECK.json',preservation)
    cap=pd.read_csv(ROOT/'data_probe/DATA_CAPABILITY.csv')
    cap['industry_scope']=cap.symbol.map(scope.set_index('symbol').scope_policy)
    qc=pd.read_csv(ROOT/'data_probe/FULL66_EXISTING_PLUS_TAIL_QC.csv')
    cap.to_csv(ROOT/'DATA_CAPABILITY.csv',index=False,encoding='utf-8-sig')
    shutil.copyfile(ROOT/'data_probe/EARNINGS_COVERAGE.csv',ROOT/'EARNINGS_COVERAGE.csv')
    tests=ET.parse(ROOT/'engineering_tests.xml').getroot()
    suites=list(tests.iter('testsuite'))
    test_count=sum(int(x.attrib.get('tests',0)) for x in suites)
    failed=sum(int(x.attrib.get('failures',0))+int(x.attrib.get('errors',0)) for x in suites)
    cases=pd.read_csv(ROOT/'CASE_RECONCILIATION.csv')
    table='|证券/日期|正式收盘|1%最高买价|可用交易日|日线新突破|主要未通过项|\n|---|---:|---:|---:|---|---|\n'
    descriptions={('WULF','2026-09-11'):'16:05 无五秒内报价；下一财报未知',('AAOI','2026-09-11'):'无新突破；结构止损不满足7%',
                  ('AMZN','2026-09-11'):'无新突破；诊断净RR约0.10',('SKHY','2026-08-31'):'不足100日；不可制造EMA100',
                  ('SKHY','2026-09-03'):'不足100日；不可制造EMA100',('BE','2026-09-02'):'诊断净RR约0.88，低于2R',
                  ('BE','2026-09-08'):'无新突破；结构止损不满足7%'}
    for c in cases.to_dict('records'):
        table+=f"|{c['symbol']} {c['date']}|{c['official_close']:.2f}|{c['close_limit_1pct']:.2f}|{c['valid_same_security_sessions']}|{'是' if c['fresh_cross_and_above_short_group'] else '否'}|{descriptions[(c['symbol'],c['date'])]}|\n"
    text=f'''DATA_GATED_PARTIAL_EXECUTION

# Ben B1 第一轮研究结果

截至 {now()}。本轮已经实际读取任务书、开发基础组件、运行工程检查、获取真实 SIP 数据并完成案例与有限财报窗口计算。**尚无可发布的完整历史收益路径，不能回答本规则有盈利优势。** 不是一轮完成了收益验收的回测，也没有新建 Ben 前向账户。

## 完成范围

- 唯一仓库 Ben-he321/premarket-radar；独立工作目录 `{REPO}`，分支 `codex/ben-b1-research`，从 cfec474 派生。原 M20/U 代码、服务及账本保留。
- 完整任务书原文及摘要配置已冻结；本轮 5 个规则/仓位组合 × 3 个费用情景，共 15 条路径；没有调节任何收益相关阈值。
- 独立 EMA、Wilder ATR、支撑分组/止损、报价限价、财报时间版本、再入场及 P50/P100/P200 资金/融资纯计算组件已经实现。**完整跨日成交编排、报价量消费去重和全持仓区间报价回放尚未完成验收。**
- 实际工程检查 {test_count} 项，失败 {failed}；119 条依赖/日历弃用警告。临时测试中的合成价格仅用于工程检查，未进入研究缓存。
- 66 个候选全部保留并逐项分类：{int((scope.scope_policy=='KEEP').sum())} 个当前业务范围保留，5 个排除，{int((scope.scope_policy=='UNKNOWN').sum())} 个 UNKNOWN。SM/BATL 为直接油气；AVAV 为主营防务；DXYZ 为封闭式基金；CCXI 当前为非经营业务 SPAC。SIDU 的主营防务占比尚不足以判断，保留 UNKNOWN。PLTR、RKLB、电力/燃料电池及加密相关正股未因客户或名字被自动排除。
- 当前业务来源不能成为历史已知行业分类；全部历史探索标 `CURRENT_SCOPE_APPLIED_TO_HISTORICAL_EXPLORATION`。稳定本地证券版本ID与全球证券ID分开，未核实的全球ID仍 UNKNOWN。

## 真实数据与覆盖

Alpaca SIP 成功返回 {probe['api_pages']} 个分页；SDK 内部重试总尝试次数未单独计数，不能把成功页数当作全部 HTTP 尝试数。Finnhub 配置不存在，权限与历史跨度**未测试**。没有购买订阅或调用付费模型 API。

已有 66 证券 raw/all 的 1,250 个对象完成哈希、行数和身份复核。仅定向补齐最近完整交易日 **2026-09-11**：66 候选+SPY/QQQ/SOXX，raw/all/split 各69行。原对象未覆盖。全池 raw/all 各122,448行、日期重复0；这不代表历史无缺口。

现存交易日缺口：BATL 2日、CLSK 1日、CCXI 16日、SMCI 349日。缺口原因保留 UNKNOWN，不能把它们自动认定为停牌、上市前或零成交量。SKHY 发行人明确美国ADR于7月10日开始交易，但当前 `asof=-` 数据从7月13日开始；7月10日是另外的缺价/映射待核项。

定向缓存含890,189条bar记录、3,113条报价、1,079条逐笔成交，另有207条最新日线覆盖记录。这是有限窗口的真实数据，**不是66只股票全部历史分钟/报价已经下载**。字段检查未发现重复、无效OHLC、缺价或无效数量；不同请求重叠不能当成额外独立市场样本。缓存恢复读取已实际通过，恢复检查新增HTTP请求0。

全池日线粗筛覆盖至9月11日，得到 **{coarse['coarse_events']:,} 个**候选事件；这是当前 all 复权快照的日线粗筛，不是可执行信号数量。先前11,373是未补9月11日的结果。数据对象空分片曾被校验器误判为身份不符，已经修复、回归测试并保留失败记录；最终66只全部完成，错误0。冻结首10事件未改变，已定向读取分钟与盘后报价；AXTI 2016-06-09报价为空。2016报价量的历史归一化口径尚未验证，不能机械套用2026年的单位。

## 案例核验

7个案例的供应商正式收盘均与实际 condition 6 收盘竞价匹配；其中5个与15:59最后一分钟末价不同。**1%买价上限使用正式收盘，不用15:59末价替代。** 仅核验这7个收盘点不等于已核验完整100/300日暖机的所有收盘与公司行动。

{table}

WULF 的约2.19诊断RR是在缺实际ask时以 C 加10bp摩擦计算的结构参考，不能称为当时可成交的2R机会。所有7例下一次财报仍 UNKNOWN，正式订单数0。用户SKHY手动161.37仅保留为案例备注，没有绑定某条EMA或写进全池规则。附件未带原截图像素，不能声称逐像素复现图表。

## 有限财报窗口的实际计算

已核实 WULF、BE、AAOI 各一个季度的事前计划和实际发布日期。保留原计划日期、发布时间精度与真实本次接收时间。DATE精度公告的保守可用边界单列，**不把它写成真实午夜发布时间**；历史网络接收时间为 UNKNOWN。AMZN 的事前网页只约定电话会，不能自动当作收益公告计划；SKHY 实际发布的纽约日期仍未完整核实。

在上述三个独立来源窗口内实际计算 **{run['announcement_window_symbol_sessions']} 个证券交易日**；其中 **{run['preblackout_symbol_sessions']} 个禁做区间前交易日**可通过已知公告日期过滤，日线新突破为 **{run['preblackout_fresh_signals']}**。其他日期仍受到财报禁区、未知下一事件或数据资格限制。未因没有信号再寻找更有利窗口。

15条固定路径完成了资格评估，**严格合格收益路径0、交易订单0、完整campaign 0**。账户摘要的期末权益、收益、CAGR、回撤为空，表示未评估；不能把它们填成5500、0%收益、0%回撤来宣传低风险。

SPY/QQQ本金5500、分红到账再投及融资指数对照尚未运行：目前没有可执行策略的共同资金部署区间。旧SPY/QQQ经济性审计与350美元费用结论完整保留，不用不同日期的旧曲线替代本轮基准。

## 成本、融资与原系统

融资内核将5500视为初始净权益，贷款另记。按自然日ACT/360计息、净权益扣应计息、月度入账；没有把11000当本金。8%/12%利率与50%初始、30%/50%维持保证金只是冻结假设，**不是用户账户权限、历史券商house schedule或真实融资验证**。基础/25bp/双佣金与融资压力已有配置与工程验证，未产生真实历史融资成绩。

本轮新增购买、付费模型调用、真实资金流均0。既有共享订阅与研发实际支出 UNKNOWN。0/月、150/月费用输出保留状态；没有交易评估区间时不造费用后利润。150美元是上限假设，全年1800美元仅为算术支出诊断，不是必须花满的授权，也不是从CAGR直接扣减的比例。

保全检查：{len(before['files'])} 个旧文件，变化 {len(changed)}；原工作目录 {'干净' if not old_status.strip() else '有变更需核对'}，原服务PID {live['pid']}，错误数 {len(live['errors'])}。本次未重启原服务、重置账本、自动合并main或启动Ben前向。

## 下一步与恢复

1. 完成连续事前财报日历与改期记录；已有免费凭证可复用，但Key本身不能证明多年PIT覆盖。
2. 按当前规则补齐合格事件的100/300日同证券RTH/公司行动和全持仓报价路径，核实历史报价数量单位。
3. 在合格输入上完成跨日成交编排与集成回归，然后执行同一冻结矩阵、SPY/QQQ与融资对照。保持本轮阈值，不扩大搜索。

Ben当前只需审阅或上传同目录ZIP；本轮无需开券商账户、购买服务或重新复制Alpaca密钥。所有后台下载/研究任务已结束，只有独立只读看板可保留运行。恢复入口见 `RECOVERY.md` 和 `TASK_STATE.json`。

**已完成工程基础、全池粗筛、真实数据核验和有限窗口计算；尚不能判断初步盈利优势。核心不足是连续财报/执行证据与完整事件回放集成，不是通过调参能够解决的问题。**
'''
    md('BEN_B1_RESULTS.md',text)
    md('RULES_AND_SOURCES.md',f'''# 冻结规则与来源

协议：BEN_B1_FROZEN_RESEARCH_20260913；SHA256 `{sha(ROOT/'frozen_config.json')}`。
完整原文随包 `source/docs/ben_b1/BEN_B1_TASK_20260913.txt`；原文优先于机器摘要，USER_CONFIRMED 与 RESEARCH_DEFAULT 分开。分支 codex/ben-b1-research，基线 cfec474。所有阈值在新收益计算前冻结，属于本轮研究定义，不声称最优。

计算规范参考 [Fidelity EMA](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/ema)、[Fidelity ATR](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/atr)。[Alpaca aggregation FAQ](https://docs.alpaca.markets/us/docs/market-data-faq)区分日线与分钟的成交条件，日线成交量不能默认RTH；[报价单位变更](https://docs.alpaca.markets/us/v1.1/changelog/marketdata-bid-and-ask-size-display-change)说明2025-11-03起按股展示。

企业范围证据见 UNIVERSE_POLICY.csv、scope_additions.json；财报证据见 data_probe/official_earnings_evidence.json。只链接支持相应事实的发行人或官方资料。下载的资料不是盈利证明。

未调用QuantSkills算法或付费模型，本轮没有新增Skill安装；旧项目Skills状态不重写。本轮纯计算由src/ben_b1实现，真实数据验收状态见结果报告。
''')
    md('MARGIN_ASSUMPTIONS.md','''# 融资研究假设

所有账户真实初始权益5500 USD。P50目标50%/最多两只且不借款；P100目标100%/一只；P200最高2倍净权益/一只。必须扣买费、保留每条剩余止损腿退出费，按整数股与实际现金规划；部分成交不等于主动缩仓。

无用户实体/证券历史保证金资料，故 P200 标 HYPOTHETICAL_MARGIN_NOT_BROKER_VALIDATED。初始50%、维持30%；固定维持50%压力。利率历史曲线 UNKNOWN，使用8%/12%假设ACT/360；周末计息，月末过账，净权益每日减应计息，不把月度过账再记一次损失。不引用当前5.130%作为多年利率。

恒等式：净权益=结算现金+待结算款+持仓市值−贷款−应计利息。预留影响可用额度而不是重复费用。卖出产生的待结算款不能立即用于现金新购；结算日期必须由调用者用当时交易规则传入，完整历史结算集成仍待验收。

维持要求不足时，基于给定可执行bid与显示数量计算所需减仓；缺价保留缺口，不造清算价。权益<=0停止新开仓，CAGR留空。本轮只完成纯函数账本测试，未运行历史融资头寸。

手续费每实际订单1美元，压力2美元；额外摩擦10bp/25bp，bid/ask价差已在成交价中不重复扣。买入摩擦后价格不能越过限价，单一订单多次部分成交只收一次订单佣金；真实券商账单尚未核实。
''')
    md('LIMITATIONS.md','''# 未完成与证据边界

- 严格全历史收益、逐年/滚动收益、SPY/QQQ和融资指数基准未完成；空执行文件是明确未评估，不是遗漏文件后推断零交易。
- 无66股连续PIT财报库。只核实三个事前计划事件；不能以旧季度已结束推断下一季度日期。Finnhub凭证缺失，权限/跨度待验证。
- 日线all快照只作粗筛，存在已观察数据、当前股票池和当前业务分类偏差。不是新保留集或盲测。
- 7个正式收盘有真实竞价核对；100/300日全暖机RTH、每项公司行动与16:05当时数据版本未逐点核实。
- 日线缺口原因UNKNOWN；SKHY短美国历史与首上市日API缺口单列。稳定本地版本ID不是已核实CUSIP。
- 2016报价量单位是否回写归一化尚不清楚；AXTI首10窗口报价为空。不能用分钟low触价保证整仓成交。
- 历史报价代理不保证真实券商成交；Basic延迟不支持按本规则原时点的自然前向。
- 全跨日策略成交编排、消费报价数量去重、止损生效事件和恢复集成仍未完成真实验收。纯函数工程测试不能替代这一层。
- 利率/保证金只是冻结假设；税费、真实实体house规则与共享订阅费用UNKNOWN。
- SIDU当前主营防务程度证据不足，UNKNOWN；没有按回测盈亏改变分类。
''')
    started=pd.Timestamp(read(ROOT/'frozen_config.json')['frozen_at']); elapsed=(pd.Timestamp.now(tz='UTC')-started).total_seconds()
    md('TIME_AND_COST_AUDIT.md',f'''# 时间与成本

冻结时间 {started.isoformat()}；本报告时间 {now()}；冻结后已用 {elapsed:.0f} 秒。未耗尽8小时上限而停止在数据/集成资格限制，不能因此称完整任务已验收。

成功Alpaca分页 {probe['api_pages']}，缓存恢复检查新增HTTP0；SDK内部重试总次数UNKNOWN。官方网页直接HTTP收据另列于数据证据；浏览工具业务核查不纳入该API页数，工具总访问次数未统一计数。

本轮新订阅购买0、付费模型API调用0、券商请求0、资金流0。既有Codex/其他共同订阅成本与研发实际支出UNKNOWN，没有擅自分摊。150美元月预算不等于已发生费用。行情研究对象和报告均保存在 {ROOT}，本机重启仍保留；本轮没有云存储发布。

所有原始网络接收时间未知者写UNKNOWN；没有从mtime推断。DATE公告保守可用边界与真实发布时间分列。工程修复记录见 IMPLEMENTATION_REVIEW.md。
''')
    md('IMPLEMENTATION_REVIEW.md','''# 工程修复记录（未调参）

1. 粗筛的未知压力状态字符串判断不严，修为只接受两种明确完整状态；增加缺压力回归。
2. 加入Parquet身份核验时空历史分片误报身份不符，实际观察到20证券错误；改为空分片保留空、非空才核验证券，并通过空片/错身份/哈希篡改测试。失败记录保存在coarse/attempt_before_empty_chunk_fix，未删除。
3. 最新9月11日作为独立overlay，不重写旧对象；最终粗筛事件11382，初始11373，冻结首10未改变。
4. 原始重复分片按冻结manifest次序选择最新，冲突与选中记录另存；执行排序仍需要当时spread，粗筛顺序不能冒充交易排序。
5. 7例收盘竞价全部匹配供应商日线；纠正5例15:59末价替代正式C的问题。
6. 计划日期与实际日期分开；DATE可用边界不称真实午夜发布时间；15路径标已资格检查而非合格。
7. 实际读取的派生Parquet加入哈希固定与篡改测试。报价组件结果不是账户成交，部分成交后净RR仍需在完整回放层核验。
8. 所有修复都属于数据/工程错误纠正；本金、EMA、6%/10%、ATR倍数、7%、2R、财报窗口与费用阈值没有变化。
''')
    md('RECOVERY.md',f'''# 本轮恢复入口

工作目录：{REPO}
分支：codex/ben-b1-research
输出：{ROOT}
Python：{OLD / '.venv/Scripts/python.exe'}

状态：DATA_GATED_PARTIAL_EXECUTION。完整任务和参数在 docs/ben_b1；本轮没有Ben前向账户。先读TASK_STATE.json、BEN_B1_RESULTS.md、LIMITATIONS.md。

只读看板启动入口：pages/15_Ben_B1收盘突破.py；已运行时不要重复启动。实际地址与PID见DASHBOARD_RUNTIME.json。看板重启只读取已有结果，不自动下载/研究。

已有数据恢复必须校验data_probe/data_cache_manifest.json与research/consumed_input_hashes.json。src.ben_b1.data_probe 有持久分页缓存，必要补数只能写本B1目录，使用原项目Alpaca配置和共享限速器。不要调用旧research prepare或旧账本写入。

src.ben_b1.research 是有限资格/案例计算入口，不是完整历史交易模拟器。若补齐输入，先在新版本运行目录完成成交编排与回归，再按原冻结矩阵执行，禁止悄悄覆盖本轮输出。没有后台研究自动继续。
''')
    write(ROOT/'engineering_summary.json',{'tests':test_count,'failures':failed,'dependency_warnings':119,'synthetic_research_prices':False,'at':now()})
    status=read(ROOT/'TASK_STATE.json')
    status.update(at=now(),status='DATA_GATED_PARTIAL_EXECUTION',historical_experiment='FINITE_EVIDENCE_WINDOWS_COMPLETED_FULL_PERFORMANCE_NOT_EVALUATED',
                  research_results='NO_PROFITABILITY_CLAIM',background_research_running=False,tests_run_this_precheck=test_count,
                  strict_matrix_paths_completed=0,required_user_action=None,received_attachment_sha256=read(ROOT/'frozen_config.json')['source_sha256'],
                  market_successful_pages=probe['api_pages'],unverified=['FULL_PIT_CALENDAR','FULL_RTH_ACTION_WARMUP','FULL_EVENT_REPLAY_INTEGRATION','FINANCED_BENCHMARKS'])
    write(ROOT/'TASK_STATE.json',status)
    return preservation


def package():
    allowed=[]
    def add(p,arc):
        if Path(p).is_file():allowed.append((Path(p),arc))
    for name in ['BEN_B1_RESULTS.md','RULES_AND_SOURCES.md','frozen_config.json','CASE_RECONCILIATION.csv','UNIVERSE_POLICY.csv',
                 'DATA_CAPABILITY.csv','EARNINGS_COVERAGE.csv','MARGIN_ASSUMPTIONS.md','account_summary.csv','TIME_AND_COST_AUDIT.md',
                 'LIMITATIONS.md','IMPLEMENTATION_REVIEW.md','RECOVERY.md','engineering_tests.log','engineering_tests.xml','engineering_summary.json',
                 'PRESERVATION_CHECK.json','old_files_before.json','scope_additions.json','scope_receipt.json','TASK_STATE.json','DASHBOARD_RUNTIME.json','delivery_code.json']:
        add(ROOT/name,name)
    for p in (ROOT/'research').iterdir():
        if p.suffix in ['.csv','.json']:add(p,'research/'+p.name)
    for name in ['all_candidate_events.csv','full_pool_coverage.csv','first10_probe_events.csv','input_manifest.json','errors.json','summary.json','duplicate_revision_review.json']:
        add(ROOT/'coarse'/name,'coarse/'+name)
    for name in ['CASE_DATA_RECONCILIATION.csv','CLOSING_AUCTION_RECONCILIATION.csv','FIRST10_PROBE_RESULTS.csv',
                 'official_earnings_evidence.json','finnhub_capability.json','latest_overlay_manifest.json','PROBE_DATA_QUALITY.csv',
                 'ACTION_UNIT_CHECK.csv','CACHE_RESUME_CHECK.json','data_cache_manifest.json','TIME_FIELDS_AND_LIMITATIONS.md','SKHY_FIRST_SESSION_GAP.json',
                 'FULL66_AUDIT_SUMMARY.json','FULL66_EXISTING_PLUS_TAIL_QC.csv','FULL66_INPUT_OBJECT_LINEAGE.csv','PROBE_STATE.json']:
        add(ROOT/'data_probe'/name,'data_probe/'+name)
    for folder in ['src/ben_b1','docs/ben_b1']:
        for p in (REPO/folder).rglob('*'):
            if p.is_file() and p.suffix in ['.py','.md','.json','.txt']:add(p,'source/'+p.relative_to(REPO).as_posix())
    for p in (REPO/'tests').glob('test_ben_b1*.py'):add(p,'source/tests/'+p.name)
    add(REPO/'pages/15_Ben_B1收盘突破.py','source/pages/15_Ben_B1收盘突破.py')
    manifest={'at':now(),'status':'DATA_GATED_PARTIAL_EXECUTION','allowlist_only':True,
              'excluded':['secrets','env files','raw market Parquet/pages','databases','other project files'],
              'members':[{'name':arc,'bytes':p.stat().st_size,'sha256':sha(p)} for p,arc in allowed]}
    write(ROOT/'OUTPUT_MANIFEST.json',manifest)
    add(ROOT/'OUTPUT_MANIFEST.json','OUTPUT_MANIFEST.json')
    target=ROOT/'verification_ben_b1_research_bundle.zip'
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for p,arc in allowed:
            if any(part in {'.streamlit','.env','cache','private_backup'} for part in Path(arc).parts) or p.suffix in {'.parquet','.sqlite','.db'}:
                raise ValueError('BUNDLE_ALLOWLIST_VIOLATION')
            z.write(p,arc)
    with zipfile.ZipFile(target) as z:
        assert z.testzip() is None
        for entry in manifest['members']:
            import hashlib
            assert hashlib.sha256(z.read(entry['name'])).hexdigest()==entry['sha256']
    write(ROOT/'DELIVERY.json',{'at':now(),'status':'DATA_GATED_PARTIAL_EXECUTION','zip':str(target),'size_bytes':target.stat().st_size,
                              'sha256':sha(target),'members':len(allowed),'zip_crc_and_member_sha256_verified':True,
                              'background_research_running':False,'ben_forward_created':False})
    print(json.dumps(read(ROOT/'DELIVERY.json')))


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--package',action='store_true');args=p.parse_args()
    if args.package:package()
    else:report()
