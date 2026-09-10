"""Single durable entrypoint; UI never creates research workers."""
import argparse
import os
import time
import traceback
from filelock import FileLock,Timeout
from .runtime import root,read,write,state,event,utc


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['run-all','validate-universe','sync','incremental','research','backup','paper-service','paper-once','stop-paper','status'])
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--hours',type=float,default=8)
    parser.add_argument('--days',type=int,default=30)
    args=parser.parse_args();deadline=time.time()+min(args.hours,8)*3600
    if args.command=='status':print(read(root()/'status.json',{}));print(read(root()/'paper/status.json',{}));return
    if args.command=='stop-paper':
        (root()/'paper').mkdir(exist_ok=True);(root()/'paper/STOP').write_text(utc());return
    if args.command in ('paper-service','paper-once'):
        from .paper import service
        print(service(min(args.days,30),args.command=='paper-once'));return
    try:
        with FileLock(str(root()/'orchestrator.lock'),timeout=0):
            from .universe import validate
            from .data import sync,coverage
            from .actions import download
            from .research import run
            from .backup import backup_verify
            universe=read(root()/'universe.json') if args.resume else None
            universe=universe or validate()
            command=args.command
            state('RUNNING',command=command,deadline_epoch=deadline)
            if command in ('sync','incremental','run-all'):
                if not sync(universe,incremental=command=='incremental',deadline=deadline):return
                coverage(universe)
            if command in ('research','run-all'):
                download(universe)
                with FileLock(str(root()/'research.lock'),timeout=0):run(universe,deadline)
                from .report import produce
                produce()
            if command in ('backup','run-all'):backup_verify()
            if command=='run-all':
                from .paper import launch
                if read(root()/'engineering_checks.json',{}).get('status')=='PASS':write(root()/'paper_launch.json',launch())
                else:write(root()/'paper_launch.json',{'status':'BLOCKED_ENGINEERING_CHECKS_REQUIRED'})
            state('COMPLETE',command=command)
    except Timeout:print('ALREADY_RUNNING: existing worker owns this stage')
    except Exception as exc:
        state('ERROR',error_type=type(exc).__name__,error=str(exc),recovery='python -m src.watchlist run-all --resume')
        event('engineering','ERROR',error=str(exc));traceback.print_exc();raise


if __name__=='__main__':main()
