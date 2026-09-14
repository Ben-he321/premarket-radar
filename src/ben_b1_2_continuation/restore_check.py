"""Actual full-prefix restore proof on the copied original; no new events."""
import ctypes
import gc
import os
import time
import traceback
import pandas_market_calendars as mcal
from src.ben_b1_2.compact import stream_hash, CanonicalEncoder
from .engine import StreamingSharedEngine
from .runtime import *

def process_memory():
    class Counters(ctypes.Structure):
        _fields_=[('cb',ctypes.c_ulong),('faults',ctypes.c_ulong)]+[(n,ctypes.c_size_t) for n in ('peak','rss','peakpaged','paged','peaknonpaged','nonpaged','pagefile','peakpagefile')]
    c=Counters();c.cb=ctypes.sizeof(c)
    if not ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(-1),ctypes.byref(c),ctypes.sizeof(c)):raise OSError('Process memory unavailable')
    return {'rss_mib':c.rss/1024**2,'peak_rss_mib':c.peak/1024**2}

def main():
    result_path=ROOT/'engineering/REAL_PREFIX_RESTORE.json'
    if result_path.exists():raise RuntimeError('EXISTING_RESTORE_EVIDENCE_NO_AUTOMATIC_RETRY')
    backup=read(ROOT/'BACKUP_VERIFICATION.json');assert backup['status'].startswith('PASS_')
    cp=ACCOUNT/'checkpoint.json';before=sha(cp)
    assert before==backup['original_before']['checkpoint.json']['sha256']
    wrapper=read(cp);payload=wrapper['payload'];original_digest=stream_hash(payload)
    assert wrapper['sha256']==original_digest
    archive=payload['state']['archive'];old_path=archive['path']
    assert Path(old_path).resolve()==(SOURCE_ACCOUNT/'replay_archive.sqlite').resolve()
    archive['path']=str((ACCOUNT/'replay_archive.sqlite').resolve())
    relocated_digest=stream_hash(payload)
    archive['path']=old_path;assert stream_hash(payload)==original_digest
    archive['path']=str((ACCOUNT/'replay_archive.sqlite').resolve())
    expected={key:stream_hash(value) for key,value in payload.items()}
    expected_state={key:stream_hash(value) for key,value in payload['state'].items()}
    wrapper['sha256']=relocated_digest
    tmp=cp.with_suffix('.relocated.tmp')
    with tmp.open('w',encoding='utf-8',newline='\n') as f:
        for piece in CanonicalEncoder().iterencode(wrapper):f.write(piece)
        f.flush();os.fsync(f.fileno())
    os.replace(tmp,cp)
    result={'at':utc(),'status':'RUNNING','events_run':0,'new_historical_dates_executed':False,
        'original_wrapper_sha256':original_digest,'relocated_wrapper_sha256':relocated_digest,
        'only_archive_path_changed':True,'original_archive_path':old_path,'copy_archive_path':archive['path'],
        'original_checkpoint_file_sha256':before,'relocated_checkpoint_file_sha256':sha(cp),
        'expected_payload_hashes':expected,'expected_state_component_hashes':expected_state,
        'streaming_engine_sha256':sha(REPO/'src/ben_b1_2_continuation/engine.py'),
        'resource_limit':{'free_ram_min_mib':800,'max_round_deadline':DEADLINE},'progress':[]}
    write(result_path,result)
    del wrapper,payload,archive;gc.collect()
    start=time.monotonic()
    def hook(progress):
        resources_now=guard()
        row={**progress,'at':utc(),**process_memory(),**resources_now}
        result['progress'].append(row);write(result_path,result)
        print(json.dumps(row),flush=True)
    StreamingSharedEngine.verification_hook=staticmethod(hook)
    engine=None
    try:
        status('ACTUAL_ORIGINAL_PREFIX_STREAM_RESTORE',research_running=False,restore_pid=os.getpid())
        engine=StreamingSharedEngine.restore(cp,mcal.get_calendar('NYSE').schedule('2025-01-01','2026-12-31'))
        got=engine._payload()
        result['actual_payload_hashes']={k:stream_hash(v) for k,v in got.items()}
        result['actual_state_component_hashes']={k:stream_hash(v) for k,v in engine.state.items()}
        assert result['actual_payload_hashes']==expected
        assert result['actual_state_component_hashes']==expected_state
        assert engine.state_digest()==relocated_digest
        assert engine.config.run_id==RUN_ID and engine.ledger.cash==87.92
        positions={k:v['quantity'] for k,v in engine.ledger.positions.items()}
        assert positions=={'BE':27,'NVDA':14}
        assert engine.state['events_processed']==5602986 and engine.state['b12_completed_day']['day']=='2026-01-22'
        result.update(status='PASS_COMPLETE_ORIGINAL_PREFIX_RESTORED_WITH_IDENTICAL_STATE',
            cash=engine.ledger.cash,positions=positions,exit_fee_reserve=engine.ledger.reserved_exit_fees,
            checkpoint_at=engine.state['at'],events_processed=engine.state['events_processed'],
            order_count=len(engine.orders),fill_count=len(engine.ledger.fills),
            full_integrity_check=True,all_block_hashes_and_counts_verified=True,
            payload_and_all_state_components_identical=True,elapsed_seconds=time.monotonic()-start,**process_memory())
    except Exception as exc:
        result.update(status='NOT_VERIFIED_RESTORE_STOPPED',error_type=type(exc).__name__,error=str(exc),traceback=traceback.format_exc(),elapsed_seconds=time.monotonic()-start,**process_memory())
        raise
    finally:
        if engine is not None:engine.close()
        result['original_recovery_files_unchanged']={n:sha(SOURCE_ACCOUNT/n)==r['sha256'] and (SOURCE_ACCOUNT/n).stat().st_mtime_ns==r['mtime_ns'] for n,r in backup['original_before'].items()}
        write(result_path,result)
        status('ORIGINAL_PREFIX_RESTORE_FINISHED',restore_status=result['status'],research_running=False,restore_pid=None)
if __name__=='__main__':main()
