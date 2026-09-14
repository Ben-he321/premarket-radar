"""Fixed engineering verification for the two archive join loop hints."""
import shutil
import xml.etree.ElementTree as ET
from src.ben_b1_2.compact import stream_hash
from .runtime import *
from .migration import processes


def proof_matches_expected(actual,expected,expected_sha256):
    return bool(actual.get('status')=='PASS' and actual.get('expected_sha256')==expected_sha256
        and actual.get('wrapper_sha256')==expected['wrapper_sha256'] and actual.get('payload_hashes')==expected['payload_hashes']
        and actual.get('state_hashes')==expected['state_hashes'] and actual.get('day')==expected['day']
        and actual.get('full_archive_verification_completed') is True and actual.get('events_executed_before_prefix_verification')==0
        and actual.get('same_account_run_id')==RUN_ID and actual.get('same_deadline')==DEADLINE)


def verify_restart(engine,root):
    expected_path=root/'engineering/QUERY_FIX_RESTORE_EXPECTED.json'
    if not expected_path.exists():raise ValueError('QUERY_FIX_PREFIX_EXPECTED_PROOF_MISSING')
    expected=read(expected_path);dest=root/'engineering/QUERY_FIX_ACTUAL_PREFIX_RESTORE.json'
    day=engine.state['b12_completed_day']['day']
    if day>expected['day']:
        prior=read(dest) if dest.exists() else {}
        if not proof_matches_expected(prior,expected,sha(expected_path)):raise ValueError('QUERY_FIX_FIRST_PREFIX_RESTORE_MISSING')
        anchor=expected['archive_anchor']
        row=engine._db.execute('SELECT block_hash FROM archive_blocks WHERE sequence=?',(anchor['applied_sequence'],)).fetchone()
        if engine.state['archive']['applied_sequence']<anchor['applied_sequence'] or not row or row[0]!=anchor['applied_hash']:
            raise ValueError('QUERY_FIX_SAVED_PREFIX_ANCHOR_CHANGED_OR_UNCLAIMED')
        return
    payload={k:stream_hash(v) for k,v in engine._payload().items()}
    state={k:stream_hash(v) for k,v in engine.state.items()}
    if day!=expected['day'] or engine.state_digest()!=expected['wrapper_sha256'] or payload!=expected['payload_hashes'] or state!=expected['state_hashes']:
        raise ValueError('QUERY_FIX_RESTORED_PREFIX_NOT_IDENTICAL')
    if dest.exists():
        prior=read(dest)
        if not proof_matches_expected(prior,expected,sha(expected_path)):raise ValueError('QUERY_FIX_PRIOR_PREFIX_PROOF_CHANGED')
        return
    write(dest,{'at':utc(),'status':'PASS','expected_sha256':sha(expected_path),'full_archive_verification_completed':True,
        'wrapper_sha256':engine.state_digest(),'payload_hashes':payload,'state_hashes':state,
        'day':day,'cash':engine.ledger.cash,'positions':{s:p['quantity'] for s,p in engine.ledger.positions.items()},
        'events_processed':engine.state['events_processed'],'events_executed_before_prefix_verification':0,
        'same_account_run_id':engine.config.run_id,'same_deadline':DEADLINE})


def run_case(mode):
    from . import dense_check,crash_check
    target=ROOT/'engineering/query_revision4';previous=ROOT/'engineering/dense_original/BLOCK_PROOFS.json'
    baseline=target/'engineering/dense_original/BLOCK_PROOFS.json'
    if not baseline.exists():baseline.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(previous,baseline)
    if sha(previous)!=sha(baseline):raise ValueError('ORIGINAL_FIXED_ENGINEERING_BASELINE_CHANGED')
    if mode=='dense':
        if (target/'engineering/dense_bounded').exists():raise ValueError('QUERY_DENSE_ALREADY_STARTED')
        dense_check.ROOT=target;dense_check.run('bounded');name='DENSE_REAL_EQUIVALENCE.json'
    elif mode=='crash':
        crash_check.ROOT=target;crash_check.run();name='REAL_DENSE_CRASH_RECOVERY.json'
    else:raise ValueError('UNKNOWN_QUERY_CASE')
    proof=read(target/'engineering'/name)
    write(ROOT/'engineering'/('QUERY_'+name),{**proof,'at':utc(),'original_baseline_sha256':sha(previous),
        'only_archive_conflict_query_loop_direction_changed':True,'new_research_parameters':False})


def freeze():
    if processes():raise ValueError('HISTORICAL_WRITER_ACTIVE')
    previous=ROOT/'ENGINEERING_GATE_revision_3.json';old=read(previous);dest=ROOT/'ENGINEERING_GATE_revision_4.json'
    if dest.exists():raise ValueError('QUERY_GATE_ALREADY_EXISTS')
    permitted={str(REPO/'src/ben_b1_2_continuation'/n) for n in ('execution.py','runner.py')};changes=[];files={}
    for name,expected in old['files'].items():
        actual=sha(name)
        if actual!=expected:
            if name not in permitted:raise ValueError('UNEXPECTED_QUERY_FIX_CHANGE:'+name)
            changes.append({'path':name,'before_sha256':expected,'after_sha256':actual})
        files[name]=actual
    if {r['path'] for r in changes}!=permitted:raise ValueError('QUERY_FIX_PATCH_SCOPE_MISMATCH')
    suite=ET.parse(ROOT/'engineering/query_direction_integration.xml').getroot().find('testsuite')
    if suite is None or int(suite.attrib['tests'])!=84 or any(int(suite.attrib[k]) for k in ('failures','errors','skipped')):raise ValueError('QUERY_REGRESSION_NOT_PASS')
    required=[ROOT/'engineering'/n for n in ('QUERY_DENSE_REAL_EQUIVALENCE.json','QUERY_REAL_DENSE_CRASH_RECOVERY.json','QUERY_FIX_FULL_BACKUP.json','QUERY_FIX_RESTORE_EXPECTED.json','QUERY_DIRECTION_PLAN.json')]
    for p in required:
        if read(p).get('status')!='PASS':raise ValueError('QUERY_REQUIRED_EVIDENCE_NOT_PASS:'+str(p))
    details=[ROOT/'engineering/query_revision4/engineering'/n for n in ('dense_original/BLOCK_PROOFS.json','dense_bounded/BLOCK_PROOFS.json','dense_bounded/ENGINEERING_RUN.json')]
    details += [REPO/'src/ben_b1_2_continuation'/n for n in ('dense_check.py','crash_check.py')]
    for p in required+details+[previous,ROOT/'PLANNED_QUERY_FIX_STOP.json',ROOT/'engineering/query_direction_integration.xml',Path(__file__),REPO/'tests/test_ben_b1_2_query_direction.py']:
        files[str(p)]=sha(p)
    write(dest,{**old,'at':utc(),'revision':4,'status':'PASS','files':files,'changes':changes,
        'prior_gate':{'path':str(previous),'sha256':sha(previous)},'same_authorization_start':STARTED_AT,'same_hard_deadline':DEADLINE,
        'scope':'Only stage-outer conflict query hints; exact latest preserved checkpoint state verification before new dates',
        'query_integration_cases':84,'actual_preserved_prefix_restore_required_before_new_dates':True})
    write(ROOT/'ACTIVE_ENGINEERING_GATE.json',{'path':str(dest),'sha256':sha(dest),'original_gate_sha256':sha(ROOT/'ENGINEERING_GATE.json')})
    print({'status':'PASS','gate':str(dest),'same_deadline':DEADLINE},flush=True)


if __name__=='__main__':
    import sys
    freeze() if sys.argv[1]=='freeze' else run_case(sys.argv[1])
