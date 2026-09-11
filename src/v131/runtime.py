import os
import sqlite3
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from src.v13.runtime import read, write, sha, digest, utc, CODE
from src.v13.runtime import root as prior
from src.v11.runtime import root as v11root, source
from src.v13.runtime import CONDITIONS, CUTOFF, protocol
LABEL='ENGINEERING_RESTATEMENT_REUSED_DATA'

def save_csv(name,data):
    import pandas as pd
    f=data.copy() if isinstance(data,pd.DataFrame) else pd.DataFrame(data)
    f['experiment_id']='V1_3_1_FIXED_INTENT_RESTATEMENT';f['evidence_label']=LABEL
    f.to_csv(root()/name,index=False);return f

def root():
    p=v11root().parent/'watchlist-v1_3_1-evidence-forward'
    p.mkdir(exist_ok=True)
    return p

def state(stage, **kw):
    x={'stage':stage,'at':utc(),'pid':os.getpid(),**kw}
    write(root()/'TASK_STATE.json',x);print(x,flush=True)

def check_deadline():
    if datetime.now(timezone.utc)>datetime.fromisoformat(read(root()/'RUN_BUDGET.json')['deadline']):
        raise TimeoutError('EIGHT_HOUR_WINDOW_EXHAUSTED_CHECKPOINT_SAVED')

def takeover():
    if (root()/'takeover.json').exists():return
    write(root()/'RUN_BUDGET.json',{'started_at':'2026-09-11T13:43:54+00:00','deadline':'2026-09-11T21:43:54+00:00'})
    files={p:sha(p) for p in read(prior()/'prior_result_hashes.json')['files']}
    for p in prior().rglob('*'):
        if p.is_file() and p.suffix not in ('.lock','.tmp'):files[str(p)]=sha(p)
    write(root()/'prior_result_hashes.json',{'at':utc(),'files':files,'live_ledgers':'Logical immutable-prefix check; natural appends allowed'})
    live={}
    for p in [v11root()/'experimental_paper/ledger.sqlite']:
        dest=root()/'private_backup'/p.name;dest.parent.mkdir(exist_ok=True)
        with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) as c,sqlite3.connect(dest) as d:
            c.backup(d)
            live[str(p)]={table:{r[0]:digest(r[1]) for r in c.execute(f'SELECT id,payload FROM {table}')} for table in ('intents','events')}
    write(root()/'old_ledger_prefix.json',live)
    processes=subprocess.check_output(['powershell','-NoProfile','-Command',"[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Get-CimInstance Win32_Process | Where-Object {$_.Name -match 'python'} | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | ConvertTo-Json"],text=True,encoding='utf-8-sig')
    write(root()/'takeover.json',{'at':utc(),'baseline':subprocess.check_output(['git','rev-parse','HEAD'],cwd=CODE,text=True).strip(),'branch':subprocess.check_output(['git','branch','--show-current'],cwd=CODE,text=True).strip(),'processes':processes,'code_hashes':{str(p.relative_to(CODE)):sha(p) for d in ('src/v11','src/v13','src/watchlist') for p in (CODE/d).glob('*.py')}})
    state('OLD_EVIDENCE_FROZEN',files=len(files))

def preservation():
    old=read(root()/'prior_result_hashes.json')['files'];changed=[p for p,h in old.items() if not Path(p).exists() or sha(p)!=h]
    ledger_changes=[];counts={}
    for p,tables in read(root()/'old_ledger_prefix.json').items():
        with sqlite3.connect(Path(p).as_uri()+'?mode=ro',uri=True) as c:
            for table,rows in tables.items():
                current={r[0]:digest(r[1]) for r in c.execute(f'SELECT id,payload FROM {table}')}
                ledger_changes.extend(f'{table}:{k}' for k,v in rows.items() if current.get(k)!=v)
                counts[table]={'before':len(rows),'now':len(current)}
    x={'at':utc(),'status':'PASS' if not changed and not ledger_changes else 'FAIL','static_files_checked':len(old),'changed':changed,'old_ledger_modified_or_deleted':ledger_changes,'natural_counts':counts}
    write(root()/'preservation_check.json',x);return x
