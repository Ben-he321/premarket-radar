"""OFFLINE MOCK fixtures only. No generated rows enter research storage."""
from datetime import date
import json
import numpy as np
import pandas as pd
import pytest
from src.watchlist.engine import Ledger,exit_quote,replay,diagnostics,metrics
from src.watchlist.features import feature_frame,signal
from src.watchlist.research import choose,boundaries,block_ci
from src.watchlist.data import plan,manifest,load,location,_publish
from src.watchlist.runtime import root,write
from src.watchlist.universe import classify
from src.data.alpaca_calendar import sessions
from src.watchlist.statistics import module

SPEC={'id':'mock','family':'A','hold':3,'stop':.05,'target':.10,'volume_threshold':1.2}

def bars(n=150):
    days=[str(x) for x in sessions(date(2023,1,1),date(2024,12,31))][:n]
    c=100+np.arange(n)*.1
    return pd.DataFrame({'trade_date':days,'timestamp':pd.to_datetime(days,utc=True),'symbol':'TEST','open':c,'close':c,'high':c+2,'low':c-2,'volume':100000.,'vwap':c,'trade_count':100.})

def rec(symbol='TEST'):
    return {'symbol':symbol,'identity_version':'mock1','status':'INCLUDED','type':'COMMON','research_start':'2023-01-01'}

def test_causal_features_and_missing_sessions():
    b=bars();a=feature_frame(b,b);changed=b.copy();changed.loc[100:,'close']*=2
    c=feature_frame(changed,changed)
    pd.testing.assert_frame_equal(a.iloc[:100],c.iloc[:100])
    gap=feature_frame(b.drop(index=50),b)
    assert pd.isna(gap.iloc[50].close) and pd.isna(gap.iloc[51].return_1)

def test_entry_after_signal_and_labels_mature():
    d=feature_frame(bars(20));mask=pd.Series(False,index=d.index);mask.iloc[3]=True;mask.iloc[-1]=True
    e=diagnostics(d,SPEC,mask)
    assert len(e)==1 and e.iloc[0].entry_date>d.index[3] and e.iloc[0].exit_date<=d.index[-1]

def test_selection_cannot_read_holdout_or_unmatured():
    e=pd.DataFrame({'entry_date':['2023-01-01']*10+['2023-02-01']*5+['2025-01-01'],
                    'exit_date':['2023-01-03']*10+['2023-02-03']*5+['2025-01-03'],'return_net':[.01]*15+[100.]})
    spans=(('2023-01-01','2023-01-31'),('2023-02-01','2023-02-28'))
    before=choose({'A':e},*spans);e.loc[15,'return_net']=-1000
    assert choose({'A':e},*spans)==before
    assert choose({'A':e},spans[0],('2023-02-01','2023-02-02'))==(0.,None)

def test_windows_purge_embargo():
    days=[str(x) for x in sessions(date(2016,1,1),date(2024,1,1))]
    train,val=boundaries(days,1000,True)
    assert days.index(train[1])-days.index(train[0])+1==756
    assert days.index(val[0])-days.index(train[1])==7
    assert 1000-days.index(val[1])==7

def test_cash_integer_risk_and_simultaneous_positions():
    l=Ledger();bar={'open':100.}
    for s in 'ABCDE':l.buy(s,bar,'2024-01-02',0,SPEC,5500,100000,'2024-01-01')
    assert len(l.positions)==4 and l.cash>=0
    assert all(isinstance(p['qty'],int) and p['cost']<=1101 for p in l.positions.values())
    assert l.skips[-1]['reason']=='MAX_POSITIONS'
    assert l.fees==4

def test_expensive_share_and_unknown_liquidity_not_dropped():
    l=Ledger();assert not l.buy('X',{'open':10000},'d',0,SPEC,5500,1e6,'p')
    assert l.skips[-1]['reason']=='CAPITAL_CONSTRAINT'
    assert not l.buy('Y',{'open':100},'d',0,SPEC,5500,np.nan,'p')
    assert l.skips[-1]['reason']=='LIQUIDITY_UNKNOWN'

def test_split_dividend_settlement_and_account_identity():
    l=Ledger();l.buy('X',{'open':100},'2024-01-02',0,SPEC,5500,1e6,'2024-01-01')
    before=l.equity({'X':100});q=l.positions['X']['qty']
    l.split('X',2,'split1');l.split('X',2,'split1')
    assert l.positions['X']['qty']==q*2 and l.equity({'X':50})==pytest.approx(before)
    l.dividend('X',1,'div1','2024-01-05');l.dividend('X',1,'div1','2024-01-05')
    assert len(l.dividends)==1 and l.equity({'X':50})==pytest.approx(before+q*2)
    cash=l.cash;l.sell('X',50,'2024-01-03',1,'TEST')
    assert l.cash==cash and l.unsettled
    l.settle(1);assert l.cash==cash
    l.settle(2);assert not l.unsettled
    l.pay_dividends('2024-01-05');assert not l.dividends
    assert l.equity({})==pytest.approx(5500+l.realized+q*2)

def test_gap_and_adverse_same_bar():
    assert exit_quote({'open':80,'low':70,'high':120},95,110)==(80.,'GAP_STOP')
    assert exit_quote({'open':100,'low':90,'high':120},95,110)==(95.,'STOP_AND_TARGET_ADVERSE')

def test_current_final_volume_does_not_decide_open_and_initial_drawdown():
    b=bars(3);days=b.trade_date.tolist();b.loc[0,['open','high','low','close']]=[100,100,90,90]
    b.loc[0,'volume']=0
    orders={days[0]:[{'symbol':'TEST','spec':SPEC,'rank':0,'previous_volume':100000,'signal_date':'2022-12-30'}]}
    l,c=replay({'TEST':b},orders,days)
    assert len(l.trades)==1 and c.drawdown.iloc[0]<0

def test_empty_cash_path():
    l,c=replay({}, {},['2024-01-02','2024-01-03'])
    assert c.equity.tolist()==[5500,5500] and metrics(pd.DataFrame())['win_rate'] is None


def test_intraday_exit_cannot_free_slot_at_earlier_open():
    b=bars(3);b['open']=100.;b['close']=100.;b['low']=99.;b['high']=101.
    frames={s:b.copy() for s in 'ABCDE'};frames['A'].loc[1,'low']=90
    days=b.trade_date.tolist()
    order=lambda s:{'symbol':s,'spec':SPEC,'rank':0,'previous_volume':100000,'signal_date':'2022-12-30'}
    l,_=replay(frames,{days[0]:[order(s) for s in 'ABCD'],days[1]:[order('E')]},days)
    assert any(x['symbol']=='E' and x['reason']=='MAX_POSITIONS' for x in l.skips)

def test_identity_excludes_etf_and_does_not_guess():
    assert classify('UNKNOWN',None)['status']=='IDENTITY_UNRESOLVED'
    etf={'ETF':'Y','Security Name':'Example ETF','exchange':'NASDAQ','source':'mock'}
    assert classify('ETF',etf)['status']=='EXCLUDED_BY_TYPE'

def test_pool_addition_gets_all_years_cached_old_is_untouched():
    a=rec('OLD');b=rec('NEW');end=date(2024,12,31)
    write(location(a,'raw')/'manifest.json',{'schema':2,'identity_version':'mock1','adjustment':'raw','chunks':{'2023-OLD':{'status':'OK','end':'2023-12-31'},'2024-OLD':{'status':'OK','end':'2024-12-31'}}})
    write(location(a,'all')/'manifest.json',{'schema':2,'identity_version':'mock1','adjustment':'all','chunks':{'2023-OLD':{'status':'OK','end':'2023-12-31'},'2024-OLD':{'status':'OK','end':'2024-12-31'}}})
    u={'records':[a,b],'references':[],'universe_version':'new','mapping_version':'m'}
    jobs=plan(u,end)
    assert len(jobs)==4 and {j['record']['symbol'] for j in jobs}=={'NEW'}
    assert min(j['start'] for j in jobs)==date(2023,1,1)

def test_identity_and_schema_cache_separation():
    a=rec();write(location(a,'all')/'manifest.json',{'schema':1,'identity_version':'mock1','adjustment':'all','chunks':{}})
    with pytest.raises(ValueError,match='CACHE_VERSION'):manifest(a,'all')
    b={**a,'identity_version':'mock2'};assert manifest(b,'all')['chunks']=={}

def test_adjustment_pending_blocks_research():
    write(root()/'revision_pending/TEST.json',{'status':'REBUILDING_ALL'})
    with pytest.raises(ValueError,match='ADJUSTMENT_REBUILD'):load(rec(),'all')

def test_quarantine_preserves_response_but_not_bad_day():
    b=bars(3);b.loc[1,'vwap']=0
    job={'record':rec(),'adj':'raw','start':date(2023,1,3),'end':date(2023,1,5),'key':'2023-TEST','source':'TEST','signature':'mock'}
    status,_=_publish(job,b,{'universe_version':'mock','mapping_version':'mock'})
    assert status=='OK' and len(load(rec(),'raw'))==2
    entry=manifest(rec(),'raw')['chunks']['2023-TEST']
    assert len(pd.read_parquet(entry['quarantine_path']))==3 and entry['quarantined_dates']==[b.trade_date.iloc[1]]

def test_skill_statistics_known_answers():
    try:
        dsr=module('backtest-overfit/backtest-overfit/scripts/deflated_sharpe.py','test_dsr')
        pbo=module('backtest-overfit/backtest-overfit/scripts/pbo_cscv.py','test_pbo')
    except FileNotFoundError:pytest.skip('Optional pinned GPL skills not installed')
    assert dsr.probabilistic_sharpe_ratio(0,0,100,0,3)==pytest.approx(.5)
    assert dsr.expected_max_sharpe(.1,1)==0
    assert dsr.annualised_to_per_period(np.sqrt(252))==pytest.approx(1)
    # Known ranking: each configuration excels only in one half -> OOS reversal.
    a=np.array([[2,0],[3,1],[0,2],[1,3]],float)
    result=pbo.probability_of_backtest_overfitting(a,n_blocks=2)
    assert result.pbo==1 and result.n_splits==2


def test_single_security_failure_isolated_and_resume(monkeypatch):
    from src.watchlist import data
    from src.data.alpaca_client import DataAccessError
    monkeypatch.setattr(data,'finalized_day',lambda:date(2023,1,5))
    good=rec('TEST');bad=rec('FAIL')
    universe={'records':[good,bad],'references':[],'universe_version':'mock','mapping_version':'mock'}
    class Client:
        def __init__(self):self.calls=[]
        def probe(self,kind,end):return {'status':'ACCESS_OK' if kind=='historical' else 'SIP_PERMISSION_DENIED'}
        def bars(self,symbols,start,end,adj):
            self.calls.append(symbols)
            if 'FAIL' in symbols:raise DataAccessError('MOCK_FAILURE')
            return bars(3)
    client=Client();assert data.sync(universe,client=client)
    assert len(load(good,'raw'))==3 and load(bad,'raw').empty
    jobs=data.plan(universe,date(2023,1,5))
    assert len(jobs)==2 and all(j['record']['symbol']=='FAIL' for j in jobs)
