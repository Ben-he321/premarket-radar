"""B1.2 isolated paths, immutable protocol and persistent progress."""
from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import os
import subprocess
import time

REPO = Path(__file__).resolve().parents[2]
ROOT = Path.home()/'BenAITradingData'/'ben-b1-2-window-portfolio-20260914'
B11 = ROOT.parent/'ben-b1-1-replay-20260914'
B1 = ROOT.parent/'ben-b1-research-20260913'
OLD_REPO = REPO.parent/'premarket-radar-ai-m1'
START, END, TAIL_END, WARMUP = '2026-01-02', '2026-03-31', '2026-04-30', '2025-06-01'
STARTED_AT = '2026-09-14T07:35:00+00:00'
DEADLINE = '2026-09-14T15:35:00+00:00'

def utc(): return datetime.now(timezone.utc).isoformat()

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def read(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def write(path, value):
    p=Path(path); p.parent.mkdir(parents=True,exist_ok=True)
    q=p.with_suffix(p.suffix+f'.{os.getpid()}.tmp')
    q.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str,allow_nan=False),encoding='utf-8')
    for attempt in range(6):
        try:q.replace(p);break
        except PermissionError:
            if attempt==5:raise
            time.sleep(.05*2**attempt)

def status(stage, **fields):
    p=ROOT/'TASK_STATE.json';old=read(p) if p.exists() else {}
    record={**old,'stage':stage,'updated_at':utc(),'worker_pid':os.getpid(),**fields}
    write(p,record)
    with (ROOT/'STAGE_LOG.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(record,default=str)+'\n')

def deadline():
    if datetime.now(timezone.utc)>=datetime.fromisoformat(DEADLINE):raise TimeoutError('FROZEN_EIGHT_HOUR_RESOURCE_DEADLINE')

def freeze():
    ROOT.mkdir(parents=True,exist_ok=True)
    p=ROOT/'B12_PROTOCOL.json'
    if p.exists():return read(p)
    v={'version':'BEN_B1_2_WINDOW_COMMON_P50_V1','frozen_at':utc(),
       'started_at':STARTED_AT,'deadline_utc':DEADLINE,'resource_limit_hours':8,
       'base_commit':'0ba89632425d1f1adc4989be5b55e069208766d3',
       'branch':'codex/ben-b1-2-window-portfolio','task_sha256':sha(REPO/'docs/ben_b1_2/TASK_20260914.txt'),
       'first20_source':str(B11/'data/FIRST20_FROZEN.csv'),'first20_sha256':sha(B11/'data/FIRST20_FROZEN.csv'),
       'first20_earnings_source':str(B11/'earnings/PRIMARY_EARNINGS.json'),'first20_earnings_sha256':sha(B11/'earnings/PRIMARY_EARNINGS.json'),
       'original_config':read(B1/'frozen_config.json'),'original_config_sha256':sha(B1/'frozen_config.json'),
       'universe_policy_sha256':sha(B1/'UNIVERSE_POLICY.csv'),'candidates':66,
       'start':START,'end':END,'warmup_request_start':WARMUP,'exit_only_tail_end':TAIL_END,
       'tail_rule':'No entry after March31. Existing campaigns may naturally exit and settle until April30; retain any remaining position/receivable/unsettled cash. No fabricated liquidation.',
       'common_accounts':['Q0_P50_A','Q1_P50_A','Q0_P50_B','Q1_P50_B'],
       'capital_each':5500,'position_target_nav_fraction':0.5,'max_positions':2,
       'quote_modes':{'Q0':'B11 fixed session close+5min; original20 outcomes/hash reused without rerun',
          'Q1':'POST_REVIEW_EXECUTION_HYPOTHESIS: first arriving fully eligible quote in [close+5min,close+15min); same timestamp quotes grouped before original ranking'},
       'shared_cash':'One ledger per account; integer shares, exit fee reserve, pending orders and T+1 settlement share actual cash; no stitching independent sample accounts',
       'quote_age_seconds':5,'spread_fraction':0.003,'close_price_cap':1.01,'max_initial_stop_fraction':0.07,'minimum_net_rr':2,
       'commission_usd':1,'base_execution_friction_fraction':0.001,
       'financial_layers':['A_VERIFIED_PIT','B_RETROSPECTIVE_EARNINGS_EXCLUSION','C_UNKNOWN_NO_ENTRY'],
       'quarter_price_input_policy':'All66 per-day scope; KEEP warmup RTH minutes plus vendor official close. Missing or action-uncertain inputs gate affected symbol/day, not other candidates; whole portfolio labeled COVERAGE_LIMITED.',
       'minute_acquisition':'Fixed warmup and quarter+tail for KEEP; reuse complete old supersets; no blind all-symbol historical quote download',
       'entry_quote_acquisition':'Dates with causal RTH fresh-cross/structural prefilter or actual post-exit reentry; chronological day then existing identity order, never subsequent outcome selection',
       'holding_quote_acquisition':'Union of actual holdings at each shared session boundary, complete regular-session quotes plus active decision boundary; entry dates may request next sessions only once positions exist',
       'historical_daily_availability':'Historical final RTH snapshot at close+1minute is MODEL ASSUMPTION. Actual historical network receipt UNKNOWN; current Basic live availability NOT_VERIFIED.',
       'ranking_volume':'Prior20 RTH volume incl independently observed auction only. Unknown secondary volume blocks tied spreads only; no replacement by all-session vendor volume.',
       'benchmarks':'SPY and QQQ separate5500 integer-share accounts, first regular open Jan2, quarter end close Mar31, commission1 per executed order/friction0.001; dividends credited actual payable date, whole-share reinvest next session open, no automatic end sale; all-adjusted total-return reference separately.',
       'deferred':['P100','P200','remaining ablation matrix','Ben forward'],
       'prohibited':['old result/cache write','old live service restart','paid service','paid model API','broker calls','parameter search','fake historical receipts']}
    write(p,v);write(REPO/'docs/ben_b1_2/protocol.json',v)
    status('PROTOCOL_FROZEN',started_at=STARTED_AT,deadline_utc=DEADLINE,persistent_research_worker_running=False,
           ben_forward_running=False,paid_model_calls=0,paid_purchases=0,errors=[])
    return v

def preserve():
    dest=ROOT/'PRESERVATION_BEFORE.json'
    if dest.exists():return read(dest)
    paths={p for p in B11.rglob('*') if p.is_file() and p.suffix not in {'.lock','.tmp'}}
    prior=read(B11/'PRESERVATION_BEFORE.json')
    paths.update(Path(p) for p in prior['files'] if '/forward/' not in p.replace('\\','/'))
    result={'at':utc(),'files':{str(p):sha(p) for p in sorted(paths)},
            'live_policy':'Old live ledger may evolve naturally; no mutation/reset/restart from B12.',
            'old_repo_status':subprocess.check_output(['git','status','--porcelain'],cwd=OLD_REPO,text=True)}
    write(dest,result);return result

if __name__=='__main__':
    freeze();print(json.dumps({'output':str(ROOT),'preserved_files':len(preserve()['files'])}))
