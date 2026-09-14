"""Small explicit engineering fixtures; no research accounts are initialized."""
from types import SimpleNamespace
import sqlite3
import pytest
from src.ben_b1_2_continuation import migration as m
from src.ben_b1_2.compact import stream_hash

def fixture(tmp_path):
    archive=tmp_path/'source.sqlite';dest=tmp_path/'destination.sqlite';cp=tmp_path/'checkpoint.json'
    payload={'config':{'run_id':'ENGINEERING_FIXTURE'},'ledger':{'state':{'cash':87.92,'positions':{'BE':{'quantity':27}}}},
        'state':{'archive':{'path':str(archive.resolve()),'applied_sequence':1,'applied_hash':'fixture-anchor','archived_events':9,'archived_zero_consumption_inventories':5},
            'b12_completed_day':{'day':'2026-01-23','begin_fills':13},'at':'2026-01-23T21:15:00Z','events_processed':9,
            'orders':{'order1':{'remaining':2}},'quote_inventory':{'quote1':{'consumed':3}},'stop_legs':[4,8]}}
    m.write(cp,{'payload':payload,'sha256':stream_hash(payload)})
    return cp,archive,dest,payload

def test_only_archive_path_and_wrapper_change_with_all_components_preserved(tmp_path):
    cp,archive,dest,payload=fixture(tmp_path)
    original=m.read(cp);result=m.relocate_checkpoint(cp,archive,dest);got=m.read(cp)
    assert got['sha256']==stream_hash(got['payload'])==result['relocated_wrapper_sha256']
    got['payload']['state']['archive']['path']=str(archive.resolve())
    assert got['payload']==original['payload']
    assert result['cash']==87.92 and result['archive_anchor']['applied_sequence']==1
    assert result['original_state_hashes']['quote_inventory']==result['expected_state_hashes']['quote_inventory']

@pytest.mark.parametrize('failure',['wrapper','path'])
def test_invalid_source_does_not_modify_checkpoint(tmp_path,failure):
    cp,archive,dest,payload=fixture(tmp_path)
    if failure=='wrapper':m.write(cp,{'payload':payload,'sha256':'corrupt'})
    before=cp.read_bytes()
    with pytest.raises(ValueError):m.relocate_checkpoint(cp,archive if failure=='wrapper' else tmp_path/'wrong.sqlite',dest)
    assert cp.read_bytes()==before

def prepared(tmp_path,monkeypatch):
    cp,archive,dest,payload=fixture(tmp_path)
    expected=m.relocate_checkpoint(cp,archive,dest);proof=tmp_path/'engineering/STORAGE_MIGRATION_EXPECTED.json';m.write(proof,expected)
    got=m.read(cp)['payload'];monkeypatch.setattr(m.tempfile,'gettempdir',lambda:str(tmp_path/'_sqlite_tmp'))
    engine=SimpleNamespace(_payload=lambda:got,state=got['state'],state_digest=lambda:stream_hash(got),
        ledger=SimpleNamespace(cash=87.92,positions={'BE':{'quantity':27}}),config=SimpleNamespace(run_id='ENGINEERING_FIXTURE'))
    return expected,engine

def test_actual_state_binding_pass_then_financial_mutation_rejected(tmp_path,monkeypatch):
    expected,engine=prepared(tmp_path,monkeypatch)
    m.verify_migrated_engine(engine,tmp_path)
    result=m.read(tmp_path/'engineering/STORAGE_MIGRATION_RESTORE.json')
    assert result['status']=='PASS' and result['events_executed_before_verification']==0
    proof=tmp_path/'engineering/STORAGE_MIGRATION_RESTORE.json';before=(m.sha(proof),proof.stat().st_mtime_ns)
    m.verify_migrated_engine(engine,tmp_path)
    assert (m.sha(proof),proof.stat().st_mtime_ns)==before
    engine.state['quote_inventory']['quote1']['consumed']=0
    with pytest.raises(ValueError,match='STATE_DIGEST'):m.verify_migrated_engine(engine,tmp_path)

def test_wrong_process_temp_is_rejected_before_restore_approval(tmp_path,monkeypatch):
    expected,engine=prepared(tmp_path,monkeypatch)
    monkeypatch.setattr(m.tempfile,'gettempdir',lambda:str(tmp_path/'wrong'))
    with pytest.raises(ValueError,match='PROCESS_TEMP'):m.verify_migrated_engine(engine,tmp_path)
    assert not (tmp_path/'engineering/STORAGE_MIGRATION_RESTORE.json').exists()

def test_later_legal_checkpoint_requires_unchanged_migration_archive_prefix(tmp_path,monkeypatch):
    expected,engine=prepared(tmp_path,monkeypatch);m.verify_migrated_engine(engine,tmp_path)
    engine.state['b12_completed_day']['day']='2026-01-26'
    db=sqlite3.connect(':memory:');engine._db=db
    try:
        db.execute('CREATE TABLE archive_blocks(sequence INTEGER,block_hash TEXT)')
        db.execute('INSERT INTO archive_blocks VALUES(1,?)',('fixture-anchor',))
        m.verify_migrated_engine(engine,tmp_path)
        engine.state['archive']['applied_sequence']=0
        with pytest.raises(ValueError,match='NOT_CLAIMED'):m.verify_migrated_engine(engine,tmp_path)
        engine.state['archive']['applied_sequence']=1
        db.execute("UPDATE archive_blocks SET block_hash='changed'")
        with pytest.raises(ValueError,match='PREFIX_ANCHOR'):m.verify_migrated_engine(engine,tmp_path)
    finally:db.close()
