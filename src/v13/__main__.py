import argparse
from filelock import FileLock, Timeout
from .runtime import *

def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['freeze','run','report','status']);args=parser.parse_args()
    if args.command=='status':print(read(root()/'TASK_STATE.json'));return
    if args.command=='freeze':print(freeze()['protocol_hash']);return
    if args.command=='report':
        from .report import build
        print(build());return
    try:
        with FileLock(str(root()/'research.lock'),timeout=0):
            p=freeze();check_deadline()
            if (root()/'COMPUTATION_RECEIPT.json').exists():
                state('ALREADY_COMPLETE_NO_RECOMPUTATION');return
            from .calibration import run as calibration
            from .data import prepare
            from .research import run as conditional
            from .accounts import run as accounts
            from .analysis import run as analysis
            for name,fn in [('METHOD_CALIBRATION',calibration)]:
                phase(name,'START');fn();phase(name,'END')
            phase('PINNED_REAL_INPUTS','START',data_version=p['data_version']);data,bench=prepare();phase('PINNED_REAL_INPUTS','END')
            for name,fn in [('REAL_CONDITIONS',lambda:conditional(data,bench)),('REAL_ACCOUNTS',lambda:accounts(data)),('FIXED_COMPARISONS',analysis)]:
                phase(name,'START');fn();phase(name,'END')
            receipt={'started_at':read(root()/'RUN_BUDGET.json')['started_at'],'completed_at':utc(),'protocol_hash':p['protocol_hash'],
                     'data_version':p['data_version'],'source_commit':p['source_commit'],'accounts':486,'conditional_cells':1782,
                     'descriptive_factor_cells':990,'paid_calls':0,'new_downloads':0,'new_paper_accounts':0}
            write(root()/'COMPUTATION_RECEIPT.json',receipt);state('REAL_COMPUTATION_COMPLETE',**receipt)
    except Timeout:raise SystemExit('EXISTING_WORKER_NO_DUPLICATE')
    except Exception as exc:
        error={'at':utc(),'type':type(exc).__name__,'reason':str(exc)}
        errors=read(root()/'error_history.json',[]);errors.append(error);write(root()/'error_history.json',errors)
        state('CHECKPOINT_ERROR_RESUMABLE',error=error);raise

if __name__=='__main__':main()
