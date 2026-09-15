"""Artificial unit inputs in pytest temp directories; never historical evidence."""
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import csv
import json
from pathlib import Path

import pytest

from src.futures_f0.input import QualifiedInputs, small_json
from src.futures_f0.model import ContractSpec, SessionBar
from src.futures_f0.rolls import decide_roll
from src.futures_f0.runtime import digest
from src.futures_f0.__main__ import run_normalized
from src.futures_f0.config import Config, ROOT

UTC=timezone.utc


def spec(contract='MESH2', **extra):
    base=ContractSpec(contract,'SP500','MES',5,0.25,date(2021,1,1),date(2022,3,18),date(2022,3,11),
        exchange='CME',quote_unit='SP500_index_points',verified=True,calendar_verified=True,
        vendor_definition_verified=True,boundary_verified=True,source='ENGINEERING_FIXTURE')
    return replace(base,**extra)


def bar(contract, day, close, volume, *, known=None):
    start=datetime.combine(day-timedelta(days=1),datetime.min.time(),UTC)+timedelta(hours=23)
    end=datetime.combine(day,datetime.min.time(),UTC)+timedelta(hours=22)
    return SessionBar(contract,day,start,end,known or end,close,close+1,close-1,close,volume,
                      '1'*64,session_verified=True,next_session=day+timedelta(days=1))


def pair(day, new_volume=200):
    return (bar('MESH2',day,100,100),bar('MESM2',day,110,new_volume))


def test_roll_needs_two_prior_consecutive_days_not_future_volume():
    old=spec(); new=spec('MESM2',last_trade=date(2022,6,17),safe_exit_session=date(2022,6,10))
    a,b=pair(date(2022,2,1)),pair(date(2022,2,2))
    known=b[0].available_at
    roll=decide_roll(old,new,[a,b],decision_at=known,next_session=date(2022,2,3))
    assert roll.reason=='TWO_PRIOR_SESSION_VOLUME_CROSS' and roll.execution_offset==10
    assert decide_roll(old,new,[a],decision_at=known,next_session=date(2022,2,2)) is None
    with pytest.raises(ValueError,match='FUTURE'):
        decide_roll(old,new,[a,b],decision_at=known-timedelta(seconds=1),next_session=date(2022,2,3))
    with pytest.raises(ValueError,match='CONSECUTIVE'):
        decide_roll(old,new,[a,pair(date(2022,2,3))],decision_at=known+timedelta(days=2),next_session=date(2022,2,4))


def test_safety_roll_does_not_need_volume_win_and_never_scales_product():
    old=spec(safe_exit_session=date(2022,2,3))
    new=spec('MESM2',last_trade=date(2022,6,17),safe_exit_session=date(2022,6,10))
    p=pair(date(2022,2,2),new_volume=20)
    roll=decide_roll(old,new,[p],decision_at=p[0].available_at,next_session=date(2022,2,3))
    assert roll.reason=='FIVE_SESSION_DELIVERY_BOUNDARY'
    with pytest.raises(ValueError,match='UNITS_MISMATCH'):
        decide_roll(old,replace(new,multiplier=1),[p],decision_at=p[0].available_at,next_session=date(2022,2,3))
    with pytest.raises(ValueError,match='MOCK_ROLL'):
        decide_roll(old,new,[(replace(p[0],is_mock=True),p[1])],decision_at=p[0].available_at,next_session=date(2022,2,3))


def manifest_fixture(tmp_path, definition_updates=None):
    definitions=[asdict(spec())]
    if definition_updates: definitions[0].update(definition_updates)
    entries={
        'definitions':definitions,'calendar':{},'settlements':[],
        'status':[],'mapping':[],'bars':[]}
    manifest={'kind':'EXCHANGE_FUTURES_ACTUAL_CONTRACTS','mock':False,
              'integration_review':'VERIFIED_RAW_TO_NORMALIZED','source_object_hashes':['1'*64]}
    for name,data in entries.items():
        p=tmp_path/(name+'.json');p.write_text(json.dumps(data,default=str),encoding='utf-8')
        manifest[name]={'path':p.name,'verified':True,'source':'ENGINEERING_FIXTURE_ONLY','sha256':digest(p)}
    p=tmp_path/'manifest.json';p.write_text(json.dumps(manifest),encoding='utf-8')
    return p


@pytest.mark.parametrize('updates',[{'multiplier':500},{'tick_size':25},
    {'listed':'2010-01-01'},{'vendor_definition_verified':False},{'market':'NOT_IN_POOL'},
    {'quote_unit':'CENTS_INSTEAD_OF_USD'},{'exchange':'OTHER_EXCHANGE'}])
def test_bad_contract_does_not_become_qualified(tmp_path,updates):
    p=manifest_fixture(tmp_path,updates)
    with pytest.raises(ValueError,match='NO_QUALIFIED'):
        QualifiedInputs(p,ROOT/'docs/futures_f0/contract_registry.csv')


def test_single_bad_contract_quarantined_while_valid_contract_retained(tmp_path):
    p=manifest_fixture(tmp_path)
    m=json.loads(p.read_text()); defs=tmp_path/'definitions.json'
    data=json.loads(defs.read_text()); data.append(data[0]|{'contract_id':'UNKNOWN','vendor_definition_verified':False})
    defs.write_text(json.dumps(data));m['definitions']['sha256']=digest(defs);p.write_text(json.dumps(m))
    inputs=QualifiedInputs(p,ROOT/'docs/futures_f0/contract_registry.csv')
    assert set(inputs.specs)=={'MESH2'} and inputs.quarantine_count==1


def test_raw_download_cannot_skip_normalization_review(tmp_path):
    p=manifest_fixture(tmp_path);m=json.loads(p.read_text());m.pop('integration_review');p.write_text(json.dumps(m))
    with pytest.raises(ValueError,match='NORMALIZATION_REVIEW'):
        QualifiedInputs(p,ROOT/'docs/futures_f0/contract_registry.csv')


def test_blocked_run_does_not_initialize_six_empty_accounts(tmp_path, monkeypatch):
    # This test isolates the input gate; an expired real pilot deadline is
    # covered by the resource tests and must not make this unit test age out.
    monkeypatch.setattr('src.futures_f0.__main__.Guard.check', lambda *a, **k: None)
    output=tmp_path/'futures-f0-mock-test'
    p=tmp_path/'bad_manifest.json';p.write_text('{"mock":true}')
    with pytest.raises(ValueError,match='REAL_EXCHANGE'):
        run_normalized(Config(output),p,'10')
    assert not (output/'runs').exists()


def test_bounded_metadata_and_nonfinite_json_rejected(tmp_path):
    p=tmp_path/'bad.json';p.write_text('{"a":NaN}')
    with pytest.raises(ValueError,match='NONFINITE'):small_json(p)
    with pytest.raises(ValueError,match='SIZE_BOUNDARY'):small_json(p,limit=1)


def test_root_registry_units_and_pool_match_protocol():
    from decimal import Decimal
    from src.futures_f0.protocol import MARKETS
    with (ROOT/'docs/futures_f0/contract_registry.csv').open(encoding='utf-8-sig',newline='') as f:
        rows=list(csv.DictReader(f))
    assert len(rows)==13 and len({r['root'] for r in rows})==13
    for row in rows:
        assert row['market'] in MARKETS
        assert Decimal(row['usd_multiplier_per_quote_unit'])*Decimal(row['tick_in_quote_units'])==Decimal(row['tick_value_usd'])
        assert row['verification_status']=='ROOT_TEMPLATE_ONLY' and row['contract_id']=='UNKNOWN'


@pytest.mark.parametrize('missing', ['status', 'tradable_open', 'tradable_stop', 'session_verified', 'is_mock'])
def test_omitted_execution_evidence_never_inherits_tradable_default(tmp_path, missing):
    inputs = QualifiedInputs(manifest_fixture(tmp_path), ROOT/'docs/futures_f0/contract_registry.csv')
    row = json.loads(json.dumps(asdict(bar('MESH2', date(2022, 1, 4), 100, 100)), default=str))
    row.pop(missing)
    with pytest.raises(ValueError, match='EXPLICIT_BAR_QUALIFICATION'):
        inputs._bar(row)


@pytest.mark.parametrize('value', ['true', 'false', 1, None])
def test_truthy_values_are_not_verified_execution_flags(tmp_path, value):
    inputs = QualifiedInputs(manifest_fixture(tmp_path), ROOT/'docs/futures_f0/contract_registry.csv')
    row = json.loads(json.dumps(asdict(bar('MESH2', date(2022, 1, 4), 100, 100)), default=str))
    row['tradable_open'] = value
    with pytest.raises(ValueError, match='EVIDENCE_FLAGS_MUST_BE_BOOLEAN'):
        inputs._bar(row)
