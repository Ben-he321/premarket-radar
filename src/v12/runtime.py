import os
import time
import hashlib
import subprocess
from pathlib import Path
from datetime import date,datetime,timedelta,timezone
from src.v11.runtime import read,write,digest,utc,root as v11root,source
from src.data.alpaca_calendar import sessions

FACTORS=['return_1','return_5','return_20','return_60','relative20']
CUTOFF='2026-03-10'
LABEL='HISTORICAL_EXPLORATORY_REUSED_DATA'

def root():
    p=Path(os.environ.get('V12_RUN_DIR',str(v11root().parent/'watchlist-v1_2-momentum')))
    if p.resolve() in (v11root().resolve(),source().resolve()):raise ValueError('OUTPUT_MUST_BE_ISOLATED')
    p.mkdir(parents=True,exist_ok=True);return p

def state(stage,**kw):
    x={'stage':stage,'at':utc(),'pid':os.getpid(),**kw};write(root()/'TASK_STATE.json',x);print(x,flush=True)

def freeze():
    path=root()/'MOMENTUM_PROTOCOL.json'
    if path.exists():return read(path)
    snap=read(v11root()/'v1_snapshot.json');now=datetime.now(timezone.utc)
    code=Path(__file__).resolve().parents[2]
    days=[str(d) for d in sessions(date(2016,1,1),date.fromisoformat(CUTOFF))]
    windows=[]
    for start in range(524,len(days),126):
        windows.append({'id':len(windows),'train':[start-524,start-20], 'evaluation':[start,min(start+126,len(days))]})
    p={'experiment_id':'V1_2_MOMENTUM_001','frozen_at':now.isoformat(),'deadline':(now+timedelta(hours=8)).isoformat(),
       'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=code,text=True).strip(),
       'code_hashes':{str(p.relative_to(code)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [*(code/'src/v12').glob('*.py'),
                      code/'src/v11/kernel.py',code/'src/v11/paper.py',code/'src/watchlist/features.py']},
       'data_version':snap['hash'],'input_snapshot':str(v11root()/'v1_snapshot.json'),
       'v11_config_hash':read(v11root()/'config_and_engine_version.json')['hash'],
       'actions_sha256':hashlib.sha256((v11root()/'action_calendar.json').read_bytes()).hexdigest(),
       'factors':FACTORS,'formulas':{**{f'return_{h}':f'C_t/C_(t-{h})-1' for h in [1,5,20,60]},'relative20':'return_20 - SPY_return_20'},
       'horizons':[5,10,20],'cutoff':CUTOFF,'evidence_label':LABEL,'forbidden_holdout':['2026-03-11','2026-09-09'],
       'days':days,'windows':windows,'training_sessions':504,'evaluation_sessions':126,'purge_sessions':20,
       'label':'all_adjusted close[t+H]/open[t+1]-1; all H sessions observed; final label date inside window',
       'thresholds':'training factor quantiles 1/3 and 2/3, fitted on mature complete labels in training only',
       'minimum_training_events':126,'minimum_bucket_events':20,'minimum_inference_blocks':5,'minimum_ic_events':60,
       'buckets':'weak x<=q1; medium q1<x<=q2; strong x>q2; tied thresholds or factors are insufficient',
       'bootstrap':{'method':'non-overlapping calendar-date blocks; identical date draws across securities; no date shuffle within blocks',
                    'block_sessions':20,'replications':500,'seed':1729,'ci':'percentile 2.5/97.5',
                    'p_value':'two-sided centered block bootstrap (+1)/(B+1); null-centered mean spread or rank correlation',
                    'rank_ic_ci':'block bootstrap on fixed empirical within-sample ranks; not cross-sectional IC'},
       'multiplicity':{'family_size':1980,'primary_tests':'66 * 5 * 3 * (time-series IC + strong-minus-weak spread) on pooled evaluation events',
                       'adjustment':'Benjamini-Yekutieli FDR 5%, missing tests p=1; dependence allowed',
                       'other_tests':'window/training/bucket CIs descriptive; shared 15 results descriptive, no winner selection'},
       'accounts':{'versions':{'R0':'FIXED_LEGACY','R1':'R0 AND return_20>0','R2':'R0 AND return_60>0','R3':'R0 AND relative20>0'},
                   'costs':[['base',.001,1.],['stress25',.0025,1.],['double_commission',.001,2.]],
                   'initial_cash':5500,'hold':3,'stop':.05,'target':None,'risk':.005,'max_weight':.20,'max_positions':4,
                   'participation':.001,'ranking':'SYMBOL_ASCENDING','eligibility':'current verified ordinary/SPAC/ADR stocks; no DXYZ or reference ETF',
                   'windows':'same evaluation windows as conditional study; no new entries unless third-session exit lies inside its evaluation window',
                   'state':'continuous account through the development calendar; cash in embargo/train-only dates; missing exits remain marked unresolved'},
       'data_policy':'reuse pinned SIP objects, predicate cutoff before feature computation; positive traded OHLC; raw/all aligned; intrabar adjustment ratio deviation >1% excluded; no synthesis',
       'action_policy':'all labels adjustment-based; raw accounts explicit V11 actions. Complex acquiree intervals excluded and reported; acquirer shares unchanged.',
       'limitations':['Current adjusted snapshot is not point-in-time vintage','Current watchlist selection and survivorship bias',
                      'Daily open/close are statistical or paper proxies, not guaranteed executable prices','No new true out-of-sample data'],
       'paid_model_calls':0,'new_subscriptions':0,'adaptive_search':False,'auto_promotion':False}
    p['protocol_hash']=digest(p);write(path,p)
    write(root()/'input_snapshot.json',snap)
    return p

def check_deadline():
    p=freeze()
    if datetime.now(timezone.utc)>datetime.fromisoformat(p['deadline']):raise TimeoutError('EIGHT_HOUR_LIMIT_CHECKPOINT_SAVED')

def tags():
    p=freeze();return {k:p[k] for k in ['experiment_id','data_version','protocol_hash','source_commit']}|{'created_at':utc(),'evidence_label':LABEL}

def save_csv(name,rows):
    import pandas as pd
    f=rows.copy() if isinstance(rows,pd.DataFrame) else pd.DataFrame(rows)
    for k,v in tags().items():f[k]=v
    f.to_csv(root()/name,index=False);return f
