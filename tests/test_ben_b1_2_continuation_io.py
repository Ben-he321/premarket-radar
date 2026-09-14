"""Real Windows file-lock engineering checks in pytest temporary directories."""
import ctypes
import errno
import json
import os
import threading
import time
import pytest
from src.ben_b1_2_continuation import runtime as rt


def deny_delete(path):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    create=kernel.CreateFileW
    create.argtypes=[ctypes.c_wchar_p,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_void_p,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_void_p]
    create.restype=ctypes.c_void_p
    handle=create(str(path),0x80000000,1,None,3,0x80,None)
    if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    close=kernel.CloseHandle;close.argtypes=[ctypes.c_void_p];close.restype=ctypes.c_int
    return lambda:close(handle)


@pytest.mark.skipif(os.name!='nt',reason='Actual Windows sharing mode required')
def test_real_temporary_reader_lock_retries_atomic_replace(tmp_path):
    target=tmp_path/'status.json';rt.write(target,{'generation':1})
    release=deny_delete(target)
    worker=threading.Thread(target=lambda:(time.sleep(.18),release()))
    worker.start()
    try:rt.write(target,{'generation':2})
    finally:worker.join()
    assert rt.read(target)=={'generation':2}
    assert not target.with_suffix('.json.tmp').exists()


@pytest.mark.skipif(os.name!='nt',reason='Actual Windows sharing mode required')
def test_real_permanent_lock_stops_after_twelve_attempts_and_keeps_old_json(tmp_path,monkeypatch):
    target=tmp_path/'checkpoint_fixture.json';rt.write(target,{'generation':1})
    release=deny_delete(target);calls=[];original=rt.os.replace
    def replace(*args):calls.append(args);return original(*args)
    monkeypatch.setattr(rt.os,'replace',replace)
    try:
        with pytest.raises(PermissionError):rt.write(target,{'generation':2})
        assert len(calls)==12
        assert rt.read(target)=={'generation':1}
        assert rt.read(target.with_suffix('.json.tmp'))=={'generation':2}
    finally:release()


@pytest.mark.skipif(os.name!='nt',reason='Actual Windows sharing mode required')
def test_reader_closes_handle_before_parsing_and_retains_consistent_version(tmp_path,monkeypatch):
    target=tmp_path/'status.json';rt.write(target,{'generation':1})
    replacement=tmp_path/'replacement.json';rt.write(replacement,{'generation':2})
    load=rt.json.loads
    def replace_during_parse(contents):
        os.replace(replacement,target)
        return load(contents)
    monkeypatch.setattr(rt.json,'loads',replace_during_parse)
    assert rt.read(target)=={'generation':1}
    monkeypatch.setattr(rt.json,'loads',load)
    assert rt.read(target)=={'generation':2}


@pytest.mark.parametrize('error',[OSError(errno.ENOSPC,'disk full'),PermissionError(errno.EACCES,'non-Windows permission failure')])
def test_unrelated_io_failure_is_not_retried_or_hidden(tmp_path,monkeypatch,error):
    target=tmp_path/'state.json';rt.write(target,{'generation':1});calls=[]
    def fail(*args):calls.append(args);raise error
    monkeypatch.setattr(rt.os,'replace',fail)
    monkeypatch.setattr(rt.time,'sleep',lambda _:pytest.fail('Unexpected retry'))
    with pytest.raises(type(error)):rt.write(target,{'generation':2})
    assert len(calls)==1 and rt.read(target)=={'generation':1}


def test_versioned_gate_preserves_and_authenticates_original(tmp_path,monkeypatch):
    monkeypatch.setattr(rt,'ROOT',tmp_path)
    old=tmp_path/'ENGINEERING_GATE.json';new=tmp_path/'ENGINEERING_GATE_revision_2.json'
    rt.write(old,{'generation':1});old_hash=rt.sha(old)
    assert rt.active_gate_path()==old
    rt.write(new,{'generation':2})
    rt.write(tmp_path/'ACTIVE_ENGINEERING_GATE.json',{'path':str(new),'sha256':rt.sha(new),'original_gate_sha256':old_hash})
    assert rt.active_gate_path()==new
    rt.write(new,{'generation':3})
    with pytest.raises(ValueError,match='CHAIN_HASH_MISMATCH'):rt.active_gate_path()
    assert rt.sha(old)==old_hash


def test_gate_pointer_cannot_escape_round_root(tmp_path,monkeypatch):
    monkeypatch.setattr(rt,'ROOT',tmp_path)
    rt.write(tmp_path/'ACTIVE_ENGINEERING_GATE.json',{'path':str(tmp_path.parent/'ENGINEERING_GATE_revision_2.json')})
    with pytest.raises(ValueError,match='OUTSIDE_VERSIONED'):rt.active_gate_path()


@pytest.mark.skipif(os.name!='nt',reason='Windows process lock required')
@pytest.mark.parametrize('failure_stage',['STARTUP','FINAL_STATUS'])
def test_status_write_failure_preserves_primary_error_and_releases_lock(tmp_path,monkeypatch,failure_stage):
    from src.ben_b1_2_continuation import runner
    import msvcrt
    monkeypatch.setattr(rt,'ROOT',tmp_path);monkeypatch.setattr(runner,'ROOT',tmp_path)
    monkeypatch.setattr(runner,'_engine',None)
    monkeypatch.setattr(runner,'resources',lambda:{'synthetic_engineering_fixture':True})
    monkeypatch.setattr(runner,'process_memory',lambda:{})
    def no_research():raise ValueError('PRIMARY_ENGINEERING_FIXTURE_ERROR')
    monkeypatch.setattr(runner,'resume',no_research)
    real_write=rt.write
    def fail_status(path,value):
        if path.name=='RUNNER_PROCESS.json' and value.get('status')==('RUNNING' if failure_stage=='STARTUP' else 'STOPPED'):
            raise PermissionError('PERSISTENT_STATUS_LOCK_FIXTURE')
        return real_write(path,value)
    monkeypatch.setattr(runner,'write',fail_status)
    expected=PermissionError if failure_stage=='STARTUP' else ValueError
    with pytest.raises(expected):runner.main()
    error=rt.read(tmp_path/'STOP_RECORD.json')
    assert error['type']==expected.__name__
    assert list(tmp_path.glob('STOP_RECORD_*.json')) and list(tmp_path.glob('RUNNER_EXIT_*.json'))
    with (tmp_path/'continuation_runner.lock').open('r+b') as lock:
        msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
