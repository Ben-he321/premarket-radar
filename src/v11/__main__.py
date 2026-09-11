"""Explicit recovery entry points; no hidden paid services or broker calls."""
import argparse
from .runtime import root,state,write,read,utc

def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['analysis','paper-service','status','stop-paper','report'])
    args=parser.parse_args()
    if args.command=='paper-service':
        from .paper import service
        service()
    elif args.command=='status':print(read(root()/'TASK_STATE.json'));print(read(root()/'experimental_paper_status.json'))
    elif args.command=='stop-paper':
        p=root()/'experimental_paper'/'STOP';p.parent.mkdir(exist_ok=True);p.write_text(utc())
    elif args.command=='report':
        from .report import build
        build()
    elif args.command=='analysis':
        from .analysis import action_audit,attribution,mechanical,candidates,selection_recompute,audit_new_intervals
        action_audit();attribution();mechanical();candidates();selection_recompute();audit_new_intervals();state('ANALYSIS_COMPLETE')

if __name__=='__main__':main()
