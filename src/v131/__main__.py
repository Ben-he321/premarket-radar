import sys
from .runtime import state,root,write,utc
try:
    command=sys.argv[1] if len(sys.argv)>1 else 'status'
    if command=='restate':
        from .restate import run
        run()
    elif command=='probe':
        from .service import probe
        probe()
    elif command=='paper-service':
        from .service import service
        service()
    elif command=='report':
        from .report import run
        run()
    elif command=='status':
        from .runtime import read
        print(read(root()/'TASK_STATE.json'))
    else:raise ValueError('UNKNOWN_COMMAND')
except Exception as exc:
    write(root()/'last_error.json',{'at':utc(),'type':type(exc).__name__,'phase':command,'detail':str(exc) if isinstance(exc,(AssertionError,ValueError,TimeoutError)) else 'See local traceback; API credentials never logged'})
    raise
