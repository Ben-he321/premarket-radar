"""Public whitelist only, with original immutable evidence preservation QA."""
import hashlib
import zipfile
from pathlib import Path
from .runtime import *

ACCOUNT_FILES=['RUN_SPEC.json','progress.json','COMMON_CLOCK.json','QUOTE_COVERAGE.json','CLOSE_VALUATIONS.json',
    'DYNAMIC_INPUT_HASHES.json','QUARTER_END_ACCOUNT.json','QUARTER_END_LEDGER.json','QUARTER_END_CLOSE_VALUATION.json',
    'FINAL_ACCOUNT.json','RECOVERY_IDEMPOTENCY.json','ARCHIVE_MANIFEST.json','summary.json',
    'orders.csv','fills.csv','campaigns.csv','account_events.csv','data_gaps.csv','daily_equity.csv','continuous_close_equity.csv','event_trace.csv']

def recheck_sealed_storage_source(proof_path,output):
    proof=read(proof_path);source=Path(proof['source']);files=proof['files'];rows=[]
    if proof['status']!='PASS' or source.resolve()==ROOT.resolve():raise ValueError('INVALID_STORAGE_SOURCE_PROOF')
    expected_paths={r['relative_path'] for r in files}
    actual_paths={p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()}
    for row in files:
        p=source/row['relative_path'];expected=row['after'];actual=None
        if p.is_file():
            before=p.stat();digest=sha(p);after=p.stat()
            actual={'bytes':after.st_size,'mtime_ns':after.st_mtime_ns,'sha256':digest}
            stable=(before.st_size,before.st_mtime_ns)==(after.st_size,after.st_mtime_ns)
        else:stable=False
        rows.append({'relative_path':row['relative_path'],'expected':expected,'actual':actual,
                     'unchanged':stable and actual==expected})
    result={'at':utc(),'status':'PASS' if actual_paths==expected_paths and all(r['unchanged'] for r in rows) else 'FAIL',
            'source':str(source),'source_proof_sha256':sha(proof_path),'full_file_rehash':True,
            'source_database_opened':False,'checked_files':len(rows),'file_set_unchanged':actual_paths==expected_paths,
            'missing_files':sorted(expected_paths-actual_paths),'extra_files':sorted(actual_paths-expected_paths),'files':rows}
    write(output,result)
    if result['status']!='PASS':raise ValueError('SEALED_C_STORAGE_SOURCE_CHANGED')
    return result

def recheck_inputs_and_execution():
    results=[]
    sources={'DYNAMIC_INPUT_HASHES':read(ACCOUNT/'DYNAMIC_INPUT_HASHES.json'),
             'ENGINEERING_GATE':read(active_gate_path())['files']}
    for group,files in sources.items():
        for name,expected in files.items():
            p=Path(name);actual=sha(p) if p.is_file() else None
            results.append({'group':group,'path':name,'expected_sha256':expected,'actual_sha256':actual,'matches':actual==expected})
    proof={'at':utc(),'status':'PASS' if all(r['matches'] for r in results) else 'FAIL','full_file_rehash':True,
           'no_size_mtime_only_shortcut':True,'checked_files':len(results),'files':results}
    write(ROOT/'final/FINAL_INPUT_HASH_RECHECK.json',proof)
    if proof['status']!='PASS':raise ValueError('FINAL_INPUT_OR_EXECUTION_HASH_MISMATCH')
    return proof

def run():
    if read(ROOT/'RUNNER_PROCESS.json')['status']!='STOPPED':raise ValueError('NO_PACKAGE_WHILE_WRITER_ACTIVE')
    if not (ROOT/'final/ACTUAL_COMPLETION.json').exists():raise ValueError('FINAL_RECONCILIATION_REQUIRED')
    completion=read(ROOT/'final/ACTUAL_COMPLETION.json')
    recheck_inputs_and_execution()
    if (ROOT/'STORAGE_MIGRATION_COPY.json').exists():
        recheck_sealed_storage_source(ROOT/'STORAGE_MIGRATION_COPY.json',ROOT/'final/STORAGE_COPY_SOURCE_PRESERVATION_FINAL.json')
    manifest={};missing=[];protected=[]
    before=read(ROOT/'ORIGINAL_ACCOUNT_BEFORE.json')
    for relative,expected in before.items():
        p=SOURCE_ACCOUNT/relative
        ok=p.exists() and sha(p)==expected['sha256'] and p.stat().st_size==expected['bytes'] and p.stat().st_mtime_ns==expected['mtime_ns']
        protected.append({'path':str(p),'unchanged':ok})
    for account,files in read(ROOT/'COMPLETED_THREE_PRESERVATION_BEFORE.json').items():
        for path,expected in files.items():
            p=Path(path);ok=p.exists() and sha(p)==expected['sha256'] and p.stat().st_size==expected['bytes'] and p.stat().st_mtime_ns==expected['mtime_ns']
            protected.append({'path':str(p),'unchanged':ok})
    preservation={'at':utc(),'status':'PASS' if all(r['unchanged'] for r in protected) else 'FAIL','checked_files':len(protected),'files':protected}
    write(ROOT/'final/OLD_ACCOUNT_PRESERVATION_FINAL.json',preservation)
    if preservation['status']!='PASS':raise ValueError('ORIGINAL_ACCOUNT_PRESERVATION_FAILED')
    def add(path,name,required=True):
        path=Path(path)
        if not path.exists():
            if required:missing.append(name)
            return
        lowered=str(path).lower()
        if any(token in lowered for token in ('.streamlit','secrets.toml','/.env','\\.env')) or path.suffix.lower() in ('.sqlite','.db','.parquet','.wal','.shm') or 'checkpoint' in path.name.lower() or path.name.endswith(('-wal','-shm')):
            raise ValueError('FILE_OUTSIDE_PUBLIC_WHITELIST:'+name)
        manifest[name]={'path':str(path),'bytes':path.stat().st_size,'sha256':sha(path)}
    for p in (ROOT/'final').iterdir():
        if p.is_file() and p.suffix in ('.md','.json','.csv'):add(p,'results/'+p.name)
    for name in ['AUTHORIZATION.json','BACKUP_VERIFICATION.json','ENGINEERING_GATE.json','CODE_DELIVERY.json',
        'CONTINUATION_RUN_SPEC.json','TASK_STATE.json','LIVE_PROGRESS.json','RUNNER_PROCESS.json','RESOURCE_PROFILE.jsonl','STAGE_LOG.jsonl',
        'M20_U_PROCESS_PRESERVATION_BEFORE_RESUME.json','M20_U_PROCESS_PRESERVATION_AFTER.json','STOP_RECORD.json']:
        add(ROOT/name,'continuation/'+name,required=name!='STOP_RECORD.json')
    for p in (ROOT/'engineering').iterdir():
        if p.is_file() and p.suffix in ('.json','.xml','.md','.log','.txt'):
            add(p,'engineering/'+p.name)
    for mode in ('dense_original','dense_bounded'):
        for name in ('BLOCK_PROOFS.json','ENGINEERING_RUN.json','first10000_real_quotes_profile.txt'):
            add(ROOT/'engineering'/mode/name,'engineering/'+mode+'/'+name,required=name!='first10000_real_quotes_profile.txt')
    for label,code in [('Q0_A','Q0_P50_A'),('Q1_A','Q1_P50_A'),('Q0_B','Q0_P50_B'),('Q1_B','Q1_P50_B')]:
        path=ACCOUNT if label=='Q1_B' else OLD/'portfolio/compact_base_v1'/('B12_'+code+'_compact_base_v1')
        result=next(r for r in completion['accounts'] if r['account']==label.replace('_','/'))
        tail=next(r for r in completion['tails'] if r['account']==label.replace('_','/'))
        required={'RUN_SPEC.json','progress.json','COMMON_CLOCK.json','QUOTE_COVERAGE.json','CLOSE_VALUATIONS.json','DYNAMIC_INPUT_HASHES.json'}
        if result['quarter_complete']:required.update(('QUARTER_END_ACCOUNT.json','QUARTER_END_LEDGER.json','QUARTER_END_CLOSE_VALUATION.json'))
        if tail['tail_complete']:required.update(('FINAL_ACCOUNT.json','RECOVERY_IDEMPOTENCY.json','fills.csv','campaigns.csv','orders.csv'))
        for name in ACCOUNT_FILES:add(path/name,'accounts/'+label+'/'+name,required=name in required)
    for name in ['BENCHMARKS.md','RUN_SPEC.json','SUMMARY.json','INPUT_MANIFEST.json','RTH_OPEN_VERIFICATION.json','COMPLETE.json']:
        add(OLD/'benchmarks/frozen_v1'/name,'existing/benchmarks/'+name)
    for symbol in ('SPY','QQQ'):
        for name in ('ACCOUNT.json','daily.csv','fills.csv','dividends.csv','cashflows.csv','order_decisions.csv'):
            add(OLD/'benchmarks/frozen_v1'/symbol/name,'existing/benchmarks/'+symbol+'/'+name)
    for name in ('BEN_B1_2_RESULTS.md','B12_PROTOCOL.json'):
        add(OLD/name,'existing/original_B1_2/'+name)
    for p in (REPO/'src/ben_b1_2_continuation').glob('*.py'):add(p,'code/src/ben_b1_2_continuation/'+p.name)
    for name in read(active_gate_path())['files']:
        p=Path(name)
        if p.is_relative_to(REPO) and p.suffix=='.py':add(p,'code/'+str(p.relative_to(REPO)).replace('\\','/'))
    for p in (REPO/'docs/BEN_B1_2_CONTINUATION.md',REPO/'tests/test_ben_b1_2_continuation.py',REPO/'tests/test_ben_b1_2_continuation_report.py',REPO/'tests/test_ben_b1_2_continuation_io.py'):
        add(p,'code/'+str(p.relative_to(REPO)).replace('\\','/'))
    for p in ROOT.glob('ENGINEERING_GATE_revision_*.json'):add(p,'continuation/'+p.name)
    for pattern in ('STOP_RECORD_*.json','RUNNER_EXIT_*.json'):
        for p in ROOT.glob(pattern):add(p,'continuation/'+p.name)
    add(ROOT/'ACTIVE_ENGINEERING_GATE.json','continuation/ACTIVE_ENGINEERING_GATE.json',required=False)
    for pattern in ('STORAGE_MIGRATION*.json','PLANNED_STORAGE*.json','PLANNED_QUERY_FIX*.json','RUNNER_PROCESS_before_storage_stop.json','TASK_STATE_before_storage_stop.json'):
        for p in ROOT.glob(pattern):add(p,'continuation/'+p.name)
    for name in ('STORAGE_WAIT_BOUNDARY_20260914T1846Z.json','migration_watch.log','continuation_runner_02.log','continuation_runner_03.log'):
        add(ROOT/name,'continuation/'+name)
    for p in ROOT.glob('continuation_runner_*.log'):add(p,'continuation/'+p.name)
    for p in (REPO/'tests').glob('test_ben_b1_2_storage_*.py'):add(p,'code/tests/'+p.name)
    add(REPO/'tests/test_ben_b1_2_report_support.py','code/tests/test_ben_b1_2_report_support.py')
    add(REPO/'tests/test_ben_b1_2_package_preservation.py','code/tests/test_ben_b1_2_package_preservation.py')
    add(REPO/'scripts/run_b12_continuation.py','code/scripts/run_b12_continuation.py')
    for folder in ('storage_revision3','query_revision4'):
        for p in (ROOT/'engineering'/folder).rglob('*.json'):
            if 'checkpoint' not in p.name.lower() and p.name in ('BLOCK_PROOFS.json','ENGINEERING_RUN.json','DENSE_REAL_EQUIVALENCE.json','REAL_DENSE_CRASH_RECOVERY.json'):
                add(p,'engineering/'+folder+'/'+p.relative_to(ROOT/'engineering'/folder).as_posix())
    for p in (ROOT/'attempts').rglob('*'):
        if p.is_file() and p.suffix in ('.json','.jsonl','.log','.py') and 'checkpoint' not in p.name.lower():
            add(p,'attempts/'+str(p.relative_to(ROOT/'attempts')).replace('\\','/'))
    dest=ROOT/'verification_ben_b1_2_window_portfolio_bundle.zip'
    if dest.exists():raise ValueError('NEW_ROUND_BUNDLE_ALREADY_EXISTS_NO_SILENT_OVERWRITE')
    index={'at':utc(),'status':'COMPLETE_WHITELIST' if not missing else 'PACKAGE_INCOMPLETE','entries':manifest,'missing_required':missing,'old_bundle_preserved_at':str(OLD/dest.name),
        'excluded':['all market Parquet and raw pages','complete checkpoints','SQLite/WAL/SHM','private full recovery backup','credentials'],
        'private_initial_backup':str(ROOT/'private_backup/full_inactive_recovery.zip')}
    write(ROOT/'PUBLIC_BUNDLE_MANIFEST.json',index)
    with zipfile.ZipFile(dest,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name,item in manifest.items():z.write(item['path'],name)
        z.write(ROOT/'PUBLIC_BUNDLE_MANIFEST.json','PUBLIC_BUNDLE_MANIFEST.json')
    with zipfile.ZipFile(dest) as z:
        bad=z.testzip()
        if bad:raise ValueError('ZIP_CRC_FAILED:'+bad)
        for name,item in manifest.items():
            with z.open(name) as f:actual=hashlib.file_digest(f,'sha256').hexdigest()
            if actual!=item['sha256']:raise ValueError('ZIP_CONTENT_HASH_FAILED:'+name)
    delivery={'at':utc(),'package_status':index['status'],'zip':str(dest),'bytes':dest.stat().st_size,'sha256':sha(dest),'entries':len(manifest)+1,
        'all_whitelisted_contents_hash_verified':True,'missing_required':missing,'original_account_preservation':'PASS',
        'full_quarter_and_tail_complete':completion['full_quarter_and_tail_complete'],'complete_delivery':not missing and completion['full_quarter_and_tail_complete']}
    write(ROOT/'DELIVERY.json',delivery);print(delivery,flush=True)

if __name__=='__main__':run()
