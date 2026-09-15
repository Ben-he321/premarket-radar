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
    report=f'''# Futures F0 ����ʵ�ʽ��

**״̬�����̼��ͨ������ʵ�ڻ���ʷ�����ݽ�ͨ�����δִ�У�DATA_BLOCKED_NOT_RUN����û�д�������11500��Ԫ���˻�����������**

���ֽ��� {utcnow().isoformat()}�����״μ����Լ {minutes:.1f} ���ӣ�����4Сʱ���ޡ�û�������²�����������񡢵��ø���ģ�͡�����ȯ�̻򴴽��ڻ�ǰ���˻���

## 1. �����˶���ԭ������

ԭM20/U��ԭ��¼�������Զ��ָ���ʵ�ʹ�������PID {state['service_pid']} ���ڣ��������Ϊ {state['service_heartbeat']}������ {state['heartbeat_age_seconds']:.1f} �룬�����б�Ϊ�ա��������ʽ���߼�¼���� 2026-09-15 10:30:38 UTC ��ɣ�����66����ѡ������û���������������û���ͣ���ڼ���ͼ��M20�ֽ�3861.15��Ԫ��U�ֽ�3696.79��Ԫ��������4�����롢0��������

B1.2û�����С��������¼���84MB�����SHA256������ԭ1��28�ռ���һ�£��ֽ�665.96��Ԫ��NVDA14�ɡ�RKLB36�ɡ�11,059,478,528�ֽ����ݿ�������ָ��������ڣ�����12�ļ���15,419,536,129�ֽڣ����˶Դ���/�ߴ缰����֤����û�м���11GB�鵵������µ���ָ���δ���ָ�������֤���㡣��ǰֹͣԭ��������δ���״̬��������1��29���Ժ�û�����ܡ�

��ʼʱRAM��15.80GiB������4.42GiB��C��27.29GiB��D��561.86GiB���У���β�۲�RAM����{state['RAM_available_gib']:.2f}GiB��C/Dʣ��ռ�ֱ��operations/FINAL_STATE_CHECK.json��������RAM�ֱ��¼��δ���á����о�Ŀ¼�ļ�û�б�����ɾ�������á�

## 2. ʵ����ɵĹ���

Դ��Ŀ¼��`{ROOT}`����֧��`codex/futures-f0-pilot`�������־�����Ŀ¼��`{output}`�������ύ/PR�� CODE_REFERENCE.json��ԭ��Ʊ��������Python���⻷�������ã���Դ�롢���ݡ�״̬���˱��߼���ȫ������δ��װ��������

ʵ�ֲ������˶����55/20ͨ����Wilder ATR20����һ����ʱ�ζ��ִ�С�����ֹ���Ӻ���Ч���ƶ����������������ʽ𡢱�֤��ͷ������ޡ�F0/F1������ӯ�Ӳ֡�ͬ���������¡����ն�����FIFO�������㣬�Լ��������ϣ���жϻָ���ȱ�����ջ������Ӱ�촰������ů��������ԭ�ֲֲ���Ǹ������⣬����ѹ��ȱ�պ������װ�������ݡ�

��ʵ��ָ��Key��ȡ��խ��ΧԪ���ݹ��۽ӿڡ�ֻ�������������㱨�۵���ʽ�������������ϣ����ȷ�����վۺϡ�������롢������������Դ�߽硣ԭʼ��Ӧ�����ݲ����Զ���Ϊ�ϸ��о����롣��ϸ���ܼ�δ��ɽӿڼ� FUNCTIONAL_COVERAGE.csv��

**ʵ������ {count} ��̲��ԣ�ȫ��ͨ����pytest��ʱ��tests.log������1.08�룩��** �����˹����������ʱĿ¼��δд���о�����Ŀ¼����⵽���Խ��̷�ֵRSS {verification['test_peak_rss_mib']:.2f}MiB�������ڼ�ϵͳ��Ϳ���RAM {verification['test_min_system_available_gib']:.2f}GiB�������ǹ��̲���֤�ݣ����������ڻ���ʷ�����ٶȻ�����֤�ݡ�

ֻ����鷢�ֲ��޸��˳ٵ�������ݳɽ������ɳɽ����պ�����ֹ�𡢻����ֽ�©�Ʋ�������ظ����ߴ������������Դ��ڡ�mock/ͣ�Ʊ�Ƕ�ʧ�������㱨�۷��кͽ������ر�ȱ߽硣�ɰ�����ļ������䵱ʱԴ��ϣ�����հ汾�Ա���tests.log��SOURCE_HASHES.jsonΪ׼��

## 3. ��Լ����������ú���

����6�г���13������GC/MGC/1OZ��HG/MHG��CL/MCL��ZC/MZC��6E/M6E��ES/MES���ٷ�����tick������к˶ԣ��ǼǱ�ȫ����ΪROOT_TEMPLATE_ONLY�����嵽�ں�Լ��vendor definitions��������ʷ������ʵ�ʱ�֤��δȡ�ã���������˫�غ�����ɡ�

1OZ��2025-01-13��MZC��2025-02-24��MHG��2022-05-02�����У����ܲ�������ǰС��Լ�ɽ����������ֱ��۵�ÿ���۵�λ��Ԫ����ΪZC50��MZC5��1OZ��2025�Ľ���ʱ�β�������2026����ʱ�Ρ������ͳ�ͻ��������������sources.md��contract_registry.csv��

ʵ�ʼ����Ŀ���ú�ָ������������δ�ҵ�DATABENTO_API_KEY�����޶���ĿĿ¼���ļ���/��Դ�̵���δ�ҵ�����Ȩ�����ڻ����棬��������ֻ��ȯ����ʷ�˿�Ҳû�����ӡ�û��ɨ�û�ȫ����Key����ʷȨ�ޡ�ʵ�ʶ�ȡ���Ч�ں����سɱ�ΪUNKNOWN/δ��֤��δ����֤���������ֽ�֧��0��Ԫ��

�̶�������ȱ����ʵ֤ʱ����ÿ��ÿ��2��Ԫ���裻ֻ����2tick��4tick��˫Ӷ��+4tick�����龰��10%/20%��ʼ��֤���75%ά�ֱ�����Ϊ�����ĳ��������������ʷȯ�̱�֤�����߳ɽ�������۷���ʱת�о��ֽ��Ϊ������δ֤����ʵ���ۻ������е��ˡ���ͬ������ƷǨ�ƺ�������̬��֤�������δʵ�֡�

## 4. ��ʵ����������ȷδ������

| �˻����о� | �ڳ��ƻ����� | ��ĩȨ�� | ������ | ���س� | ʵ��״̬ |
|---|---:|---:|---:|---:|---|
| SPY�������� | $11,500 | $17,203.23 | $5,703.23 | 24.29% | ��ʵ�ɻ������¼��� |
| QQQ�������� | $11,500 | $18,113.89 | $6,613.89 | 34.29% | ��ʵ�ɻ������¼��� |
| F0�����Ӳ֣����������龰 | $11,500 | NA | NA | NA | ��ʵ�ڻ����벻�㣬δִ�� |
| F1��������μӲ֣����������龰 | $11,500 | NA | NA | NA | ��ʵ�ڻ����벻�㣬δִ�� |

SPY/QQQ��1003��NYSE�����գ�2022-01-03ԭʼopen������2025-12-31ԭʼclose��ֵ�������ɡ�ÿ��ί��1��Ԫ������10bp���ֺ�֧�����������˺���һ��������Ͷ�ʣ��ֽ�����Ϣ������Դ�ļ�ǰ���ϣ���䣬�����ֽ𡢹�����Ȩ���������PASS��2022��2023��2024��2025����ͬһ�����˻����ֶ������������ѱ��档

���ڱ�����ĩ�ֲ֣����������������ã�����ĩ��ȫ��������SPYΪ17185.18��QQQΪ18095.08��Ԫ��SPY��δ���˷ֺ�Ӧ��49.83��Ԫ��δ�����ɻ��ֽ�QQQ���ַֺ����Alpaca֤�ݣ�δ������˸��ˣ�˰�ѡ�Ԥ��˰�ͻ��δ�������������ʱ��Ҳδ������֤����׼�����ڻ�����ʵ����

48�����⸶���з��龰Ϊ0��1440��7200��Ԫ��ÿ��0/30/150����ʵ�ʱ��ֲɹ�֧��Ϊ0��û���ڻ�����ɹ��۳���������Ŀ��ӯ����ΪNA�����ܰ�Ԥ�㵱ʵ���˵���Ҳ���ܴ�CAGRֱ�Ӽ�һ�����ñ�����

trades/campaigns/rolls/daily_equity/margin_path/position_sizing_skipsֻ�б�ͷ���京����**û�н�����ʵ�ڻ��ز�**�����ǡ�������ʵ���е���ϸ��ס���performance.csv��ȫ���ڻ���ЧΪNA��������ԭ��û����ʵ�ڻ�����������4�г�/3������/12����/60campaign���������ӡ��س��ż���ѹ���ɱ����г����жȡ�F1������Ƽ���ͬ���ڿ�ͳ�ƾ�δ���ۣ����ܸ�PASS��ʧ��֤����

## 5. ��һ���ͻָ����

������ҪBen���һ�����ã�����worktree��`.streamlit/secrets.toml`��дDATABENTO_API_KEY�����ṩ���˻�������ʷ��ȵ���ʵ����Ч�ں�����ʹ�õ�ȷ�ϣ�Ҳ��ָ�����кϷ��ڻ��ļ�����Դ��û��Ҫ����Alpaca Key��Ҳû�й�����Ȩ��

����ɿ���������ɾ���definitions/����/����/settlement/status�뱨�۵�λ����ʵ��һ��ͨ���ȹ��������뷶Χ�����ݣ�������С��ʵ��������˳���������޾������������ʵ�ڻ�ͳ�����ۡ���ʵ��֤��/�����Լ��ֽ���֤�ݣ����ܽ���verified�ֶθĳ�true��ԭ��Сʱ��ֹΪ2026-09-15 14:47:39 UTC�����������µ��н�ִ����Ȩ�����Զ��ӳ���

�ָ��ĵ�ΪRUNBOOK.md��״̬��TASK_STATE.json������Ȩ����DATA_CAPABILITY.json������/PR��CODE_REFERENCE.json������û������F0��̨����ǰ���˻���M20/U�������У�B1.2�Դ��ڴ����ⵥ���޸���

## ������

�����Ѿ������˿ɲ��ԵĶ����ڻ����̣���û��������ʵ�ڻ�����ز⡣
ȱ��ָ������Key����ʵȨ�޼���Լ�������룬ʹ���г���ʷ������ʱ�޷����ۡ�
ͬ��SPY��QQQ����ʵ�����׼�Ѿ�����������������Ǵ���F0��F1�ĳɼ���
Ŀǰ�Ȳ���˵��ӯ�Ӳ���Ч��Ҳ����˵11500��Ԫ�˻��Ѿ��߱��ɽ����ԡ�
��һ���Ȳ���Ϸ����ݺͱ�Ҫ���룬�����̶���Χ��֤������Ǯ�������Ρ����Զ�תʵ�̡�
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
