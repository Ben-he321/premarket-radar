import os
import json
import hashlib
from pathlib import Path
from datetime import datetime,timezone
from src.data.alpaca_config import PROJECT_ROOT,load_config

def utc():return datetime.now(timezone.utc).isoformat()
def digest(x):return hashlib.sha256(json.dumps(x,sort_keys=True,default=str).encode()).hexdigest()
def read(p,default=None):return json.loads(Path(p).read_text(encoding='utf-8-sig')) if Path(p).exists() else default
def source():return Path(os.environ.get('V11_SOURCE_DIR',str(load_config().data_dir.parent/'watchlist-research-v1')))
def root():
    p=Path(os.environ.get('V11_RUN_DIR',str(load_config().data_dir.parent/'watchlist-v1_1-execution')))
    if p.resolve()==source().resolve():raise ValueError('V11_MUST_NOT_OVERWRITE_V1')
    p.mkdir(parents=True,exist_ok=True);return p
def write(p,x):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix(p.suffix+f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(x,ensure_ascii=False,indent=2,default=str,allow_nan=False),encoding='utf-8');tmp.replace(p)
def state(stage,**kw):
    x={'stage':stage,'at':utc(),'pid':os.getpid(),**kw};write(root()/'TASK_STATE.json',x);print(json.dumps(x,ensure_ascii=False),flush=True)

def freeze():
    p=root()/'config_and_engine_version.json'
    if p.exists():return read(p)
    old=read(source()/'preregistration.json')
    config={'version':'EXECUTION_V1_1_1','frozen_at':utc(),'prior_plan':old['version'],'configs':old['configs'],
            'reference_cash':5500,'risk':.005,'max_weight':.2,'max_positions':4,'commission':1.,'friction':.001,
            'participation':.001,'settlement_sessions':1,'price_decimals':4,'money_decimals':2,
            'selection_protocol':'UNCHANGED_V1_WINDOWS_THRESHOLDS_AND_CONFIGS; development only, no champion selection',
            'old_holdout':['2026-03-11','2026-09-09'],'all_restatement_label':'ALREADY_OBSERVED_ENGINEERING_RESTATEMENT_NOT_NEW_OOS',
            'field_rules':{'invalid_OHLC':'QUARANTINE_PRICE_AND_EXECUTION','invalid_vwap_only_positive_volume':'AUXILIARY_WARNING_NOT_PRICE_REJECTION',
                           'zero_volume_zero_trades':'RETAIN_REFERENCE_NONEXECUTABLE_NO_FEATURE_FILL','unknown_volume':'NO_EXECUTION_NO_ZERO_IMPUTATION'},
            'experimental':{'account':'EXPERIMENTAL_PAPER_V1_1','strategy':'FIXED_LEGACY','champion':False,'decision_ny':'06:30',
                            'latest_decision_ny':'09:25','entry_window_minutes':5,'first_regular_minute_open_proxy':True,
                            'lag_minutes':20,'budget_price_collar':.05,'signal_tie_break':'SYMBOL_ASCENDING',
                            'intent_expiry':'ENTRY_WINDOW_END; retrieval may occur later','contingent_exit_authored_with_buy':True,
                            'no_quote':'EXPIRE_ENTRY; HOLD_AND_FLAG_EXIT_UNRESOLVED','maximum_service_days':30},
            'samples':{'fixed_dates':['2016-01-04','2020-01-02','2024-01-02','2026-09-09'],'symbols':['NVDA','AMD','MSFT'],
                       'anomalies':{'SMCI':'2019-01-02','CCXI':'2026-03-03','BATL':'2021-04-09','CLSK':'2024-11-08'},
                       'split_samples':{'NVDA':'2024-06-10'}},'paid_models':False,'new_strategy_search':False}
    config['hash']=digest(config);write(p,config);return config
