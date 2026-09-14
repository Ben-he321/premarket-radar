"""QUOTE transaction snapshots retain immutable historical record values only.

Every container and every mutable financial object is still copied. Existing
fill/event records are append-only on the frozen QUOTE buy/sell/cancel paths.
Other event kinds continue to use the original full Ledger.to_dict snapshot.
"""
import copy
from dataclasses import asdict
from src.ben_b1.ledger import VERSION

def quote_ledger_snapshot(book):
    memo={id(v):v for v in book.fills.values()}
    memo.update({id(v):v for v in book.events})
    return {'version':VERSION,'config':asdict(book.config),
            'state':copy.deepcopy({k:v for k,v in vars(book).items() if k!='config'},memo)}

def quote_state_memo(state,immutable):
    memo={id(state[k]):state[k] for k in immutable if k in state}
    memo.update({id(v):v for v in state.get('quotes',{}).values()})
    memo.update({id(v):v for v in state.get('pending',{}).values() if v.get('status') in ('FILLED','CANCELLED')})
    return memo
