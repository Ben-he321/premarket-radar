"""Synthetic engineering fixtures only; never open market data or holdout."""
import subprocess
import types
import pandas as pd
import pytest
from src.watchlist.runtime import root,write,read
from src.watchlist import report
from src.watchlist.engine import metrics,Ledger,diagnostics

REVIEWED='8b601c04e6581dbf23c36920b4c7ecc55f2be89e'


def fixture_report(scenario):
    base=root();out=base/'research';out.mkdir(parents=True,exist_ok=True)
    write(base/'universe.json',{'records':[]});write(base/'coverage.json',[])
    write(base/'preregistration.json',{'holdout_start':'2020-02-01'})
    write(out/'frozen_pipeline.json',{'selections':[{'stage':'OUTER','evaluation_start':'2020-01-02','evaluation_end':'2020-01-31'}]})
    t=pd.DataFrame(columns=report.TRADE_COLUMNS)
    if scenario in ('zero_winners','zero_losses'):
        pnl=-5. if scenario=='zero_winners' else 5.
        t=pd.DataFrame([dict(symbol='MOCK',signal_date='2020-01-01',entry_date='2020-01-02',exit_date='2020-01-03',
                            qty=1,entry=100.,exit=100.+pnl,net_pnl=pnl,return_net=pnl/100,reason='MOCK',strategy='MOCK')])
    portfolios=[]
    for name in ('SHARED','PER_SYMBOL','HYBRID_REGIME'):
        for cost in ('base','stress25','stress50','double_commission'):
            stem=name+'-'+cost
            if scenario!='missing_file':t.to_parquet(out/(stem+'-trades.parquet'),index=False)
            pd.DataFrame({'date':['2020-01-02','2020-01-03'],'equity':[5500.,5500.],
                          'cash':[5500.,5500.],'unsettled':[0.,0.],'dividend_receivable':[0.,0.],'positions':[0,0]}).to_parquet(out/(stem+'-equity.parquet'),index=False)
            portfolios.append({'structure':name,'cost_case':cost,'ending_equity':5500.,'max_drawdown':0.,'outer':metrics(t),'holdout':metrics(pd.DataFrame())})
    write(out/'portfolio.json',portfolios)
    return base


@pytest.mark.parametrize('scenario,exception',[('missing_file',FileNotFoundError),('zero_trades',TypeError)])
def test_reviewed_report_failure_reproduced(scenario,exception):
    fixture_report(scenario)
    code=subprocess.check_output(['git','show',REVIEWED+':src/watchlist/report.py']).decode()
    original=types.ModuleType('src.watchlist.reviewed_report');original.__package__='src.watchlist'
    exec(compile(code,'reviewed_report.py','exec'),original.__dict__)
    with pytest.raises(exception):original.produce()


@pytest.mark.parametrize('scenario',['zero_trades','zero_winners','zero_losses','missing_file'])
def test_report_full_chain_handles_degenerate_cases(scenario):
    base=fixture_report(scenario)
    assert report.produce()=={}
    assert 'N/A' in (base/'RESULTS.md').read_text(encoding='utf-8')
    promotion=read(base/'research/promotion.json')
    assert len(promotion)==3 and not any(x['qualified'] for x in promotion)
    if scenario=='missing_file':assert all('MISSING_TRADE_FILE' in x['blockers'] for x in promotion)
    if scenario=='zero_trades':assert all('NO_COMPLETED_OUTER_TRADES' in x['blockers'] for x in promotion)
    if scenario=='zero_losses':assert all(x['largest_trade_positive_profit_share']==1 for x in promotion)


def test_commission_basis_difference_known_fixture():
    spec={'id':'MOCK','stop':.05,'target':None,'hold':1}
    d=pd.DataFrame({'open':[100.,100.],'high':[100.,100.],'low':[100.,100.],
                    'close':[100.,100.],'volume':[100000.,100000.],'market_up':[True,True]},index=['2020-01-02','2020-01-03'])
    e=diagnostics(d,spec,pd.Series([True,False],index=d.index))
    ledger=Ledger();ledger.buy('MOCK',d.iloc[1],'2020-01-03',0,spec,5500,100000,'2020-01-02')
    assert ledger.positions['MOCK']['qty']==5
    ledger.sell('MOCK',100,'2020-01-03',0,'MOCK')
    assert e.return_net.iloc[0]==pytest.approx(-.0038161838161838823)
    assert ledger.trades[0]['return_net']==pytest.approx(-3/501.5)
    assert ledger.trades[0]['net_pnl']==pytest.approx(-3.)
