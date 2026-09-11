import os
import hashlib
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from src.v11.runtime import read, write, digest, utc, root as v11root, source

LABEL = 'HISTORICAL_EXPLORATORY_REUSED_DATA'
CUTOFF = '2026-03-10'
CONDITIONS = {'M20': ('return_20', 'gt'), 'RS20': ('relative20', 'gt'), 'REV5': ('return_5', 'lt')}
FACTORS = ['return_1', 'return_5', 'return_20', 'return_60', 'relative20']
CODE = Path(__file__).resolve().parents[2]

def root():
    p = Path(os.environ.get('V13_RUN_DIR', str(v11root().parent / 'watchlist-v1_3-controlled-factor')))
    for old in [source(), v11root(), v11root().parent / 'watchlist-v1_2-momentum']:
        if p.resolve() == old.resolve() or old.resolve() in p.resolve().parents:
            raise ValueError('V13_OUTPUT_MUST_BE_ISOLATED')
    p.mkdir(parents=True, exist_ok=True)
    return p

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''): h.update(chunk)
    return h.hexdigest()

def takeover():
    path = root() / 'prior_result_hashes.json'
    if path.exists(): return read(path)
    files = {}
    prior = read(v11root().parent / 'watchlist-v1_2-momentum/prior_result_hashes.json')
    for name in prior['files']: files[name] = sha(name)
    old = v11root().parent / 'watchlist-v1_2-momentum'
    for p in old.rglob('*'):
        if p.is_file() and p.suffix not in ('.lock', '.tmp'):
            files[str(p)] = sha(p)
    for p in [v11root() / 'verification_v1_1_bundle.zip', source() / 'research/portfolio.json']:
        if p.exists(): files[str(p)] = sha(p)
    x = {'checked_at': utc(), 'baseline_commit': subprocess.check_output(['git','rev-parse','HEAD'],cwd=CODE,text=True).strip(),
         'files': files, 'excludes': 'Live V1/V1.1 service state and ledgers may change naturally; sampled read-only separately.'}
    write(path, x)
    write(root() / 'RUN_BUDGET.json', {'started_at': utc(), 'deadline': (datetime.now(timezone.utc)+timedelta(hours=8)).isoformat()})
    return x

def check_deadline():
    if datetime.now(timezone.utc) > datetime.fromisoformat(read(root()/'RUN_BUDGET.json')['deadline']):
        raise TimeoutError('EIGHT_HOUR_WALL_LIMIT_CHECKPOINTED')

def state(stage, **kw):
    x = {'stage':stage, 'at':utc(), 'pid':os.getpid(), **kw}
    write(root()/'TASK_STATE.json', x)
    print(x, flush=True)

def phase(name, event, **kw):
    p=root()/'timeline.json'; x=read(p,[])
    x.append({'phase':name,'event':event,'at':utc(),'command':'python -m src.v13 run',**kw});write(p,x)

def freeze():
    path=root()/'CONTROLLED_PROTOCOL.json'
    if path.exists():
        p=read(path); body={k:v for k,v in p.items() if k!='protocol_hash'}
        if digest(body)!=p['protocol_hash']:raise ValueError('PROTOCOL_TAMPERED')
        return p
    takeover()
    old=read(v11root().parent/'watchlist-v1_2-momentum/MOMENTUM_PROTOCOL.json')
    snap=read(v11root()/'v1_snapshot.json')
    p={'experiment_id':'V1_3_CONTROLLED_FACTOR_001','frozen_at':utc(),'evidence_label':LABEL,
       'data_version':snap['hash'],'cutoff':CUTOFF,'excluded_holdout':['2026-03-11','2026-09-09'],
       'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=CODE,text=True).strip(),
       'code_hashes':{x.relative_to(CODE).as_posix():sha(x) for x in [*(CODE/'src/v13').glob('*.py'),CODE/'src/v11/kernel.py',CODE/'src/watchlist/engine.py',CODE/'src/watchlist/features.py']},
       'days':old['days'],'windows':old['windows'],'train_sessions':504,'purge_sessions':20,'evaluation_sessions':126,
       'conditions':CONDITIONS,'horizons':[5,10,20],'descriptive_factors':FACTORS,
       'eligibility':'Known signal-day valid raw/all and finite all three condition factors; >=126 common valid factor dates in the fixed 504-session training window. Same eligibility for every condition, U and random. Current verified identities are NOT historical point-in-time identities.',
       'minimum_training_observations':126,'minimum_group_observations':20,'minimum_blocks':8,
       'entry':'Signal t after close; attempt next calendar session open; missing next quote is an execution failure, never used to prefilter the signal. No deferred catch-up entry.',
       'label':'All-adjusted next-session open to t+H close; require all H observed traded bars and maturity inside evaluation window; missing-path exclusions apply ONLY to descriptive statistics.',
       'probability':'Per symbol/condition/window mean(condition) on common eligible TRAIN factor observations; no future returns or mature future labels used.',
       'random_seeds':list(range(13001,13051)),
       'random_hash':'first 8 SHA256 bytes big-endian / 2**64 of activity|seed|symbol|signal_date; same random draw across H and conditions; never select a seed',
       'priority':'ascending SHA256(priority|symbol|signal_date), shared by all versions; independent of all future data',
       'accounts':{'cash':5500,'risk':.005,'max_weight':.2,'max_positions':4,'stop':.05,'target':None,'integer_new_buys':True,
                   'costs':[['base',.001,1.],['stress25',.0025,1.],['double_commission',.001,2.]],
                   'count_real_and_unconditional':36,'count_random_base':450,'settlement_sessions':1,'participation_previous_volume':.001,
                   'holding':'Exit at stop if touched, otherwise Hth session close. No new entries unless scheduled Hth session belongs to same evaluation window. Missing held quote retains position with stale mark flag.',
                   'complex_actions':'Do NOT exclude entries using future actions. Encountered unresolved held complex action is recorded; raw-price proxy replay continues but all later account valuations are explicitly UNVERIFIED_COMPLEX_ACTION. Primary comparison downgraded if condition or any of its 50 controls is affected. Acquirer-only share-unchanged events are disclosed separately.'},
       'primary':{'count':9,'estimand':'Mean daily change in condition account equity / 5500 minus mean across ALL 50 corresponding random accounts, common calendar, base cost. Includes cash and costs; no claim of equal exposure.',
                  'direction':'two-sided; positive incremental mean required for positive exploratory lead','alpha':.05,'adjustment':'Benjamini-Yekutieli, family=9; missing/uncalibrated comparisons p=1',
                  'bootstrap_replications':10000,'bootstrap_seed':131729,'block_sessions':60,'method':'Synchronous nonoverlapping calendar block pairs bootstrap on daily increments; null-centered two-sided p=(1+count(abs(boot-estimate)>=abs(estimate)))/(10001); percentile 95% CI',
                  'descriptive_only':'All per-stock, window, factor Rank IC, U_H, costs, seed percentiles and label tests. No champion selection. Fixed empirical rank bootstrap not claimed calibrated.'},
       'calibration':{'seed':131301,'panels':100,'days':1800,'securities':66,'effects':[0.,.00005,.0002,.0005],
                      'ar_market':.3,'ar_idiosyncratic':.2,'missing_probability':.1,'overlap_sessions':20,
                      'noise':'Each of nine paired daily increment series = common market plus average idiosyncratic AR over 66 securities, smoothed over 20 sessions, then known constant effect; 10% MAR observations missing.',
                      'guard':'Nominal null rejection <=10%, 95% CI coverage >=90% pooled across 900 null tests; passing is limited simulation evidence, not proof for nonstationary account paths. Otherwise all primary tests DESCRIPTION_ONLY.',
                      'synthetic_location':'OS temporary test directory only; calibration report bundled with SYNTHETIC_METHOD_TEST_ONLY label, never performance.'},
       'limitations':['Chosen after V1.2: reused observed historical exploration, not new blind OOS','Current watchlist and adjusted snapshot selection/vintage bias',
                      'Daily open/stop prices are proxies; no intrabar ordering or time-weighted intraday exposure available','Mean frozen acceptance does not match actual positions/trades/exposure',
                      'Block bootstrap approximates time dependence, not a proof under regime shifts','Complex actions/missing held prices invalidate confident executable profitability'],
       'paid_calls':0,'new_accounts_started':False,'parameter_search':False,'auto_promotion':False}
    p['protocol_hash']=digest(p);write(path,p);write(root()/'input_snapshot.json',snap)
    write(root()/'actions.json',read(v11root()/'action_calendar.json'))
    return p

@lru_cache(maxsize=1)
def protocol(): return freeze()

def save_csv(name, data):
    import pandas as pd
    f=data.copy() if isinstance(data,pd.DataFrame) else pd.DataFrame(data)
    f['experiment_id']=protocol()['experiment_id']; f['evidence_label']=LABEL
    p=root()/name;p.parent.mkdir(parents=True,exist_ok=True);f.to_csv(p,index=False)
    return f
