"""Frozen nine primary comparisons, all seeds retained. No promotion."""
import numpy as np
import pandas as pd
from .runtime import *
from .statistics import summary, by_adjust

def run():
    rows=read(root()/'account_summaries.json');p=protocol();by_id={x['id']:x for x in rows};primary=[];matching=[];concentration=[]
    calibration=read(root()/'calibration_receipt.json');method_ok=calibration['calibration_guard_passed']
    for h in p['horizons']:
        u=by_id[f'U-H{h}-base']
        for c in CONDITIONS:
            real=by_id[f'{c}-H{h}-base'];random=[x for x in rows if x['condition']==c and x['horizon']==h and x['kind']=='ACTIVITY_RANDOM']
            assert sorted(x['seed'] for x in random)==p['random_seeds']
            curve=pd.read_csv(root()/f'accounts/{real["id"]}-equity.csv')
            controls=[pd.read_csv(root()/f'accounts/{x["id"]}-equity.csv') for x in random]
            assert all(x.date.equals(curve.date) for x in controls)
            means=np.mean([x.equity.to_numpy() for x in controls],axis=0)
            delta=np.diff(np.r_[5500.,curve.equity.to_numpy()])/5500-np.diff(np.r_[5500.,means])/5500
            out=summary(delta);quality_ok=all(x['status']=='COMPUTED' and not x['missing_held_bar_sessions'] and not x['open_positions'] for x in [real,*random])
            primary.append({'condition':c,'horizon':h,**out,'primary_p':out['p'] if method_ok and quality_ok else 1.,
                            'calibration_guard_passed':method_ok,'account_replay_verified':quality_ok,
                            'inference_status':'EXPLORATORY_TEST' if method_ok and quality_ok else 'DESCRIPTION_ONLY_CALIBRATION_OR_DATA_LIMIT',
                            'net_pnl_usd':real['net_pnl_usd'],'random_mean_pnl_usd':float(np.mean([x['net_pnl_usd'] for x in random])),
                            'random_pnl_p05':float(np.quantile([x['net_pnl_usd'] for x in random],.05)),
                            'random_pnl_p95':float(np.quantile([x['net_pnl_usd'] for x in random],.95)),
                            'random_seed_count':len(random),'delta_pnl_vs_U':real['net_pnl_usd']-u['net_pnl_usd'],
                            'affected_random_accounts':sum(x['status']!='COMPUTED' or bool(x['missing_held_bar_sessions']) or bool(x['open_positions']) for x in random)})
            for refname,refs in [('RANDOM_ALL_50_MEAN',random),('UNCONDITIONAL',[u])]:
                row={'condition':c,'horizon':h,'reference':refname}
                for k in ['net_pnl_usd','completed_trades','total_cost_usd','close_utilization','post_entry_open_utilization','mean_holding_sessions']:
                    values=[x[k] for x in refs if x[k] is not None];m=float(np.mean(values)) if values else None
                    row['condition_'+k]=real[k];row['reference_'+k]=m;row['delta_'+k]=real[k]-m if m is not None and real[k] is not None else None
                row['exact_activity_match']=False;matching.append(row)
    adjusted=by_adjust([x['primary_p'] for x in primary])
    for row,q in zip(primary,adjusted):
        row['q_by']=float(q);row['positive_increment_exploratory']=q<=.05 and row['mean'] is not None and row['mean']>0
        row['positive_absolute_net_profit']=row['net_pnl_usd']>0
        row['promotion']='NOT_PERMITTED_HISTORICAL_REUSED_EXPLORATION'
    for r in rows:
        if r['kind']=='ACTIVITY_RANDOM':continue
        t=pd.read_csv(root()/f'accounts/{r["id"]}-trades.csv')
        if t.empty:continue
        t['exit_year']=t.exit_date.str[:4]
        for dimension in ['symbol','exit_year']:
            for value,g in t.groupby(dimension):concentration.append({'account_id':r['id'],'dimension':dimension,'value':value,
                'trades':len(g),'net_pnl_usd':float(g.total_net_pnl.sum()),'absolute_pnl_share':float(g.total_net_pnl.abs().sum()/t.total_net_pnl.abs().sum())})
        best=t.nlargest(5,'total_net_pnl')
        concentration.append({'account_id':r['id'],'dimension':'top_five_winning_trades','value':'TOP_5','trades':len(best),
                              'net_pnl_usd':float(best.total_net_pnl.sum()),'absolute_pnl_share':float(best.total_net_pnl.abs().sum()/t.total_net_pnl.abs().sum())})
    save_csv('primary_nine_tests.csv',primary);write(root()/'primary_nine_tests.json',primary)
    save_csv('activity_matching_errors.csv',matching);save_csv('concentration.csv',concentration)
    cost=[]
    for x in rows:
        if x['kind']=='ACTIVITY_RANDOM':continue
        base=by_id[f'{x["condition"]}-H{x["horizon"]}-base']
        cost.append({'account_id':x['id'],'condition':x['condition'],'horizon':x['horizon'],'cost_case':x['cost_case'],
                     **{f'delta_{k}_vs_base':x[k]-base[k] for k in ['net_pnl_usd','total_cost_usd','completed_trades','close_utilization','post_entry_open_utilization']}})
    save_csv('cost_exposure_differences.csv',cost)
    coverage=read(root()/'data_coverage.json')
    write(root()/'incomplete_and_limits.json',{'expected_accounts':486,'completed_accounts':len(rows),'missing_accounts':[],
        'securities_without_qualified_evaluation':[x for x in coverage if x['status']!='AVAILABLE'],
        'accounts_with_complex_actions_or_missing_prices':[{k:x[k] for k in ['id','status','missing_held_bar_sessions','open_positions','complex_action_hits']} for x in rows if x['status']!='COMPUTED' or x['missing_held_bar_sessions'] or x['open_positions']],
        'inference_limited':[x for x in primary if x['inference_status']!='EXPLORATORY_TEST'],
        'not_proven':['New blind out-of-sample advantage','Exact activity/exposure matching','Guaranteed real fills','Point-in-time universe or adjusted vintage','Rank-bootstrap calibration']})
