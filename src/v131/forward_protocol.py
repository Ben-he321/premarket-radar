from datetime import datetime,timezone,date,timedelta,time
from .runtime import *
from .identity import registry
from .kernel import VERSION
from src.data.alpaca_calendar import sessions
from src.v11.data import NY

BOOKS={'EXP_M20_H20':'M20','CONTROL_U_H20':'U'}
def freeze():
    path=root()/'FORWARD_PROTOCOL.json'
    if path.exists():
        p=read(path);assert p['protocol_hash']==digest({k:v for k,v in p.items() if k!='protocol_hash'});return p
    now=datetime.now(timezone.utc);day=now.astimezone(NY).date()
    first=next(d for d in sessions(day,day+timedelta(days=14)) if datetime.combine(d,time(6,30),NY)>now)
    observation=sessions(first,first+timedelta(days=120))[:60]
    p={'experiment_id':'V1_3_1_FORWARD_FROZEN_001','frozen_at':utc(),'started_at':utc(),'version':VERSION,'books':BOOKS,
       'per_book_initial_cash':5500,'capital_semantics':'Independent comparison books; never sum into user capital 11000',
       'status':'EXPERIMENTAL_UNPROVEN','conditions':{'M20':'return_20 > 0','U':'all prequalified candidates'},'hold':20,'stop':.05,'target':None,
       'risk':.005,'max_weight':.2,'max_positions':4,'commission':1.,'friction':.001,'participation':.001,'new_buys':'INTEGER',
       'settlement_sessions':1,'reserved_cash':'held exit fee + pending buy and exit fee + negative unsettled liabilities; not an asset and not an early fee',
       'priority':'ascending SHA256(priority|symbol|signal_date), same as original V13',
       'qualification':{'anchor':'2016-01-04','train':504,'purge':20,'evaluation':126,'minimum_common_training_rows':126,'current_factors':['return_20','relative20','return_5'],'causal_raw_all_scale_check':.01,'universe':'66 preserved; operating equities only; DXYZ diagnostic only','point_in_time_limit':'Current exchange/name evidence; historical stable identifiers may remain UNKNOWN'},
       'experimental':{'budget_price_collar':.05,'decision_ny':'06:30','latest_decision_ny':'09:25','lag_minutes':20,'entry_window_minutes':5},
       'feed':'sip','data_mode':'BASIC_DELAYED_REAL_HISTORICAL_MINUTES','price_proxy':'First eligible RTH minute open; stop-first then H20 close; no broker fills',
       'raw_daily_scope':'SIP daily fields have different trade-condition and session eligibility; no assumption that all daily fields are RTH-only',
       'unknown_action':'Block only affected symbol; never infer a corporate conversion from ticker text',
       'no_quote':'No made-up fill: expire entry only after successful empty query; hold and flag exit until eligible quote',
       'first_planned_decision':datetime.combine(first,time(6,30),NY).isoformat(),'observation_sessions':[str(d) for d in observation],
       'scheduled_observation_end':str(observation[-1]),'after_observation':'Stop new entry; continue existing exits/settlement until flat, then stop',
       'universe_version':read(prior()/'input_snapshot.json')['universe']['universe_version'],'identity_registry_hash':digest(registry()),
       'shared_limit_requests_per_minute':120,'paid_api_calls':0,'broker_access':False,'auto_promotion':False,'legacy_account_modified':False,
       'deployment':'Local Windows, requires machine awake and network; page closure independent; login restart task',
       'historical_restatement_is_forward':False}
    p['protocol_hash']=digest(p);write(path,p);return p

def book_config(name):
    p=freeze();return {**p,'account':name,'condition':BOOKS[name]}
