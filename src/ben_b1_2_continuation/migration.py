"""Copy an inactive whole round; relocate only the checkpoint archive path.

This is storage maintenance, never a new account or new research protocol.
Original directories, sidecars, abandoned spools and all attempt records remain.
"""
import gc
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from src.ben_b1_2.compact import stream_hash, CanonicalEncoder
from .runtime import read, write, sha, utc, STARTED_AT, DEADLINE, RUN_ID, REPO

C_ROOT=Path('C:/Users/benhe/BenAITradingData/ben-b1-2-continuation-20260914T154608Z')
D_ROOT=Path('D:/BenAITradingData/ben-b1-2-continuation-20260914T154608Z')

def processes():
    command="Get-CimInstance Win32_Process | Where-Object {$_.Name -match '^python' -and $_.CommandLine -match 'src\\.ben_b1_2_continuation\\.runner|run_b12_continuation\\.py'} | Select-Object ProcessId,ParentProcessId,CreationDate,CommandLine | ConvertTo-Json -Compress"
    import json
    raw=subprocess.check_output(['powershell','-NoProfile','-Command',command],text=True).strip()
    result=json.loads(raw) if raw else []
    return result if isinstance(result,list) else [result]

def wait_and_stop():
    """Bounded watcher, stops exactly the known historical writer after a save."""
    end=time.monotonic()+1800
    account=C_ROOT/'account'/RUN_ID
    expected={30292,33240}
    initial=processes()
    if {r['ProcessId'] for r in initial}!=expected:raise ValueError('UNEXPECTED_HISTORICAL_PROCESS_SET')
    while time.monotonic()<end:
        current=processes()
        if {r['ProcessId'] for r in current}!=expected:raise RuntimeError('HISTORICAL_PROCESS_EXITED_BEFORE_PLANNED_MIGRATION')
        if {r['ProcessId']:r['CreationDate'] for r in current}!={r['ProcessId']:r['CreationDate'] for r in initial}:raise RuntimeError('PID_REUSED')
        progress=read(account/'progress.json')
        if progress['processed_day']>='2026-01-23':
            record={'at':utc(),'status':'PLANNED_STOP_AFTER_DURABLE_DATE_FOR_STORAGE_COPY',
                'same_round_started_at':STARTED_AT,'same_deadline':DEADLINE,'processes_before':current,
                'durable_progress_before_stop':progress,'original_and_failed_attempt_records_preserved':True,
                'reason':'Move this continuation copy to available D drive without removing C files',
                'uncommitted_next_day_if_any_will_resume_from_same_checkpoint':True}
            write(C_ROOT/'PLANNED_STORAGE_STOP_REQUEST.json',record)
            subprocess.run(['powershell','-NoProfile','-Command','Stop-Process -Id 30292 -ErrorAction Stop'],check=True)
            for _ in range(30):
                if not processes():break
                time.sleep(1)
            else:raise RuntimeError('HISTORICAL_LAUNCHER_NOT_EXITED_NO_COPY')
            record.update(stopped_at=utc(),processes_after=processes(),durable_progress_after_stop=read(account/'progress.json'))
            write(C_ROOT/'PLANNED_STORAGE_STOP.json',record)
            old=read(C_ROOT/'RUNNER_PROCESS.json')
            write(C_ROOT/'RUNNER_PROCESS_before_storage_stop.json',old)
            write(C_ROOT/'RUNNER_PROCESS.json',{**old,'status':'STOPPED','finished_at':utc(),'stop_kind':'PLANNED_STORAGE_MIGRATION','natural_exit':False})
            write(C_ROOT/'TASK_STATE_before_storage_stop.json',read(C_ROOT/'TASK_STATE.json'))
            write(C_ROOT/'TASK_STATE.json',{'stage':'PLANNED_STORAGE_MIGRATION_STOP','at':utc(),'research_running':False,'persistent_research_worker_running':False,'started_at':STARTED_AT,'deadline_utc':DEADLINE})
            print(record,flush=True);return
        time.sleep(2)
    raise TimeoutError('BOUNDED_MIGRATION_WATCH_NO_DURABLE_DAY')

def fingerprint(path):
    st=path.stat();return {'bytes':st.st_size,'mtime_ns':st.st_mtime_ns,'sha256':sha(path)}

def copy_inactive():
    if processes():raise ValueError('HISTORICAL_WRITER_STILL_RUNNING')
    if read(C_ROOT/'RUNNER_PROCESS.json')['status']!='STOPPED':raise ValueError('WRITER_STATUS_NOT_STOPPED')
    if D_ROOT.exists():raise ValueError('TARGET_EXISTS_NO_OVERWRITE')
    # All files, including checkpoint, WAL/SHM when present, and failed spools.
    paths=sorted(p for p in C_ROOT.rglob('*') if p.is_file())
    total=sum(p.stat().st_size for p in paths)
    if shutil.disk_usage(D_ROOT.anchor).free<total+10*1024**3:raise OSError('DESTINATION_CAPACITY')
    D_ROOT.mkdir(parents=True,exist_ok=False)
    manifest=[]
    for i,source in enumerate(paths):
        before=fingerprint(source);dest=D_ROOT/source.relative_to(C_ROOT)
        dest.parent.mkdir(parents=True,exist_ok=True)
        with source.open('rb') as src,dest.open('xb') as dst:shutil.copyfileobj(src,dst,1024*1024)
        shutil.copystat(source,dest)
        copied=fingerprint(dest);after=fingerprint(source)
        if before!=after or before!=copied:raise ValueError('FULL_COPY_FINGERPRINT_MISMATCH:'+str(source))
        manifest.append({'relative_path':source.relative_to(C_ROOT).as_posix(),'before':before,'after':after,'copy':copied})
        if i%100==0:print({'phase':'INACTIVE_WHOLE_ROUND_COPY','files_done':i+1,'files_total':len(paths)},flush=True)
    if processes():raise ValueError('WRITER_REAPPEARED_DURING_COPY')
    # Recheck every source after all copying, not just immediately after one file.
    for row in manifest:
        if fingerprint(C_ROOT/row['relative_path'])!=row['before']:raise ValueError('SOURCE_CHANGED_AFTER_COPY')
    write(D_ROOT/'STORAGE_MIGRATION_COPY.json',{'at':utc(),'status':'PASS','source':str(C_ROOT),'destination':str(D_ROOT),
        'originals_deleted':False,'all_existing_sqlite_related_files_included':True,'source_database_opened':False,
        'source_full_fileset_unchanged':True,'file_count':len(paths),'logical_bytes':total,'files':manifest,
        'processes_absent_after_copy':True,'same_deadline':DEADLINE})
    print({'status':'PASS','file_count':len(paths),'logical_bytes':total},flush=True)

def relocate_checkpoint(checkpoint,expected_old_archive,new_archive):
    wrapper=read(checkpoint);payload=wrapper['payload']
    original=stream_hash(payload)
    if original!=wrapper['sha256']:raise ValueError('MIGRATION_ORIGINAL_WRAPPER_HASH')
    archive=payload['state']['archive']
    old=archive['path']
    if Path(old).resolve()!=Path(expected_old_archive).resolve():raise ValueError('UNEXPECTED_ARCHIVE_SOURCE')
    original_fields={k:stream_hash(v) for k,v in payload.items()}
    original_state={k:stream_hash(v) for k,v in payload['state'].items()}
    archive['path']=str(Path(new_archive).resolve())
    expected_fields={k:stream_hash(v) for k,v in payload.items()}
    expected_state={k:stream_hash(v) for k,v in payload['state'].items()}
    digest=stream_hash(payload)
    archive['path']=old
    if stream_hash(payload)!=original:raise ValueError('NON_PATH_MIGRATION_MUTATION')
    archive['path']=str(Path(new_archive).resolve());wrapper['sha256']=digest
    tmp=checkpoint.with_suffix('.storage-relocated.tmp')
    with tmp.open('w',encoding='utf-8',newline='\n') as f:
        for piece in CanonicalEncoder().iterencode(wrapper):f.write(piece)
        f.flush();os.fsync(f.fileno())
    os.replace(tmp,checkpoint)
    return {'original_wrapper_sha256':original,'relocated_wrapper_sha256':digest,
        'original_payload_hashes':original_fields,'original_state_hashes':original_state,
        'expected_payload_hashes':expected_fields,'expected_state_hashes':expected_state,
        'only_archive_path_changed':True,'original_archive_path':old,'new_archive_path':str(new_archive),
        'cash':payload['ledger']['state']['cash'],'completed_day':payload['state']['b12_completed_day']['day'],
        'last_event_at':payload['state']['at'],'events_processed':payload['state']['events_processed'],
        'archive_anchor':{k:archive[k] for k in ('applied_sequence','applied_hash','archived_events','archived_zero_consumption_inventories')},
        'checkpoint_file_sha256':sha(checkpoint)}

def prepare():
    if processes():raise ValueError('WRITER_ACTIVE')
    proof=D_ROOT/'engineering/STORAGE_MIGRATION_EXPECTED.json'
    if proof.exists():raise ValueError('MIGRATION_ALREADY_PREPARED')
    account=D_ROOT/'account'/RUN_ID;cp=account/'checkpoint.json'
    copy=read(D_ROOT/'STORAGE_MIGRATION_COPY.json')
    if copy['status']!='PASS':raise ValueError('FULL_COPY_NOT_PASS')
    before=sha(cp)
    row=next(r for r in copy['files'] if r['relative_path']==cp.relative_to(D_ROOT).as_posix())
    if before!=row['copy']['sha256']:raise ValueError('COPIED_CHECKPOINT_CHANGED')
    progress=read(account/'progress.json')
    checkpoint_day=read(cp)['payload']['state']['b12_completed_day']['day']
    if checkpoint_day!=progress['processed_day']:raise ValueError('MIGRATION_REQUIRES_MATCHED_DURABLE_CURSOR')
    result=relocate_checkpoint(cp,C_ROOT/'account'/RUN_ID/'replay_archive.sqlite',account/'replay_archive.sqlite')
    if result['completed_day']!=progress['processed_day']:raise ValueError('MIGRATION_REQUIRES_MATCHED_DURABLE_CURSOR')
    write(proof,{'at':utc(),'status':'PASS_ONLY_PATH_RELOCATED_PENDING_FULL_RESTORE','same_start':STARTED_AT,'same_deadline':DEADLINE,
        'before_checkpoint_file_sha256':before,'copy_proof_sha256':sha(D_ROOT/'STORAGE_MIGRATION_COPY.json'),
        'events_executed':0,**result})
    print({'status':'PREPARED','day':result['completed_day'],'cash':result['cash']},flush=True)

def verify_migrated_engine(engine,root):
    """Called after real full archive restore and before any new dated input."""
    proof=root/'engineering/STORAGE_MIGRATION_EXPECTED.json'
    if not proof.exists():
        if root.resolve()==D_ROOT.resolve():raise ValueError('MIGRATION_EXPECTED_PROOF_MISSING')
        return
    expected=read(proof);dest=root/'engineering/STORAGE_MIGRATION_RESTORE.json'
    temp=Path(tempfile.gettempdir()).resolve()
    if temp!= (root/'_sqlite_tmp').resolve():raise ValueError('SQLITE_PROCESS_TEMP_NOT_RELOCATED')
    if dest.exists():
        old=read(dest)
        if old['status']!='PASS' or old['expected_proof_sha256']!=sha(proof):raise ValueError('PRIOR_STORAGE_RESTORE_NOT_PASS')
        if engine.state['b12_completed_day']['day']>expected['completed_day']:
            anchor=expected['archive_anchor']
            if engine.state['archive']['applied_sequence']<anchor['applied_sequence']:raise ValueError('MIGRATION_PREFIX_NOT_CLAIMED_BY_CHECKPOINT')
            row=engine._db.execute('SELECT block_hash FROM archive_blocks WHERE sequence=?',(anchor['applied_sequence'],)).fetchone()
            if not row or row[0]!=anchor['applied_hash']:raise ValueError('MIGRATION_PREFIX_ANCHOR_CHANGED')
            return
    if engine.state_digest()!=expected['relocated_wrapper_sha256']:raise ValueError('MIGRATED_RESTORE_STATE_DIGEST_MISMATCH')
    actual_payload={k:stream_hash(v) for k,v in engine._payload().items()}
    actual_state={k:stream_hash(v) for k,v in engine.state.items()}
    if actual_payload!=expected['expected_payload_hashes'] or actual_state!=expected['expected_state_hashes']:raise ValueError('MIGRATED_RESTORE_COMPONENT_MISMATCH')
    if dest.exists():return  # The first actual PASS time and evidence stay immutable.
    write(dest,{'at':utc(),'status':'PASS','expected_proof_sha256':sha(proof),'all_archive_hashes_verified_by_actual_restore':True,
        'payload_hashes':actual_payload,'state_hashes':actual_state,'cash_positions_costs_stop_legs_orders_reserves_settlements_consumed_quotes_preserved':True,
        'events_executed_before_verification':0,'completed_day':expected['completed_day'],'cash':engine.ledger.cash,
        'positions':{k:v['quantity'] for k,v in engine.ledger.positions.items()},'temp_directory':str(temp),
        'same_run_id':engine.config.run_id,'same_deadline':DEADLINE})

def freeze():
    """Bind immutable copy/relocation evidence; real restore is an entry gate."""
    import xml.etree.ElementTree as ET
    from . import runtime
    if processes():raise ValueError('WRITER_ACTIVE_DURING_MIGRATION_FREEZE')
    if runtime.ROOT!=D_ROOT:raise ValueError('RUNTIME_ROOT_NOT_MIGRATED')
    dest=D_ROOT/'ENGINEERING_GATE_revision_3.json'
    if dest.exists():raise ValueError('MIGRATION_GATE_ALREADY_EXISTS')
    previous=C_ROOT/'ENGINEERING_GATE_revision_2.json';old=read(previous)
    changes=[];files={}
    permitted={str(REPO/'src/ben_b1_2_continuation'/n) for n in ('runtime.py','runner.py','execution.py')}
    permitted.add(str(REPO/'src/ben_b1/replay.py'))
    for name,expected in old['files'].items():
        actual=sha(name)
        if actual!=expected:
            if name not in permitted:raise ValueError('NON_MIGRATION_SOURCE_OR_PROOF_CHANGED:'+name)
            changes.append({'path':name,'before_sha256':expected,'after_sha256':actual})
        files[name]=actual
    if {r['path'] for r in changes}!=permitted:raise ValueError('UNEXPECTED_STORAGE_PATCH_SCOPE')
    expected=read(D_ROOT/'engineering/STORAGE_MIGRATION_EXPECTED.json')
    if expected['status']!='PASS_ONLY_PATH_RELOCATED_PENDING_FULL_RESTORE':raise ValueError('RELOCATION_NOT_PASS')
    required_tests=[(D_ROOT/'engineering/storage_migration_unit_v3.xml',6),(D_ROOT/'engineering/storage_migration_integration.xml',55)]
    for xml,count in required_tests:
        suite=ET.parse(xml).getroot().find('testsuite')
        if suite is None or int(suite.attrib['tests'])!=count or any(int(suite.attrib[k]) for k in ('failures','errors','skipped')):raise ValueError('MIGRATION_REGRESSION_NOT_PASS')
    dense=D_ROOT/'engineering/STORAGE_DENSE_REAL_EQUIVALENCE.json';crash=D_ROOT/'engineering/STORAGE_REAL_CRASH_RECOVERY.json'
    if read(dense).get('status')!='PASS' or read(crash).get('status')!='PASS':raise ValueError('PHYSICAL_STORAGE_REAL_ENGINEERING_NOT_PASS')
    for p in [previous,D_ROOT/'STORAGE_MIGRATION_COPY.json',D_ROOT/'engineering/STORAGE_MIGRATION_EXPECTED.json',
              D_ROOT/'PLANNED_STORAGE_STOP.json',Path(__file__),REPO/'src/ben_b1_2_continuation/storage_check.py',
              REPO/'src/ben_b1_2_continuation/rollback.py',REPO/'tests/test_ben_b1_2_storage_rollback.py',
              REPO/'tests/test_ben_b1_2_storage_migration.py',REPO/'scripts/run_b12_continuation.py',dense,crash]+[p for p,n in required_tests]:
        files[str(p)]=sha(p)
    write(dest,{**old,'at':utc(),'revision':3,'status':'PASS','files':files,'changes':changes,
        'same_authorization_start':STARTED_AT,'same_hard_deadline':DEADLINE,
        'prior_gate':{'path':str(previous),'sha256':sha(previous)},'same_account_and_run_id':RUN_ID,
        'storage_copy_and_payload_relocation_proved':True,'mandatory_actual_archive_restore_and_component_match_before_new_dates':True,
        'financial_decision_fee_inventory_and_event_order_rules_unchanged':True,'migration_regression_cases':55,
        'storage_and_transaction_snapshot_implementation_changed_with_real_equivalence_proof':True,
        'migration_unit_cases_repeated_in_integration':6,
        'scope':'D drive relocation; mandatory migration-state verification; TEMP WITHOUT ROWID, bounded TEMP cache, ascending-ID physical archive inserts; QUOTE-only immutable record value memo with all mutable containers/financial branches copied'})
    write(D_ROOT/'ACTIVE_ENGINEERING_GATE.json',{'path':str(dest),'sha256':sha(dest),'original_gate_sha256':sha(D_ROOT/'ENGINEERING_GATE.json')})
    print({'status':'PASS','gate':str(dest),'real_migration_restore':'REQUIRED_BEFORE_ANY_NEW_DATED_EVENT'},flush=True)

if __name__=='__main__':
    import sys
    {'watch':wait_and_stop,'copy':copy_inactive,'prepare':prepare,'freeze':freeze}[sys.argv[1]]()
