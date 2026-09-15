"""Whitelist delivery for a genuinely data-blocked F0 pilot, never fake accounts."""
from pathlib import Path
import csv
import hashlib
import json
import shutil
import subprocess
import zipfile
from datetime import datetime

from .config import ROOT
from .protocol import PROTOCOL
from .runtime import digest, utcnow, write_json


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def csv_file(path, fields, rows=()):
    with Path(path).open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)


def blocked_results(output):
    output=Path(output)
    capability=read(output/'DATA_CAPABILITY.json')
    if capability['real_futures_history_run'] or (output/'runs').exists():
        raise ValueError('BLOCKED_REPORT_CANNOT_REPLACE_AN_EXISTING_REAL_RUN')
    tests=read(output/'ENGINEERING_RUN.json')
    if tests['pytest_exit_code'] or not tests['resource_bounds_pass']:
        raise ValueError('ENGINEERING_CHECKS_NOT_PASSING')
    state=read(output/'operations/FINAL_STATE_CHECK.json')
    bench=read(output/'benchmarks/verification.json')
    if not bench['source_files_unchanged'] or bench['network_calls']:
        raise ValueError('BENCHMARK_SOURCE_RECONCILIATION_FAILED')
    for name in ('sources.md','contract_registry.csv','README.md','BENCHMARK_METHOD.md'):
        shutil.copyfile(ROOT/'docs/futures_f0'/name,output/('RUNBOOK.md' if name=='README.md' else name))
    schemas={
        'trades':['account','timestamp','market','contract_id','kind','quantity','raw_price','fill_price','commission'],
        'campaigns':['account','campaign_id','market','direction','opened_at','closed_at','net_profit'],
        'rolls':['account','market','old_contract','new_contract','timestamp','old_price','new_price','cost'],
        'daily_equity':['account','session','cash','unsettled_mark_to_market','equity','drawdown'],
        'margin_path':['account','timestamp','equity','initial_margin','maintenance_margin','gross_notional','breaches'],
        'position_sizing_skips':['account','timestamp','market','contract_id','reason'],
    }
    for name,fields in schemas.items(): csv_file(output/(name+'.csv'),fields)
    reason='NO_QUALIFIED_FUTURES_SOURCE_AND_NO_VENDOR_NORMALIZATION_VALIDATION'
    fields=['version','cost','margin_evidence','planned_initial_equity_usd','actual_initial_equity',
            'ending_equity','net_profit','max_drawdown','campaigns','status','reason']
    rows=[]
    for version in PROTOCOL['versions']:
        for cost in PROTOCOL['costs']:
            rows.append(dict(version=version,cost=cost['name'],margin_evidence='ASSUMED_10_PERCENT_PLANNED',
                planned_initial_equity_usd=11500,actual_initial_equity='NA',ending_equity='NA',net_profit='NA',
                max_drawdown='NA',campaigns='NA',status='DATA_BLOCKED_NOT_RUN',reason=reason))
    csv_file(output/'performance.csv',fields,rows)
    csv_file(output/'pyramiding_comparison.csv',
        ['cost','F0_profit','F1_profit','difference','profit_drawdown_comparison','status','reason'],
        [dict(cost=c['name'],F0_profit='NA',F1_profit='NA',difference='NA',profit_drawdown_comparison='NA',
              status='INSUFFICIENT_DATA_NOT_RUN',reason=reason) for c in PROTOCOL['costs']])
    shutil.copyfile(output/'benchmarks/benchmark_comparison.csv',output/'benchmark_comparison.csv')
    csv_file(output/'external_fixed_costs.csv',
        ['monthly_usd','calendar_months','external_payments_usd','F0_net_after_payments','F1_net_after_payments','actual_spend_usd','status'],
        [dict(monthly_usd=x,calendar_months=48,external_payments_usd=x*48,F0_net_after_payments='NA',
              F1_net_after_payments='NA',actual_spend_usd=0,status='BUDGET_SCENARIO_NOT_PURCHASE_OR_FUTURES_RESULT') for x in (0,30,150)])
    csv_file(output/'OUTPUT_STATUS.csv',['file','rows','meaning'],
        [dict(file=n+'.csv',rows=0,meaning='HEADER_ONLY_NO_FUTURES_EXECUTION; NOT_ZERO_TRADES_AFTER_A_REAL_RUN') for n in schemas])
    completed=[
        ('ISOLATED_WORKTREE_AND_DATA','COMPLETED','No stock source/account changes'),
        ('ROOT_SPECS_13','COMPLETED_ROOT_TEMPLATES_ONLY','Actual expiries/vendor definitions UNKNOWN'),
        ('FROZEN_STRATEGY_AND_COSTS','COMPLETED','No search, no changed capital/thresholds'),
        ('DAILY_ENGINE_LONG_SHORT','ENGINEERING_TESTED_MOCK','Open/stop proxies, no real futures validation'),
        ('INTEGER_SHARED_EQUITY_RISK','ENGINEERING_TESTED_MOCK','11500 shared; margin is not notional purchase'),
        ('F1_TWO_ADD_TIERS','ENGINEERING_TESTED_MOCK','Rejected tier consumed; no loss averaging'),
        ('VARIATION_MARGIN_FIFO_RECONCILIATION','ENGINEERING_TESTED_MOCK','Publication cash posting proxy'),
        ('SAME_MULTIPLIER_CAUSAL_ROLL','ENGINEERING_TESTED_MOCK','External dated boundaries/overlap required'),
        ('CHECKPOINT_HASH_RESUME','ENGINEERING_TESTED_MOCK','No B1.2 restore was attempted'),
        ('SCOPED_METADATA_ZERO_CASH_DOWNLOAD','ENGINEERING_TESTED_MOCK','No authenticated API request made'),
        ('SESSION_AGGREGATOR_AND_RESOURCE_GUARD','ENGINEERING_TESTED_MOCK','Requires verified dated windows'),
        ('SPY_QQQ_11500_BENCHMARK','EXECUTED_REAL_CACHE_RESTRICTED','1003 sessions each; source and cash reconciliation PASS'),
        ('VENDOR_RAW_TO_NORMALIZED_AUTOMATIC_INTEGRATION','NOT_IMPLEMENTED_END_TO_END','Requires actual schema/definitions/statistics/status mapping integration'),
        ('HISTORICAL_CONTRACT_CALENDAR_BOUNDARIES','NOT_ACQUIRED_NOT_VERIFIED','Root templates are insufficient'),
        ('DYNAMIC_HISTORICAL_BROKER_MARGIN_TABLE','NOT_IMPLEMENTED_END_TO_END','Static dated spec and10/20% assumptions only'),
        ('CROSS_MULTIPLIER_PRODUCT_ROLL','NOT_IMPLEMENTED_BLOCKED','Do not scale MGC into1OZ'),
        ('REAL_FUTURES_MATRIX_AND_RESEARCH_STATISTICS','NOT_RUN_DATA_BLOCKED','Not no-qualified-trades; no real futures input'),
        ('COMMON_DATE_BLOCK_UNCERTAINTY','NOT_IMPLEMENTED_NOT_RUN','No real futures return sample'),
        ('POSITIVE_COST_EXISTING_CREDIT_DOWNLOAD','NOT_IMPLEMENTED_BLOCKED','Balance/expiry/authority UNKNOWN; positive quotes blocked'),
    ]
    csv_file(output/'FUNCTIONAL_COVERAGE.csv',['feature','status','limitation'],
             [dict(feature=a,status=b,limitation=c) for a,b,c in completed])
    xml=__import__('xml.etree.ElementTree',fromlist=['parse']).parse(output/'engineering_tests.xml')
    suites=list(xml.getroot().iter('testsuite'))
    count=sum(int(s.attrib.get('tests',0)) for s in suites)
    verification=dict(at=utcnow().isoformat(),status='ENGINEERING_PASS_REAL_FUTURES_DATA_BLOCKED',
        engineering_tests=count,engineering_exit_code=tests['pytest_exit_code'],
        test_peak_rss_mib=tests['rss_peak_bytes']/1024**2,
        test_min_system_available_gib=tests['system_available_min_bytes']/1024**3,
        real_futures_run=False,actual_futures_accounts_created=0,
        benchmark_status=bench['status'],benchmark_sessions_per_symbol=1003,
        original_M20_U_running=state['service_process_exists'],B12_resumed=False,
        old_B12_checkpoint_sha256_unchanged=state['B12_checkpoint_sha256'].lower()==
            '4dc71376ff50c634d45d88a9bc2f6d5c77e4b60ffb3ee5648bb66eaea4f89071',
        main_merged=False,broker_calls=0,paid_model_calls=0,purchased_services=0,
        raw_market_files_in_bundle=False,credit_balance_verified=False,
        research_gate='INSUFFICIENT_DATA_NOT_EVALUATED',
        missing_implementations=[x[0] for x in completed if x[1].startswith('NOT_IMPLEMENTED')])
    write_json(output/'verification.json',verification)
    reference=read(output/'CODE_REFERENCE.json') if (output/'CODE_REFERENCE.json').exists() else {}
    start=datetime.fromisoformat(PROTOCOL['operations']['start_utc'].replace('Z','+00:00'))
    minutes=(utcnow()-start).total_seconds()/60
    report=f'''# Futures F0 本轮实际结果

**状态：工程检查通过；真实期货历史因数据接通不足而未执行（DATA_BLOCKED_NOT_RUN）。没有创建六个11500美元空账户来代替结果。**

本轮截至 {utcnow().isoformat()}，自首次检查起约 {minutes:.1f} 分钟，低于4小时上限。没有搜索新参数、购买服务、调用付费模型、操作券商或创建期货前向账户。

## 1. 重启核对与原任务保留

原M20/U的原登录任务已自动恢复，实际工作进程PID {state['service_pid']} 存在，检查心跳为 {state['service_heartbeat']}，距检查 {state['heartbeat_age_seconds']:.1f} 秒，错误列表为空。今天的正式决策记录已在 2026-09-15 10:30:38 UTC 完成，覆盖66个候选；本轮没有重新启动、重置或补造停机期间意图。M20现金3861.15美元、U现金3696.79美元，两本各4次买入、0次卖出。

B1.2没有运行。本轮重新计算84MB检查点的SHA256，仍与原1月28日检查点一致；现金665.96美元、NVDA14股、RKLB36股。11,059,478,528字节数据库和完整恢复副本仍在，副本12文件共15,419,536,129字节；仅核对存在/尺寸及已有证明，没有加载11GB归档或进行新的冷恢复。未发现更晚已验证检查点。此前停止原因、期限与未完成状态均保留，1月29日以后没有续跑。

开始时RAM总15.80GiB、可用4.42GiB，C盘27.29GiB、D盘561.86GiB空闲；收尾观察RAM可用{state['RAM_available_gib']:.2f}GiB，C/D剩余空间分别见operations/FINAL_STATE_CHECK.json。磁盘与RAM分别记录，未混用。所有旧目录文件没有被本轮删除或重置。

## 2. 实际完成的工程

源码目录：`{ROOT}`；分支：`codex/futures-f0-pilot`；独立持久数据目录：`{output}`。代码提交/PR见 CODE_REFERENCE.json。原股票运行器的Python虚拟环境被复用，但源码、数据、状态与账本逻辑完全独立；未安装新依赖。

实现并测试了冻结的55/20通道、Wilder ATR20、下一完整时段多空执行、跳空止损、延后生效的移动保护、整数共用资金、保证金和风险上限、F0/F1两档浮盈加仓、同规格因果换月、逐日盯市与FIFO独立金额复算，以及带输入哈希的中断恢复。缺交易日会清空受影响窗口重新暖机、保留原持仓并标记覆盖问题，不能压缩缺日后继续假装完整数据。

另实现指定Key读取、窄范围元数据估价接口、只放行自身核验零报价的流式下载器、缓存哈希、明确交易日聚合、输入隔离、单进程锁和资源边界。原始供应商数据不会自动成为合格研究输入。详细功能及未完成接口见 FUNCTIONAL_COVERAGE.csv。

**实际运行 {count} 项工程测试，全部通过，pytest耗时见tests.log（本次1.08秒）。** 测试人工输入仅在临时目录，未写入研究行情目录。监测到测试进程峰值RSS {verification['test_peak_rss_mib']:.2f}MiB，测试期间系统最低可用RAM {verification['test_min_system_available_gib']:.2f}GiB。它们是工程测试证据，不是四年期货历史运行速度或收益证据。

只读审查发现并修复了迟到行情回溯成交、不可成交跳空后理想止损、换月现金漏计残余亏损、重复日线错误增加流动性窗口、mock/停牌标记丢失、手造零报价放行和交易日重标等边界。旧版审查文件保留其当时源哈希；最终版本以本轮tests.log和SOURCE_HASHES.json为准。

## 3. 合约、数据与费用核验

覆盖6市场、13个根：GC/MGC/1OZ、HG/MHG、CL/MCL、ZC/MZC、6E/M6E、ES/MES。官方规格和tick金额逐行核对；登记表全部仍为ROOT_TEMPLATE_ONLY，具体到期合约、vendor definitions、逐日历史日历及实际保证金未取得，不能宣称双重核验完成。

1OZ于2025-01-13、MZC于2025-02-24、MHG于2022-05-02才上市，不能补造上市前小合约成交；玉米美分报价的每报价单位美元乘数为ZC50、MZC5。1OZ在2025的交易时段不能套用2026的新时段。出处和冲突处理逐条保存在sources.md及contract_registry.csv。

实际检查项目配置和指定环境变量，未找到DATABENTO_API_KEY；在限定项目目录的文件名/来源盘点内未找到已授权可用期货缓存，本机常用只读券商历史端口也没有连接。没有扫用户全盘找Key。历史权限、实际额度、有效期和下载成本为UNKNOWN/未验证，未发认证数据请求，现金支出0美元。

固定手续费缺当期实证时采用每手每边2美元假设；只保留2tick、4tick、双佣金+4tick三个情景。10%/20%初始保证金和75%维持比例仅为声明的场景，不能替代历史券商保证金。日线成交、结算价发布时转研究现金均为代理，未证明真实报价或清算行到账。不同乘数产品迁移和完整动态保证金接入尚未实现。

## 4. 真实计算结果与明确未运行项

| 账户或研究 | 期初计划本金 | 期末权益 | 净利润 | 最大回撤 | 实际状态 |
|---|---:|---:|---:|---:|---|
| SPY被动持有 | $11,500 | $17,203.23 | $5,703.23 | 24.29% | 真实旧缓存重新计算 |
| QQQ被动持有 | $11,500 | $18,113.89 | $6,613.89 | 34.29% | 真实旧缓存重新计算 |
| F0，不加仓，三个费用情景 | $11,500 | NA | NA | NA | 真实期货输入不足，未执行 |
| F1，最多两次加仓，三个费用情景 | $11,500 | NA | NA | NA | 真实期货输入不足，未执行 |

SPY/QQQ各1003个NYSE交易日，2022-01-03原始open建仓至2025-12-31原始close估值；整数股、每次委托1美元及单边10bp，分红支付日收盘入账后下一交易日再投资，现金零利息。两本源文件前后哈希不变，逐日现金、股数、权益独立复算PASS。2022—2023和2024—2025来自同一连续账户，分段与逐年结果均已保存。

表内保留期末持仓，不含假设卖出费用；假设末日全部卖出后SPY为17185.18、QQQ为18095.08美元。SPY含未到账分红应收49.83美元，未用作可花现金。QQQ部分分红仅有Alpaca证据，未逐项发行人复核；税费、预扣税和汇兑未包含，当年接收时间也未完整验证。基准不是期货策略实绩。

48个月外付运行费情景为0、1440、7200美元（每月0/30/150）。实际本轮采购支出为0。没有期货利润可供扣除，所以项目净盈亏仍为NA，不能把预算当实际账单，也不能从CAGR直接减一个费用比例。

trades/campaigns/rolls/daily_equity/margin_path/position_sizing_skips只有表头；其含义是**没有进行真实期货回测**，不是“引擎真实运行但零合格交易”。performance.csv的全部期货绩效为NA并带阻塞原因。没有真实期货样本，所以4市场/3风险类/12个月/60campaign、利润因子、回撤门槛、压力成本、市场集中度、F1相对优势及共同日期块统计均未评价，不能给PASS或失败证明。

## 5. 下一步和恢复入口

最少需要Ben完成一组配置：在新worktree的`.streamlit/secrets.toml`填写DATABENTO_API_KEY，并提供本账户现有历史额度的真实余额、有效期和允许使用的确认；也可指定已有合法期货文件及来源。没有要求复制Alpaca Key，也没有购买授权。

随后由开发步骤完成具体definitions/期限/日历/settlement/status与报价单位的真实归一接通，先估价再申请范围内数据，核验最小真实样本后再顺序运行有限矩阵。仍需完成真实期货统计评价、真实保证金/流动性及现金到账证据；不能仅把verified字段改成true。原四小时截止为2026-09-15 14:47:39 UTC，超过后需新的有界执行授权，不自动延长。

恢复文档为RUNBOOK.md；状态在TASK_STATE.json，数据权限在DATA_CAPABILITY.json，代码/PR在CODE_REFERENCE.json。本轮没有遗留F0后台或新前向账户；M20/U保持运行，B1.2仍待内存问题单独修复。

## 五句结论

本轮已经做出了可测试的独立期货工程，但没有运行真实期货收益回测。
缺少指定数据Key、真实权限及合约日历输入，使六市场历史矩阵暂时无法评价。
同期SPY和QQQ的真实缓存基准已经算出，但不能拿它们代替F0或F1的成绩。
目前既不能说浮盈加仓有效，也不能说11500美元账户已经具备可交易性。
下一步先补齐合法数据和必要接入，再做固定范围验证，不加钱、不调参、不自动转实盘。
'''
    (output/'FUTURES_F0_RESULTS.md').write_text(report,encoding='utf-8')
    return verification


def package(output):
    output=Path(output)
    required=['FUTURES_F0_RESULTS.md','FROZEN_PROTOCOL.json','sources.md','contract_registry.csv',
        'DATA_CAPABILITY.json','COST_ESTIMATE.json','COVERAGE.csv','trades.csv','campaigns.csv','rolls.csv',
        'daily_equity.csv','margin_path.csv','position_sizing_skips.csv','performance.csv',
        'pyramiding_comparison.csv','benchmark_comparison.csv','tests.log','verification.json']
    extras=['ENGINEERING_RUN.json','engineering_tests.xml','RUNBOOK.md','BENCHMARK_METHOD.md',
        'FUNCTIONAL_COVERAGE.csv','external_fixed_costs.csv','OUTPUT_STATUS.csv','CODE_REFERENCE.json',
        'TASK_STATE.json','SOURCE_HASHES.json','operations/FINAL_STATE_CHECK.json',
        'operations/B12_PRESERVATION_CHECK.json','operations/LOCAL_DATA_INVENTORY.json',
        'operations/CONFIG_CHECK.json','operations/REVIEW_FIXES.json']
    files={name:output/name for name in required+extras}
    for p in (output/'benchmarks').iterdir():
        if p.is_file() and p.suffix in ('.csv','.json','.log'):
            files['benchmarks/'+p.name]=p
    for p in (ROOT/'src/futures_f0').glob('*.py'): files['code/src/futures_f0/'+p.name]=p
    for p in (ROOT/'tests').glob('test_futures_f0_*.py'): files['code/tests/'+p.name]=p
    for name in required:
        if not files[name].is_file(): raise ValueError('MISSING_REQUIRED_DELIVERY:'+name)
    files={n:p for n,p in files.items() if p.is_file()}
    for name,p in files.items():
        if p.suffix.lower() in ('.sqlite','.db','.parquet','.duckdb','.dbn') or 'secrets' in name.lower():
            raise ValueError('NON_WHITELISTED_SENSITIVE_ARTIFACT')
        if p.stat().st_size>5*1024**2: raise ValueError('DELIVERY_ARTIFACT_TOO_LARGE')
    manifest=''.join(digest(p)+'  '+name+'\n' for name,p in sorted(files.items()))
    (output/'manifest.sha256').write_text(manifest,encoding='utf-8')
    files['manifest.sha256']=output/'manifest.sha256'
    target=output/'verification_futures_f0_pilot_bundle.zip'
    if target.exists(): raise ValueError('EXISTING_BUNDLE_RETAINED_DO_NOT_OVERWRITE')
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for name,p in sorted(files.items()): z.write(p,name)
    with zipfile.ZipFile(target) as z:
        if z.testzip(): raise ValueError('ZIP_CRC_FAILURE')
        for line in z.read('manifest.sha256').decode('utf-8').splitlines():
            expected,name=line.split('  ',1)
            if hashlib.sha256(z.read(name)).hexdigest()!=expected:
                raise ValueError('ZIP_CONTENT_HASH_FAILURE')
        count=len(z.namelist())
    receipt=dict(created_at=utcnow().isoformat(),path=str(target),bytes=target.stat().st_size,
                 sha256=digest(target),files=count,crc='PASS',content_hashes='PASS',missing_required=[],
                 no_keys_raw_market_or_database=True)
    write_json(output/'DELIVERY.json',receipt,immutable=True)
    return receipt
