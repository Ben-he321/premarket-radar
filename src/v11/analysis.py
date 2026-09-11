"""Read-only V1 attribution and explicitly separate observed-data restatements."""
from collections import defaultdict
from pathlib import Path
import hashlib
import json
import numpy as np
import pandas as pd
from .runtime import root,source,read,write,freeze,state,utc,digest
from .data import snapshot,load,feature_input
from .kernel import replay,candidate_event
from src.watchlist.features import feature_frame,signal
from src.watchlist.research import choose

LABEL='ALREADY_OBSERVED_ENGINEERING_RESTATEMENT_NOT_NEW_OOS'
CASES=[('base',.001,1.),('stress25',.0025,1.),('stress50',.005,1.),('double_commission',.001,2.)]
PAYMENTS={('MSFT','2019-11-20'):('2019-12-12','https://news.microsoft.com/source/2019/09/18/microsoft-announces-quarterly-dividend-increase-and-new-share-repurchase-program/'),
          ('DPZ','2020-03-12'):('2020-03-30','https://ir.dominos.com/static-files/adf86b28-3231-4b2a-bdc8-62a0d698cf79'),
          ('STX','2018-03-20'):('2018-04-04','https://www.sec.gov/Archives/edgar/data/1137789/000119312518238271/d832608d10k.htm'),
          ('QCOM','2018-09-04'):('2018-09-26','https://www.qualcomm.com/news/releases/2018/07/qualcomm-announces-quarterly-cash-dividend'),
          ('AMAT','2019-11-20'):('2019-12-12','https://www.appliedmaterials.com/jp/ja/news-and-media/press-releases/cash-dividend-announcement-190905-jp.html')}

def action_audit():
    records=snapshot()['universe']['records'];calendar=defaultdict(list);events=[];intervals=[]
    for rec in records:
        s=rec['symbol'];doc=read(source()/'actions'/f'{s}.json',{})
        for kind,rows in doc.get('data',{}).items():
            for a in rows:
                day=a.get('ex_date') or a.get('effective_date') or a.get('process_date')
                e={'symbol':s,'kind':kind,'date':day,'event_id':a.get('id'),'provider_record':a,'status':'PROVIDER_EVIDENCE_NOT_EXHAUSTIVE'}
                if a.get('symbol')==s and kind in ('forward_splits','reverse_splits') and a.get('old_rate') and a.get('new_rate'):
                    calendar[day].append({'kind':'split','symbol':s,'ratio':float(a['new_rate'])/float(a['old_rate']),'id':a['id']})
                if a.get('symbol')==s and kind=='cash_dividends':
                    override=PAYMENTS.get((s,day));pay=a.get('payable_date') or (override[0] if override else None)
                    calendar[day].append({'kind':'dividend','symbol':s,'rate':float(a['rate']),'id':a['id'],'pay_date':pay})
                    e.update(pay_date=pay,payment_evidence=override[1] if override else ('ALPACA' if pay else 'UNKNOWN_RECEIVABLE_ONLY'))
                events.append(e)
        for path in (source()/'research').glob('*-trades.parquet'):
            t=pd.read_parquet(path);t=t[t.symbol==s] if 'symbol' in t else pd.DataFrame()
            for _,trade in t.iterrows():
                hits=[e for e in events if e['symbol']==s and e['date'] and trade.entry_date<e['date']<=trade.exit_date]
                status='NO_DISCLOSED_ACTION_IN_PROVIDER_INTERVAL' if not hits else 'ACTION_PRESENT'
                complex_hits=[e for e in hits if e['kind'] not in ('cash_dividends','forward_splits','reverse_splits')]
                for e in complex_hits:
                    a=e['provider_record']
                    e['held_role']='ACQUIRER_UNCHANGED_SHARES' if a.get('acquirer_symbol')==s and a.get('acquiree_symbol')!=s else 'REQUIRES_SECURITY_LEVEL_REVIEW'
                    if e['held_role']=='ACQUIRER_UNCHANGED_SHARES' and s=='SM':e['independent_evidence']='https://www.sec.gov/Archives/edgar/data/1509589/000110465926008521/tm264358d6_8k.htm'
                intervals.append({'result':path.stem,'symbol':s,'entry':trade.entry_date,'exit':trade.exit_date,'status':status,
                                  'events':hits,'independent_completeness':'NOT_VERIFIED'})
    write(root()/'action_calendar.json',dict(calendar));write(root()/'held_action_intervals.json',intervals)
    audit=read(root()/'gap_and_actions_audit.json',{})
    audit['actions']={'intervals':len(intervals),'with_events':sum(bool(x['events']) for x in intervals),
                      'payment_overrides':[{ 'symbol':s,'ex_date':d,'pay_date':p,'source':u} for (s,d),(p,u) in PAYMENTS.items()],
                      'evidence':'held_action_intervals.json','completeness':'PROVIDER_INTERVAL_COVERAGE_NOT_EXHAUSTIVE_ISSUER_CERTIFICATION',
                      'unknown_policy':'Unknown payment remains receivable. Complex acquiree actions require review; never silently converted.'}
    write(root()/'gap_and_actions_audit.json',audit);return dict(calendar)

def amounts(t):
    x=t.net_pnl if len(t) else pd.Series(dtype=float);wins=x[x>0];loss=x[x<0]
    return {'completed_trades':len(t),'net_usd':float(x.sum()),'profit_factor_usd':float(wins.sum()/-loss.sum()) if len(loss) else None,
            'mean_win_usd':float(wins.mean()) if len(wins) else None,'mean_loss_usd':float(loss.mean()) if len(loss) else None,
            'positive_count':len(wins),'negative_count':len(loss)}

def attribution():
    rows=[]
    windows=read(source()/'research'/'frozen_pipeline.json')['selections']
    for structure in freeze()['prior_structures'] if 'prior_structures' in freeze() else snapshot()['old_config']['structures']:
        for case,fric,fee in CASES:
            p=source()/'research'/f'{structure}-{case}-trades.parquet'
            if not p.exists():
                rows.append({'structure':structure,'case':case,'status':'MISSING_FILE_NOT_ZERO_TRADES'});continue
            t=pd.read_parquet(p);t['year']=t.exit_date.str[:4];t['family']=t.strategy.str.split('-').str[0]
            t['holding_rule']=t.strategy.str.extract(r'-h(\d+)-')[0]
            spans=sorted(set((x['evaluation_start'],x['evaluation_end'],x['stage']) for x in windows))
            t['window']=[next((f'{a}/{b}/{stage}' for a,b,stage in spans if a<=d<=b),'OUTSIDE_FROZEN_WINDOW') for d in t.entry_date]
            t['fees_usd']=2*fee;t['friction_usd']=t.qty*(t.entry*fric/(1+fric)+t.exit*fric/(1-fric))
            t['accounting_addback_pnl']=t.net_pnl+t.fees_usd+t.friction_usd
            curve=pd.read_parquet(source()/'research'/f'{structure}-{case}-equity.parquet')
            utilization=float(((curve.equity-curve.cash-curve.unsettled-curve.dividend_receivable)/curve.equity).mean())
            skips=read(source()/'research'/f'{structure}-{case}-skips.json',[])
            for group in ['ALL','symbol','year','family','holding_rule','reason','window']:
                groups=[('ALL',t)] if group=='ALL' else t.groupby(group,dropna=False)
                for key,g in groups:
                    rows.append({'structure':structure,'case':case,'group':group,'key':key,**amounts(g),
                                 'commissions_usd':float(g.fees_usd.sum()),'friction_usd':float(g.friction_usd.sum()),
                                 'cost_to_absolute_net_ratio':float((g.fees_usd.sum()+g.friction_usd.sum())/abs(g.net_pnl.sum())) if g.net_pnl.sum()!=0 else None,
                                 'same_fills_accounting_addback_usd':float(g.accounting_addback_pnl.sum()),
                                 'account_mean_capital_utilization':utilization,'account_skips':len(skips),
                                 'basis':LABEL,'count_domain':'SINGLE_ACCOUNT_COMPLETED_TRADES',
                                 'addback_basis':'ACCOUNTING_ONLY_SAME_FILLS_NOT_EXECUTABLE_COUNTERFACTUAL'})
    pd.DataFrame(rows).to_csv(root()/'loss_attribution.csv',index=False)
    state('LOSS_ATTRIBUTION_COMPLETE',rows=len(rows))

def mechanical():
    snap=snapshot();actions=read(root()/'action_calendar.json',{})
    from . import kernel
    version=digest({'kernel':Path(kernel.__file__).read_text(),'actions':actions,'snapshot':snap['hash']})
    out=root()/'restatement'/version[:16];out.mkdir(parents=True,exist_ok=True)
    frames={r['symbol']:load(r['symbol']) for r in snap['universe']['records'] if r['status']=='INCLUDED' and r['symbol']!='DXYZ'}
    meta={r['symbol']:r['identity_version'] for r in snap['universe']['records']};results=[]
    for structure in snap['old_config']['structures']:
        intents=read(source()/'research'/f'{structure}-intents.json');intent_hash=digest(intents)
        for case,fric,fee in CASES:
            summary_path=out/f'{structure}-{case}.json'
            if summary_path.exists():results.append(read(summary_path));continue
            old=pd.read_parquet(source()/'research'/f'{structure}-{case}-equity.parquet')
            ledger,curve=replay(frames,intents,old.date.tolist(),actions,fee,fric,meta)
            curve.to_parquet(out/f'{structure}-{case}-equity.parquet',index=False)
            pd.DataFrame(ledger.trades).to_parquet(out/f'{structure}-{case}-trades.parquet',index=False)
            write(out/f'{structure}-{case}-skips.json',ledger.skips)
            t0=pd.read_parquet(source()/'research'/f'{structure}-{case}-trades.parquet');t1=pd.DataFrame(ledger.trades)
            join=t0.merge(t1,on=['symbol','entry_date'],suffixes=('_old','_new'))
            join.to_csv(out/f'{structure}-{case}-fill-differences.csv',index=False)
            item={'structure':structure,'case':case,'old_equity':float(old.equity.iloc[-1]),'new_equity':float(curve.equity.iloc[-1]),
                  'input_version':version,'evidence_directory':str(out),
                  'delta_usd':float(curve.equity.iloc[-1]-old.equity.iloc[-1]),'old_trades':len(t0),'new_trades':len(t1),
                  'common_entries':len(join),'quantity_changed':int((join.qty_old!=join.qty_new).sum()),
                  'new_fees':ledger.fees,'new_skips':len(ledger.skips),'dividend_receivable':sum(x['amount'] for x in ledger.dividends),
                  'intent_hash':intent_hash,'basis':LABEL,'method':'FIXED_INTENTS_FULL_ACCOUNT_REPLAY_UNCHANGED_SIGNAL_RANK_PARAMETERS'}
            write(summary_path,item);results.append(item);state('MECHANICAL_RESTATEMENT',structure=structure,case=case)
    pd.DataFrame(results).to_csv(root()/'mechanical_restatement.csv',index=False)

def candidates():
    from . import kernel
    snap=snapshot();p=snap['old_config']
    version=digest({'kernel':Path(kernel.__file__).read_text(),'snapshot':snap['hash'],'actions':read(root()/'action_calendar.json',{})})
    out=root()/'candidate_restatement'/version[:16];out.mkdir(parents=True,exist_ok=True)
    benchmark=feature_input('SPY');evidence=[];new={};old={};differences=[]
    actions=read(root()/'action_calendar.json',{})
    for rec in snap['universe']['records']:
        s=rec['symbol'];f=feature_frame(feature_input(s),benchmark).loc[:p['development_end']]
        raw=load(s).set_index('trade_date').reindex(f.index);raw['trade_date']=raw.index
        # Reindexing makes missing exchange sessions explicit; never fabricate a price.
        raw['execution_eligible']=raw.execution_eligible.fillna(False).astype(bool)
        evidence.append({'symbol':s,'computed_at':utc(),'feature_data_asof':str(f.index[-1]) if len(f) else None,
                         'input_snapshot':snap['hash'],'features':{k:{'nonmissing':int(f[k].notna().sum()),
                         'sha256':hashlib.sha256(pd.util.hash_pandas_object(f[k],index=True).values.tobytes()).hexdigest()} for k in p['features']},
                         'independent_effectiveness':'NOT_TESTED'})
        path=out/f'{s}-events.parquet';rows=[]
        if path.exists():events=pd.read_parquet(path)
        else:
            for spec in p['configs']:
                next_allowed=0;mask=signal(f,spec)
                for i in np.flatnonzero(mask.to_numpy()):
                    start=i+1;end=start+spec['hold']
                    if start<next_allowed or end>len(f):continue
                    # PreserveSame full-path observability as V1, not a new missing-data search.
                    path0=raw.iloc[start:end]
                    if not path0.execution_eligible.all():continue
                    event=candidate_event(s,path0,spec,float(raw.iloc[i].volume),str(f.index[i]),{s:rec['identity_version']},actions=actions)
                    if event['status']=='COMPLETED':
                        event.update(config=spec['id'],market_up=bool(f.iloc[i].market_up),basis=LABEL)
                        rows.append(event);next_allowed=int(f.index.get_loc(event['exit_date']))+1
            events=pd.DataFrame(rows);events.to_parquet(path,index=False)
        new[s]={c['id']:events[events.config==c['id']] if len(events) else pd.DataFrame() for c in p['configs']}
        op=source()/'research'/f'{s}-development-events.parquet';prior=pd.read_parquet(op) if op.exists() else pd.DataFrame()
        old[s]={c['id']:prior[prior.config==c['id']] if len(prior) else pd.DataFrame() for c in p['configs']}
        if len(events) and len(prior):
            j=prior.merge(events,on=['symbol','config','signal_date'],suffixes=('_v1','_v11'))
            differences.extend(j[['symbol','config','signal_date','return_net_v1','return_net_v11','qty','net_pnl','entry_cost']].to_dict('records'))
        state('UNIFIED_CANDIDATE_DEVELOPMENT_ONLY',symbol=s,completed=len(evidence),total=66)
    write(root()/'feature_run_evidence.json',evidence);pd.DataFrame(differences).to_csv(root()/'candidate_cost_differences.csv',index=False)
    selections=[]
    for x in read(source()/'research'/'frozen_pipeline.json')['selections']:
        if x['stage']=='FINAL_HOLDOUT' or not x['eligible'] or not x['training']:continue
        s=x['symbol'];a=choose(old[s],x['training'],x['validation']);b=choose(new[s],x['training'],x['validation'])
        selections.append({'symbol':s,'evaluation_start':x['evaluation_start'],'old_selection':a[1],'new_selection':b[1],
                           'old_score':a[0],'new_score':b[0],'changed':a[1]!=b[1], 'basis':LABEL,
                           'scope':'PER_SYMBOL_DEVELOPMENT_SELECTION_SUBEXPERIMENT_NO_HOLDOUT_RESELECTION'})
    pd.DataFrame(selections).to_csv(root()/'selection_restatement.csv',index=False)
    write(root()/'candidate_restatement_summary.json',{'status':'COMPLETE','symbols':len(evidence),'selection_rows':len(selections),
          'changed_selections':sum(x['changed'] for x in selections),'holdout_reselection':False,
          'limitations':['Shared/regime selection recalculation not yet included; old fixed intents replayed separately.',
          'Current all-adjusted snapshot is not point-in-time historical vintage.','Independent factor effectiveness not tested.']})
    write(root()/'candidate_version.json',{'version':version,'path':str(out)})

def selection_recompute():
    """Same training shortlist/validation rules for all three structures, development only."""
    p=snapshot()['old_config'];sources={};rows=[];rank_rows=[];cache={}
    candidate_dir=Path(read(root()/'candidate_version.json',{'path':str(root()/'candidate_restatement')})['path'])
    for version,directory,suffix in [('V1',source()/'research','-development-events.parquet'),('V11',candidate_dir,'-events.parquet')]:
        symbols={}
        for rec in snapshot()['universe']['records']:
            path=directory/(rec['symbol']+suffix);e=pd.read_parquet(path) if path.exists() else pd.DataFrame()
            symbols[rec['symbol']]={c['id']:e[e.config==c['id']] if len(e) else pd.DataFrame() for c in p['configs']}
        pooled={}
        for c in p['configs']:
            parts=[v[c['id']] for s,v in symbols.items() if s!='DXYZ' and len(v[c['id']])]
            pooled[c['id']]=pd.concat(parts,ignore_index=True) if parts else pd.DataFrame()
        sources[version]=(symbols,pooled)
    for x in read(source()/'research'/'frozen_pipeline.json')['selections']:
        if x['stage']=='FINAL_HOLDOUT' or not x['eligible'] or not x['training']:continue
        for structure,regime in [('PER_SYMBOL',None),('SHARED',None),('HYBRID_UP',True),('HYBRID_DOWN',False)]:
            training=x['training'] if structure=='PER_SYMBOL' else x['shared_training']
            validation=x['validation'] if structure=='PER_SYMBOL' else x['shared_validation']
            result={}
            for version,(symbols,pooled) in sources.items():
                events=symbols[x['symbol']] if structure=='PER_SYMBOL' else pooled
                key=(version,x['symbol'] if structure=='PER_SYMBOL' else 'POOL',structure,tuple(training),tuple(validation))
                if key not in cache:
                    cache[key]=choose(events,training,validation,shared=structure!='PER_SYMBOL',regime=regime)
                    ranks=[]
                    for c in p['configs']:
                        if c['id']=='FIXED_LEGACY':continue
                        e=events[c['id']];scores=[];counts=[]
                        for span in [training,validation]:
                            a=e[(e.entry_date>=span[0])&(e.exit_date<=span[1])] if len(e) else e
                            if len(a) and regime is not None:a=a[a.market_up==regime]
                            values=a.groupby('entry_date').return_net.mean() if len(a) and structure!='PER_SYMBOL' else (a.return_net if len(a) else pd.Series(dtype=float))
                            scores.append(float(values.mean()) if len(values) else None);counts.append(len(values))
                        ranks.append({'version':version,'symbol':key[1],'structure':structure,'training_start':training[0],
                                      'validation_end':validation[1],'config':c['id'],'train_mean':scores[0],
                                      'validation_mean':scores[1],'train_count':counts[0],'validation_count':counts[1]})
                    eligible=sorted([r for r in ranks if r['train_count']>=10],key=lambda r:(-r['train_mean'],r['config']))
                    rank_by={r['config']:i+1 for i,r in enumerate(eligible)}
                    for r in ranks:r['training_rank']=rank_by.get(r['config']);r['shortlisted']=r['config'] in [a['config'] for a in eligible[:3]]
                    rank_rows.extend(ranks)
                result[version]=cache[key]
            rows.append({'symbol':x['symbol'],'structure':structure,'evaluation_start':x['evaluation_start'],
                         'old_selection':result['V1'][1],'new_selection':result['V11'][1],
                         'old_score':result['V1'][0],'new_score':result['V11'][0],
                         'changed':result['V1'][1]!=result['V11'][1],'basis':LABEL})
    pd.DataFrame(rows).to_csv(root()/'selection_restatement.csv',index=False)
    pd.DataFrame(rank_rows).to_csv(root()/'candidate_ranking_evidence.csv',index=False)
    write(root()/'candidate_restatement_summary.json',{'status':'COMPLETE','symbols':66,'selection_rows':len(rows),
          'changed_selections':sum(r['changed'] for r in rows),'holdout_reselection':False,'structures':['PER_SYMBOL','SHARED','HYBRID_UP','HYBRID_DOWN'],
          'scope':'Separate development-only measurement experiment, not promoted, not a new strategy search',
          'limitations':['Current all-adjusted snapshot is not point-in-time vintage.','Independent factor effectiveness not tested.']})

def audit_new_intervals():
    """Check new reference-event/action overlap; acquisition buyer is not acquiree."""
    directory=Path(read(root()/'candidate_version.json')['path']);rows=[];total=0
    for rec in snapshot()['universe']['records']:
        s=rec['symbol'];events=pd.read_parquet(directory/f'{s}-events.parquet');total+=len(events)
        if events.empty:continue
        for kind,actions in read(source()/'actions'/f'{s}.json',{}).get('data',{}).items():
            for a in actions:
                day=a.get('ex_date') or a.get('effective_date') or a.get('process_date')
                if not day:continue
                matched=events[(events.entry_date<day)&(events.exit_date>=day)]
                if matched.empty:continue
                role='EXPLICIT_SPLIT_OR_DIVIDEND_LEDGER' if kind in ('cash_dividends','forward_splits','reverse_splits') else (
                    'ACQUIRER_UNCHANGED_SHARES' if a.get('acquirer_symbol')==s and a.get('acquiree_symbol')!=s else 'UNRESOLVED_COMPLEX_ACTION')
                rows.append({'symbol':s,'date':day,'kind':kind,'event_id':a['id'],'reference_events':len(matched),'treatment':role,
                             'provider_record':a,'independent_completeness':'NOT_CERTIFIED'})
    write(root()/'candidate_action_intervals.json',{'reference_events_checked':total,'overlaps':rows,
          'unresolved_complex_overlaps':sum(r['reference_events'] for r in rows if r['treatment']=='UNRESOLVED_COMPLEX_ACTION')})
