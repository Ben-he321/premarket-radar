import argparse
from filelock import FileLock,Timeout
from .runtime import root,freeze,state,write,read,utc

def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['freeze','run','report','status']);args=parser.parse_args()
    if args.command=='freeze':print(freeze()['protocol_hash']);return
    if args.command=='status':print(read(root()/'TASK_STATE.json'));return
    if args.command=='report':
        from .report import build
        print(build());return
    try:
        with FileLock(str(root()/'research.lock'),timeout=0):
            freeze();state('RUNNING_FROZEN_EXPERIMENT')
            from .research import conditional
            from .accounts import run
            conditional();run();state('REAL_COMPUTATION_COMPLETE')
            if not (root()/'COMPUTATION_RECEIPT.json').exists():write(root()/'COMPUTATION_RECEIPT.json',read(root()/'TASK_STATE.json'))
    except Timeout:raise SystemExit('EXISTING_RESEARCH_WORKER_NO_DUPLICATE_STARTED')
    except Exception as exc:
        write(root()/'last_error.json',{'at':utc(),'type':type(exc).__name__,'reason':str(exc)})
        state('CHECKPOINT_ERROR_RESUMABLE',error=type(exc).__name__);raise

if __name__=='__main__':main()
