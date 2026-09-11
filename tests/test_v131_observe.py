"""MOCK read-only reporting fixtures. No market requests or research replay."""
from copy import deepcopy
from datetime import datetime,date
from pathlib import Path
import json,sqlite3
from src.v131.observe import read_book,metrics,decision_time,complete_cycle,export_cycles,run
from src.v131.runtime import write,sha
from src.data.alpaca_calendar import sessions

def empty():
    return {'ledger':{'cash':5500.,'positions':{},'pending':{},'unsettled':[],'dividends':[],'trades':[],'fees':0.,'realized':0.},'intents':[],'events':[],'orders':[]}

def db(path,book):
    path.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(path) as c:
        for table in ['state','intents','events','orders']:c.execute(f'CREATE TABLE {table} (id TEXT,payload TEXT)')
        c.execute('INSERT INTO state VALUES (?,?)',('1',json.dumps(book['ledger'])))
        for table in ['intents','events','orders']:
            for i,x in enumerate(book[table]):c.execute(f'INSERT INTO {table} VALUES (?,?)',(str(i),json.dumps(x)))

def test_readonly_missing_database_and_unchanged_file(tmp_path):
    p=tmp_path/'book.sqlite';assert read_book(p) is None and not p.exists()
    db(p,empty());before=sha(p);assert metrics(read_book(p))['net_pnl']==0;assert sha(p)==before

def test_calendar_and_europe_us_dst_difference():
    sep=decision_time(datetime.fromisoformat('2026-09-11T15:00:00+00:00'))
    assert sep['new_york']=='2026-09-14T06:30:00-04:00' and sep['madrid']=='2026-09-14T12:30:00+02:00'
    october=decision_time(datetime.fromisoformat('2026-10-23T15:00:00+00:00'))
    assert october['madrid']=='2026-10-26T11:30:00+01:00'
    november=decision_time(datetime.fromisoformat('2026-10-30T15:00:00+00:00'))
    assert november['madrid']=='2026-11-02T12:30:00+01:00'

def test_cost_not_double_deducted_and_reserve_not_equity():
    b=empty();l=b['ledger'];l.update(cash=4499.,fees=1.,pending={'p':50.})
    l['positions']={'X':{'qty':10,'last':100,'entry_friction':.2,'exit_fee_reserve':1}}
    m=metrics(b);assert m['equity']==5499 and m['net_pnl']==-1 and m['reserved_cash']==51 and m['cost_total']==1.2

def cycle_fixture():
    name='EXP_M20_H20';b=empty();due=len(sessions(date(2016,1,1),date(2026,9,16)))-1
    b['intents']=[{'id':'buy','created_at':'2026-09-14T10:30:00+00:00'},{'id':'exit','parent_id':'buy'}]
    b['events']=[{'type':'BUY','intent_id':'buy','effective_at':'2026-09-14T13:30:00+00:00','booked_at':'2026-09-14T13:51:00+00:00'},
      {'type':'SELL','intent_id':'exit','exit_date':'2026-09-15','booked_at':'2026-09-15T14:51:00+00:00','exit_proceeds':21.},
      {'type':'CASH_SETTLEMENT','booked_at':'2026-09-16T04:00:00+00:00','amount':21.}]
    held=deepcopy(b);held['ledger']['positions']={'X':{'paper_intent':'buy'}}
    sold=deepcopy(b);sold['ledger']['unsettled']=[{'due':due,'amount':21.}]
    history=[{'observed_at':'2026-09-14T15:00:00+00:00','books':{name:held}},
             {'observed_at':'2026-09-15T15:00:00+00:00','books':{name:sold}}]
    return name,b,history

def test_cycle_requires_all_actual_stages_and_matching_settlement(tmp_path):
    name,b,history=cycle_fixture();assert complete_cycle(b,history,name)
    missing=deepcopy(b);missing['events']=missing['events'][:-1];assert complete_cycle(missing,history,name) is None
    assert complete_cycle(b,history[1:],name) is None
    mismatch=deepcopy(b);mismatch['events'][-1]['amount']=22;assert complete_cycle(mismatch,history,name) is None
    result=export_cycles(tmp_path,{name:b},history);p=Path(result[name]['path']);h=sha(p)
    export_cycles(tmp_path,{name:b},history);assert sha(p)==h

def test_cycle_export_redacts_unexpected_sensitive_fields(tmp_path):
    import zipfile
    name,b,history=cycle_fixture();b['intents'][0]['api_key']='MOCK_SECRET_NEVER_EXPORT'
    result=export_cycles(tmp_path,{name:b},history)
    with zipfile.ZipFile(result[name]['path']) as z:
        assert 'MOCK_SECRET_NEVER_EXPORT' not in z.read('cycle_events.json').decode()
        assert z.testzip() is None and sorted(z.namelist())==['cycle_events.json','manifest.json']

def test_no_fake_cycle_and_old_receipt_remains_unknown(tmp_path):
    for name in ['EXP_M20_H20','CONTROL_U_H20']:db(tmp_path/'forward'/name/'ledger.sqlite',empty())
    write(tmp_path/'FORWARD_PROTOCOL.json',{'protocol_hash':'MOCK'})
    write(tmp_path/'forward_status.json',{'pid':123,'service':'RUNNING','heartbeat':'2026-09-14T11:00:00+00:00'})
    write(tmp_path/'forward/decisions/2026-09-14.json',{'coverage':[{'symbol':'X','state':'QUALIFIED','signal_date':'2026-09-11'}]})
    source=tmp_path/'forward_cache/2026-09-11/decision_inputs.json'
    write(source,{'cutoff':'2026-09-12T04:00:00+00:00','candidates':[{'symbol':'X','M20':False}]})
    h=sha(source);out=run(tmp_path,datetime.fromisoformat('2026-09-14T11:01:00+00:00'))
    assert not list((tmp_path/'forward_observation').glob('*.zip')) and sha(source)==h
    archive=tmp_path/'forward_observation'/out['decision_archive'][0]['report'];doc=json.loads(archive.read_text(encoding='utf-8'))
    assert doc['input_sources'][0]['market_received_at']=='UNKNOWN'
    assert doc['input_sources'][0]['pipeline_assembled_at']=='UNKNOWN'
