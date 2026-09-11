"""Engineering fixtures for timing, dependence and accounting; not real research evidence."""
import numpy as np
import pandas as pd
from src.v12.data import label_frame
from src.v12.statistics import inference,by_adjust,draws,nonoverlap
from src.v12.accounts import trade_components

def test_labels_enter_next_open_and_mature_inside_calendar():
    d=pd.DataFrame({'open':[10,20,30,40,50,60],'high':[12,22,32,42,52,62],
                    'low':[9,19,29,39,49,59],'close':[11,21,31,41,51,61],'volume':[100]*6},index=list('abcdef'))
    f=label_frame(d,d,5)
    assert f.label.iloc[0]==61/20-1 and f.entry_date.iloc[0]=='b' and f.exit_date.iloc[0]=='f'
    assert f.label.iloc[1:].isna().all()
    d.loc['c','volume']=0
    assert label_frame(d,d,5).label.isna().all()

def test_missing_reference_is_not_zero_excess():
    d=pd.DataFrame({'open':[100.]*6,'high':[101.]*6,'low':[99.]*6,'close':[100.]*6,'volume':[100.]*6},index=list('abcdef'))
    f=label_frame(d,pd.DataFrame(),5)
    assert f.label.iloc[0]==0 and f.spy_label.isna().all() and f.excess_spy.isna().all()

def test_candidate_export_cent_rounding_reconciles():
    from src.v11.kernel import candidate_event
    path=pd.DataFrame({'trade_date':['2020-01-02','2020-01-03'],'open':[100.,100.],'high':[101.,101.],
                       'low':[99.,99.],'close':[100.,100.],'volume':[100000.,100000.]})
    spec={'id':'fixture','hold':2,'stop':.05,'target':None}
    a={'2020-01-03':[{'symbol':'X','kind':'dividend','rate':.115,'id':'d','pay_date':None}]}
    t=candidate_event('X',path,spec,100000,'2020-01-01',actions=a)
    assert t['price_net_pnl']==-3 and t['dividend_entitlement']==.5750000000000001
    assert t['total_net_pnl']==-2.43 and t['return_net']==t['total_net_pnl']/t['entry_cost']

def test_no_fake_ci_for_short_history_or_no_reference():
    d=pd.DataFrame({'position':np.arange(10),'label':np.arange(10)/100,'factor':np.arange(10),
                    'bucket':['weak']*5+['strong']*5,'excess_spy':[np.nan]*10})
    rows,summary=inference(d,5,[0,10])
    assert summary['status']=='INSUFFICIENT_EVIDENCE' and summary['ic_p'] is None
    assert all(r['mean_ci_low'] is None and r['spy_excess_mean'] is None for r in rows)

def test_common_date_blocks_deterministic_and_dependence_adjusted():
    a=draws(tuple(range(10)));b=draws(tuple(range(10)))
    assert np.array_equal(a,b) and (a.sum(axis=1)==10).all()
    assert nonoverlap(range(20),5)==4
    q=by_adjust([.001,.2,None]);assert q[0]>=.001 and q[-1]==1 and len(q)==3

def test_price_dividend_total_components_with_split():
    t={'symbol':'X','entry_date':'2020-01-01','exit_date':'2020-01-06','qty':10,'net_pnl':-3.,'entry_cost':501.5,'return_net':-3/501.5}
    actions={'2020-01-02':[{'kind':'split','symbol':'X','ratio':2,'id':'s'}],
             '2020-01-03':[{'kind':'dividend','symbol':'X','rate':.5,'id':'d'}]}
    x=trade_components(t,actions)
    assert x['price_net_pnl']==-3 and x['dividend_entitlement']==5 and x['total_net_pnl']==2
    assert x['return_net']==2/501.5
