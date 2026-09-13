"""Pipeline contracts: no synthetic market fallback and no silent identity mix."""
import hashlib
import json
from pathlib import Path
import pandas as pd
import pytest
from src.ben_b1 import scan
from src.ben_b1.rules import initial_stop_plan, net_reward_risk, nearest_overhead, constrained_entry_limit
from src.ben_b1.research import make_revision, read_frame


def source(tmp_path, monkeypatch, symbol='X', wrong=False, empty=False):
    root=tmp_path/'source';root.mkdir()
    output=tmp_path/'research';output.mkdir()
    monkeypatch.setattr(scan,'DATA',root);monkeypatch.setattr(scan,'ROOT',output)
    record={'symbol':symbol,'identity_version':'v1','research_start':'2020-01-01'}
    p=root/'object.parquet'
    columns=['symbol','trade_date','open','high','low','close','volume']
    rows=[] if empty else [['WRONG' if wrong else symbol,'2020-01-02',10,11,9,10.5,100]]
    pd.DataFrame(rows,columns=columns).to_parquet(p,index=False)
    manifest={'symbol':symbol,'identity_version':'v1','adjustment':'raw','chunks':{'a':{'status':'OK','path':'object.parquet','sha256':hashlib.sha256(p.read_bytes()).hexdigest()}}}
    mp=root/'datasets'/symbol/'v1/raw/manifest.json';mp.parent.mkdir(parents=True);mp.write_text(json.dumps(manifest))
    return record,p,mp


def test_empty_historical_chunk_is_not_wrong_identity(tmp_path,monkeypatch):
    record,_,_=source(tmp_path,monkeypatch,empty=True)
    assert scan.load(record,'raw',[]).empty


def test_wrong_security_is_never_substituted(tmp_path,monkeypatch):
    record,_,_=source(tmp_path,monkeypatch,wrong=True)
    with pytest.raises(ValueError,match='PARQUET_SECURITY_MISMATCH'):
        scan.load(record,'raw',[])


def test_tampered_object_is_rejected_before_research(tmp_path,monkeypatch):
    record,p,_=source(tmp_path,monkeypatch)
    p.write_bytes(p.read_bytes()+b'corruption')
    with pytest.raises(ValueError,match='SOURCE_HASH_MISMATCH'):
        scan.load(record,'raw',[])


def test_revision_selection_is_recorded(tmp_path,monkeypatch):
    record,p,mp=source(tmp_path,monkeypatch)
    f=pd.read_parquet(p);f.loc[0,'close']=10.6;p2=p.with_name('revision.parquet');f.to_parquet(p2,index=False)
    m=json.loads(mp.read_text());m['chunks']['b']={'status':'OK','path':'revision.parquet','sha256':hashlib.sha256(p2.read_bytes()).hexdigest()};mp.write_text(json.dumps(m))
    audit=[];f=scan.load(record,'raw',[],audit)
    assert f.close.iloc[0]==10.6 and audit[0]['different_ohlcv'] and len(audit)==1


def test_missing_pressure_never_becomes_no_known_overhead():
    overhead=nearest_overhead(100,{20:98,50:97,100:96},float('nan'))
    stop=initial_stop_plan(100,100,{10:98,20:97,50:96,100:95},20)
    complete=overhead['status'] in {'KNOWN_OVERHEAD','NO_KNOWN_OVERHEAD_IN_DEFINED_SET'}
    assert not net_reward_risk(100,stop.legs,overhead['price'],overhead_inputs_complete=complete)['allowed']


def test_component_limit_api_compatible_and_fee_stress_lowers_limit():
    stops=initial_stop_plan(100,100,{10:98,20:97,50:96,100:95},20)
    baseline=constrained_entry_limit(100,stops.legs,105,commission=1,exit_friction=.001)
    stress=constrained_entry_limit(100,stops.legs,105,commission=2,exit_friction=.0025)
    assert stress<=baseline<=101


def test_date_only_release_adapter_does_not_fabricate_time():
    event={'symbol':'X','conservative_known_from_date':'2026-07-17','planned_publication_time_ny':'UNKNOWN',
           'planned_release_date_ny':'2026-08-06',
           'actual_date_ny':'2026-08-06','actual_time_ny':'UNKNOWN','planned_source':'https://issuer.example/release',
           'release_session':'UNKNOWN'}
    revision=make_revision(event)
    assert revision.actual_release_at is None and revision.actual_release_date=='2026-08-06'


def test_missing_source_is_error_not_empty_success(tmp_path,monkeypatch):
    monkeypatch.setattr(scan,'DATA',tmp_path)
    with pytest.raises(FileNotFoundError):
        scan.load({'symbol':'X','identity_version':'v1'},'raw',[])


def test_research_reader_verifies_consumed_parquet(tmp_path):
    p=tmp_path/'data.parquet';pd.DataFrame({'symbol':['X'],'close':[10.]}).to_parquet(p,index=False)
    req={'complete':True,'symbol':'X','path':str(tmp_path)}
    pinned={str(p):hashlib.sha256(p.read_bytes()).hexdigest()}
    assert len(read_frame(req,pinned))==1
    pd.DataFrame({'symbol':['X'],'close':[20.]}).to_parquet(p,index=False)
    with pytest.raises(ValueError,match='CONSUMED_INPUT_HASH_MISMATCH'):
        read_frame(req,pinned)
