"""Synthetic engineering fixtures are isolated from real cached research."""
import numpy as np
import pandas as pd
from src.v13.accounts import uniform,priority,make_orders,replay_indexed
from src.v13.data import eligibility
from src.v13.statistics import resolution,distribution
from src.v11.kernel import replay

def fixture():
    days=[f'2020-01-{i:02}' for i in range(1,9)]
    raw=pd.DataFrame({'trade_date':days,'open':[100.]*8,'high':[101.]*8,'low':[99.]*8,'close':[100.]*8,'volume':[100000.]*8})
    orders={days[1]:[{'symbol':'X','signal_date':days[0],'previous_volume':100000.,'spec':{'id':'test','hold':5,'stop':.05,'target':None}}]}
    bars={d:{'X':r} for d,r in zip(days,raw.to_dict('records'))}
    return days,raw,orders,bars

def test_indexed_replay_uses_same_cash_fill_kernel():
    days,raw,orders,bars=fixture()
    actions={days[3]:[{'kind':'dividend','symbol':'X','rate':.115,'id':'d','pay_date':days[5]}]}
    a,ca=replay({'X':raw},orders,days,actions)
    b,cb,_,_=replay_indexed(bars,orders,days,actions,{},1.,.001,{})
    assert np.allclose(ca.equity,cb.equity) and a.cash==b.cash
    assert a.trades[0]['qty']==b.trades[0]['qty'] and a.trades[0]['net_pnl']==b.trades[0]['net_pnl']

def test_future_missing_quote_does_not_remove_known_intent():
    days,raw,_,bars=fixture()
    rows={0:[{'symbol':'X','signal_date':days[0],'previous_volume':100000.,'window_end':8,
              'accepted':{'M20':True},'probabilities':{'M20':1.}}]}
    orders=make_orders(rows,days,'M20',5)
    assert len(orders[days[1]])==1
    bars.pop(days[1]);ledger,_,_,skips=replay_indexed(bars,orders,days,{}, {},1.,.001,{})
    assert not ledger.trades and skips['NO_EXECUTABLE_BAR']==1

def test_future_action_not_entry_filter_and_degrades_held_account():
    days,raw,orders,bars=fixture()
    complex_={days[3]:[{'symbol':'X','status':'UNVERIFIED_COMPLEX_ACTION','kind':'spin','day':days[3]}]}
    a,curve,issues,_=replay_indexed(bars,orders,days,{},complex_,1.,.001,{})
    assert len(a.trades)==1 and len(issues)==1
    assert not curve.unverified_complex_action.iloc[:3].any() and curve.unverified_complex_action.iloc[3:].all()

def test_missing_held_quote_keeps_position_and_exposure_includes_same_day_exit():
    days,raw,orders,bars=fixture();bars.pop(days[6]);bars.pop(days[5])
    a,c,_,_=replay_indexed(bars,orders,days,{}, {},1.,.001,{})
    assert a.trades[0]['exit_date']==days[7] and c.missing_held_bars.sum()==2
    assert c.post_entry_open_utilization.iloc[-1]>0 and c.close_utilization.iloc[-1]==0

def test_split_on_missing_quote_preserves_share_unit_valuation():
    days,raw,orders,bars=fixture();bars.pop(days[3])
    for day in days[4:]:
        for k in ['open','high','low','close']:bars[day]['X'][k]/=2
    actions={days[3]:[{'symbol':'X','kind':'split','ratio':2,'id':'s'}]}
    _,curve,_,_=replay_indexed(bars,orders,days,actions,{},1.,.001,{})
    assert abs(curve.equity.iloc[3]-curve.equity.iloc[2])<.01

def test_training_probability_never_reads_evaluation_values():
    dates=list('abcdefghij')
    f=pd.DataFrame({'return_20':[.1]*10,'relative20':[.1]*10,'return_5':[-.1]*10,'observed':[True]*10},index=dates)
    raw=pd.DataFrame({'trade_date':dates,'execution_eligible':[True]*10})
    p={'windows':[{'id':0,'train':[0,4],'evaluation':[6,10]}],'days':dates,'minimum_training_observations':4}
    a,pa,_=eligibility(f,raw,p);f.iloc[6:8,0]=-.9;b,pb,_=eligibility(f,raw,p)
    assert np.array_equal(a,b) and np.array_equal(pa['M20'][6:],pb['M20'][6:])
    assert (pa['M20'][6:]==1).all()

def test_finite_resolution_and_by_step_up():
    old=resolution(1980,500);new=resolution(9,10000)
    assert not old[0]['minimum_p_reachable_at_rank'] and old[-1]['minimum_p_reachable_at_rank']
    assert all(x['minimum_p_reachable_at_rank'] for x in new)
    assert new[0]['minimum_p']>0

def test_date_blocks_share_draws_and_constant_shift():
    x=np.sin(np.arange(1200)/30)/100
    est,boot,_=distribution(np.column_stack([x,x+.01]))
    assert np.allclose(boot[:,1]-boot[:,0],.01) and np.isclose(est[1]-est[0],.01)

def test_hashes_are_reproducible_and_priority_rotates():
    assert uniform(13001,'X','2020-01-01')==uniform(13001,'X','2020-01-01')
    assert uniform(13001,'X','2020-01-01')!=uniform(13002,'X','2020-01-01')
    orders={tuple(sorted(['X','Y'],key=lambda s:priority(s,f'2020-01-{i:02}'))) for i in range(1,30)}
    assert len(orders)==2

def test_empty_qualified_sample_never_expands_to_full_calendar():
    from src.v13.data import label_frame
    days,raw,_,_=fixture();f=raw.set_index('trade_date');f['return_20']=.1
    labels=label_frame(f,f,5);base=labels[np.zeros(len(labels),bool)].copy()
    base['return_20']=f.return_20.reindex(base.index)
    assert base.empty and not base.position.isna().any()

def test_windows_checkpoint_retries_without_losing_data(tmp_path,monkeypatch):
    import src.v13.runtime as runtime
    original=runtime.atomic_write;calls=[]
    def busy(path,value):
        calls.append(1)
        if len(calls)<3:raise PermissionError('temporary Windows reader lock')
        return original(path,value)
    monkeypatch.setattr(runtime,'atomic_write',busy);monkeypatch.setattr(runtime.time,'sleep',lambda _:None)
    runtime.write(tmp_path/'state.json',{'completed':9})
    assert runtime.read(tmp_path/'state.json')=={'completed':9} and len(calls)==3

def test_round_trip_trade_csv_retains_half_cent_kernel_result(tmp_path):
    from src.v11.kernel import rounded
    price=-6.72;dividend=.525*7
    t=pd.DataFrame([{'price':price,'dividend':dividend,'net':rounded(price+dividend)}])
    p=tmp_path/'trades.csv';t.to_csv(p,index=False)
    read=pd.read_csv(p,float_precision='round_trip')
    assert rounded(read.price.iloc[0]+read.dividend.iloc[0])==read.net.iloc[0]

def test_numpy_false_remains_false_after_report_json_roundtrip(tmp_path):
    from src.v13.runtime import write,read
    p=tmp_path/'primary.json';write(p,{'positive_increment_exploratory':np.bool_(False),'count':np.int64(0)})
    x=read(p)
    assert x['positive_increment_exploratory'] is False and type(x['count']) is int
