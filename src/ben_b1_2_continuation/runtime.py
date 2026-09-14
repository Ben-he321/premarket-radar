from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import os
import ctypes
import shutil
import time
if os.name=='nt':import msvcrt

REPO = Path(__file__).resolve().parents[2]
OLD = Path('C:/Users/benhe/BenAITradingData/ben-b1-2-window-portfolio-20260914')
ROOT = OLD.parent/'ben-b1-2-continuation-20260914T154608Z'
RUN_ID = 'B12_Q1_P50_B_compact_base_v1'
SOURCE_ACCOUNT = OLD/'portfolio/compact_base_v1'/RUN_ID
ACCOUNT = ROOT/'account'/RUN_ID
STARTED_AT = '2026-09-14T15:46:08+00:00'
DEADLINE = '2026-09-14T23:46:08+00:00'

def utc(): return datetime.now(timezone.utc).isoformat()
def read(p):
    # Minimize reader locks, request all sharing flags and parse only after close.
    # Shared DELETE alone did not guarantee MoveFileEx replacement on this host;
    # the writer's bounded retry remains necessary.
    p=Path(p)
    if os.name!='nt':return json.loads(p.read_text(encoding='utf-8-sig'))
    library=ctypes.WinDLL('kernel32',use_last_error=True)
    create=library.CreateFileW
    create.argtypes=[ctypes.c_wchar_p,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_void_p,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_void_p]
    create.restype=ctypes.c_void_p
    handle=create(str(p),0x80000000,0x7,None,3,0x80,None)
    if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    fd=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
    with os.fdopen(fd,'r',encoding='utf-8-sig') as stream:contents=stream.read()
    return json.loads(contents)
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def write(p,x):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix(p.suffix+'.tmp')
    tmp.write_text(json.dumps(x,ensure_ascii=False,indent=2,allow_nan=False,default=str)+'\n',encoding='utf-8')
    # Readers outside this module (UI, antivirus) can briefly deny replacement.
    # Keep the previous complete JSON visible and retain .tmp on bounded failure.
    for attempt in range(12):
        try:
            os.replace(tmp,p);return
        except PermissionError as exc:
            if getattr(exc,'winerror',None) not in (5,32,33) or attempt==11:raise
            time.sleep(min(.025*(attempt+1),.25))

def active_gate_path():
    original=ROOT/'ENGINEERING_GATE.json';pointer=ROOT/'ACTIVE_ENGINEERING_GATE.json'
    if not pointer.exists():return original
    record=read(pointer);p=Path(record['path']).resolve()
    if p.parent!=ROOT.resolve() or not p.name.startswith('ENGINEERING_GATE_revision_'):raise ValueError('ACTIVE_GATE_OUTSIDE_VERSIONED_ROUND_ROOT')
    if sha(original)!=record['original_gate_sha256'] or sha(p)!=record['sha256']:raise ValueError('ACTIVE_GATE_CHAIN_HASH_MISMATCH')
    return p
def resources():
    class Memory(ctypes.Structure):
        _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong)]+[(s,ctypes.c_ulonglong) for s in ('total','available','totalpage','availablepage','totalvirtual','availablevirtual','extended')]
    m=Memory();m.length=ctypes.sizeof(m)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):raise OSError('Memory status unavailable')
    return {'at':utc(),'free_memory_mib':m.available/1024**2,'free_disk_bytes':shutil.disk_usage(ROOT if ROOT.exists() else OLD).free}
def guard():
    if datetime.now(timezone.utc)>=datetime.fromisoformat(DEADLINE):raise TimeoutError('CONTINUATION_EIGHT_HOUR_DEADLINE')
    r=resources()
    if r['free_memory_mib']<800:raise MemoryError('CONTINUATION_RAM_GUARD_BELOW_800_MIB')
    if r['free_disk_bytes']<2*1024**3:raise OSError('CONTINUATION_DISK_GUARD_BELOW_2_GIB')
    return r
def status(stage,**kw):
    p=ROOT/'TASK_STATE.json';old=read(p) if p.exists() else {}
    x={**old,'stage':stage,'updated_at':utc(),'started_at':STARTED_AT,'deadline_utc':DEADLINE,'pid':os.getpid(),**kw}
    write(p,x)
    with (ROOT/'STAGE_LOG.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(x,default=str)+'\n')
    print(json.dumps({'stage':stage,**kw},default=str),flush=True)
