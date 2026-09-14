"""Mock SQL fixtures only; real event equivalence runs remain separate."""
import ast
import sqlite3
from pathlib import Path
import pytest
from src.ben_b1_2_continuation import execution
from src.ben_b1_2_continuation.query_check import verify_restart
from src.ben_b1_2_continuation.runtime import write,read,sha,RUN_ID
from src.ben_b1_2.compact import stream_hash
from types import SimpleNamespace


def statements():
    tree=ast.parse(Path(execution.__file__).read_text(encoding='utf-8'))
    return [n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str)
            and n.value.startswith('SELECT 1 FROM stage_')]


def database():
    db=sqlite3.connect(':memory:')
    db.executescript('''
      CREATE TABLE archived_events(event_id TEXT PRIMARY KEY,digest TEXT NOT NULL,block_sequence INTEGER NOT NULL);
      CREATE TABLE archived_quotes(inventory_id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,block_sequence INTEGER NOT NULL);
      CREATE INDEX events_by_block ON archived_events(block_sequence,event_id);
      CREATE INDEX quotes_by_block ON archived_quotes(block_sequence,inventory_id);
      CREATE TEMP TABLE stage_events(event_id TEXT PRIMARY KEY,digest TEXT) WITHOUT ROWID;
      CREATE TEMP TABLE stage_quotes(inventory_id TEXT PRIMARY KEY,payload TEXT,digest TEXT) WITHOUT ROWID;
    ''')
    return db


@pytest.mark.parametrize('kind',['events','quotes'])
@pytest.mark.parametrize('case',['empty','new_id','same_generation_same_digest','changed_digest','old_generation','future_generation','mixed_conflict'])
def test_conflict_results_equal_original_for_all_generations(kind,case):
    query=next(q for q in statements() if 'stage_'+kind+' ' in q)
    with database() as db:
        current=7;stage=[];archived=[];expected=None
        if case!='empty':
            stage=[('key','digest')];archived=[('other','digest',current)]
        if case=='same_generation_same_digest':archived=[('key','digest',current)]
        if case=='changed_digest':archived=[('key','changed',current)];expected=(1,)
        if case=='old_generation':archived=[('key','digest',current-1)];expected=(1,)
        if case=='future_generation':archived=[('key','digest',current+1)];expected=(1,)
        if case=='mixed_conflict':
            stage=[('ok','digest'),('bad','digest')];archived=[('ok','digest',current),('bad','digest',current+1)];expected=(1,)
        if kind=='events':
            db.executemany('INSERT INTO stage_events VALUES(?,?)',stage)
            db.executemany('INSERT INTO archived_events VALUES(?,?,?)',archived)
        else:
            db.executemany('INSERT INTO stage_quotes VALUES(?,?,?)',[(k,'{}',d) for k,d in stage])
            db.executemany('INSERT INTO archived_quotes VALUES(?,?,?,?)',[(k,'{}',d,g) for k,d,g in archived])
        assert db.execute(query,(current,)).fetchone()==db.execute(query.replace('CROSS JOIN','JOIN'),(current,)).fetchone()==expected


@pytest.mark.parametrize('query',statements())
def test_stage_is_outer_loop_without_statistics(query):
    with database() as db:
        plan=[r[3] for r in db.execute('EXPLAIN QUERY PLAN '+query,(7,))]
        assert plan[0]=='SCAN s'
        assert any('SEARCH a USING INDEX sqlite_autoindex_archived_' in r for r in plan)


def mock_prefix(tmp_path):
    payload={'ledger':{'cash':87.92},'state':{'b12_completed_day':{'day':'2026-01-25'},
        'archive':{'applied_sequence':3},'events_processed':123}}
    db=sqlite3.connect(':memory:');db.execute('CREATE TABLE archive_blocks(sequence INTEGER,block_hash TEXT)')
    db.execute("INSERT INTO archive_blocks VALUES(3,'anchor')")
    engine=SimpleNamespace(state=payload['state'],_payload=lambda:payload,state_digest=lambda:stream_hash(payload),_db=db,
        ledger=SimpleNamespace(cash=87.92,positions={'BE':{'quantity':27},'NVDA':{'quantity':14}}),config=SimpleNamespace(run_id=RUN_ID))
    p=tmp_path/'engineering/QUERY_FIX_RESTORE_EXPECTED.json'
    write(p,{'day':'2026-01-25','wrapper_sha256':stream_hash(payload),'payload_hashes':{k:stream_hash(v) for k,v in payload.items()},
        'state_hashes':{k:stream_hash(v) for k,v in payload['state'].items()},'archive_anchor':{'applied_sequence':3,'applied_hash':'anchor'}})
    return engine,p


def test_restart_prefix_pass_is_immutable(tmp_path):
    engine,p=mock_prefix(tmp_path);verify_restart(engine,tmp_path)
    out=tmp_path/'engineering/QUERY_FIX_ACTUAL_PREFIX_RESTORE.json';before=sha(out)
    verify_restart(engine,tmp_path)
    assert sha(out)==before and read(out)['status']=='PASS'
    engine._db.close()


def test_restart_prefix_changed_financial_payload_rejected(tmp_path):
    engine,p=mock_prefix(tmp_path);engine._payload()['ledger']['cash']=100
    with pytest.raises(ValueError,match='NOT_IDENTICAL'):verify_restart(engine,tmp_path)
    assert not (tmp_path/'engineering/QUERY_FIX_ACTUAL_PREFIX_RESTORE.json').exists()
    engine._db.close()


def test_restart_missing_expected_proof_rejected(tmp_path):
    with pytest.raises(ValueError,match='EXPECTED_PROOF_MISSING'):verify_restart(None,tmp_path)


@pytest.mark.parametrize('fault',['unclaimed','changed_hash'])
def test_later_restart_requires_prior_pass_and_claimed_unchanged_anchor(tmp_path,fault):
    engine,p=mock_prefix(tmp_path);engine.state['b12_completed_day']['day']='2026-01-26'
    with pytest.raises(ValueError,match='FIRST_PREFIX_RESTORE_MISSING'):verify_restart(engine,tmp_path)
    engine.state['b12_completed_day']['day']='2026-01-25';verify_restart(engine,tmp_path)
    engine.state['b12_completed_day']['day']='2026-01-26'
    if fault=='unclaimed':engine.state['archive']['applied_sequence']=2
    else:engine._db.execute("UPDATE archive_blocks SET block_hash='changed'")
    with pytest.raises(ValueError,match='ANCHOR_CHANGED_OR_UNCLAIMED'):verify_restart(engine,tmp_path)
    engine._db.close()


@pytest.mark.parametrize('later',[False,True])
@pytest.mark.parametrize('field,value',[('state_hashes',{}),('full_archive_verification_completed',False),
                                      ('events_executed_before_prefix_verification',1),('same_account_run_id','wrong_account')])
def test_existing_pass_content_is_bound_and_never_overwritten(tmp_path,later,field,value):
    engine,p=mock_prefix(tmp_path);verify_restart(engine,tmp_path)
    out=tmp_path/'engineering/QUERY_FIX_ACTUAL_PREFIX_RESTORE.json';record=read(out);record[field]=value;write(out,record);before=sha(out)
    if later:engine.state['b12_completed_day']['day']='2026-01-26'
    with pytest.raises(ValueError,match='PREFIX_PROOF_CHANGED|FIRST_PREFIX_RESTORE_MISSING'):verify_restart(engine,tmp_path)
    assert sha(out)==before
    engine._db.close()
