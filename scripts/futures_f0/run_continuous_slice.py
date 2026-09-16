"""Execute the frozen 12-account matrix on a qualified continuous prefix only.

This is a minimum real-input engineering run, not the formal six-market study.
The prefix stops at the first unqualified expected session, without compressing
time, liquidating fictitiously or restarting capital beyond the gap.
"""
import argparse
from collections import Counter
from dataclasses import asdict
from pathlib import Path
import json
import sys
from filelock import FileLock

REPO=Path(__file__).resolve().parents[2];sys.path.insert(0,str(REPO))
from src.futures_f0.runtime import Guard,canonical_hash,digest,utcnow,write_json
from src.futures_f0.data import verify_input_manifest
from src.futures_f0.input import QualifiedInputs
from src.futures_f0.engine import FuturesEngine
from src.futures_f0.model import EngineConfig
from src.futures_f0.protocol import PROTOCOL


def main():
    p=argparse.ArgumentParser();p.add_argument('--round',type=Path,required=True)
    p.add_argument('--manifest',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();rd=args.round.resolve();mp=args.manifest.resolve();out=args.output.resolve()
    if not out.is_relative_to(rd) or out.exists() or not mp.is_relative_to(rd):raise ValueError('NEW_LOCAL_ROUND_OUTPUT_REQUIRED')
    state=json.loads((rd/'ROUND_STATE.json').read_text(encoding='utf-8-sig'))
    guard=Guard(state['hard_deadline_utc'],out);guard.check({'stage':'minimum_real_account_start'});out.mkdir(parents=True)
    progress=dict(started_at=utcnow().isoformat(),status='VALIDATING_INPUTS',accounts_completed=[],formal_full_accounts_completed=0)
    with FileLock(str(rd/'research.lock'),timeout=0):
        try:
            inputs=QualifiedInputs(mp,REPO/'docs/futures_f0/contract_registry.csv',guard=guard)
            batches=[];previous=None;stop=None;validations=[]
            for days in inputs.batches(guard=guard):
                if len(batches)>=128:raise ValueError('FIXED_ENGINEERING_SLICE_BATCH_BOUNDARY')
                session=days[0].signal.session
                if previous is not None and previous!=session:
                    stop=dict(reason='EXPECTED_SESSION_NOT_QUALIFIED_NO_GAP_COMPRESSION',expected=str(previous),
                              next_supplied=str(session));break
                batches.append(days);previous=days[0].signal.next_session
                validations.append(dict(session=str(session),contracts=[days[0].signal.contract_id,*[b.contract_id for b in days[0].execution]],
                    derivation_hashes=[days[0].signal.source_hash,*[b.source_hash for b in days[0].execution]],
                    roll_evidence_hash=days[0].roll.evidence_hash if days[0].roll else None))
            if inputs.quarantine_count:raise ValueError('REAL_IMPORTER_QUARANTINE_REQUIRES_REPAIR:'+json.dumps(inputs.quarantine))
            if len(batches)<56:raise ValueError('MINIMUM_CONTINUOUS_PRIOR55_PLUS_EVALUATION_NOT_MET')
            validation=dict(status='CONTINUOUS_INPUT_IMPORT_PASS',manifest_sha256=inputs.manifest_hash,
                input_start=str(batches[0][0].signal.session),input_end=str(batches[-1][0].signal.session),
                sessions=len(batches),stop_boundary=stop,batches=validations,
                unprocessed_suffix='ISOLATED_NOT_ANOTHER_ACCOUNT',full_matrix_status='NOT_RUN',
                first55_are_warmup=True,main_execution_slice_start='2024-07-01',qualification_quarantine=inputs.quarantine)
            write_json(out/'INPUT_VALIDATION.json',validation,immutable=True)
            write_json(out/'EXECUTION_SCOPE_FREEZE.json',dict(created_at=utcnow().isoformat(),
                input_validation_sha256=digest(out/'INPUT_VALIDATION.json'),selection='LONGEST_CONTIGUOUS_QUALIFIED_PREFIX_BEFORE_FIRST_QUALITY_GAP',
                selection_used_account_returns=False,original_full_research_period='2022-2025_UNCHANGED_NOT_COMPLETED',
                capital=11500,market_scope=['SP500'],other_five_markets='PENDING_NOT_REMOVED_FROM_FROZEN_FULL_STUDY',
                versions=['F0','F1'],costs=PROTOCOL['costs'],margin_fractions=[0.1,0.2],
                no_forced_trades=True,no_synthetic_final_liquidation=True),immutable=True)
            progress.update(status='EXECUTING_REAL_CONTINUOUS_INPUTS',sessions=len(batches),stop_boundary=stop)
            write_json(out/'RUN_PROGRESS.json',progress)
            source_hashes={str(p.relative_to(REPO)):digest(p) for p in (REPO/'src/futures_f0').glob('*.py')}
            write_json(out/'SOURCE_VERSION.json',dict(at=utcnow().isoformat(),starting_commit=state['source_commit'],
                source_hashes=source_hashes,manifest_sha256=inputs.manifest_hash),immutable=True)
            for margin in (0.1,0.2):
                for version in ('F0','F1'):
                    for cost in PROTOCOL['costs']:
                        guard.check({'stage':'account_start','version':version,'margin':margin,'cost':cost['name']})
                        inputs.qualification_catalog.assert_unchanged();verify_input_manifest(mp)
                        if digest(mp)!=inputs.manifest_hash:raise ValueError('INPUT_MANIFEST_CHANGED_AFTER_VALIDATION')
                        account_id=f'{version}_{cost["name"]}_M{int(margin*100)}';ad=out/'accounts'/account_id
                        ad.mkdir(parents=True);config=EngineConfig(version=version,
                            slippage_ticks=cost['slippage_ticks_per_side'],commission_multiplier=cost['commission_multiple'],
                            margin_scenario=f'ASSUMED_MARGIN_{int(margin*100)}_PERCENT',initial_margin_fraction=margin)
                        engine=FuturesEngine(inputs.specs,config);started=utcnow().isoformat();recovery=None;idempotency=[]
                        write_json(ad/'CONFIG.json',json.loads(json.dumps(asdict(config),default=str)),immutable=True)
                        for n,days in enumerate(batches):
                            guard.check({'stage':'account_session','account':account_id,'session':str(days[0].signal.session)})
                            if n==31 and not progress['accounts_completed']:
                                checkpoint=engine.checkpoint();recovered=FuturesEngine.restore(checkpoint)
                                if recovered.checkpoint()!=checkpoint:raise ValueError('REAL_CHECKPOINT_ROUNDTRIP_MISMATCH')
                                engine.process_batch(days);recovered.process_batch(days)
                                if engine.checkpoint()!=recovered.checkpoint():raise ValueError('REAL_NEXT_BATCH_RECOVERY_EQUIVALENCE_FAILED')
                                recovery=dict(status='PASS',checkpoint_sha256=checkpoint['sha256'],
                                    next_session=str(days[0].signal.session),next_state_sha256=engine.checkpoint()['sha256'])
                                write_json(ad/'MIDPOINT_RECOVERY_CHECKPOINT.json',checkpoint,immutable=True)
                                del recovered
                            else:engine.process_batch(days)
                            before=engine.checkpoint()['sha256']
                            duplicate=engine.process_batch(days)
                            if duplicate is not False or engine.checkpoint()['sha256']!=before:raise ValueError('REAL_DUPLICATE_BATCH_NOT_IDEMPOTENT')
                            idempotency.append(dict(session=str(days[0].signal.session),status='PASS',state_sha256=before))
                            write_json(ad/'CHECKPOINT.json',dict(manifest_sha256=inputs.manifest_hash,
                                account=engine.checkpoint(),last_completed_session=str(days[0].signal.session),
                                next_expected_session=str(days[0].signal.next_session),written_at=utcnow().isoformat()))
                        result=engine.finish()
                        if result.status!='EXECUTED' or result.processed_sessions!=len(batches):raise ValueError('ACCOUNT_NOT_ACTUALLY_EXECUTED')
                        feature=[x for x in result.events if x['kind']=='DAILY_FEATURES_EVALUATED']
                        ready=[x for x in feature if x['entry_warmup_complete']]
                        if not ready:raise ValueError('REAL_FEATURE_WARMUP_NEVER_COMPLETED')
                        if not result.reconciliation.get('passed',result.reconciliation.get('ok',False)):
                            # Actual reconciler uses its explicit status field.
                            if result.reconciliation.get('status')!='PASS':raise ValueError('REAL_MONEY_RECONCILIATION_FAILED:'+json.dumps(result.reconciliation))
                        write_json(ad/'RESULT.json',asdict(result),immutable=True)
                        write_json(ad/'RECOVERY_AND_IDEMPOTENCY.json',dict(recovery=recovery,
                            duplicate_batches=idempotency,other_accounts_recovery='SHARED_ENGINE_TEST_ONLY_NOT_REPEATED'),immutable=True)
                        write_json(ad/'FINAL_CHECKPOINT.json',engine.checkpoint(),immutable=True)
                        summary=dict(account_id=account_id,status='REAL_CONTINUOUS_PREFIX_EXECUTED',started_at=started,stopped_at=utcnow().isoformat(),
                            sessions=result.processed_sessions,first_complete_features=ready[0]['session'],ready_sessions=len(ready),
                            signal_count=sum(x['kind']=='SIGNAL_CREATED' for x in result.events),
                            trades=len(result.trades),closed_campaigns=len(result.campaigns),open_positions=len(result.open_positions),
                            cash=engine.cash,marked_equity=engine.equity(),engineering_prefix_net_profit=engine.equity()-11500,
                            max_drawdown=min((x['drawdown'] for x in result.daily_equity),default=None),
                            no_position_settlements=sum(x['kind']=='SETTLEMENT_NO_HELD_CONTRACT' for x in result.events),
                            skips=dict(Counter(x['reason'] for x in result.skips)),reconciliation=result.reconciliation,
                            full_2022_2025_return=None,formal_strategy_evaluation='NOT_EVALUATED',
                            stop_boundary=stop,nonzero_fill_path='NOT_EXERCISED_REAL_INPUT' if not result.trades else 'EXERCISED')
                        write_json(ad/'SUMMARY.json',summary,immutable=True);progress['accounts_completed'].append(summary)
                        write_json(out/'RUN_PROGRESS.json',progress)
                        print(json.dumps({k:summary[k] for k in ('account_id','sessions','ready_sessions','signal_count','trades','cash')}),flush=True)
                        del engine,result
            inputs.qualification_catalog.assert_unchanged()
            if any(digest(REPO/p)!=h for p,h in source_hashes.items()):raise ValueError('SOURCE_CHANGED_DURING_ACCOUNT_RUN')
            progress['status']='COMPLETED_REAL_ENGINEERING_PREFIX_FORMAL_FULL_MATRIX_NOT_RUN'
        except BaseException as error:
            progress.update(status='STOPPED_ERROR',error_type=type(error).__name__,error=str(error));raise
        finally:
            progress.update(stopped_at=utcnow().isoformat(),peak_rss_bytes=guard.peak_rss,minimum_available_bytes=guard.min_available)
            write_json(out/'RUN_PROGRESS.json',progress)


if __name__=='__main__':main()
