"""Freeze candidate rules and chronological protocol before any performance read."""
from datetime import date
from src.data.alpaca_calendar import sessions, finalized_day
from .runtime import root,write,read,digest,utc,event


def register():
    existing=read(root()/'preregistration.json')
    if existing:return existing
    days=sessions(date(2016,1,1),finalized_day())
    configs=[]
    for family in ('A','B','C'):
        for hold in (3,5):
            for threshold in (1.2,1.5):
                configs.append({'id':f'{family}-h{hold}-v{threshold}','family':family,'hold':hold,'volume_threshold':threshold,'stop':.05,'target':.10})
    configs.append({'id':'FIXED_LEGACY','family':'LEGACY','hold':3,'volume_threshold':1.5,'stop':.05,'target':None})
    plan={'registered_at':utc(),'schema':1,'engine':'RESEARCH_ENGINE_V1','cutoff':str(days[-1]),
          'holdout_start':str(days[-126]),'holdout_sessions':126,'holdout_evaluations_allowed':1,
          'development_end':str(days[-127]),'long_windows':[756,126,63],'short_windows':[252,63,63],
          'step':63,'purge_sessions':6,'embargo_sessions':6,'max_holding_sessions':5,
          'configs':configs,'search_configurations':12,'fixed_baselines':1,'max_research_improvements':3,
          'improvements_used':0,'optional_model_calls':0,'optional_model_budget_usd':25,'optional_model_max_calls':100,
          'optional_models':'DISABLED_NO_PAID_CALLS','initial_resource_seconds':28800,
          'features':['return_1','return_5','return_20','return_60','ma5_gap','ma10_gap','ma20_gap','ma60_gap','ma20_slope',
                      'atr14_ratio','vol20','downside_vol20','volume_ratio5','volume_ratio20','dollar_volume20','breakout20','range_position20',
                      'gap','intraday','rsi14','relative20','drawdown60'],
          'structures':['SHARED','PER_SYMBOL','HYBRID_REGIME'],
          'selection':'Train shortlist top 3 by mature-event mean net return (>=10 events); validate >=5 events and mean>0; deterministic id ties; else cash. Shared aggregates by date first. No OOS score used in selection.',
          'short_history':'No tuning below 378 sessions; fixed baseline only if outside holdout, else evidence unavailable',
          'signal_timing':'Finalized daily t at next NY 06:00; earliest following executable open; no same-bar open entry',
          'costs':{'commission':1.0,'friction_bps':10,'stress_bps':[25,50],'commission_stress':2.0,'settlement_sessions':1},
          'portfolio':{'cash':5500,'max_positions':4,'max_weight':.20,'risk_fraction':.005,'integer_shares':True,'max_volume_participation':.001},
          'promotion':{'min_outer_windows':3,'min_completed_nonoverlap_oos_trades':50,'net_expectancy_positive':True,'stress_25bps_nonnegative':True,'largest_trade_profit_share_max':.5,'largest_window_profit_share_max':.6,'requires_verified_ledger_and_actions':True},
          'paper':'No qualified champion means cash/observation, never force trades',
          'data_limits':'Current adjusted snapshot not point-in-time vintage; current watchlist survivor/selection bias; actions completeness must be separately verified',
          'statistics':'Date-block bootstrap 20 sessions, 500 replications seed 1729; DSR/PBO only full finite matrix with sufficient trials, otherwise UNAVAILABLE_WITH_REASON; no statistic-targeted tuning',
          'baseline_formula':'Exact existing _detect_signals: abs(low-ma5)/ma5<=.02, close>=ma10, close>previous close, volume>previous5volume*1.5. Legacy return conclusions not modified.'}
    plan['version']=digest(plan)
    write(root()/'preregistration.json',plan)
    event('preregister','FROZEN',plan,{'version':plan['version'],'holdout_start':plan['holdout_start']})
    return plan
