"""Version a Windows I/O-only repair without rewriting the first attempt gate."""
import xml.etree.ElementTree as ET
from .runtime import *


def run():
    original=ROOT/'ENGINEERING_GATE.json';gate=read(original)
    dest=ROOT/'ENGINEERING_GATE_revision_2.json'
    if dest.exists() or (ROOT/'ACTIVE_ENGINEERING_GATE.json').exists():raise ValueError('REPAIR_GATE_ALREADY_FROZEN')
    if read(ROOT/'RUNNER_PROCESS.json')['status']!='STOPPED':raise ValueError('WRITER_MUST_BE_STOPPED')
    proof_path=ROOT/'attempts/attempt_01_20260914T171910Z/RECOVERY_STATE_PRESERVATION.json'
    proof=read(proof_path)
    if proof['status']!='PASS' or not proof['all_saved_account_files_identical_to_verified_jan22_copy']:raise ValueError('SAVED_PREFIX_NOT_IDENTICAL')
    for record in proof['files']:
        if sha(record['path'])!=record['expected']:raise ValueError('SAVED_PREFIX_CHANGED_AFTER_PROOF')
    changed=[];files={}
    permitted={str(REPO/'src/ben_b1_2_continuation'/name) for name in ('runtime.py','runner.py')}
    for name,expected in gate['files'].items():
        actual=sha(name)
        if actual!=expected:
            if name not in permitted:raise ValueError('FINANCIAL_OR_PREVIOUS_PROOF_CHANGED:'+name)
            changed.append({'path':name,'before_sha256':expected,'after_sha256':actual})
        files[name]=actual
    if {r['path'] for r in changed}!=permitted:raise ValueError('UNEXPECTED_IO_REPAIR_SCOPE')
    xml=ROOT/'engineering/windows_io_repair_regressions_v2.xml';suite=ET.parse(xml).getroot().find('testsuite')
    if suite is None or any(int(suite.attrib[k]) for k in ('failures','errors','skipped')):raise ValueError('IO_REPAIR_TESTS_NOT_PASS')
    if int(suite.attrib['tests'])!=62:raise ValueError('REQUIRED_62_IO_REPORT_AND_EXECUTION_CASES')
    for p in (original,proof_path,xml,REPO/'tests/test_ben_b1_2_continuation_io.py',Path(__file__)):
        files[str(p)]=sha(p)
    value={**gate,'at':utc(),'revision':2,'status':'PASS','original_gate_at_preserved':gate['at'],
        'original_gate':{'path':str(original),'sha256':sha(original)},'same_authorization_start':STARTED_AT,'same_hard_deadline':DEADLINE,
        'new_historical_dates_executed_before_gate':True,'prior_jan23_attempt_was_authorized_by_original_gate':True,
        'prior_jan23_attempt_not_durably_committed':True,'last_completed_day_unchanged':'2026-01-22',
        'financial_execution_sorting_and_hash_algorithms_unchanged':True,'io_repair_regressions_passed':62,
        'changes':changed,'files':files,'scope':'Windows shared-delete reads, bounded atomic JSON replacement, versioned gate and attempt evidence metadata only'}
    write(dest,value)
    write(ROOT/'ACTIVE_ENGINEERING_GATE.json',{'path':str(dest),'sha256':sha(dest),'original_gate_sha256':sha(original)})
    print({'status':'PASS','gate':str(dest),'tests':62,'deadline':DEADLINE},flush=True)


if __name__=='__main__':run()
