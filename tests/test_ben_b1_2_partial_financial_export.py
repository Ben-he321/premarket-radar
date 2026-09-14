"""Synthetic report fixtures only; no historical replay or database access."""
import csv
import copy
import pytest
from src.ben_b1.ledger import Ledger
from src.ben_b1_2.compact import stream_hash
from src.ben_b1_2_continuation import report


def fixture(tmp_path,monkeypatch):
    monkeypatch.setattr(report,'ROOT',tmp_path)
    report.write(tmp_path/'RUNNER_PROCESS.json',{'status':'STOPPED'})
    source=tmp_path/'account';output=tmp_path/'public';output.mkdir()
    ledger=Ledger().to_dict()
    ledger['state']['fills']={'f1':{'fill_id':'f1','quantity':2,'execution_price':100}}
    ledger['state']['events']=[{'type':'TEST_FILL','date':'2026-01-23','amount':200}]
    cp={'ledger':ledger,'state':{'pending':{'o1':{'order_id':'o1','status':'FILLED'}},
        'stops':{'ABC':[95,90]},'b12_completed_day':{'day':'2026-01-23'},
        'at':'2026-01-23T21:15:00+00:00','quote_inventory':{'PRIVATE_QUOTE':'excluded'}}}
    report.write(source/'checkpoint.json',{'payload':cp,'sha256':stream_hash(cp)})
    return source,output,cp


def test_saved_financial_rows_are_exact_and_checkpoint_unchanged(tmp_path,monkeypatch):
    source,output,cp=fixture(tmp_path,monkeypatch)
    before=(source/'checkpoint.json').read_bytes()
    result=report.export_durable_financial_evidence(source,output)
    assert result['financial_ledger']==cp['ledger']
    assert result['execution_financial_state']['stops']==cp['state']['stops']
    assert result['checkpoint_proof']['engine_restored'] is False
    assert result['events_executed']==0 and result['sqlite_opened'] is False
    assert result['row_counts']=={'orders':1,'fills':1,'account_events':1,'settlements':0}
    with (output/'Q1_B_DURABLE_FILLS.csv').open(encoding='utf-8-sig') as stream:
        rows=list(csv.DictReader(stream))
    assert rows[0]['fill_id']=='f1' and rows[0]['quantity']=='2'
    assert not any('PRIVATE_QUOTE' in p.read_text(encoding='utf-8-sig') for p in output.iterdir())
    assert (source/'checkpoint.json').read_bytes()==before


def test_no_export_while_writer_active_or_into_account(tmp_path,monkeypatch):
    source,output,cp=fixture(tmp_path,monkeypatch)
    report.write(tmp_path/'RUNNER_PROCESS.json',{'status':'RUNNING'})
    with pytest.raises(ValueError,match='WRITER_ACTIVE'):report.export_durable_financial_evidence(source,output)
    assert not list(output.iterdir())
    report.write(tmp_path/'RUNNER_PROCESS.json',{'status':'STOPPED'})
    with pytest.raises(ValueError,match='OUTSIDE_ACCOUNT'):report.export_durable_financial_evidence(source,source)


def test_tampered_saved_financial_data_is_rejected_before_export(tmp_path,monkeypatch):
    source,output,cp=fixture(tmp_path,monkeypatch)
    changed=copy.deepcopy(cp);changed['ledger']['state']['cash']=99999
    report.write(source/'checkpoint.json',{'payload':changed,'sha256':stream_hash(cp)})
    with pytest.raises(ValueError,match='WRAPPER_HASH_FAILED'):report.export_durable_financial_evidence(source,output)
    assert not list(output.iterdir())
