"""Isolated B1.1 protocol, preservation and progress records. No execution API."""
from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import subprocess

REPO = Path(__file__).resolve().parents[2]
ROOT = Path.home() / 'BenAITradingData' / 'ben-b1-1-replay-20260914'
PREVIOUS = ROOT.parent / 'ben-b1-research-20260913'
OLD_REPO = REPO.parent / 'premarket-radar-ai-m1'


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for data in iter(lambda: handle.read(1024*1024), b''):
            h.update(data)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str, allow_nan=False), encoding='utf-8')
    tmp.replace(path)


def status(stage, **fields):
    path = ROOT/'TASK_STATE.json'
    previous = read(path) if path.exists() else {}
    write(path, {**previous, 'stage': stage, 'updated_at': utc(), **fields})


def freeze():
    ROOT.mkdir(parents=True, exist_ok=True)
    path = ROOT/'B11_PROTOCOL.json'
    if path.exists():
        return read(path)
    config = PREVIOUS/'frozen_config.json'
    protocol = {
        'version': 'BEN_B1_1_REPLAY_20260914', 'frozen_at': utc(),
        'start': '2026-01-02', 'end': '2026-09-11', 'max_hours': 8,
        'base_commit': subprocess.check_output(['git','rev-parse','HEAD'], cwd=REPO, text=True).strip(),
        'branch': subprocess.check_output(['git','branch','--show-current'], cwd=REPO, text=True).strip(),
        'task_sha256': sha(REPO/'docs/ben_b1_1/TASK_20260914.txt'),
        'b1_config_sha256': sha(config), 'b1_config_unchanged': read(config),
        'universe_policy_sha256': sha(PREVIOUS/'UNIVERSE_POLICY.csv'),
        'engineering_sample_limit': 20,
        'engineering_selection': 'Existing coarse basic-price-structure pass, fixed interval, date then identity_sort_hash. Retain excluded names in coverage but no Ben orders. No subsequent-outcome selection.',
        'qualification_layers': ['A_VERIFIED_PIT','B_RETROSPECTIVE_EARNINGS_EXCLUSION','C_UNKNOWN_NO_ENTRY'],
        'price_basis_layers': ['VENDOR_STANDARD_FIELDS','INDEPENDENTLY_VERIFIED'],
        'quote_model': 'Historical SIP L1 model fills; no claim of queue position or real execution',
        'quote_size_rule': 'SHARES for the fixed post-2025-11-03 interval, supported by vendor documentation; no inference about pre-change history rewrite',
        'received_time_rule': 'New retrieval time separate from historical event availability. Unrecorded historical network receipts remain UNKNOWN.',
        'sample_is_portfolio': False,
        'portfolio_precondition': 'Continuous declared interval, common capital and all 66 candidate eligibility/ranking; no concatenation of selected examples',
        'prohibited': ['paid_service','paid_model_api','real_broker','Ben_forward','old_result_overwrite','strategy_parameter_search'],
    }
    write(path, protocol)
    write(REPO/'docs/ben_b1_1/protocol.json', protocol)
    # Old B1 is frozen, including the original package. The live V1.3.1 service
    # may legitimately evolve its ledger; preserve its input/source evidence and
    # record read-only ledger snapshots separately, never restore over live data.
    paths = [p for p in PREVIOUS.rglob('*') if p.is_file() and p.suffix not in {'.lock','.tmp'}]
    old_static = read(PREVIOUS/'old_files_before.json')['files']
    paths += [Path(p) for p in old_static if '/forward/' not in p.replace('\\','/')]
    write(ROOT/'PRESERVATION_BEFORE.json', {'at':utc(), 'files':{str(p):sha(p) for p in sorted(set(paths))},
        'old_repo_status':subprocess.check_output(['git','status','--porcelain'],cwd=OLD_REPO,text=True),
        'live_ledger_policy':'Read-only snapshots; legitimate old service writes are not rolled back.'})
    status('ENGINE_AND_DATA_DEVELOPMENT', started_at=utc(), orchestration='ACTIVE_CODEX_TASK',
           persistent_research_worker_running=False, ben_forward_running=False,
           paid_service_purchases=0, paid_model_calls=0, errors=[])
    return protocol


def preserve_check():
    before=read(ROOT/'PRESERVATION_BEFORE.json')
    rows=[{'path':p,'expected':h,'actual':sha(p) if Path(p).exists() else None} for p,h in before['files'].items()]
    changed=[r for r in rows if r['expected']!=r['actual']]
    result={'checked_at':utc(),'checked_files':len(rows),'changed':changed,'status':'PASS' if not changed else 'CHANGED_REVIEW_REQUIRED',
            'old_repo_status':subprocess.check_output(['git','status','--porcelain'],cwd=OLD_REPO,text=True)}
    write(ROOT/'PRESERVATION_CHECK.json',result)
    return result


if __name__=='__main__':
    print(json.dumps({'protocol':freeze()['version'],'output':str(ROOT)},ensure_ascii=False))
