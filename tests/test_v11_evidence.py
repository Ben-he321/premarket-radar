"""Causal feature engineering and report/UI boundary checks, not alpha tests."""
from pathlib import Path
import numpy as np
import pandas as pd
from streamlit.testing.v1 import AppTest
from src.watchlist.features import feature_frame,signal
from src.v11.report import FORMULAS
from src.v11.paper import Account
from datetime import datetime,timezone

def test_feature_prefix_invariance_and_formulas():
    from src.data.alpaca_calendar import sessions
    from datetime import date
    days=[str(x) for x in sessions(date(2020,1,1),date(2020,9,1))]
    c=np.arange(len(days),dtype=float)+100
    f=pd.DataFrame(dict(trade_date=days,open=c,high=c+1,low=c-1,close=c,volume=c*100))
    a=feature_frame(f,f);g=f.copy();g.loc[100:,'close']*=2;b=feature_frame(g,f)
    numeric=[name for name,*_ in FORMULAS if name not in ('market_up','observed')]
    pd.testing.assert_frame_equal(a[numeric].iloc[:100],b[numeric].iloc[:100])
    assert np.isclose(a.return_20.iloc[80],180/160-1)
    assert np.isclose(a.atr14_ratio.iloc[80],2/180)
    assert np.isclose(a.relative20.iloc[80],0)
    assert len(numeric)==22

def test_new_page_empty_reports_do_not_invent_results(tmp_path,monkeypatch):
    monkeypatch.setenv('V11_RUN_DIR',str(tmp_path/'new'))
    monkeypatch.setenv('V11_SOURCE_DIR',str(tmp_path/'old'))
    p=Path(__file__).resolve().parents[1]/'pages/13_V1_1执行.py'
    app=AppTest.from_file(str(p)).run(timeout=20)
    assert not app.exception
    assert any('缺失' in w.value for w in app.warning)

def test_cash_settles_without_quotes(tmp_path):
    a=Account(tmp_path/'test.sqlite',{'experimental':{'budget_price_collar':.05}})
    with a.connect() as c:
        l=a.ledger(c);l.cash=5000;l.unsettled=[{'due':0,'amount':497}];a.save(c,l)
    a.roll_cash(datetime(2026,9,11,tzinfo=timezone.utc))
    assert a.status()['cash']==5497
    a.roll_cash(datetime(2026,9,11,tzinfo=timezone.utc));assert a.status()['cash']==5497
