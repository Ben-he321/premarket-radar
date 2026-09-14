"""Seal the inactive original as a full private backup, then copy, never reset."""
import hashlib
import json
import shutil
import subprocess
import zipfile
from .runtime import *

def stat(p):
    s=p.stat();return {'bytes':s.st_size,'mtime_ns':s.st_mtime_ns,'sha256':sha(p)}
def main():
    ROOT.mkdir(parents=True,exist_ok=True)
    if (ROOT/'BACKUP_VERIFICATION.json').exists():raise RuntimeError('EXISTING_BACKUP_REVIEW_REQUIRED_NO_OVERWRITE')
    ps="Get-CimInstance Win32_Process | Where-Object {$_.Name -match '^python' -and $_.CommandLine -match 'src\\.ben_b1_2\\.(portfolio|samples)|PARTIAL_ISOLATED_RESTORE'} | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    existing=subprocess.check_output(['powershell','-NoProfile','-Command',ps],text=True).strip()
    if existing and json.loads(existing):raise RuntimeError('ORIGINAL_REPLAY_OR_RESTORE_IS_RUNNING')
    authorization={'started_at':STARTED_AT,'deadline_utc':DEADLINE,'hours':8,'new_round':True,
        'old_round_untouched':True,'original_account':str(SOURCE_ACCOUNT),'same_run_id':RUN_ID,
        'resume_next_session':'2026-01-23','principal_not_reset':5500,'cash_to_preserve':87.92,
        'positions_to_preserve':{'BE':27,'NVDA':14},'main_executor_only':True,'read_only_reviewers_allowed':True,
        'no_new_history_before_recovery_dense_equivalence_idempotence_interruption_gate':True,
        'prohibited':['new_strategy','parameter_search','new_account_stitching','paid_service','paid_model_API','broker','M20_U_mutation'],
        'worktree':str(REPO),'base_commit':'00c11f9396227551216cf2f16caedab662ff1e0b'}
    write(ROOT/'AUTHORIZATION.json',authorization)
    status('FULL_INACTIVE_RECOVERY_BACKUP_STARTED',research_running=False,resources=guard())
    paths=sorted(p for p in SOURCE_ACCOUNT.iterdir() if p.is_file())
    required={'checkpoint.json','replay_archive.sqlite','replay_archive.sqlite-wal','replay_archive.sqlite-shm','RUN_SPEC.json','DYNAMIC_INPUT_HASHES.json','progress.json'}
    assert required <= {p.name for p in paths}
    before={p.name:stat(p) for p in paths}
    write(ROOT/'ORIGINAL_ACCOUNT_BEFORE.json',before)
    # Other three finished accounts remain references; protect their exact bytes.
    finished={}
    for name in ('B12_Q0_P50_A_compact_base_v1','B12_Q1_P50_A_compact_base_v1','B12_Q0_P50_B_compact_base_v1'):
        parent=SOURCE_ACCOUNT.parent/name
        assert read(parent/'RECOVERY_IDEMPOTENCY.json')['status']=='PASS'
        finished[name]={str(p):stat(p) for p in sorted(parent.rglob('*')) if p.is_file()}
    write(ROOT/'COMPLETED_THREE_PRESERVATION_BEFORE.json',finished)
    backup=ROOT/'private_backup/full_inactive_recovery.zip';backup.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(backup,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=1,allowZip64=True) as z:
        for p in paths:
            guard();z.write(p,p.name)
    after={p.name:stat(p) for p in paths}
    assert before==after, 'ORIGINAL_CHANGED_DURING_PRIVATE_BACKUP'
    ACCOUNT.mkdir(parents=True,exist_ok=False)
    copies={}
    with zipfile.ZipFile(backup) as z:
        assert sorted(z.namelist())==sorted(before)
        for item in z.infolist():
            assert Path(item.filename).name==item.filename and item.filename in before
            guard();dest=ACCOUNT/item.filename
            with z.open(item) as f,dest.open('xb') as out:shutil.copyfileobj(f,out,1024*1024)
            copies[item.filename]={'bytes':dest.stat().st_size,'sha256':sha(dest)}
            assert copies[item.filename]['sha256']==before[item.filename]['sha256']
    result={'at':utc(),'status':'PASS_FULL_INACTIVE_FILESET_PRIVATE_BACKUP_AND_COPY',
        'original_before':before,'original_after':after,'copies':copies,'private_zip':str(backup),'private_zip_sha256':sha(backup),
        'private_zip_bytes':backup.stat().st_size,'includes_wal_shm_checkpoint':True,'original_sqlite_connected':False,
        'database_recovery_semantics_verified':False,'checkpoint_archive_path_not_yet_changed':True,'research_events_executed':0}
    write(ROOT/'BACKUP_VERIFICATION.json',result)
    status('FULL_BACKUP_VERIFIED_PENDING_RESTORE',backup_bytes=backup.stat().st_size,copied_files=len(copies),research_running=False)
if __name__=='__main__':main()
