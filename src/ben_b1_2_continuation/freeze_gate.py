"""Freeze completed engineering evidence before any new account date."""
import xml.etree.ElementTree as ET
from .runtime import *

def run():
    p=ROOT/'ENGINEERING_GATE.json'
    if p.exists():raise ValueError('GATE_ALREADY_FROZEN_NO_SILENT_REWRITE')
    proofs=[ROOT/'BACKUP_VERIFICATION.json',ROOT/'engineering/REAL_PREFIX_RESTORE.json',
        ROOT/'engineering/DENSE_REAL_EQUIVALENCE.json',ROOT/'engineering/REAL_DENSE_CRASH_RECOVERY.json',
        ROOT/'engineering/REAL_DENSE_INPUT_EQUIVALENCE_v2.json',ROOT/'engineering/NTFS_COMPRESSION_PROOF.json']
    for f in proofs:
        if not read(f).get('status','').startswith('PASS'):raise ValueError('ENGINEERING_PROOF_NOT_PASS:'+str(f))
    xml=ROOT/'engineering/final_engine_gate_v2.xml';suite=ET.parse(xml).getroot().find('testsuite')
    if suite is None or int(suite.attrib['failures']) or int(suite.attrib['errors']) or int(suite.attrib['skipped']):raise ValueError('ENGINEERING_REGRESSIONS_NOT_PASS')
    extra_xml=ROOT/'engineering/guard_observability_regressions.xml';extra=ET.parse(extra_xml).getroot().find('testsuite')
    if extra is None or int(extra.attrib['failures']) or int(extra.attrib['errors']) or int(extra.attrib['skipped']):raise ValueError('FINAL_GUARD_REGRESSIONS_NOT_PASS')
    code=[REPO/'src/ben_b1'/name for name in ('replay.py','ledger.py','rules.py','events.py')]
    code += [REPO/'src/ben_b1_2'/name for name in ('replay.py','compact.py','shared.py','portfolio.py','data.py','runtime.py')]
    code += [REPO/'src/ben_b1_2_continuation'/name for name in ('runtime.py','engine.py','execution.py','inputs.py','runner.py')]
    write(p,{'status':'PASS','at':utc(),'new_historical_dates_executed_before_gate':False,'tests_passed':int(suite.attrib['tests']),
        'one_original_q1_b_account_only':True,'first_new_day':'2026-01-23','no_financial_config_changes':True,
        'final_guard_cases_passed':int(extra.attrib['tests']),
        'financial_and_all_component_real_equivalence':True,'files':{str(f):sha(f) for f in proofs+[xml,extra_xml]+code}})
    status('ENGINEERING_GATE_PASSED_READY_TO_RESUME_SAME_ACCOUNT',research_running=False,gate=str(p))
if __name__=='__main__':run()
