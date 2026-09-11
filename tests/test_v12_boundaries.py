"""Isolated fixtures for the two inherited runtime boundaries."""
from datetime import datetime,date,timedelta,timezone
from src.v11.paper import Account,decision_cycle
from src.v11.runtime import write
from tests.test_v11_paper import CONFIG,SPEC,NOW,CUTOFF,candidate,decide,Market

def test_held_block_does_not_prevent_other_held_stop(tmp_path):
    a=Account(tmp_path/'ledger.sqlite',CONFIG);decide(a,items=[candidate('AMD'),candidate('NVDA')])
    a.match(NOW.replace(hour=13,minute=56),Market([('2026-09-11T13:30:00Z',100,101,99,100)]))
    assert set(a.status()['positions'])=={'AMD','NVDA'}
    m=Market([('2026-09-11T13:36:00Z',90,91,89,90)])
    a.match(NOW.replace(hour=14,minute=0),m,{'2026-09-11':[{'symbol':'AMD','kind':'BLOCK','id':'review'}]})
    assert 'AMD' in a.status()['positions'] and 'NVDA' not in a.status()['positions']
    assert a.status()['sell_fills']==1

def test_partial_failure_retries_only_pending_and_never_duplicates(tmp_path,monkeypatch):
    import src.v11.paper as p
    monkeypatch.setenv('V11_RUN_DIR',str(tmp_path/'v11'))
    a=Account(tmp_path/'ledger.sqlite',CONFIG);records=[{'symbol':s,'status':'INCLUDED'} for s in ['AMD','NVDA']];calls=[]
    def loader(now,market,symbols):
        calls.append(symbols)
        failures=[{'symbol':'NVDA','reason':'REQUEST_FAILED'}] if len(calls)==1 else []
        write(p.root()/'forward_input_status.json',{'issues':failures})
        return [candidate(s) for s in symbols if not (s=='NVDA' and failures)],SPEC,CUTOFF,now.isoformat()
    kwargs=dict(input_loader=loader,actions_loader=lambda *a:{})
    first=decision_cycle(a,None,NOW,records,clock_now=lambda:NOW,**kwargs)
    assert first['symbols']['AMD']['status']=='SIGNAL_COMPLETED'
    assert first['symbols']['NVDA']['status']=='PENDING_RETRY'
    a=Account(tmp_path/'ledger.sqlite',CONFIG)
    later=NOW+timedelta(minutes=2)
    second=decision_cycle(a,None,later,records,clock_now=lambda:later,**kwargs)
    assert calls==[['AMD','NVDA'],['NVDA']]
    assert a.status()['immutable_intents']==4
    decision_cycle(a,None,later,records,clock_now=lambda:later,**kwargs)
    assert len(calls)==2 and a.status()['immutable_intents']==4

def test_retry_budget_and_late_fetch_never_backdates(tmp_path,monkeypatch):
    import src.v11.paper as p
    monkeypatch.setenv('V11_RUN_DIR',str(tmp_path/'v11'))
    a=Account(tmp_path/'ledger.sqlite',CONFIG);records=[{'symbol':'AMD','status':'INCLUDED'}]
    def failed(now,market,symbols):raise TimeoutError()
    for minutes in [0,2,4,6]:
        now=NOW+timedelta(minutes=minutes)
        doc=decision_cycle(a,None,now,records,input_loader=failed,clock_now=lambda:now)
    assert doc['symbols']['AMD']['attempts']==3
    assert doc['symbols']['AMD']['status']=='RETRY_LIMIT_REACHED'
    late=NOW.replace(hour=14)
    doc=decision_cycle(a,None,late,records,input_loader=failed,clock_now=lambda:late)
    assert doc['symbols']['AMD']['status']=='MISSED_WINDOW' and a.status()['immutable_intents']==0

def test_successful_fetch_finishes_after_window(tmp_path,monkeypatch):
    import src.v11.paper as p
    monkeypatch.setenv('V11_RUN_DIR',str(tmp_path/'v11'))
    a=Account(tmp_path/'ledger.sqlite',CONFIG)
    def loader(now,market,symbols):
        write(p.root()/'forward_input_status.json',{'issues':[]})
        return [candidate()],SPEC,CUTOFF,now.isoformat()
    doc=decision_cycle(a,None,NOW,[{'symbol':'NVDA','status':'INCLUDED'}],input_loader=loader,actions_loader=lambda *a:{},clock_now=lambda:NOW.replace(hour=14))
    assert doc['symbols']['NVDA']['status']=='MISSED_WINDOW' and a.status()['immutable_intents']==0
