"""Bounded preregistered research. Selection never reads outer/holdout returns."""
from collections import defaultdict
from pathlib import Path
import time
import numpy as np
import pandas as pd
from src.data.alpaca_calendar import sessions
from .runtime import root,read,write,digest,utc,event,state
from .preregister import register
from .data import load,manifest
from .features import feature_frame,signal
from .engine import diagnostics,metrics,replay


def slice_events(e,start,end):
    if e.empty:return e
    return e[(e.entry_date>=start)&(e.exit_date<=end)]


def choose(events,train,validation,shared=False,regime=None):
    def score(e,span):
        x=slice_events(e,*span)
        if regime is not None and len(x):x=x[x.market_up==regime]
        if shared and len(x):return x.groupby('entry_date').return_net.mean()
        return x.return_net if len(x) else pd.Series(dtype=float)
    ranking=[]
    for key,e in events.items():
        if key=='FIXED_LEGACY':continue
        x=score(e,train)
        if len(x)>=10:ranking.append((float(x.mean()),key))
    ranking=sorted(ranking,key=lambda x:(-x[0],x[1]))[:3]
    eligible=[]
    for _,key in ranking:
        x=score(events[key],validation)
        if len(x)>=5 and x.mean()>0:eligible.append((float(x.mean()),key))
    return sorted(eligible,key=lambda x:(-x[0],x[1]))[0] if eligible else (0.,None)


def boundaries(days,start,long=False):
    train_n,val_n=(756,126) if long else (252,63)
    # 6-session embargo before evaluation and a separate 6-session purge gap.
    val_end=start-7;val_start=val_end-val_n+1;train_end=val_start-7
    train_start=train_end-train_n+1
    if train_start<0:return None
    return (days[train_start],days[train_end]),(days[val_start],days[val_end])


def action_calendar(records):
    result=defaultdict(list);limitations=[]
    for rec in records:
        s=rec['symbol'];doc=read(root()/'actions'/(s+'.json'),{})
        if doc.get('status')!='ACCESS_OK':limitations.append({'symbol':s,'issue':'ACTIONS_UNAVAILABLE'});continue
        limitations.append({'symbol':s,'issue':'COMPLETENESS_NOT_INDEPENDENTLY_VERIFIED'})
        for kind,rows in doc.get('data',{}).items():
            for a in rows:
                if a.get('symbol')!=s:continue
                if kind in ('forward_splits','reverse_splits'):
                    day=a.get('ex_date')
                    if day and a.get('old_rate') and a.get('new_rate'):
                        result[day].append({'kind':'split','symbol':s,'ratio':float(a['new_rate'])/float(a['old_rate']),'id':a['id']})
                elif kind=='cash_dividends':
                    day=a.get('ex_date')
                    if day:result[day].append({'kind':'dividend','symbol':s,'rate':float(a['rate']),'id':a['id'],'pay_date':a.get('payable_date')})
                    if not a.get('payable_date'):limitations.append({'symbol':s,'issue':'DIVIDEND_PAY_DATE_UNKNOWN','event_id':a['id']})
                elif kind not in ('stock_splits',):limitations.append({'symbol':s,'issue':'UNIMPLEMENTED_ACTION_REQUIRES_REVIEW','kind':kind,'event_id':a.get('id')})
    return dict(result),limitations


def block_ci(events,days):
    if events.empty:return {'status':'UNAVAILABLE_NO_EVENTS'}
    # Resample date blocks, retaining all same-date correlated securities together.
    agg=events.groupby('entry_date').return_net.agg(['sum','count']).reindex(days,fill_value=0)
    a=agg.to_numpy();rng=np.random.default_rng(1729);values=[]
    if len(a)<40:return {'status':'UNAVAILABLE_TOO_FEW_DATES'}
    for _ in range(500):
        starts=rng.integers(0,len(a),size=int(np.ceil(len(a)/20)))
        idx=np.concatenate([(np.arange(20)+j)%len(a) for j in starts])[:len(a)]
        totals=a[idx].sum(axis=0)
        if totals[1]:values.append(totals[0]/totals[1])
    return {'status':'DATE_BLOCK_DIAGNOSTIC','block_sessions':20,'replications':500,'low':float(np.quantile(values,.025)),'high':float(np.quantile(values,.975))} if values else {'status':'UNAVAILABLE_NO_EVENTS'}


def run(universe,deadline=None):
    p=register();out=root()/'research';out.mkdir(exist_ok=True)
    records=[r for r in universe['records'] if r['status']=='INCLUDED']
    versions={r['symbol']:{adj:manifest(r,adj).get('version') for adj in ('raw','all')} for r in records}
    version=digest({'plan':p['version'],'data':versions,'engine':'RESEARCH_ENGINE_V1'})
    existing=read(out/'summary.json',{})
    if existing.get('status')=='COMPLETE':
        event('research','FROZEN_RESULT_REUSED',version,{'original_input_version':existing.get('input_version'),'current_data_version':version,
                                                      'new_data_requires_separate_future_experiment':existing.get('input_version')!=version})
        return existing
    if read(out/'holdout_receipt.json'):
        raise ValueError('HOLDOUT_ALREADY_READ: explicit new experiment required; no automatic retuning')
    days=[str(d) for d in sessions(pd.Timestamp('2016-01-01').date(),pd.Timestamp(p['cutoff']).date())]
    dev_days=[d for d in days if d<=p['development_end']]
    ref=next(r for r in universe['references'] if r['symbol']=='SPY');benchmark=load(ref,'all')
    frames={};raw={};events={};descriptive=[];trials=[];feature_audits=[]
    for rec in records:
        if deadline and time.time()>deadline:raise TimeoutError('RESOURCE_DEADLINE')
        s=rec['symbol']
        try:
            raw[s]=load(rec,'raw');frames[s]=feature_frame(load(rec,'all'),benchmark)
            d=frames[s].loc[:p['development_end']]
            events[s]={}
            for spec in p['configs']:
                e=diagnostics(d,spec,signal(d,spec)) if len(d) else pd.DataFrame()
                if len(e):e['symbol']=s
                events[s][spec['id']]=e
                trials.append({'symbol':s,'config':spec['id'],'development':metrics(e),'status':'DIAGNOSTIC_ONLY','parameters':spec})
            descriptive.append({'symbol':s,'type':rec['type'],'rows':len(raw[s]),'development_sessions':len(d),
                                'status':'INSUFFICIENT_EVIDENCE' if len(d)<378 else 'RESEARCHED',
                                'current_identity_only':True,'earliest_observation':None if raw[s].empty else raw[s].trade_date.min()})
            # Training-only audit. No transformation estimated on validation/OOS/holdout.
            sample=d.iloc[:min(252,len(d))][p['features']]
            corr=sample.corr(min_periods=60)
            pairs=[{'a':a,'b':b,'r':float(corr.loc[a,b])} for i,a in enumerate(p['features']) for b in p['features'][i+1:] if pd.notna(corr.loc[a,b]) and abs(corr.loc[a,b])>.90]
            feature_audits.append({'symbol':s,'fit_end':str(sample.index[-1]) if len(sample) else None,'method':'TRAIN_ONLY_CORRELATION_NO_RESIDUALIZATION','high_correlation_pairs':pairs})
            state('RESEARCH_FEATURES',symbol=s,completed=len(descriptive),total=len(records))
        except ValueError as exc:
            descriptive.append({'symbol':s,'status':'BLOCKED_DATA_VERSION','reason':str(exc)})
            frames.pop(s,None);raw.pop(s,None)
    write(out/'symbols.json',descriptive);write(out/'all_trials.json',trials);write(out/'features_audit.json',feature_audits)
    for s,config_events in events.items():
        nonempty=[e for e in config_events.values() if len(e)]
        if nonempty:pd.concat(nonempty).to_parquet(out/(s+'-development-events.parquet'),index=False)
    equity_events={s:v for s,v in events.items() if s!='DXYZ'}
    pooled={c['id']:pd.concat([v[c['id']] for v in equity_events.values() if len(v[c['id']])],ignore_index=True)
            if any(len(v[c['id']]) for v in equity_events.values()) else pd.DataFrame() for c in p['configs']}
    selections=[];intents={k:defaultdict(list) for k in p['structures']};specs={c['id']:c for c in p['configs']}
    selection_trials=0

    def window(start,end,stage):
        nonlocal selection_trials
        short=boundaries(days,start);long=boundaries(days,start,True)
        shared_span=long or short
        if shared_span is None:return
        shared=choose(pooled,*shared_span,shared=True)
        up=choose(pooled,*shared_span,shared=True,regime=True)
        down=choose(pooled,*shared_span,shared=True,regime=False)
        selection_trials+=36
        for s,d in frames.items():
            available=d.loc[:days[start-1]]
            # Only past observed sessions determine eligibility and window family.
            eligible=int(available.observed.sum())>=378 and s!='DXYZ'
            span=long if long and len(available.loc[long[0][0]:].dropna(subset=['close']))>=882 else short
            chosen=choose(events[s],*span) if eligible and span else (0.,None)
            selection_trials+=12 if eligible else 0
            choices={'SHARED':shared if eligible else (0.,None),'PER_SYMBOL':chosen,'HYBRID_REGIME':up if eligible else (0.,None)}
            selections.append({'symbol':s,'evaluation_start':days[start],'evaluation_end':days[end-1],'stage':stage,
                               'training':span[0] if span else None,'validation':span[1] if span else None,
                               'shared_training':shared_span[0],'shared_validation':shared_span[1],
                               'per_symbol':chosen[1],'shared':choices['SHARED'][1],'hybrid_up':up[1] if eligible else None,'hybrid_down':down[1] if eligible else None,
                               'eligible':eligible})
            if not eligible:continue
            for structure in p['structures']:
                for pos in range(start,end):
                    prior=days[pos-1];day=days[pos]
                    if prior not in d.index:continue
                    choice=choices[structure]
                    if structure=='HYBRID_REGIME':choice=up if bool(d.loc[prior,'market_up']) else down
                    rank,key=choice
                    if key is None:continue
                    mask=signal_cache[(s,key)]
                    if bool(mask.get(prior,False)):
                        rv=raw[s].set_index('trade_date')
                        vol=float(rv.loc[prior,'volume']) if prior in rv.index else float('nan')
                        intents[structure][day].append({'symbol':s,'spec':specs[key],'rank':rank,'previous_volume':vol,'signal_date':prior})
        state('WALK_FORWARD_SELECTION',evaluation_start=days[start],stage_detail=stage)

    signal_cache={(s,c['id']):signal(d,c) for s,d in frames.items() for c in p['configs']}
    # Aligned, non-overlapping outer calendar windows. Shared/per-symbol/hybrid use identical dates.
    for start in range(390,len(dev_days)-62,63):
        if deadline and time.time()>deadline:raise TimeoutError('RESOURCE_DEADLINE')
        window(start,start+63,'OUTER')
    hold_start=len(dev_days)
    window(hold_start,len(days),'FINAL_HOLDOUT')
    frozen={'frozen_at':utc(),'input_version':version,'plan_version':p['version'],'selections':selections,
            'all_evaluated_candidate_symbol_trials':len(trials),'selection_score_evaluations':selection_trials,
            'structures':p['structures'],'improvements_used':0,'no_oos_selection':True}
    write(out/'frozen_pipeline.json',frozen)
    # Receipt is written before any holdout performance calculation. A crash never
    # silently rereads the holdout for revised selection; deterministic recovery uses saved artifacts.
    write(out/'holdout_receipt.json',{'started_at':utc(),'frozen_hash':digest(frozen),'status':'EVALUATING_ONCE','input_version':version})
    for structure,orders in intents.items():write(out/(structure+'-intents.json'),dict(orders))
    actions,limitations=action_calendar(records);write(out/'action_limitations.json',limitations)
    portfolio=[]
    evaluation_days=days[390:]
    for structure,orders in intents.items():
        for name,fric,fee in [('base',.001,1.),('stress25',.0025,1.),('stress50',.005,1.),('double_commission',.001,2.)]:
            if deadline and time.time()>deadline:raise TimeoutError('RESOURCE_DEADLINE_AFTER_FREEZE')
            ledger,curve=replay(raw,orders,evaluation_days,actions,fee,fric)
            curve.to_parquet(out/f'{structure}-{name}-equity.parquet',index=False)
            t=pd.DataFrame(ledger.trades)
            if len(t):t.to_parquet(out/f'{structure}-{name}-trades.parquet',index=False)
            write(out/f'{structure}-{name}-skips.json',ledger.skips)
            outer=t[t.exit_date<p['holdout_start']] if len(t) else t
            holdout=t[t.entry_date>=p['holdout_start']] if len(t) else t
            cross=t[(t.entry_date<p['holdout_start'])&(t.exit_date>=p['holdout_start'])] if len(t) else t
            portfolio.append({'structure':structure,'cost_case':name,'status':'RAW_ACTIONS_DIAGNOSTIC_NOT_PROMOTABLE',
                              'ending_equity':float(curve.equity.iloc[-1]),'max_drawdown':float(curve.drawdown.min()),
                              'outer':metrics(outer),'holdout':metrics(holdout),'boundary_crossing_trades':len(cross),
                              'fees':ledger.fees,'skipped':len(ledger.skips),'cash':ledger.cash,
                              'outer_expectancy_ci':block_ci(outer,dev_days[390:])})
            state('ACCOUNT_REPLAY',structure=structure,cost_case=name,trades=len(t))
    write(out/'portfolio.json',portfolio)
    # Fixed baseline is frozen and never selected by outer performance.
    baseline_orders=defaultdict(list)
    raw_index={s:f.set_index('trade_date') for s,f in raw.items()}
    for s,d in frames.items():
        if s=='DXYZ':continue
        for pos in range(390,len(days)):
            prior=days[pos-1]
            if bool(signal_cache[(s,'FIXED_LEGACY')].get(prior,False)) and prior in raw_index[s].index:
                baseline_orders[days[pos]].append({'symbol':s,'spec':specs['FIXED_LEGACY'],'rank':0,'previous_volume':float(raw_index[s].loc[prior,'volume']),'signal_date':prior})
    legacy,lc=replay(raw,baseline_orders,evaluation_days,actions)
    lc.to_parquet(out/'FIXED_LEGACY-base-equity.parquet',index=False)
    if legacy.trades:pd.DataFrame(legacy.trades).to_parquet(out/'FIXED_LEGACY-base-trades.parquet',index=False)
    benchmarks=[{'name':'CASH','starting_equity':5500,'ending_equity':5500,'status':'CASH_NO_INTEREST'}]
    for rec in universe['references']:
        b=load(rec,'all').set_index('trade_date').reindex(evaluation_days)
        eq=5500*b.close/b.close.iloc[0]
        pd.DataFrame({'date':evaluation_days,'equity':eq.to_numpy()}).to_parquet(out/(rec['symbol']+'-benchmark.parquet'),index=False)
        benchmarks.append({'name':rec['symbol'],'starting_equity':5500,'ending_equity':float(eq.iloc[-1]),'status':'ALL_ADJUSTED_UNIT_TOTAL_RETURN_NO_EXECUTABLE_ORDERS_OR_COSTS'})
    benchmarks.append({'name':'FIXED_LEGACY','starting_equity':5500,'ending_equity':float(lc.equity.iloc[-1]),'status':'RAW_ACCOUNT_DIAGNOSTIC_SAME_COSTS','trades':len(legacy.trades)})
    write(out/'benchmarks.json',benchmarks)
    holdout_reports=[]
    for s,d in frames.items():
        hd=d.loc[p['holdout_start']:]
        for c in p['configs']:
            e=diagnostics(hd,c,signal_cache[(s,c['id'])].reindex(hd.index,fill_value=False)) if len(hd) else pd.DataFrame()
            holdout_reports.append({'symbol':s,'config':c['id'],'metrics':metrics(e),'status':'FROZEN_HOLDOUT_DIAGNOSTIC_NO_RESELECTION'})
    write(out/'holdout_diagnostics.json',holdout_reports)
    matrix=pd.DataFrame({key:e.groupby('exit_date').return_net.mean() if len(e) else pd.Series(dtype=float) for key,e in pooled.items()}).reindex(dev_days,fill_value=0).fillna(0)
    matrix.to_parquet(out/'complete-config-date-matrix.parquet')
    from .statistics import audit_statistics
    stats=audit_statistics(matrix,len(trials)+selection_trials);write(out/'statistics.json',stats)
    summary={'status':'COMPLETE','completed_at':utc(),'input_version':version,'plan_version':p['version'],
             'candidate_symbols':len(universe['records']),'research_symbols':len(frames),'configs':12,'fixed_baselines':1,
             'trials_recorded':len(trials),'selection_score_evaluations':selection_trials,'holdout_evaluations':1,
             'optional_model_calls':0,'optional_model_cost_usd':0,'champion':None,'paper_mode':'CASH_OBSERVATION',
             'promotion':'BLOCKED_ACTIONS_AND_EXECUTION_SEMANTICS_VERIFICATION',
             'limitations':['Company-action completeness and dividend payment dates not independently verified',
                            'Daily SIP OHLC includes extended-hours qualifying trades; daily open is not verified regular-session executable open',
                            'All-adjusted features use a current snapshot, not point-in-time historical vintages',
                            'Current watchlist selection/survivorship bias; DXYZ CEF must be interpreted separately',
                            'No strategy is authorized for forward fills; all account curves are research diagnostics']}
    write(out/'holdout_receipt.json',{'finished_at':utc(),'frozen_hash':digest(frozen),'status':'COMPLETE','evaluations':1,'input_version':version})
    write(out/'summary.json',summary);event('research','COMPLETE',version,summary)
    return summary
