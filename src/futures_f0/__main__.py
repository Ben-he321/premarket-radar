"""Explicit isolated commands. No scheduler, broker or synthetic-data fallback."""
import argparse
import csv
from dataclasses import asdict
from pathlib import Path
import json

from filelock import FileLock

from .config import ROOT, load_config
from .data import write_capability
from .engine import FuturesEngine
from .input import QualifiedInputs
from .model import EngineConfig
from .protocol import PROTOCOL
from .runtime import Guard, canonical_hash, utcnow, write_json


def export_csv(path, rows):
    if not rows:
        Path(path).write_text('status\nNO_RECORDED_EVENTS_CONSULT_RUN_STATUS\n',encoding='utf-8')
        return
    keys=list(dict.fromkeys(k for row in rows for k in row))
    with Path(path).open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=keys);w.writeheader()
        for row in rows:
            w.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in row.items()})


def run_normalized(config, manifest, margin, round_dir=None):
    # A later user-authorized round owns its deadline/output, while the original
    # strategy and original round's files remain immutable.
    if round_dir is not None:
        from .acquisition import Continuation
        continuation = Continuation(round_dir)
        guard, round_output = continuation.guard, continuation.output
        continuation.close()
        if config.data_dir.resolve() != continuation.config.data_dir.resolve():
            raise ValueError('CONTINUATION_ENGINE_DATA_ROOT_MISMATCH')
    else:
        guard=Guard(PROTOCOL['operations']['deadline_utc'],config.data_dir)
        round_output=config.data_dir
    guard.check({'stage':'before_real_input_qualification'})
    # No account is created until the actual-source input gate passes.
    inputs=QualifiedInputs(manifest,ROOT/'docs/futures_f0/contract_registry.csv')
    frozen=json.loads((config.data_dir/'FROZEN_PROTOCOL.json').read_text(encoding='utf-8-sig'))
    frozen_content={k:v for k,v in frozen.items() if k not in ('protocol_sha256','frozen_at','attachment_sha256')}
    if frozen_content != PROTOCOL:
        raise ValueError('FROZEN_PROTOCOL_CHANGED')
    # Reject a one-day connectivity sample before creating any research account.
    # Real research may legitimately have zero fills once sufficient input is
    # present; a connection probe must not be advertised as such a backtest.
    preflight_counts = {}
    trading_dates = set()
    for batch in inputs.batches(guard):
        for day in batch:
            if day.signal.status == 'QUALIFIED':
                preflight_counts[day.market] = preflight_counts.get(day.market, 0) + 1
                if EngineConfig().trading_start <= day.signal.session <= EngineConfig().trading_end:
                    trading_dates.add(day.signal.session)
    if not trading_dates or max(preflight_counts.values(), default=0) < 56:
        raise ValueError('INSUFFICIENT_QUALIFIED_DAILY_INPUT_NO_ACCOUNT_CREATED')
    output=round_output/'runs'/('normalized_'+inputs.manifest_hash[:12]+'_'+margin)
    output.mkdir(parents=True,exist_ok=True)
    if (output/'COMPLETED.json').exists():
        raise ValueError('COMPLETED_MATRIX_REUSED_NO_AUTOMATIC_RERUN')
    for version in ('F0','F1'):
        for scenario in PROTOCOL['costs']:
            account_id=version+'_'+scenario['name']
            account=output/account_id
            if (account/'COMPLETED.json').exists(): continue
            cfg=EngineConfig(version=version,slippage_ticks=scenario['slippage_ticks_per_side'],
                commission_multiplier=scenario['commission_multiple'],
                margin_scenario='ASSUMED_MARGIN_'+margin+'_PERCENT',initial_margin_fraction=int(margin)/100)
            checkpoint_path=account/'checkpoint.json'
            checkpoint=None
            if checkpoint_path.exists():
                checkpoint=json.loads(checkpoint_path.read_text(encoding='utf-8'))
                if checkpoint['input_manifest_sha256']!=inputs.manifest_hash or checkpoint['protocol_sha256']!=frozen['protocol_sha256']:
                    raise ValueError('RESUME_SOURCE_OR_PROTOCOL_CHANGED')
                engine=FuturesEngine.restore(checkpoint['engine'])
                if engine.config!=cfg: raise ValueError('RESUME_CONFIG_CHANGED')
            else:
                engine=FuturesEngine(inputs.specs,cfg)
            account.mkdir(parents=True,exist_ok=True)
            for batch in inputs.batches(guard):
                if engine.last_session and batch[0].signal.session<=engine.last_session:
                    continue
                engine.process_batch(batch)
                reading=guard.check({'account':account_id,'last_session':str(engine.last_session)})
                write_json(checkpoint_path,dict(engine=engine.checkpoint(),input_manifest_sha256=inputs.manifest_hash,
                    protocol_sha256=frozen['protocol_sha256'],resource=reading))
            result=engine.finish()
            write_json(account/'result.json',asdict(result),immutable=True)
            write_json(account/'input_quarantine.json',dict(count=inputs.quarantine_count,
                       first_1000=inputs.quarantine),immutable=True)
            for name in ('trades','campaigns','rolls','daily_equity','margin_path','skips','events'):
                export_csv(account/(('position_sizing_skips' if name=='skips' else name)+'.csv'),getattr(result,name))
            write_json(account/'COMPLETED.json',dict(at=utcnow().isoformat(),status=result.status,
                       source_manifest_sha256=inputs.manifest_hash,real_input=True),immutable=True)
    write_json(output/'COMPLETED.json',dict(at=utcnow().isoformat(),status='MATRIX_EXECUTED_REPORT_REVIEW_REQUIRED',
                margin_evidence='ASSUMED_NOT_HISTORICAL',input_sha256=inputs.manifest_hash),immutable=True)
    return output


def main(argv=None):
    parser=argparse.ArgumentParser(description='F0 isolated historical pilot; no forward/broker actions')
    parser.add_argument('command',choices=['preflight','run-normalized'])
    parser.add_argument('--data-dir')
    parser.add_argument('--secrets-file')
    parser.add_argument('--input-manifest')
    parser.add_argument('--round-dir', help='Existing separately authorized continuation; never edits the frozen protocol')
    parser.add_argument('--margin',choices=['10','20'],default='10')
    args=parser.parse_args(argv)
    if args.command == 'preflight' and args.round_dir:
        parser.error('Continuation preflight must use acquisition metadata; preserve the original capability report')
    config=load_config(args.data_dir,args.secrets_file)
    config.data_dir.mkdir(parents=True,exist_ok=True)
    with FileLock(str(config.data_dir/'f0.lock'),timeout=0):
        if args.command=='preflight':
            result=write_capability(config)
            print(json.dumps({'status':'DATA_PREFLIGHT','key_present':result['key_present'],
                              'permission':result['historical_permission'],'data_dir':str(config.data_dir)}))
        else:
            if not args.input_manifest: parser.error('--input-manifest is required')
            error_root = config.data_dir if not args.round_dir else None
            try:
                if args.round_dir:
                    # Do not use an unvalidated user path even for error logs.
                    # Invalid paths must not overwrite an older round's output.
                    from .acquisition import Continuation
                    validation = Continuation(args.round_dir)
                    try:
                        if validation.config.data_dir.resolve() != config.data_dir.resolve():
                            raise ValueError('CONTINUATION_ENGINE_DATA_ROOT_MISMATCH')
                        error_root = validation.output
                    finally:
                        validation.close()
                output=run_normalized(config,args.input_manifest,args.margin,args.round_dir)
                print(json.dumps({'output':str(output)}))
            except Exception as error:
                # No credential-bearing request exception is included in reports.
                if error_root is not None:
                    value = dict(at=utcnow().isoformat(), error_type=type(error).__name__,
                                 status='STOPPED_NO_AUTOMATIC_RETRY')
                    write_json(error_root/'run_errors'/(utcnow().strftime('%Y%m%dT%H%M%S%fZ')+'.json'),
                               value, immutable=True)
                    write_json(error_root/'LAST_RUN_ERROR.json',value)
                raise


if __name__=='__main__': main()
