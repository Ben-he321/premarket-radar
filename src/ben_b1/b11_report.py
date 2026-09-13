"""Write the Chinese delivery from completed, immutable final_v4 results only."""
from __future__ import annotations
import json
import shutil
import subprocess
from pathlib import Path
from datetime import datetime,timezone
from .b11_runtime import ROOT,REPO,PREVIOUS,read,write,sha,utc,status,preserve_check
from .b11_deliver import md


def report():
    selected=read(ROOT/'FINAL_RUN_SELECTION.json')
    final=[read(Path(x['output'])/'summary.json') for x in selected['samples']]
    assert len(final)==40 and sum(x['error_count'] for x in final)==0
    filled=[x for x in final if x['buy_fills']]
    table='|独立样本|买入日|股数/卖出腿|最后卖出日|完成结算|期末现金/净权益|净盈亏|\n|---|---|---:|---|---|---:|---:|\n'
    for x in filled:
        c=x['campaigns']['campaigns'][0]
        table+=f"|{x['symbol']} · B/P50/base|{x['sample_date']}|{x['buy_shares']} / 2|{c['exit_at'][:10]}|{x['processed_through']}|${x['latest_equity']['net_equity']:,.2f}|${c['net_profit']:,.2f}|\n"
    integration=read(ROOT/'engineering/ENGINE_TEST_SUMMARY.json')
    preserved=preserve_check()
    assert preserved['status']=='PASS'
    live=read(ROOT.parent/'watchlist-v1_3_1-evidence-forward/forward_status.json')
    write(ROOT/'OLD_FORWARD_READONLY_AFTER.json',{'checked_at':utc(),'read_only':True,'snapshot':live,
        'B11_writes_to_old_ledger':False,'live_service_may_legitimately_advance':True})
    errors={'created_at':utc(),'final_version':'final_v4','final_engine_errors':0,'records':[
        {'version':'actual_v1','issue':'Optional empty CSV cells were NaN and failed strict RUN_SPEC JSON before replay','repair':'Convert missing optional fields to JSON null; regression runs real Inputs/run_sample in isolated test directory','status':'FIXED'},
        {'version':'actual_v2','issue':'MSTR sparse exit quotes delayed far-leg fill; ORCL path intentionally stopped after 5 days for input diagnosis','repair':'Freeze actual triggered holding-path requests; acquire complete specified RTH quote windows; retain initial outputs','status':'SUPERSEDED_BY_ENRICHED_FINAL'},
        {'version':'diagnostic_full','issue':'Transient Windows PermissionError replacing checkpoint while reading a long missing-quote path','repair':'Bounded atomic replacement retries; permanent failure preserves old checkpoint. Regression passed','status':'FIXED_FAILED_OUTPUT_PRESERVED'},
        {'version':'final_v3','issue':'Supplemental manifest was in output root, adapter expected data subdirectory. B run deliberately stopped before accepting it as final','repair':'Copy immutable manifest to declared adapter path with identical hash, pin it in RUN_SPEC, run both tiers final_v4','status':'FIXED_PARTIAL_OUTPUT_PRESERVED'}]}
    write(ROOT/'ERROR_AND_REPAIR_LOG.json',errors)
    start=read(ROOT/'TASK_STATE.json')['started_at']
    cost={'checked_at':utc(),'started_at':start,'elapsed_seconds':(datetime.now(timezone.utc)-datetime.fromisoformat(start)).total_seconds(),
        'budget_hours':8,'new_service_purchase_usd':0,'paid_model_api_calls':0,'broker_calls':0,'Ben_forward_started':False,
        'stock_successful_http_pages':768+53,'corporate_action_successful_pages':1,'corporate_action_http_attempts':1,
        'stock_http_attempts_including_retries':None,'sdk_hidden_retries':None,'finnhub_http_attempts':0,
        'finnhub_zero_reason':'NO_CREDENTIAL_IN_AUTHORIZED_PROJECT_CONFIGURATION',
        'issuer_source_http_receipts':len(read(ROOT/'earnings/PRIMARY_EARNINGS.json').get('source_receipts',[])) or None,
        'browser_and_web_visits':None,'initial_cached_page_uses':3,
        'existing_subscription_cost':'NOT_RETRIEVED_NOT_ALLOCATED; zero new purchases does not mean total project economic cost is zero',
        'research_worker_running':False,'download_worker_running':False}
    write(ROOT/'TIME_AND_COST_AUDIT.json',cost)
    shutil.copyfile(REPO/'docs/ben_b1_1/RULE_INTENT_ALIGNMENT.md',ROOT/'RULE_INTENT_ALIGNMENT.md')
    md('BEN_B1_1_RESULTS.md',f'''# Ben B1.1 真实回放结果

**已真正执行完整事件引擎，完成两条真实 SIP 历史输入驱动的 P50 模型交易周期。全池策略绩效尚未完成验收。**

截至 {utc()}。最终版本 `final_v4`。选择窗口固定为 **2026-01-02—2026-09-11**，冻结前20候选实际日期为 **1月2日—1月13日**；17个证券名称中15个KEEP取得深层数据，AVAV/DXYZ按原范围排除。所有66候选仍在覆盖表内，没有以能否成交或盈亏替换样本。

## 实际执行了什么

相同固定20项分别运行严格财报层A、实际财报日期诊断层B，共 **40次真实输入回放、{sum(x['events_processed'] for x in final):,}个处理事件**。未成交项也实际推进至固定入场窗口到期。持仓项推进至卖出和自然结算，没有倒填或手工指定买入。最终错误0、未退出持仓0、未结算资金0；40项真实恢复幂等性检查全部通过。

|层级|模型买入/卖出|完成周期|其他逐项结果|
|---|---:|---:|---|
|A：事前计划证据|0 / 0|0|18项缺完整当时计划；2项范围排除|
|B：RETROSPECTIVE_EARNINGS_EXCLUSION|2 / 4|2|12项固定16:05无5秒内有效报价；3项财报覆盖未知；1项规则拒绝；2项范围排除|

财报未知的3项是BMNR两项和MRNA一项。BMNR的10-Q申报日不能冒充实际财报公布日；MRNA存在1月12日初步年度业绩更新，不能把普通季度日期之间的空白直接当作连续完整财报资料。B层其余有界普通季度实际发布区间来自发行人/SEC原文；这不是全年所有非定期公告的穷尽审计。

{table}

两项各自本金5500美元；每次买入/卖出按原base模型收1美元佣金，买卖分别增加10bp摩擦，观察价取真实SIP ask/bid；每周期佣金3美元已在盈亏中扣除。两本最终负债、利息、预留均0。它们不是竞争同一资金的组合，**不得相加当作全池收益**，也不计算短样本年化、宣传胜率或优势显著性。

MSTR两腿8/8股，先近腿、后远腿止损；ORCL两腿7/6股，移动止损按前日决定、下一常规盘生效后卖出。相应时间、原始报价、数量库存消耗和现金流水见最终版本CSV、CONSUMED_QUOTE_EVIDENCE及独立复核JSON。财报强退、部分成交和融资压力等分支在工程测试中实际通过；本轮两条真实成交没有触发这些全部分支，不能写成它们均已获得真实样本验证。

PLTR的1月7日日线突破与净RR约5.536成立，但16:05有效ask181.27低于短均线组顶部EMA20约181.32648，入场形态失效而拒绝。不能用加摩擦后的模拟成交价去恢复原始报价形态。

## 数据接通、缺失与时间含义

原有关联项目Alpaca凭证实际有效，本轮继续SIP/Basic，无IEX、Futu、yfinance或合成研究数据回退。取得 **3,043,187条真实分钟记录**；15只证券各有145—148日截至2026-01-02之前的常规盘预热及固定区间174交易日数据，重复时间戳、无效OHLC、缺失OHLC检查均0。日线raw/split/all分开保存，官方收盘与末一分钟收盘分开；收盘竞价的价格/量条件另附核对。

为自然产生的持仓补齐MSTR 1月9日09:30—09:56，ORCL 1月12/13日完整常规盘及1月14日09:30—10:40报价。追加489,125条跨请求去重后报价，窄宽MSTR窗口另有18,036条重复，库存只消费一次。查询完整不表示每秒都有更新；原5秒门槛保持。旧MSTR稀疏输入下-110.89美元的结果保留为诊断版本，不再用它冒充完整路径。

报价单位冻结为本轮2026日期的shares，有供应商说明支持；不外推2025年11月3日前历史是否被供应商回写。历史L1只能产生模型成交，不能证明排队位置、真实成交或同样的实盘可执行性。CSV中 `price_is_proxy=True` 表示历史模型成交，不表示用分钟价格替代bid/ask；本轮没有启用MINUTE_BAR_EXECUTION_PROXY成交。

历史事件时间、这次实际下载收据与旧网络接收时间分开。旧网络接收时间UNKNOWN。最终RTH日线在收盘后1分钟可用属于本轮历史工程假设，**不证明Basic当时在16:05之前已实时收到定稿数据**。价格使用供应商标准字段；尚未独立逐点复核的历史仍明确分层，未永久硬编码全部阻断。

前20日量能核对313个证券日期，15个日期仍UNKNOWN，影响GLW两个窗口、ORCL一个窗口。不会填零。量能为原排序的次级条件，单证券工程样本或价差已唯一排序时不因不需要的次级字段新增拒单条件；并列且需要未知量能时停止该排序。66行覆盖表列出其余45个KEEP尚无本轮深层连续输入，不缩减原池。

Finnhub在本项目及明确关联旧项目的实际Provider、Secrets、项目.env和指定环境入口中均未找到有效凭证。**CREDENTIAL_MISSING，实际接口权限NOT_TESTED**，不是AUTH_FAILED，也不是供应商无历史权限。需要时填写既有 `premarket-radar-ai-m1/.streamlit/secrets.toml` 顶层 `FINNHUB_API_KEY`；无需再次复制Alpaca密钥。即使配置完成，仍须实测近期/历史/实际公布端点，并核对能否提供历史计划公开版本，不能把空数组当财报安全。

发行人免费公告及现有授权Alpaca已足够完成本轮两条诊断路径，**本轮无需额外外部操作或购买**。全池严格PIT能否仅靠现有免费权限完整解决仍未知；单独补一个Finnhub key不是资格保证。

## 工程完成与尚未验证

完整事件内核已接入冻结信号、跨日交易、分档/移动止损、15:50因果数据、版本化财报退出、报价数量去重、部分成交净7%/2R复算、现金/预留/结算、公司行动、P200显式模拟负债计息与保证金假设、原子恢复。**{integration['tests']}项工程测试通过，失败0**；测试数据只在临时目录，不进入研究目录。Windows文件暂时占用修复有界重试；持续失败保留旧检查点。所有已知开发失败及恢复版本保留在ERROR_AND_REPAIR_LOG。

本轮要求的事件内核功能没有以未实现占位符替代运行。尚未验证的是：全池共同资金真实回放、其余原15矩阵真实历史情景及SPY/QQQ组合比较；真实输入中未出现的财报强退/融资/拆股分支；所有历史计划PIT和旧接收延迟。新的供应商拆股字段若出现仍须显式审核比例，缺少现金替代零股金额则保留未知权益，不能自动捏造金额。

**P50全池连续输入资格未满足，因此未运行其余矩阵及基准，不再初始化15个空账户冒充已跑完。**全池必须让声明的候选在连续区间竞争同一5500美元资金；目前只具备固定20工程样本的输入。现有SPY/QQQ缓存存在，但没有可比较的全池账户，所以本轮不提供策略相对基准收益。缺数据不是策略亏损；两条亏损样本也不是独立盈利检验或无效性证明。

## 保全、运行与恢复

唯一仓库 `Ben-he321/premarket-radar`；分支 `codex/ben-b1-1-replay`；原B1基线e769f69及原包保持，{preserved['checked_files']}个旧文件哈希复核PASS。原M20/U服务及账本未修改、未重启；未启动Ben前向账户，未下券商订单、未买服务、未调用付费模型API、未合并main。新增购买支出0美元；现有订阅账单未读取或摊销，不能据此声称项目全部经济成本为零。

新只读页面：[http://localhost:8522/](http://localhost:8522/)。它在线不代表研究仍在后台运行。正式回放和下载均已结束；页面只读取结果。当前PID与心跳快照见RUNTIME_STATUS。

工作目录：`{REPO}`。持久数据/报告/恢复目录：`{ROOT}`。机器重启后本地文件保留；只读页面进程需重新启动，不承诺不存在的自动研究任务。

验收包：`verification_ben_b1_1_replay_bundle.zip`。FINAL_RUN_SELECTION指定最终A/B均为final_v4，旧版本仅供审计。包内有源码、测试、输入哈希、各样本事件/账务、覆盖和来源；不含密钥、原始行情库或完整数据库。完整检查点含缓存库存，因此留在本地，ZIP提供真实恢复哈希证明；单独ZIP不是原始数据备份。

下一步仅为补齐全池连续历史计划与行情资格，再按既定规则运行共同资金P50。当前没有新增策略、阈值或参数搜索的依据，也不启动Ben前向。
''')
    md('RECOVERY.md',f'''# B1.1 恢复入口

最终输入与输出都在 `{ROOT}`，代码目录 `{REPO}`。FINAL_RUN_SELECTION.json固定最终A/B各20样本为final_v4。原B1与M20/U路径没有改变。

本次回放和下载已完成，当前没有继续下载或研究的后台进程。只读看板 http://localhost:8522/ 在线时仅展示报告，不会触发重放。进程重启不会删除本地数据。

恢复时使用原已验证Python环境和同版本源码，在此工作目录执行 `python -m src.ben_b1.b11_research --tier A --version final_v4` 或B层同入口。已完成且哈希相同的样本读取既有summary；未完成时读取本样本原子checkpoint，重复事件跳过。输入或核心源码改变将显式拒绝复用，请新建版本，不删除旧目录。

40份RECOVERY_IDEMPOTENCY证据均为真实输入最后批次重复回放的实际PASS。恢复原始研究状态依赖本机原始输入与checkpoint；它们按要求未放ZIP。验收包不能单独冒充原始行情备份。源码快照与输入哈希可用于定位精确恢复版本。

只重新打包已有证据可使用b11_deliver；默认拒绝覆盖已交付ZIP。不会因打开报告自动重跑研究。失败版本actual_v1、actual_v2、diagnostic_full、final_v3保留，最终结果只取final_v4。
''')
    return {'report':str(ROOT/'BEN_B1_1_RESULTS.md'),'samples':40,'model_campaigns':len(filled),'tests':integration['tests']}


if __name__=='__main__':print(json.dumps(report(),ensure_ascii=False))
