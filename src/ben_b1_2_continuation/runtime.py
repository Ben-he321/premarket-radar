from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import os
import ctypes
import shutil

REPO = Path(__file__).resolve().parents[2]
OLD = Path('C:/Users/benhe/BenAITradingData/ben-b1-2-window-portfolio-20260914')
ROOT = OLD.parent/'ben-b1-2-continuation-20260914T154608Z'
RUN_ID = 'B12_Q1_P50_B_compact_base_v1'
SOURCE_ACCOUNT = OLD/'portfolio/compact_base_v1'/RUN_ID
ACCOUNT = ROOT/'account'/RUN_ID
STARTED_AT = '2026-09-14T15:46:08+00:00'
DEADLINE = '2026-09-14T23:46:08+00:00'

def utc(): return datetime.now(timezone.utc).isoformat()
def read(p): return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def write(p,x):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix(p.suffix+'.tmp')
    tmp.write_text(json.dumps(x,ensure_ascii=False,indent=2,allow_nan=False,default=str)+'\n',encoding='utf-8')
    os.replace(tmp,p)
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
