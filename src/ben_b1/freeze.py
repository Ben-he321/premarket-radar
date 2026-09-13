"""Freeze the supplied protocol before looking at new research outcomes."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import shutil
import subprocess

REPO = Path(__file__).resolve().parents[2]
ROOT = Path.home() / 'BenAITradingData' / 'ben-b1-research-20260913'
OLD = REPO.parent / 'premarket-radar-ai-m1'
DATA = ROOT.parent / 'watchlist-research-v1'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str, allow_nan=False), encoding='utf-8')
    tmp.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat()


def run(source):
    ROOT.mkdir(parents=True, exist_ok=True)
    target = REPO / 'docs/ben_b1/BEN_B1_TASK_20260913.txt'
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and sha(target) != sha(source):
        raise ValueError('FROZEN_SOURCE_MISMATCH')
    if not target.exists():
        shutil.copyfile(source, target)
    protocol = {
        'version': 'BEN_B1_FROZEN_RESEARCH_20260913', 'experiment_id': 'BEN_B1_BATCH_001',
        'source_sha256': sha(target), 'source': 'docs/ben_b1/BEN_B1_TASK_20260913.txt',
        'base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
        'branch': subprocess.check_output(['git', 'branch', '--show-current'], cwd=REPO, text=True).strip(),
        'universe_sha256': sha(REPO / 'config/watchlist.json'), 'candidate_count': 66,
        'initial_equity_usd': 5500, 'history_start_request': '2016-01-01',
        'endpoint': 'last completed NYSE session at batch freeze; no current session bars',
        'max_initial_batch_hours': 8, 'data_source': 'Alpaca', 'feed': 'sip',
        'classification': 'HISTORICAL_EXPLORATORY_REUSED_DATA',
        'user_confirmed': {'ema_periods': [5, 10, 20, 50, 100], 'after_close_entry': True,
            'max_close_premium': .01, 'stop_below_support': .01, 'max_initial_leg_distance': .07,
            'exclude_direct_oil_gas': ['SM', 'BATL'], 'earnings_avoidance_required': True},
        'research_defaults': {'ema_seed': 'SMA first n valid RTH closes, recursive alpha=2/(n+1)',
            'strict_warmup': 100, 'preferred_warmup': 300, 'atr': 'Wilder14',
            'entry_minutes_after_close': 5, 'expiry_minutes_after_close': 15,
            'active_exit_minutes_before_close': 10, 'max_spread_mid': .003, 'quote_max_age_seconds': 5,
            'pressure_emas': [20, 50, 100], 'prior_high_days': 30, 'minimum_net_rr': 2,
            'support_core': [10, 20], 'support_neighbors': [50, 100], 'support_neighborhood_close': .01,
            'cluster_max_range_close': .005, 'two_leg_distance_entry': [.005, .03],
            'deviation_ema5_pct': .06, 'deviation_ema10_pct': .10, 'deviation_atr5': 2, 'deviation_atr10': 3,
            'failure_window_sessions': 5, 'failure_delta_pct_min': .0025, 'failure_delta_pct_max': .0075,
            'failure_delta_atr': .25, 'reentry_full_sessions': 2, 'reentry_touch_atr': .5,
            'reentry_close_ema5_atr': 1, 'earnings_blackout_prior_sessions': 3,
            'earnings_resume': 'first full session with NY date strictly after actual release date, close+5min',
            'cash_interest': 0, 'rate_base': .08, 'rate_stress': .12, 'interest_day_count': 'ACT/360',
            'initial_margin': .50, 'maintenance_margin': .30, 'maintenance_stress': .50},
        'matrix': [{'rule': r, 'position': p, 'cost': c, 'commission': 2 if c == 'double_commission' else 1,
                    'friction_bps': 25 if c == 'stress25' else 10}
                   for r,p in [('B1_FULL','P50_PRIMARY'), ('B1_FULL','P100_CONCENTRATED'),
                               ('B1_FULL','P200_MARGIN_RESEARCH'), ('B1_NO_DEVIATION','P50_PRIMARY'),
                               ('B1_NO_TWO_FAILURE','P50_PRIMARY')]
                   for c in ['base','stress25','double_commission']],
        'margin_stress': ['12_PERCENT_RATE', '50_PERCENT_MAINTENANCE'],
        'fixed_cost_diagnostic_monthly': [0,150], 'new_purchases_allowed': False,
        'paid_model_calls_allowed': False, 'ben_forward_allowed': False,
        'probe_selection': 'cases first; first10 valid coarse first-entry events ordered date then stable local identity hash; no outcome sorting',
        'data_gates': ['VERIFIED_PIT', 'VERIFIED_RTH_100', 'SAME_UNIT_ACTIONS', 'QUOTE_EXECUTION', 'FULL_HELD_PATH'],
        'missing_quote_proxy': 'MINUTE_BAR_EXECUTION_PROXY, never mixed with executable results',
        'actual_earnings_only': 'RETROSPECTIVE_EARNINGS_EXCLUSION, never mixed with strict PIT',
        'rules_authority': 'full taskbook controls over this machine-readable digest; defaults are not optimized or literature-proven',
    }
    frozen = ROOT / 'frozen_config.json'
    if frozen.exists():
        old = json.loads(frozen.read_text(encoding='utf-8'))
        if {k:v for k,v in old.items() if k != 'frozen_at'} != protocol:
            raise ValueError('PROTOCOL_ALREADY_FROZEN_DIFFERENT')
    else:
        protocol['frozen_at'] = now()
        write(frozen, protocol)
    shutil.copyfile(frozen, REPO / 'docs/ben_b1/frozen_config.json')
    # Preserve old result and ledger files. Volatile heartbeats are separately snapshotted.
    previous = ROOT / 'old_files_before.json'
    if not previous.exists():
        paths = []
        for dirname in ['watchlist-v1_3_1-evidence-forward', 'watchlist-v1_3-controlled-factors',
                        'watchlist-v1_2-momentum', 'watchlist-v1_1-execution',
                        'economics-audit-20260912', 'economics-supplement-spy-150-20260912']:
            d = ROOT.parent / dirname
            if not d.exists():
                continue
            paths += [p for p in d.iterdir() if p.is_file() and p.suffix in ['.md','.csv']]
        live = ROOT.parent / 'watchlist-v1_3_1-evidence-forward' / 'forward'
        paths += [p for p in live.rglob('*') if p.is_file() and p.suffix in ['.json','.jsonl','.csv'] and 'heartbeat' not in p.name]
        write(previous, {'at': now(), 'files': {str(p):sha(p) for p in paths},
                         'old_git_status': subprocess.check_output(['git','status','--porcelain'],cwd=OLD,text=True)})
    status = json.loads((ROOT / 'TASK_STATE.json').read_text(encoding='utf-8'))
    status.update(status='B1_PROTOCOL_FROZEN_ENGINEERING_AND_DATA_PROBE', at=now(),
                  requested_attachment_found=True, received_attachment=str(source),
                  received_attachment_sha256=sha(source), rule_freeze='FROZEN',
                  required_user_action=None, research_results='PENDING', background_research_running=True)
    write(ROOT / 'TASK_STATE.json', status)
    print(json.dumps({'status':'FROZEN','path':str(frozen),'sha256':sha(frozen),'preservation_files':len(json.loads(previous.read_text(encoding='utf-8'))['files'])}))


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('source',type=Path)
    run(parser.parse_args().source)
